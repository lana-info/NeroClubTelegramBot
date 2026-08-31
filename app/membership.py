from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
from typing import Any

from .access import effective_access
from .integrations.telegram import TelegramClient, TelegramError
from .site_access import queue_site_access_job


class MembershipError(RuntimeError):
    pass


INVITE_LIFETIME = timedelta(hours=24)


async def create_personal_invite(
    db: sqlite3.Connection,
    user_id: int,
    telegram: TelegramClient,
    chat_id: int | str,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    subscription = db.execute(
        "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
    ).fetchone()
    if not user:
        raise MembershipError("user not found")
    if effective_access(user, subscription) != "active":
        raise MembershipError("invite requires active access")
    if dry_run:
        return {"user_id": user_id, "status": "dry_run", "invite_link": None}
    expire_date = int((datetime.now(timezone.utc) + INVITE_LIFETIME).timestamp())
    result = await telegram.create_chat_invite_link(
        chat_id, expire_date=expire_date, creates_join_request=True
    )
    link = result.get("invite_link") if isinstance(result, dict) else None
    if not link:
        raise MembershipError("Telegram returned no invite link")
    db.execute(
        "INSERT OR IGNORE INTO telegram_invites(user_id, chat_id, invite_link, expires_at) VALUES (?, ?, ?, ?)",
        (user_id, str(chat_id), link, datetime.fromtimestamp(expire_date, timezone.utc).isoformat()),
    )
    db.execute(
        "INSERT INTO audit_log(actor, action, user_id, details) VALUES (?, ?, ?, ?)",
        ("telegram", "telegram.invite_created", user_id, "personal join-request invite created"),
    )
    return {"user_id": user_id, "status": "created", "invite_link": link, "expires_in_hours": 24}


def _active_member_status(status: str | None) -> bool:
    return status in {"member", "restricted", "administrator", "creator"}


def _telegram_user_label(user: sqlite3.Row) -> str:
    username = (user["telegram_username"] or "").strip().lstrip("@")
    return f"@{username}" if username else "без username"


async def _warn_before_removal(
    db: sqlite3.Connection,
    telegram: TelegramClient,
    user: sqlite3.Row,
    chat_id: int | str,
    *,
    admin_telegram_ids: tuple[int, ...],
    chat_label: str,
) -> bool:
    """Send one warning per day and return whether a previous warning exists."""
    today = datetime.now(timezone.utc).date().isoformat()
    previous = db.execute(
        "SELECT 1 FROM telegram_removal_warnings WHERE user_id = ? AND chat_id = ? "
        "AND warning_date < ? LIMIT 1",
        (user["id"], str(chat_id), today),
    ).fetchone()
    if previous:
        return True
    already_sent = db.execute(
        "SELECT 1 FROM telegram_removal_warnings WHERE user_id = ? AND chat_id = ? "
        "AND warning_date = ? LIMIT 1",
        (user["id"], str(chat_id), today),
    ).fetchone()
    if already_sent:
        return False
    if not admin_telegram_ids:
        return False
    text = (
        "⚠️ Предупреждение об удалении\n\n"
        f"Пользователь: {_telegram_user_label(user)}\n"
        f"Telegram ID: {user['telegram_id']}\n"
        f"Место: в {chat_label}\n\n"
        "Подписка не отмечена как активная. Если до следующей ежедневной проверки "
        "оплата не будет отмечена в таблице, пользователь будет удалён."
    )
    for admin_id in admin_telegram_ids:
        await telegram.send_message(admin_id, text)
    db.execute(
        "INSERT OR IGNORE INTO telegram_removal_warnings(user_id, chat_id, warning_date) VALUES (?, ?, ?)",
        (user["id"], str(chat_id), today),
    )
    return False


async def process_pending_telegram_invite_jobs(
    db: sqlite3.Connection,
    telegram: TelegramClient,
    chat_ids: tuple[int | str, ...],
    *,
    dry_run: bool,
    limit: int = 20,
) -> dict[str, int]:
    jobs = db.execute(
        "SELECT id, aggregate_key, payload FROM outbox_jobs "
        "WHERE kind = 'telegram.invite' AND status = 'pending' ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    if dry_run:
        return {"processed": 0, "failed": 0, "skipped": len(jobs)}
    processed = failed = 0
    for job in jobs:
        payload = json.loads(job["payload"])
        try:
            user = db.execute("SELECT * FROM users WHERE id = ?", (int(payload["user_id"]),)).fetchone()
            if not user:
                raise MembershipError("user not found")
            subscription = db.execute(
                "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user["id"],)
            ).fetchone()
            if effective_access(user, subscription) != "active":
                raise MembershipError("invite requires active access")
            links = []
            expire_date = int((datetime.now(timezone.utc) + INVITE_LIFETIME).timestamp())
            for target_chat_id in chat_ids:
                member = await telegram.get_chat_member(target_chat_id, user["telegram_id"])
                member_status = member.get("status") if isinstance(member, dict) else None
                if _active_member_status(member_status):
                    continue
                result = await telegram.create_chat_invite_link(
                    target_chat_id, expire_date=expire_date, creates_join_request=True
                )
                link = result.get("invite_link") if isinstance(result, dict) else None
                if not link:
                    raise MembershipError("Telegram returned no invite link")
                db.execute(
                    "INSERT OR IGNORE INTO telegram_invites(user_id, chat_id, invite_link, expires_at) VALUES (?, ?, ?, ?)",
                    (user["id"], str(target_chat_id), link, datetime.fromtimestamp(expire_date, timezone.utc).isoformat()),
                )
                links.append(link)
            if links:
                await telegram.send_message(
                    user["telegram_id"],
                    "Оплата подтверждена. Используйте персональную ссылку для вступления "
                    "в течение 24 часов:\n"
                    + "\n".join(links),
                )
            db.execute(
                "UPDATE outbox_jobs SET status = 'done', processed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (job["id"],),
            )
            processed += 1
        except (MembershipError, TelegramError, ValueError, KeyError, json.JSONDecodeError) as exc:
            db.execute(
                "UPDATE outbox_jobs SET status = 'failed', attempts = attempts + 1, last_error = ? WHERE id = ?",
                (str(exc), job["id"]),
            )
            failed += 1
    return {"processed": processed, "failed": failed, "skipped": 0}


async def process_telegram_join_request(
    db: sqlite3.Connection,
    telegram: TelegramClient,
    update_id: int,
    join_request: dict[str, Any],
    *,
    allowed_chat_ids: tuple[int | str, ...],
) -> str:
    event_chat_id = (join_request.get("chat") or {}).get("id")
    if not any(str(event_chat_id) == str(target) for target in allowed_chat_ids):
        _mark_update_processed(db, update_id)
        return "ignored"
    applicant = (join_request.get("from") or {}).get("id")
    invite_link = ((join_request.get("invite_link") or {}).get("invite_link"))
    if not isinstance(applicant, int) or not isinstance(invite_link, str):
        _mark_update_processed(db, update_id)
        return "ignored"
    invite = db.execute(
        "SELECT ti.*, u.telegram_id FROM telegram_invites ti JOIN users u ON u.id = ti.user_id "
        "WHERE ti.chat_id = ? AND ti.invite_link = ? AND ti.status = 'pending'",
        (str(event_chat_id), invite_link),
    ).fetchone()
    expires_at = parse_invite_expiry(invite["expires_at"]) if invite else None
    if not invite or invite["telegram_id"] != applicant or not expires_at or expires_at <= datetime.now(timezone.utc):
        await telegram.decline_chat_join_request(event_chat_id, applicant)
        if invite:
            db.execute("UPDATE telegram_invites SET status = 'expired' WHERE id = ?", (invite["id"],))
        _mark_update_processed(db, update_id)
        return "declined"
    user = db.execute("SELECT * FROM users WHERE id = ?", (invite["user_id"],)).fetchone()
    subscription = db.execute(
        "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (invite["user_id"],)
    ).fetchone()
    if effective_access(user, subscription) != "active":
        await telegram.decline_chat_join_request(event_chat_id, applicant)
        db.execute("UPDATE telegram_invites SET status = 'expired' WHERE id = ?", (invite["id"],))
        _mark_update_processed(db, update_id)
        return "declined"
    await telegram.approve_chat_join_request(event_chat_id, applicant)
    db.execute(
        "UPDATE telegram_invites SET status = 'used', used_at = CURRENT_TIMESTAMP WHERE id = ?",
        (invite["id"],),
    )
    db.execute(
        "UPDATE users SET telegram_membership_status = 'member', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (invite["user_id"],),
    )
    _mark_update_processed(db, update_id)
    return "approved"


def parse_invite_expiry(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _mark_update_processed(db: sqlite3.Connection, update_id: int) -> None:
    db.execute(
        "UPDATE inbox_events SET processed_at = CURRENT_TIMESTAMP WHERE provider = 'telegram' AND external_event_id = ?",
        (str(update_id),),
    )


async def revoke_expired_telegram_invites(
    db: sqlite3.Connection,
    telegram: TelegramClient,
    *,
    dry_run: bool,
    limit: int = 100,
) -> dict[str, int]:
    invites = db.execute(
        "SELECT id, chat_id, invite_link, expires_at FROM telegram_invites WHERE status = 'pending' "
        "ORDER BY id LIMIT ?", (limit,)
    ).fetchall()
    now = datetime.now(timezone.utc)
    invites = [invite for invite in invites if (parse_invite_expiry(invite["expires_at"]) or now) <= now]
    if dry_run:
        return {"revoked": 0, "would_revoke": len(invites), "failed": 0}
    revoked = failed = 0
    for invite in invites:
        try:
            await telegram.revoke_chat_invite_link(invite["chat_id"], invite["invite_link"])
            db.execute("UPDATE telegram_invites SET status = 'expired' WHERE id = ?", (invite["id"],))
            revoked += 1
        except TelegramError:
            failed += 1
    return {"revoked": revoked, "would_revoke": 0, "failed": failed}


async def reconcile_members(
    db: sqlite3.Connection,
    telegram: TelegramClient,
    chat_id: int | str,
    *,
    dry_run: bool,
    removal_enabled: bool = True,
    site_deactivation_enabled: bool = True,
    admin_telegram_ids: tuple[int, ...] | None = None,
    chat_label: str = "чате",
) -> dict[str, int]:
    users = db.execute("SELECT * FROM users WHERE telegram_id IS NOT NULL").fetchall()
    checked = active = denied = removed = would_remove = failed = 0
    for user in users:
        try:
            subscription = db.execute(
                "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user["id"],)
            ).fetchone()
            member = await telegram.get_chat_member(chat_id, user["telegram_id"])
            member_status = member.get("status") if isinstance(member, dict) else None
            access = effective_access(user, subscription)
            db.execute(
                "UPDATE users SET telegram_membership_status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (member_status or "unknown", user["id"]),
            )
            checked += 1
            if access == "active":
                active += 1
                db.execute(
                    "DELETE FROM telegram_removal_warnings WHERE user_id = ? AND chat_id = ?",
                    (user["id"], str(chat_id)),
                )
                if member_status == "left" and site_deactivation_enabled:
                    queue_site_access_job(db, user["id"], "deactivate", f"reconcile-left-{user['id']}")
            else:
                denied += 1
                if removal_enabled and member_status in {"member", "restricted", "administrator", "creator"}:
                    if dry_run:
                        would_remove += 1
                    else:
                        if admin_telegram_ids is not None:
                            warned_before = await _warn_before_removal(
                                db,
                                telegram,
                                user,
                                chat_id,
                                admin_telegram_ids=admin_telegram_ids,
                                chat_label=chat_label,
                            )
                            if not warned_before:
                                continue
                        db.execute(
                            "UPDATE users SET telegram_ban_source = 'system', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (user["id"],),
                        )
                        await telegram.ban_chat_member(chat_id, user["telegram_id"])
                        removed += 1
        except Exception:
            failed += 1
    return {
        "checked": checked, "active": active, "denied": denied,
        "removed": removed, "would_remove": would_remove, "failed": failed,
    }


async def process_pending_telegram_restore_jobs(
    db: sqlite3.Connection,
    telegram: TelegramClient,
    chat_id: int | str,
    *,
    additional_chat_ids: tuple[int | str, ...] = (),
    dry_run: bool,
    limit: int = 20,
) -> dict[str, int]:
    jobs = db.execute(
        "SELECT id, aggregate_key, payload, attempts FROM outbox_jobs "
        "WHERE kind = 'telegram.restore' AND status = 'pending' ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    processed = failed = skipped = 0
    for job in jobs:
        payload = json.loads(job["payload"])
        if dry_run:
            skipped += 1
            continue
        try:
            user = db.execute("SELECT * FROM users WHERE id = ?", (int(payload["user_id"]),)).fetchone()
            if not user or not user["telegram_banned"]:
                raise MembershipError("manual Telegram ban is no longer present")
            subscription = db.execute(
                "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user["id"],)
            ).fetchone()
            db.execute("UPDATE users SET telegram_banned = 0 WHERE id = ?", (user["id"],))
            if effective_access(db.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone(), subscription) != "active":
                db.execute("UPDATE users SET telegram_banned = 1 WHERE id = ?", (user["id"],))
                raise MembershipError("restore requires active access")
            chat_ids = [chat_id]
            for additional_chat_id in additional_chat_ids:
                if additional_chat_id not in chat_ids:
                    chat_ids.append(additional_chat_id)
            invite_links = []
            for target_chat_id in chat_ids:
                await telegram.unban_chat_member(target_chat_id, user["telegram_id"])
                invite_result = await telegram.create_chat_invite_link(
                    target_chat_id,
                    expire_date=int((datetime.now(timezone.utc) + INVITE_LIFETIME).timestamp()),
                    creates_join_request=True,
                )
                invite_link = invite_result.get("invite_link") if isinstance(invite_result, dict) else None
                if not invite_link:
                    raise MembershipError("Telegram returned no invite link")
                invite_links.append(invite_link)
            await telegram.send_message(
                user["telegram_id"],
                "Вас разблокировали. Используйте новые ссылки для вступления:\n"
                + "\n".join(invite_links),
            )
            db.execute(
                "UPDATE users SET telegram_banned = 0, telegram_ban_source = NULL, "
                "telegram_membership_status = 'unknown', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (user["id"],),
            )
            db.execute("UPDATE outbox_jobs SET status = 'done', processed_at = CURRENT_TIMESTAMP WHERE id = ?", (job["id"],))
            if payload.get("command_id"):
                db.execute(
                    "UPDATE sheets_commands SET status = 'done', result = ?, completed_at = CURRENT_TIMESTAMP WHERE command_id = ?",
                    (json.dumps({"invite_links_sent": len(invite_links)}, ensure_ascii=False), payload["command_id"]),
                )
            processed += 1
        except (MembershipError, ValueError, KeyError, json.JSONDecodeError, TelegramError) as exc:
            db.execute("UPDATE users SET telegram_banned = 1 WHERE id = ?", (int(payload["user_id"]),))
            db.execute(
                "UPDATE outbox_jobs SET status = 'failed', attempts = attempts + 1, last_error = ? WHERE id = ?",
                (str(exc), job["id"]),
            )
            if payload.get("command_id"):
                db.execute(
                    "UPDATE sheets_commands SET status = 'error', result = ?, completed_at = CURRENT_TIMESTAMP WHERE command_id = ?",
                    (json.dumps({"error": str(exc)}, ensure_ascii=False), payload["command_id"]),
                )
            failed += 1
    return {"processed": processed, "failed": failed, "skipped": skipped}

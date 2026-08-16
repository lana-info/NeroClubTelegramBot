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
    expire_date = int((datetime.now(timezone.utc) + timedelta(minutes=15)).timestamp())
    result = await telegram.create_chat_invite_link(
        chat_id, expire_date=expire_date, creates_join_request=True
    )
    link = result.get("invite_link") if isinstance(result, dict) else None
    if not link:
        raise MembershipError("Telegram returned no invite link")
    db.execute(
        "INSERT INTO audit_log(actor, action, user_id, details) VALUES (?, ?, ?, ?)",
        ("telegram", "telegram.invite_created", user_id, "personal join-request invite created"),
    )
    return {"user_id": user_id, "status": "created", "invite_link": link, "expires_in_minutes": 15}


async def reconcile_members(
    db: sqlite3.Connection,
    telegram: TelegramClient,
    chat_id: int | str,
    *,
    dry_run: bool,
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
                if member_status == "left":
                    queue_site_access_job(db, user["id"], "deactivate", f"reconcile-left-{user['id']}")
            else:
                denied += 1
                if member_status in {"member", "restricted", "administrator", "creator"}:
                    if dry_run:
                        would_remove += 1
                    else:
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
            await telegram.unban_chat_member(chat_id, user["telegram_id"])
            invite_result = await telegram.create_chat_invite_link(
                chat_id,
                expire_date=int((datetime.now(timezone.utc) + timedelta(minutes=15)).timestamp()),
                creates_join_request=True,
            )
            invite_link = invite_result.get("invite_link") if isinstance(invite_result, dict) else None
            if not invite_link:
                raise MembershipError("Telegram returned no invite link")
            await telegram.send_message(
                user["telegram_id"],
                "Вас разблокировали. Используйте новую ссылку для вступления:\n" + invite_link,
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
                    (json.dumps({"invite_link_sent": True}, ensure_ascii=False), payload["command_id"]),
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

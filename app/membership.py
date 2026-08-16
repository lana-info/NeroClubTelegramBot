from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
from typing import Any

from .access import effective_access
from .integrations.telegram import TelegramClient
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

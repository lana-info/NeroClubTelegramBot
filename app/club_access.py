"""Club resource rights, separate from billing and Telegram admission."""
from __future__ import annotations

import sqlite3

from .integrations.telegram import TelegramError


class ClubCheckError(RuntimeError):
    pass


def is_member(member: dict) -> bool:
    return member.get("status") in {"member", "administrator", "creator"} or (
        member.get("status") == "restricted" and member.get("is_member") is True
    )


async def refresh_club_member(db, user_id, telegram, chat_id):
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if user["telegram_banned"] or user["access_override"] == "deny":
        return False
    try:
        member = await telegram.get_chat_member(chat_id, user["telegram_id"])
    except (TelegramError, ValueError, TypeError) as exc:
        raise ClubCheckError("Не удалось проверить участие в группе. Попробуйте позже.") from exc
    if not isinstance(member, dict) or member.get("status") not in {
        "member", "administrator", "creator", "restricted", "left", "kicked"
    }:
        raise ClubCheckError("Не удалось проверить участие в группе. Попробуйте позже.")
    status = member["status"] if is_member(member) else (
        "kicked" if member["status"] == "kicked" else "left"
    )
    db.execute("UPDATE users SET telegram_membership_status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (status, user_id))
    return is_member(member)

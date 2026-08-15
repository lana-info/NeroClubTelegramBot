from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def effective_access(user: sqlite3.Row, subscription: sqlite3.Row | None) -> str:
    if user["access_override"] == "deny":
        return "denied"
    if user["whitelist"] or user["access_override"] == "allow":
        return "active"
    if not subscription:
        return "denied"
    paid_until = parse_dt(subscription["provider_paid_until"])
    manual_until = parse_dt(user["manual_access_until"])
    latest = max([d for d in (paid_until, manual_until) if d], default=None)
    return "active" if latest and latest > utc_now() else "denied"


def get_user(db: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def upsert_user(db: sqlite3.Connection, payload: dict[str, Any]) -> sqlite3.Row:
    telegram_id = payload.get("telegram_id")
    if telegram_id is None:
        raise ValueError("telegram_id is required")
    existing = db.execute(
        "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()
    if existing:
        db.execute(
            """UPDATE users SET telegram_username = COALESCE(?, telegram_username),
               wordpress_user_id = COALESCE(?, wordpress_user_id),
               wordpress_login = COALESCE(?, wordpress_login),
               wordpress_email = COALESCE(?, wordpress_email),
               wordpress_role = COALESCE(?, wordpress_role),
               updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
            (
                payload.get("telegram_username"),
                payload.get("wordpress_user_id"),
                payload.get("wordpress_login"),
                payload.get("wordpress_email"),
                payload.get("wordpress_role"),
                existing["id"],
            ),
        )
    else:
        db.execute(
            """INSERT INTO users
               (telegram_id, telegram_username, wordpress_user_id, wordpress_login, wordpress_email, wordpress_role)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                telegram_id,
                payload.get("telegram_username"),
                payload.get("wordpress_user_id"),
                payload.get("wordpress_login"),
                payload.get("wordpress_email"),
                payload.get("wordpress_role"),
            ),
        )
    return db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()


def apply_command(
    db: sqlite3.Connection, command_id: str, user_id: int, action: str, payload: dict[str, Any], actor: str
) -> dict[str, Any]:
    allowed = {"whitelist", "unwhitelist", "deny", "allow", "extend"}
    if action not in allowed:
        raise ValueError(f"unsupported action: {action}")
    existing = db.execute(
        "SELECT status, result FROM sheets_commands WHERE command_id = ?", (command_id,)
    ).fetchone()
    if existing:
        return {"command_id": command_id, "status": existing["status"], "result": existing["result"]}

    if action == "whitelist":
        db.execute("UPDATE users SET whitelist = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (user_id,))
    elif action == "unwhitelist":
        db.execute("UPDATE users SET whitelist = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (user_id,))
    elif action in {"deny", "allow"}:
        db.execute(
            "UPDATE users SET access_override = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (action, user_id),
        )
    elif action == "extend":
        until = payload.get("manual_access_until")
        if not until or not parse_dt(until):
            raise ValueError("manual_access_until must be an ISO-8601 datetime")
        db.execute(
            "UPDATE users SET manual_access_until = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (until, user_id),
        )

    result = json.dumps({"action": action, "user_id": user_id}, ensure_ascii=False)
    db.execute(
        """INSERT INTO sheets_commands(command_id, user_id, action, payload, requested_by, status, result, completed_at)
           VALUES (?, ?, ?, ?, ?, 'done', ?, CURRENT_TIMESTAMP)""",
        (command_id, user_id, action, json.dumps(payload), actor, result),
    )
    db.execute(
        "INSERT INTO audit_log(actor, action, user_id, details) VALUES (?, ?, ?, ?)",
        (actor, f"sheets.{action}", user_id, result),
    )
    return {"command_id": command_id, "status": "done", "result": result}

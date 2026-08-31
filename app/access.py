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
    if user["access_override"] == "deny" or user["telegram_banned"]:
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
    allowed = {
        "whitelist", "unwhitelist", "deny", "allow", "extend", "restore_telegram", "issue_invite",
        "issue_credentials", "resend_delivery", "revoke_site_access", "restore_site_access",
    }
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

    restore_queued = False
    site_command_queued = False
    if action in {"revoke_site_access", "restore_site_access"}:
        site_action = "deactivate" if action == "revoke_site_access" else "restore"
        db.execute(
            "INSERT OR IGNORE INTO outbox_jobs(kind, aggregate_key, payload) VALUES (?, ?, ?)",
            (
                f"site.{site_action}", f"sheets-site-{site_action}-{command_id}",
                json.dumps({"command_id": command_id, "user_id": user_id, "action": site_action}),
            ),
        )
        site_command_queued = True
    if action == "restore_telegram":
        user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user or not user["telegram_banned"]:
            raise ValueError("user does not have a manual Telegram ban")
        db.execute(
            "UPDATE users SET telegram_membership_status = 'unknown', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (user_id,),
        )
        db.execute("UPDATE users SET telegram_banned = 0 WHERE id = ?", (user_id,))
        restored_user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        subscription = db.execute(
            "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
        ).fetchone()
        if effective_access(restored_user, subscription) == "active":
            db.execute(
                "INSERT OR IGNORE INTO outbox_jobs(kind, aggregate_key, payload) VALUES (?, ?, ?)",
                ("telegram.restore", f"sheets-telegram-restore-{command_id}", json.dumps({
                    "command_id": command_id, "user_id": user_id, "action": action,
                }, ensure_ascii=False)),
            )
            restore_queued = True
        db.execute("UPDATE users SET telegram_banned = 1 WHERE id = ?", (user_id,))

    if action == "issue_invite":
        db.execute(
            "INSERT OR IGNORE INTO outbox_jobs(kind, aggregate_key, payload) VALUES (?, ?, ?)",
            ("telegram.invite", f"sheets-telegram-invite-{command_id}", json.dumps({
                "command_id": command_id, "user_id": user_id, "action": action,
            }, ensure_ascii=False)),
        )

    if action in {"issue_credentials", "resend_delivery"}:
        db.execute(
            "INSERT INTO outbox_jobs(kind, aggregate_key, payload) VALUES (?, ?, ?)",
            ("site.credentials", f"sheets-site-access-{command_id}", json.dumps({
                "command_id": command_id, "user_id": user_id, "action": action,
            }, ensure_ascii=False)),
        )
        status = "queued"
        result = json.dumps({"action": action, "user_id": user_id, "status": status}, ensure_ascii=False)
    elif site_command_queued:
        status = "queued"
        result = json.dumps({"action": action, "user_id": user_id, "status": status}, ensure_ascii=False)
    elif action in {"restore_telegram", "issue_invite"}:
        status = "queued" if restore_queued else "done"
        if action == "issue_invite":
            status = "queued"
        result = json.dumps({"action": action, "user_id": user_id, "status": status}, ensure_ascii=False)
    else:
        status = "done"
        result = json.dumps({"action": action, "user_id": user_id}, ensure_ascii=False)
    db.execute(
        """INSERT INTO sheets_commands(command_id, user_id, action, payload, requested_by, status, result, completed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, CASE WHEN ? = 'done' THEN CURRENT_TIMESTAMP ELSE NULL END)""",
        (command_id, user_id, action, json.dumps(payload), actor, status, result, status),
    )
    db.execute(
        "INSERT INTO audit_log(actor, action, user_id, details) VALUES (?, ?, ?, ?)",
        (actor, f"sheets.{action}", user_id, result),
    )
    return {"command_id": command_id, "status": status, "result": result}

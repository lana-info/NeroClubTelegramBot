from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Any

from .access import effective_access, upsert_user


USER_HEADERS = [
    "user_id", "telegram_id", "username", "wordpress_email", "wordpress_role",
    "access", "provider", "provider_paid_until", "manual_access_until", "whitelist",
    "access_override", "action", "command_id", "requested_by", "command_status",
    "last_result", "updated_at",
]

SITE_HEADERS = [
    "user_id", "telegram_id", "username", "wordpress_login", "wordpress_email",
    "access_status", "subscription_until", "website_access", "credential_status",
    "credential_expires_at", "last_delivery_status", "last_requested_at", "action",
    "command_id", "last_result",
]


def _sheet_datetime(value: str | None) -> str | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _latest_command(db: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM sheets_commands WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()


def _latest_delivery(db: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    for delivery in db.execute(
        "SELECT status, created_at, payload FROM outbox_jobs "
        "WHERE kind = 'site.credentials' ORDER BY id DESC"
    ).fetchall():
        try:
            payload = json.loads(delivery["payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        if payload.get("user_id") == user_id:
            return delivery
    return None


def rows_for_users_sheet(db: sqlite3.Connection) -> list[list[Any]]:
    rows: list[list[Any]] = [USER_HEADERS]
    for user in db.execute("SELECT * FROM users ORDER BY id").fetchall():
        subscription = db.execute(
            "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user["id"],)
        ).fetchone()
        command = _latest_command(db, user["id"])
        rows.append([
            user["id"], user["telegram_id"], user["telegram_username"] or "",
            user["wordpress_email"] or "", user["wordpress_role"] or "",
            effective_access(user, subscription), subscription["provider"] if subscription else "",
            subscription["provider_paid_until"] if subscription else "",
            user["manual_access_until"] or "", "yes" if user["whitelist"] else "no",
            user["access_override"], "none", command["command_id"] if command else "",
            command["requested_by"] if command else "", command["status"] if command else "",
            command["result"] if command else "", user["updated_at"],
        ])
    return rows


def rows_for_site_access_sheet(db: sqlite3.Connection) -> list[list[Any]]:
    rows: list[list[Any]] = [SITE_HEADERS]
    for user in db.execute("SELECT * FROM users ORDER BY id").fetchall():
        subscription = db.execute(
            "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user["id"],)
        ).fetchone()
        command = _latest_command(db, user["id"])
        access = effective_access(user, subscription)
        delivery = _latest_delivery(db, user["id"])
        rows.append([
            user["id"], user["telegram_id"], user["telegram_username"] or "",
            user["wordpress_login"] or "", user["wordpress_email"] or "", access,
            subscription["provider_paid_until"] if subscription else "",
            "active" if access == "active" and user["wordpress_email"] else "denied",
            delivery["status"] if delivery else "not_requested", "",
            delivery["status"] if delivery else "not_requested", delivery["created_at"] if delivery else "",
            "none", command["command_id"] if command else "", command["result"] if command else "",
        ])
    return rows


def dashboard_rows(db: sqlite3.Connection) -> list[list[Any]]:
    users = db.execute("SELECT * FROM users").fetchall()
    active = 0
    whitelist = 0
    for user in users:
        subscription = db.execute(
            "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user["id"],)
        ).fetchone()
        active += effective_access(user, subscription) == "active"
        whitelist += bool(user["whitelist"])
    payments = db.execute("SELECT COUNT(*) AS count FROM inbox_events WHERE provider = 'stripe'").fetchone()["count"]
    failures = db.execute("SELECT COUNT(*) AS count FROM outbox_jobs WHERE status = 'failed'").fetchone()["count"]
    return [
        ["metric", "value"], ["total_users", len(users)], ["active_users", active],
        ["expired_or_denied_users", len(users) - active], ["whitelist_users", whitelist],
        ["stripe_events", payments], ["failed_jobs", failures],
    ]


def import_users(db: sqlite3.Connection, rows: list[dict[str, Any]]) -> list[dict[str, int]]:
    if len(rows) > 1000:
        raise ValueError("at most 1000 users can be imported at once")
    result: list[dict[str, int]] = []
    for payload in rows:
        telegram_id = payload.get("telegram_id")
        if not isinstance(telegram_id, int):
            raise ValueError("telegram_id must be an integer")
        user = upsert_user(db, {
            "telegram_id": telegram_id,
            "telegram_username": payload.get("username"),
            "wordpress_email": payload.get("wordpress_email"),
            "wordpress_login": payload.get("wordpress_login"),
            "wordpress_role": payload.get("wordpress_role"),
        })
        override = payload.get("access_override")
        if override in {"none", "allow", "deny"}:
            db.execute(
                "UPDATE users SET whitelist = ?, access_override = ?, manual_access_until = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (1 if payload.get("whitelist") in {True, "yes", "true"} else 0, override,
                 _sheet_datetime(payload.get("manual_access_until")), user["id"]),
            )
        provider = payload.get("provider") or "legacy"
        paid_until = _sheet_datetime(payload.get("provider_paid_until"))
        if paid_until:
            subscription_id = f"legacy-{telegram_id}-{provider}"
            db.execute(
                """INSERT INTO subscriptions(user_id, provider, provider_subscription_id, billing_status,
                   payment_status, provider_paid_until) VALUES (?, ?, ?, 'active', 'paid', ?)
                   ON CONFLICT(provider, provider_subscription_id) DO UPDATE SET
                   user_id = excluded.user_id, provider_paid_until = excluded.provider_paid_until,
                   billing_status = 'active', payment_status = 'paid', updated_at = CURRENT_TIMESTAMP""",
                (user["id"], provider, subscription_id, paid_until),
            )
        result.append({"telegram_id": telegram_id, "user_id": user["id"]})
    return result

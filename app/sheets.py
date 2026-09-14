from __future__ import annotations

from calendar import monthrange
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

SETTINGS_HEADERS = ["name", "enabled", "description", "updated_at"]

SETTINGS_DESCRIPTIONS = {
    "telegram_group_removal": "Удалять пользователей из Telegram-группы",
    "telegram_channel_removal": "Удалять пользователей из специального канала",
    "wordpress_deactivation": "Деактивировать доступ WordPress",
    "wordpress_access": "Выдавать и восстанавливать доступ WordPress",
    "app_keys": "Выдавать пользователям ключи приложений",
    "reminders": "Отправлять напоминания о подписке",
}

SHEET_PAYMENT_HEADERS = [
    "payment_id", "telegram_id", "paid_at", "plan", "status", "applied_until", "processed_at", "error",
    "payment_provider", "amount_usd", "provider_payment_id",
]


def _sheet_datetime(value: str | None) -> str | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _sheet_payment_date(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("paid_at is required and must be YYYY-MM-DD")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("paid_at is required and must be YYYY-MM-DD") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc)


def _add_calendar_month(value: datetime) -> datetime:
    month = value.month + 1
    year = value.year
    if month == 13:
        year += 1
        month = 1
    return value.replace(year=year, month=month, day=min(value.day, monthrange(year, month)[1]))


def _payment_result(row: sqlite3.Row, *, status: str | None = None) -> dict[str, Any]:
    return {
        "payment_id": row["payment_id"],
        "telegram_id": row["telegram_id"],
        "paid_at": (row["paid_at"] or "")[:10],
        "plan": row["plan"],
        "status": status or row["status"],
        "applied_until": (row["applied_until"] or "")[:10],
        "error": row["error"] or "",
        "payment_provider": row["payment_provider"] or "",
        "amount_usd": row["amount_usd"] or "",
        "provider_payment_id": row["provider_payment_id"] or "",
    }


def process_sheet_payments(db: sqlite3.Connection, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for payload in rows:
        payment_id = payload.get("payment_id")
        if not isinstance(payment_id, str) or not payment_id:
            raise ValueError("payment_id is required")
        existing = db.execute(
            "SELECT * FROM sheet_payment_results WHERE payment_id = ?", (payment_id,)
        ).fetchone()
        if existing:
            results.append(_payment_result(existing, status="duplicate"))
            continue

        telegram_id = payload.get("telegram_id")
        if isinstance(telegram_id, str) and telegram_id.isdigit():
            telegram_id = int(telegram_id)
        paid_at = payload.get("paid_at")
        payment_provider = str(payload.get("payment_provider") or "").strip().lower()
        amount_usd = payload.get("amount_usd")
        provider_payment_id = str(payload.get("provider_payment_id") or "").strip()
        error = ""
        user = None
        paid_at_value = None
        if not isinstance(telegram_id, int):
            error = "telegram_id is required and must be numeric"
        else:
            try:
                paid_at_value = _sheet_payment_date(paid_at)
            except ValueError as exc:
                error = str(exc)
            if not error:
                user = db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
                if not user:
                    error = "user not found for telegram_id"
        if not error and payment_provider and payment_provider not in {"stripe", "paypal"}:
            error = "payment_provider must be stripe or paypal"
        if not error and amount_usd not in {None, ""}:
            try:
                amount_usd = int(amount_usd)
            except (TypeError, ValueError):
                error = "amount_usd must be 10 or 20"
            if not error and amount_usd not in {10, 20}:
                error = "amount_usd must be 10 or 20"

        if payment_provider and provider_payment_id:
            provider_duplicate = db.execute(
                "SELECT * FROM sheet_payment_results WHERE payment_provider = ? AND provider_payment_id = ?",
                (payment_provider, provider_payment_id),
            ).fetchone()
            if provider_duplicate:
                results.append(_payment_result(provider_duplicate, status="duplicate"))
                continue

        event_payload = json.dumps({
            "payment_id": payment_id, "telegram_id": telegram_id, "paid_at": paid_at, "plan": "monthly",
            "payment_provider": payment_provider, "amount_usd": amount_usd,
            "provider_payment_id": provider_payment_id,
        }, ensure_ascii=False)
        inserted_event = db.execute(
            "INSERT OR IGNORE INTO inbox_events(provider, external_event_id, event_type, payload, processed_at) "
            "VALUES ('sheet', ?, 'manual_payment', ?, CURRENT_TIMESTAMP)",
            (payment_id, event_payload),
        )
        if not inserted_event.rowcount:
            completed = db.execute(
                "SELECT * FROM sheet_payment_results WHERE payment_id = ?", (payment_id,)
            ).fetchone()
            if completed:
                results.append(_payment_result(completed, status="duplicate"))
                continue
            raise ValueError("payment is already being processed; retry")

        applied_until = None
        status = "error" if error else "processed"
        if user and paid_at_value:
            paid_dates = [
                parse for (raw,) in db.execute(
                    "SELECT provider_paid_until FROM subscriptions WHERE user_id = ? AND payment_status = 'paid'",
                    (user["id"],),
                ).fetchall()
                if raw and (parse := _sheet_datetime(raw))
            ]
            current_until = max((datetime.fromisoformat(value) for value in paid_dates), default=paid_at_value)
            applied_until = _add_calendar_month(max(current_until, paid_at_value)).isoformat()
            subscription_id = f"manual-{user['telegram_id']}"
            db.execute(
                """INSERT INTO subscriptions(user_id, provider, provider_subscription_id, billing_status, payment_status, provider_paid_until)
                   VALUES (?, 'sheet', ?, 'active', 'paid', ?)
                   ON CONFLICT(provider, provider_subscription_id) DO UPDATE SET
                   user_id = excluded.user_id, provider_paid_until = excluded.provider_paid_until,
                   billing_status = 'active', payment_status = 'paid', updated_at = CURRENT_TIMESTAMP""",
                (user["id"], subscription_id, applied_until),
            )
            db.execute(
                "INSERT OR IGNORE INTO outbox_jobs(kind, aggregate_key, payload) VALUES (?, ?, ?)",
                ("telegram.invite", f"sheet-payment-{payment_id}", json.dumps({
                    "user_id": user["id"], "source": "sheet", "payment_id": payment_id,
                }, ensure_ascii=False)),
            )

        db.execute(
            """INSERT INTO sheet_payment_results
               (payment_id, user_id, telegram_id, paid_at, plan, status, applied_until, error,
                payment_provider, amount_usd, provider_payment_id)
               VALUES (?, ?, ?, ?, 'monthly', ?, ?, ?, ?, ?, ?)""",
            (payment_id, user["id"] if user else None, telegram_id, paid_at_value.isoformat() if paid_at_value else paid_at,
             status, applied_until, error, payment_provider, amount_usd, provider_payment_id),
        )
        stored = db.execute("SELECT * FROM sheet_payment_results WHERE payment_id = ?", (payment_id,)).fetchone()
        results.append(_payment_result(stored))
    return results


def rows_for_payments_sheet(db: sqlite3.Connection) -> list[list[Any]]:
    rows: list[list[Any]] = [SHEET_PAYMENT_HEADERS]
    for payment in db.execute("SELECT * FROM sheet_payment_results ORDER BY id").fetchall():
        rows.append([
            payment["payment_id"], payment["telegram_id"] or "", (payment["paid_at"] or "")[:10], payment["plan"],
            payment["status"], (payment["applied_until"] or "")[:10], payment["processed_at"], payment["error"] or "",
            payment["payment_provider"] or "", payment["amount_usd"] or "", payment["provider_payment_id"] or "",
        ])
    return rows


def sync_whitelists(db: sqlite3.Connection, rows: list[dict[str, Any]], actor: str) -> int:
    updated = 0
    for payload in rows:
        user_id = payload.get("user_id")
        if isinstance(user_id, str) and user_id.isdigit():
            user_id = int(user_id)
        if not isinstance(user_id, int):
            raise ValueError("user_id is required and must be numeric")
        user = db.execute("SELECT whitelist FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            raise LookupError("user not found")
        enabled = payload.get("whitelist") in {True, "yes", "true", "TRUE", "Yes"}
        if bool(user["whitelist"]) == enabled:
            continue
        db.execute("UPDATE users SET whitelist = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (int(enabled), user_id))
        db.execute(
            "INSERT INTO audit_log(actor, action, user_id, details) VALUES (?, 'sheets.whitelist', ?, ?)",
            (actor, user_id, json.dumps({"whitelist": enabled}, ensure_ascii=False)),
        )
        updated += 1
    return updated


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


def rows_for_settings_sheet(db: sqlite3.Connection) -> list[list[Any]]:
    from .feature_flags import get_flags

    rows = [SETTINGS_HEADERS]
    flags = get_flags(db)
    for name, enabled in flags.items():
        updated = db.execute("SELECT updated_at FROM feature_flags WHERE name = ?", (name,)).fetchone()
        rows.append([name, "ВКЛ" if enabled else "ВЫКЛ", SETTINGS_DESCRIPTIONS[name], updated["updated_at"] if updated else ""])
    return rows


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
            # A sheet-confirmed payment is the source of truth in the current
            # MVP. Queue one invite job for this paid period; the worker later
            # creates personal links for the configured group and channel.
            db.execute(
                "INSERT OR IGNORE INTO outbox_jobs(kind, aggregate_key, payload) VALUES (?, ?, ?)",
                (
                    "telegram.invite",
                    f"sheet-payment-{telegram_id}-{provider}-{paid_until}",
                    json.dumps({"user_id": user["id"], "source": "sheet"}, ensure_ascii=False),
                ),
            )
        result.append({"telegram_id": telegram_id, "user_id": user["id"]})
    return result

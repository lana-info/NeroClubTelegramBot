from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
from typing import Any

from .access import parse_dt
from .integrations.telegram import TelegramClient, TelegramError


def _display_date(value: datetime) -> str:
    months = (
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    )
    return f"{value.day} {months[value.month - 1]} {value.year} года"


async def send_subscription_reminders(
    db: sqlite3.Connection,
    telegram: TelegramClient,
    *,
    payment_url: str = "",
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    users = db.execute(
        "SELECT u.id, u.telegram_id, s.provider_paid_until FROM users u "
        "JOIN subscriptions s ON s.id = (SELECT id FROM subscriptions WHERE user_id = u.id ORDER BY id DESC LIMIT 1) "
        "WHERE u.telegram_id IS NOT NULL AND u.reminders_enabled = 1 AND s.provider_paid_until IS NOT NULL"
    ).fetchall()
    sent = would_send = failed = skipped = 0
    for user in users:
        paid_until = parse_dt(user["provider_paid_until"])
        if not paid_until:
            skipped += 1
            continue
        days_left = (paid_until.date() - now.date()).days
        if days_left not in {7, 3, 1}:
            skipped += 1
            continue
        notification_key = f"subscription-expiry-{paid_until.date().isoformat()}-{days_left}d"
        if db.execute(
            "SELECT 1 FROM notifications WHERE user_id = ? AND notification_key = ? AND channel = 'telegram'",
            (user["id"], notification_key),
        ).fetchone():
            skipped += 1
            continue
        payment_line = (
            f"\nСсылка для оплаты: {payment_url}"
            if payment_url
            else "\nДля оплаты обратитесь к администратору. После оплаты нажмите «Сообщить об оплате»."
        )
        days_label = {1: "день", 3: "дня", 7: "дней"}[days_left]
        text = (
            f"Напоминание: подписка заканчивается через {days_left} {days_label}.\n"
            f"Дата окончания: {_display_date(paid_until)}.{payment_line}"
        )
        if dry_run:
            would_send += 1
            continue
        try:
            await telegram.send_message(user["telegram_id"], text)
            db.execute(
                "INSERT OR IGNORE INTO notifications(user_id, notification_key, channel, status) VALUES (?, ?, 'telegram', 'sent')",
                (user["id"], notification_key),
            )
            sent += 1
        except TelegramError:
            failed += 1
    return {"sent": sent, "would_send": would_send, "failed": failed, "skipped": skipped}

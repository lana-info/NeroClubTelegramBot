from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
from typing import Any

from .access import parse_dt
from .integrations.telegram import TelegramClient, TelegramError


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
        "WHERE u.telegram_id IS NOT NULL AND s.provider_paid_until IS NOT NULL"
    ).fetchall()
    sent = would_send = failed = skipped = 0
    for user in users:
        paid_until = parse_dt(user["provider_paid_until"])
        if not paid_until:
            skipped += 1
            continue
        days_left = (paid_until.date() - now.date()).days
        if days_left not in {7, 3}:
            skipped += 1
            continue
        notification_key = f"subscription-expiry-{paid_until.date().isoformat()}-{days_left}d"
        if db.execute(
            "SELECT 1 FROM notifications WHERE user_id = ? AND notification_key = ? AND channel = 'telegram'",
            (user["id"], notification_key),
        ).fetchone():
            skipped += 1
            continue
        payment_line = f"\nОплатить или продлить: {payment_url}" if payment_url else "\nДля оплаты выберите в меню «Оплатить / продлить»."
        text = (
            f"Напоминание: подписка заканчивается через {days_left} дн.\n"
            f"Дата окончания: {paid_until.strftime('%d.%m.%Y')}.{payment_line}"
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

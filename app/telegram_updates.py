from __future__ import annotations

import json
from datetime import timedelta
import secrets
import sqlite3
from typing import Any

from .access import effective_access, upsert_user, utc_now
from .integrations.telegram import TelegramClient
from .integrations.wordpress import WordPressClient, WordPressError


def _command(text: str | None) -> str | None:
    if not text or not text.startswith("/"):
        return None
    return text.split()[0].split("@", 1)[0].lower()


async def process_update(
    db: sqlite3.Connection,
    update: dict[str, Any],
    telegram: TelegramClient,
    *,
    chat_id: int | str | None = None,
    wordpress: WordPressClient | None = None,
    temporary_password_hours: int = 24,
) -> str:
    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        raise ValueError("Telegram update_id is required")
    raw = json.dumps(update, ensure_ascii=False)
    try:
        db.execute(
            "INSERT INTO inbox_events(provider, external_event_id, event_type, payload) VALUES (?, ?, ?, ?)",
            ("telegram", str(update_id), "update", raw),
        )
    except sqlite3.IntegrityError:
        return "duplicate"

    message = update.get("message") or {}
    sender = message.get("from") or {}
    telegram_id = sender.get("id")
    if not isinstance(telegram_id, int):
        return "ignored"
    user = upsert_user(
        db,
        {"telegram_id": telegram_id, "telegram_username": sender.get("username")},
    )
    message_chat_id = (message.get("chat") or {}).get("id", telegram_id)
    command = _command(message.get("text"))
    if command == "/start":
        await telegram.send_message(message_chat_id, "Добро пожаловать! Используйте /status для проверки подписки.")
    elif command == "/status":
        subscription = db.execute(
            "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user["id"],)
        ).fetchone()
        access = effective_access(user, subscription)
        until = (subscription["provider_paid_until"] if subscription else None) or user["manual_access_until"]
        text = f"Статус: {'активна' if access == 'active' else 'не активна'}."
        if until:
            text += f" Доступ до: {until}."
        await telegram.send_message(message_chat_id, text)
    elif command == "/help":
        await telegram.send_message(message_chat_id, "/start — регистрация\n/status — статус\n/site-access — доступ к сайту\n/pay — оплата\n/renew — продление")
    elif command == "/site-access":
        subscription = db.execute(
            "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user["id"],)
        ).fetchone()
        if effective_access(user, subscription) != "active":
            await telegram.send_message(message_chat_id, "Доступ к сайту доступен только при активной подписке.")
        elif wordpress is None:
            await telegram.send_message(message_chat_id, "Доступ к сайту пока не настроен. Обратитесь к администратору.")
        else:
            temporary_password = secrets.token_urlsafe(18)
            expires_at = utc_now() + timedelta(hours=temporary_password_hours)
            try:
                result = await wordpress.sync_user(
                    {
                        "action": "create_or_activate",
                        "user_id": user["wordpress_user_id"] or 0,
                        "username": user["wordpress_login"] or "",
                        "email": user["wordpress_email"] or "",
                        "role": user["wordpress_role"] or "subscriber",
                        "password": temporary_password,
                        "temporary_expires_at": expires_at.isoformat(),
                    },
                    f"telegram-site-access-{update_id}",
                )
            except (WordPressError, ValueError):
                await telegram.send_message(message_chat_id, "Не удалось выдать доступ к сайту. Попробуйте позже.")
            else:
                await telegram.send_message(
                    message_chat_id,
                    "Доступ к сайту выдан.\n"
                    f"Логин: {result['login']}\n"
                    f"Временный пароль: {temporary_password}\n"
                    f"Действует до: {expires_at.isoformat()}\n\n"
                    "Не пересылайте это сообщение. После входа смените пароль.",
                )
    db.execute("UPDATE inbox_events SET processed_at = CURRENT_TIMESTAMP WHERE provider = 'telegram' AND external_event_id = ?", (str(update_id),))
    return "processed"

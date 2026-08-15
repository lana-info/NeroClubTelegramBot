from __future__ import annotations

import json
import sqlite3
from typing import Any

from .access import effective_access, upsert_user
from .integrations.telegram import TelegramClient
from .integrations.wordpress import WordPressClient
from .site_access import SiteAccessError, issue_site_credentials
from .keys import AppKeyError, display_expiry, keys_for_user
from .telegram_menu import REPLY_KEYBOARD, command_from_text
from .telegram_menu import ADMIN_MENU_BUTTONS


async def process_update(
    db: sqlite3.Connection,
    update: dict[str, Any],
    telegram: TelegramClient,
    *,
    chat_id: int | str | None = None,
    wordpress: WordPressClient | None = None,
    app_keys_encryption_key: str = "",
    admin_telegram_ids: tuple[int, ...] = (),
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
    message_text = message.get("text") or ""
    command = command_from_text(message.get("text"))
    is_admin = telegram_id in admin_telegram_ids
    if command == "/start":
        await telegram.send_message(
            message_chat_id,
            "Добро пожаловать! Выберите нужный пункт меню.",
            reply_markup=REPLY_KEYBOARD,
        )
    elif command == "/admin":
        if not is_admin:
            await telegram.send_message(message_chat_id, "Эта команда доступна только администратору.")
        else:
            await telegram.send_message(
                message_chat_id,
                "Панель администратора:\n\n"
                "📥 Новые обращения — список сообщений пользователей\n"
                "Ответ: /reply НОМЕР ТЕКСТ",
                reply_markup={"keyboard": [[{"text": text} for text in row] for row in ADMIN_MENU_BUTTONS], "resize_keyboard": True},
            )
    elif command == "/admin_inbox":
        if not is_admin:
            await telegram.send_message(message_chat_id, "Эта команда доступна только администратору.")
        else:
            requests = db.execute(
                "SELECT sr.id, u.telegram_id, u.telegram_username, sr.message_text "
                "FROM support_requests sr JOIN users u ON u.id = sr.user_id "
                "WHERE sr.status = 'new' ORDER BY sr.id LIMIT 20"
            ).fetchall()
            if not requests:
                text = "Новых обращений нет."
            else:
                text = "Новые обращения:\n\n" + "\n\n".join(
                    f"#{item['id']} от {item['telegram_username'] or item['telegram_id']}: {item['message_text']}"
                    for item in requests
                )
            await telegram.send_message(message_chat_id, text)
    elif command == "/reply":
        if not is_admin:
            await telegram.send_message(message_chat_id, "Эта команда доступна только администратору.")
        else:
            parts = message_text.split(maxsplit=2)
            if len(parts) < 3 or not parts[1].isdigit():
                await telegram.send_message(message_chat_id, "Формат ответа: /reply НОМЕР ТЕКСТ")
            else:
                request = db.execute(
                    "SELECT sr.id, sr.user_id, u.telegram_id FROM support_requests sr "
                    "JOIN users u ON u.id = sr.user_id WHERE sr.id = ? AND sr.status = 'new'",
                    (int(parts[1]),),
                ).fetchone()
                if not request:
                    await telegram.send_message(message_chat_id, "Обращение не найдено или уже закрыто.")
                else:
                    await telegram.send_message(request["telegram_id"], f"Ответ администратора:\n\n{parts[2]}")
                    db.execute(
                        "UPDATE support_requests SET status = 'answered', updated_at = CURRENT_TIMESTAMP, resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (request["id"],),
                    )
                    await telegram.send_message(message_chat_id, "Ответ отправлен пользователю.")
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
        await telegram.send_message(
            message_chat_id,
            "Выберите действие кнопкой ниже:\n\n"
            "📊 Статус подписки — текущий доступ и дата окончания\n"
            "🔑 Мои ключи — ключи приложений и сроки\n"
            "🌐 Доступ к сайту — логин и постоянный пароль\n"
            "💳 Оплатить / продлить — ссылка на оплату\n"
            "ℹ️ Помощь — эта подсказка",
            reply_markup=REPLY_KEYBOARD,
        )
    elif command == "/support":
        parts = message_text.split(maxsplit=1)
        if not admin_telegram_ids:
            text = "Связь с администратором пока не настроена."
        elif len(parts) == 1:
            db.execute(
                "INSERT INTO support_requests(user_id, status) VALUES (?, 'awaiting_message')",
                (user["id"],),
            )
            text = "Напишите следующим сообщением, что произошло или какой вопрос нужно решить."
        else:
            request_id = _create_support_request(db, user["id"], parts[1])
            await _notify_admins(telegram, admin_telegram_ids, request_id, user, parts[1])
            text = "Сообщение отправлено администратору."
        await telegram.send_message(message_chat_id, text, reply_markup=REPLY_KEYBOARD)
    elif command == "/my-keys":
        try:
            keys = keys_for_user(db, user["id"], app_keys_encryption_key)
        except (AppKeyError, ValueError):
            keys = None
        if keys is None:
            text = "Выдача ключей пока не настроена. Обратитесь к администратору."
        elif not keys:
            text = "Для вашей активной подписки пока нет доступных ключей."
        else:
            items = []
            for item in keys:
                expires = display_expiry(item["key_expires_at"])
                items.append(f"{item['app_name']}\nКлюч: {item['key']}\nДействует до: {expires}")
            text = "Ваши ключи приложений:\n\n" + "\n\n".join(items)
        await telegram.send_message(message_chat_id, text)
    elif command in {"/pay", "/renew"}:
        await telegram.send_message(
            message_chat_id,
            "Оплата и продление будут доступны после подключения платёжного провайдера. Обратитесь к администратору.",
            reply_markup=REPLY_KEYBOARD,
        )
    elif command == "/site-access":
        subscription = db.execute(
            "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user["id"],)
        ).fetchone()
        if effective_access(user, subscription) != "active":
            await telegram.send_message(message_chat_id, "Доступ к сайту доступен только при активной подписке.")
        elif wordpress is None:
            await telegram.send_message(message_chat_id, "Доступ к сайту пока не настроен. Обратитесь к администратору.")
        else:
            try:
                await issue_site_credentials(
                    db, user["id"], telegram, wordpress,
                    idempotency_key=f"telegram-site-access-{update_id}",
                )
            except (SiteAccessError, ValueError):
                await telegram.send_message(message_chat_id, "Не удалось выдать доступ к сайту. Попробуйте позже.")
    elif message_text and not message_text.startswith("/"):
        pending = db.execute(
            "SELECT id FROM support_requests WHERE user_id = ? AND status = 'awaiting_message' ORDER BY id DESC LIMIT 1",
            (user["id"],),
        ).fetchone()
        if pending and admin_telegram_ids:
            db.execute(
                "UPDATE support_requests SET message_text = ?, status = 'new', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (message_text[:4000], pending["id"]),
            )
            await _notify_admins(telegram, admin_telegram_ids, pending["id"], user, message_text[:4000])
            await telegram.send_message(message_chat_id, "Сообщение отправлено администратору.", reply_markup=REPLY_KEYBOARD)
    db.execute("UPDATE inbox_events SET processed_at = CURRENT_TIMESTAMP WHERE provider = 'telegram' AND external_event_id = ?", (str(update_id),))
    return "processed"


def _create_support_request(db: sqlite3.Connection, user_id: int, message_text: str) -> int:
    cursor = db.execute(
        "INSERT INTO support_requests(user_id, message_text, status) VALUES (?, ?, 'new')",
        (user_id, message_text[:4000]),
    )
    return int(cursor.lastrowid)


async def _notify_admins(telegram: TelegramClient, admin_ids: tuple[int, ...], request_id: int, user: sqlite3.Row, message_text: str) -> None:
    text = (
        f"🆘 Новое обращение #{request_id}\n"
        f"Пользователь: {user['telegram_username'] or 'без username'}\n"
        f"Telegram ID: {user['telegram_id']}\n\n"
        f"{message_text}\n\n"
        f"Ответ: /reply {request_id} ваш текст"
    )
    for admin_id in admin_ids:
        await telegram.send_message(admin_id, text[:4000])

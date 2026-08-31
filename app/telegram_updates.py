from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from .access import effective_access, upsert_user
from .integrations.telegram import TelegramClient
from .integrations.wordpress import WordPressClient
from .stripe_checkout import StripeCheckoutError, create_checkout_session
from .membership import process_telegram_join_request
from .site_access import SiteAccessError, issue_site_credentials
from .site_access import queue_site_access_job
from .keys import AppKeyError, display_expiry, keys_for_user
from .telegram_menu import REPLY_KEYBOARD, command_from_text
from .telegram_menu import ADMIN_MENU_BUTTONS


async def process_update(
    db: sqlite3.Connection,
    update: dict[str, Any],
    telegram: TelegramClient,
    *,
    chat_id: int | str | tuple[int | str, ...] | None = None,
    wordpress: WordPressClient | None = None,
    app_keys_encryption_key: str = "",
    admin_telegram_ids: tuple[int, ...] = (),
    payment_url: str = "",
    payment_source: str = "sheet",
    new_member_price_usd: str = "20",
    returning_member_price_usd: str = "10",
    new_member_one_time_payment_url: str = "",
    new_member_recurring_payment_url: str = "",
    returning_member_one_time_payment_url: str = "",
    returning_member_recurring_payment_url: str = "",
    stripe_secret_key: str = "",
    stripe_price_id: str = "",
    checkout_success_url: str = "",
    checkout_cancel_url: str = "",
    feature_flags: dict[str, bool] | None = None,
) -> str:
    feature_flags = feature_flags or {
        "wordpress_access": True, "app_keys": True,
    }
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

    chat_member = update.get("chat_member")
    if chat_member is not None:
        return await _process_chat_member_update(
            db,
            update_id,
            chat_member,
            chat_id=chat_id,
        )

    chat_join_request = update.get("chat_join_request")
    if chat_join_request is not None:
        allowed = chat_id if isinstance(chat_id, tuple) else (chat_id,) if chat_id is not None else ()
        return await process_telegram_join_request(
            db, telegram, update_id, chat_join_request, allowed_chat_ids=allowed
        )

    message = update.get("message") or {}
    message_chat = message.get("chat") or {}
    if message_chat.get("type") and message_chat.get("type") != "private":
        _mark_telegram_update_processed(db, update_id)
        return "ignored"
    sender = message.get("from") or {}
    telegram_id = sender.get("id")
    if not isinstance(telegram_id, int):
        return "ignored"
    user = upsert_user(
        db,
        {"telegram_id": telegram_id, "telegram_username": sender.get("username")},
    )
    message_chat_id = message_chat.get("id", telegram_id)
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
                    f"#{item['id']} от {_display_user(item['telegram_username'], item['telegram_id'])}: {item['message_text']}"
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
            text += f" Доступ до: {format_subscription_date(until)}."
        await telegram.send_message(message_chat_id, text)
    elif command in {"/reminders_on", "/reminders_off", "/reminders_toggle"}:
        current = db.execute("SELECT reminders_enabled FROM users WHERE id = ?", (user["id"],)).fetchone()[0]
        enabled = not bool(current) if command == "/reminders_toggle" else command == "/reminders_on"
        db.execute(
            "UPDATE users SET reminders_enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (int(enabled), user["id"]),
        )
        text = "Напоминания включены." if enabled else "Напоминания выключены."
        await telegram.send_message(message_chat_id, text, reply_markup=REPLY_KEYBOARD)
    elif command == "/help":
        await telegram.send_message(
            message_chat_id,
            "Выберите действие кнопкой ниже:\n\n"
            "📊 Статус подписки — текущий доступ и дата окончания\n"
            "🔑 Мои ключи — ключи приложений\n"
            "🌐 Доступ к сайту — логин и постоянный пароль\n"
            "💳 Получить ссылку на оплату — выбрать разовую или ежемесячную оплату\n"
            "🧾 Сообщить об оплате — отправить данные платежа администратору\n"
            "🔔 Уведомления об оплате — включить или выключить напоминания\n"
            "ℹ️ Помощь — эта подсказка",
            reply_markup=REPLY_KEYBOARD,
        )
    elif command == "/support":
        parts = message_text.split(maxsplit=1) if message_text.startswith("/") else [message_text]
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
        if not feature_flags.get("app_keys", True):
            await telegram.send_message(message_chat_id, "Выдача ключей временно приостановлена администратором.")
            return "processed"
        subscription = db.execute(
            "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user["id"],)
        ).fetchone()
        if user["whitelist"] or effective_access(user, subscription) != "active":
            await telegram.send_message(
                message_chat_id,
                "Получение ключей приложений доступно только при оплаченной подписке на клуб.",
            )
            return "processed"
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
    elif command in {"/report_payment", "/confirm_payment"}:
        if payment_source == "sheet":
            if admin_telegram_ids:
                db.execute(
                    "INSERT INTO support_requests(user_id, status) VALUES (?, 'awaiting_payment')",
                    (user["id"],),
                )
            text = (
                "Чтобы я нашла ваш платёж, напишите следующим сообщением:\n"
                "• email, который использовали при оплате;\n"
                "• дату оплаты;\n"
                "• способ оплаты: Stripe или PayPal.\n\n"
                "Скриншот не нужен."
            )
        else:
            text = "Сообщите администратору email, дату и способ оплаты."
        await telegram.send_message(message_chat_id, text, reply_markup=REPLY_KEYBOARD)
    elif command in {"/pay", "/renew", "/payment_links"}:
        text = "Оплата и продление будут доступны после подключения платёжного провайдера. Обратитесь к администратору."
        try:
            if payment_source == "sheet":
                has_paid_history = db.execute(
                    "SELECT 1 FROM subscriptions WHERE user_id = ? "
                    "AND (payment_status = 'paid' OR provider_paid_until IS NOT NULL) LIMIT 1",
                    (user["id"],),
                ).fetchone() is not None
                price = returning_member_price_usd if has_paid_history else new_member_price_usd
                one_time_url = (
                    returning_member_one_time_payment_url
                    if has_paid_history else new_member_one_time_payment_url
                )
                recurring_url = (
                    returning_member_recurring_payment_url
                    if has_paid_history else new_member_recurring_payment_url
                )
                text = (
                    "Выберите способ оплаты:\n"
                    f"Сумма к оплате: {price} USD.\n\n"
                    f"• Разовая оплата: {one_time_url or 'ссылка будет добавлена администратором'}\n"
                    f"• Подписка с ежемесячным автосписанием (можно отменить в любое время): "
                    f"{recurring_url or 'ссылка будет добавлена администратором'}\n\n"
                    "Если хотите, после оплаты сообщите об оплате администратору через кнопку «🧾 Сообщить об оплате»."
                )
            elif stripe_secret_key and stripe_price_id:
                session = await create_checkout_session(
                    stripe_secret_key,
                    stripe_price_id,
                    user["id"],
                    success_url=checkout_success_url,
                    cancel_url=checkout_cancel_url,
                    customer_email=user["wordpress_email"],
                )
                db.execute(
                    "INSERT OR IGNORE INTO stripe_checkout_sessions(session_id, user_id, status) VALUES (?, ?, 'open')",
                    (session["id"], user["id"]),
                )
                text = f"Оплатить или продлить подписку:\n{session['url']}"
            elif payment_url:
                text = f"Оплатить или продлить подписку: {payment_url}"
        except StripeCheckoutError:
            text = "Не удалось создать ссылку на оплату. Попробуйте позже или обратитесь к администратору."
        await telegram.send_message(message_chat_id, text, reply_markup=REPLY_KEYBOARD)
    elif command == "/site-access":
        if not feature_flags.get("wordpress_access", True):
            await telegram.send_message(message_chat_id, "Выдача доступа к сайту временно приостановлена администратором.")
            return "processed"
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
            except (SiteAccessError, ValueError) as exc:
                text = "Не удалось выдать доступ к сайту. Попробуйте позже."
                if str(exc) == "site credentials were already delivered":
                    text = (
                        "Логин и первоначальный пароль уже были отправлены ранее. "
                        "Проверьте личные сообщения и смените пароль после входа. "
                        "Если доступ потерян, обратитесь к администратору."
                    )
                await telegram.send_message(message_chat_id, text)
    elif message_text and not message_text.startswith("/"):
        pending = db.execute(
            "SELECT id, status FROM support_requests "
            "WHERE user_id = ? AND status IN ('awaiting_message', 'awaiting_payment') "
            "AND updated_at >= datetime('now', '-10 minutes') "
            "ORDER BY id DESC LIMIT 1",
            (user["id"],),
        ).fetchone()
        if pending and admin_telegram_ids:
            db.execute(
                "UPDATE support_requests SET message_text = ?, status = 'new', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (message_text[:4000], pending["id"]),
            )
            subject = "Подтверждение оплаты" if pending["status"] == "awaiting_payment" else "Новое обращение"
            await _notify_admins(telegram, admin_telegram_ids, pending["id"], user, message_text[:4000], subject=subject)
            confirmation = "Информация отправлена администратору." if pending["status"] == "awaiting_payment" else "Сообщение отправлено администратору."
            await telegram.send_message(message_chat_id, confirmation, reply_markup=REPLY_KEYBOARD)
    db.execute("UPDATE inbox_events SET processed_at = CURRENT_TIMESTAMP WHERE provider = 'telegram' AND external_event_id = ?", (str(update_id),))
    return "processed"


async def _process_chat_member_update(
    db: sqlite3.Connection,
    update_id: int,
    chat_member: dict[str, Any],
    *,
    chat_id: int | str | tuple[int | str, ...] | None,
) -> str:
    event_chat_id = (chat_member.get("chat") or {}).get("id")
    allowed_chat_ids = chat_id if isinstance(chat_id, tuple) else (chat_id,)
    if chat_id is not None and not any(str(event_chat_id) == str(allowed) for allowed in allowed_chat_ids):
        _mark_telegram_update_processed(db, update_id)
        return "ignored"

    new_member = chat_member.get("new_chat_member") or {}
    member_user = new_member.get("user") or {}
    telegram_id = member_user.get("id")
    status = new_member.get("status")
    if not isinstance(telegram_id, int) or status not in {
        "left", "kicked", "member", "restricted", "administrator", "creator"
    }:
        _mark_telegram_update_processed(db, update_id)
        return "ignored"

    user = db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    if not user:
        _mark_telegram_update_processed(db, update_id)
        return "ignored"

    previous_status = user["telegram_membership_status"]
    ban_source = user["telegram_ban_source"]
    if status == "kicked":
        if ban_source != "system":
            db.execute(
                "UPDATE users SET telegram_membership_status = 'kicked', telegram_banned = 1, "
                "telegram_ban_source = 'admin', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (user["id"],),
            )
            queue_site_access_job(db, user["id"], "deactivate", f"telegram-kicked-{update_id}")
        else:
            db.execute(
                "UPDATE users SET telegram_membership_status = 'kicked', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (user["id"],),
            )
    elif status == "left":
        db.execute(
            "UPDATE users SET telegram_membership_status = 'left', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (user["id"],),
        )
        queue_site_access_job(db, user["id"], "deactivate", f"telegram-left-{update_id}")
    else:
        db.execute(
            "UPDATE users SET telegram_membership_status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, user["id"]),
        )
        if previous_status == "left" and not user["telegram_banned"]:
            subscription = db.execute(
                "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user["id"],)
            ).fetchone()
            if effective_access(user, subscription) == "active":
                queue_site_access_job(db, user["id"], "restore", f"telegram-restore-{update_id}")

    _mark_telegram_update_processed(db, update_id)
    return "processed"


def _mark_telegram_update_processed(db: sqlite3.Connection, update_id: int) -> None:
    db.execute(
        "UPDATE inbox_events SET processed_at = CURRENT_TIMESTAMP "
        "WHERE provider = 'telegram' AND external_event_id = ?",
        (str(update_id),),
    )


def _create_support_request(db: sqlite3.Connection, user_id: int, message_text: str) -> int:
    cursor = db.execute(
        "INSERT INTO support_requests(user_id, message_text, status) VALUES (?, ?, 'new')",
        (user_id, message_text[:4000]),
    )
    return int(cursor.lastrowid)


def _display_user(username: str | None, telegram_id: int) -> str:
    if username:
        return username if username.startswith("@") else f"@{username}"
    return f"Telegram ID {telegram_id}"


def format_subscription_date(value: str) -> str:
    months = (
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    )
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return f"{date.day} {months[date.month - 1]} {date.year} года"


async def _notify_admins(
    telegram: TelegramClient,
    admin_ids: tuple[int, ...],
    request_id: int,
    user: sqlite3.Row,
    message_text: str,
    *,
    subject: str = "Новое обращение",
) -> None:
    text = (
        f"🆘 {subject} #{request_id}\n"
        f"Пользователь: {_display_user(user['telegram_username'], user['telegram_id'])}\n"
        f"Telegram ID: {user['telegram_id']}\n\n"
        f"{message_text}\n\n"
        f"Ответ: /reply {request_id} ваш текст"
    )
    for admin_id in admin_ids:
        await telegram.send_message(admin_id, text[:4000])

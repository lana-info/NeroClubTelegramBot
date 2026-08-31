from __future__ import annotations

from typing import Any


MENU_BUTTONS = [
    ["📊 Статус подписки", "🔑 Мои ключи"],
    ["🌐 Доступ к сайту", "🧾 Сообщить об оплате"],
    ["💬 Связаться с администратором", "ℹ️ Помощь"],
]

ADMIN_MENU_BUTTONS = [["📥 Новые обращения"], ["ℹ️ Помощь"]]

REPLY_KEYBOARD: dict[str, Any] = {
    "keyboard": [{"text": text} for row in MENU_BUTTONS for text in row],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "Выберите действие",
}

# Telegram reply keyboards are flattened by the Bot API. Keep a separate row
# layout for the actual keyboard payload.
REPLY_KEYBOARD["keyboard"] = [[{"text": text} for text in row] for row in MENU_BUTTONS]

BUTTON_COMMANDS = {
    "📊 статус подписки": "/status",
    "🔑 мои ключи": "/my-keys",
    "🌐 доступ к сайту": "/site-access",
    "🧾 сообщить об оплате": "/pay",
    "ℹ️ помощь": "/help",
    "💬 связаться с администратором": "/support",
    "📥 новые обращения": "/admin_inbox",
}

BOT_COMMANDS = [
    {"command": "start", "description": "Открыть главное меню"},
    {"command": "status", "description": "Проверить подписку"},
    {"command": "my_keys", "description": "Получить ключи приложений"},
    {"command": "site_access", "description": "Получить доступ к сайту"},
    {"command": "pay", "description": "Оплатить или продлить подписку"},
    {"command": "help", "description": "Помощь"},
]


def command_from_text(text: str | None) -> str | None:
    if not text:
        return None
    normalized = text.strip().lower()
    if normalized in BUTTON_COMMANDS:
        return BUTTON_COMMANDS[normalized]
    if not normalized.startswith("/"):
        return None
    return normalized.split()[0].split("@", 1)[0]

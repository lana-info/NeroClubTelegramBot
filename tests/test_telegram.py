import asyncio
import json

import httpx

from app.db import Database
from app.integrations.telegram import TelegramClient
from app.telegram_updates import process_update


class TelegramTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.calls = []

    async def handle_async_request(self, request):
        self.calls.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}, request=request)


class WordPressTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.payload = None

    async def handle_async_request(self, request):
        self.payload = json.loads(request.content)
        return httpx.Response(200, json={"user_id": 9, "login": "anna"}, request=request)


def test_start_update_is_idempotent(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'telegram.db'}")
    db.init_schema()
    transport = TelegramTransport()
    client = TelegramClient("test-token", transport=transport)
    update = {"update_id": 10, "message": {"chat": {"id": 42}, "from": {"id": 42, "username": "anna"}, "text": "/start"}}

    with db.connect() as connection:
        first = asyncio.run(process_update(connection, update, client))
    with db.connect() as connection:
        second = asyncio.run(process_update(connection, update, client))

    assert first == "processed"
    assert second == "duplicate"
    assert len(transport.calls) == 1
    assert transport.calls[0]["chat_id"] == 42


def test_site_access_uses_active_subscription_and_does_not_store_password(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'site-access.db'}")
    db.init_schema()
    telegram_transport = TelegramTransport()
    wordpress_transport = WordPressTransport()
    telegram = TelegramClient("test-token", transport=telegram_transport)
    from app.integrations.wordpress import WordPressClient
    wordpress = WordPressClient("https://example.test", "shared-secret", transport=wordpress_transport)
    update = {"update_id": 11, "message": {"chat": {"id": 42}, "from": {"id": 42, "username": "anna"}, "text": "/site-access"}}
    with db.connect() as connection:
        user = connection.execute(
            "INSERT INTO users(telegram_id, wordpress_email) VALUES (?, ?) RETURNING id",
            (42, "anna@example.com"),
        ).fetchone()
        connection.execute(
            "INSERT INTO subscriptions(user_id, provider, provider_subscription_id, billing_status, payment_status, provider_paid_until) VALUES (?, 'stripe', 'sub_1', 'active', 'paid', '2999-01-01T00:00:00+00:00')",
            (user["id"],),
        )
        result = asyncio.run(process_update(connection, update, telegram, wordpress=wordpress))
        assert result == "processed"
        assert "password" not in json.dumps(dict(connection.execute("SELECT * FROM users").fetchone())).lower()
    assert wordpress_transport.payload["password"]
    assert "Постоянный пароль" in telegram_transport.calls[0]["text"]
    assert "temporary_expires_at" not in wordpress_transport.payload

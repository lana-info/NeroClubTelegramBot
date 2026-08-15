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

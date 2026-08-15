from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import httpx


class WordPressError(RuntimeError):
    pass


class WordPressClient:
    def __init__(self, base_url: str, shared_secret: str, *, transport: httpx.AsyncBaseTransport | None = None):
        if not base_url or not shared_secret:
            raise ValueError("WordPress integration is not configured")
        self.url = f"{base_url.rstrip('/')}/wp-json/nero-club/v1/users/sync"
        self.secret = shared_secret
        self.transport = transport

    async def sync_user(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(self.secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-Nero-Timestamp": timestamp,
            "X-Nero-Signature": signature,
            "X-Nero-Idempotency-Key": idempotency_key,
        }
        async with httpx.AsyncClient(timeout=15, transport=self.transport) as client:
            response = await client.post(self.url, content=body, headers=headers)
        if response.status_code >= 400:
            raise WordPressError(f"WordPress HTTP error: {response.status_code}")
        data = response.json()
        if not isinstance(data, dict) or "user_id" not in data:
            raise WordPressError("WordPress returned an invalid response")
        return data

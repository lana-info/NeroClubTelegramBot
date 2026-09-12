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
    def __init__(
        self,
        base_url: str,
        shared_secret: str,
        *,
        mcp_username: str = "",
        mcp_application_password: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not base_url or not shared_secret:
            raise ValueError("WordPress integration is not configured")
        base_url = base_url.rstrip("/")
        self.url = f"{base_url}/wp-json/nero-club/v1/users/sync"
        self.mcp_url = f"{base_url}/wp-json/mcp/mcp-adapter-default-server"
        self.secret = shared_secret
        self.mcp_username = mcp_username.strip()
        self.mcp_application_password = mcp_application_password.strip()
        self.transport = transport

    @property
    def mcp_enabled(self) -> bool:
        return bool(self.mcp_username and self.mcp_application_password)

    async def _mcp_execute(self, ability_name: str, parameters: dict[str, Any]) -> dict[str, Any]:
        request = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000),
            "method": "tools/call",
            "params": {
                "name": "mcp-adapter-execute-ability",
                "arguments": {"ability_name": ability_name, "parameters": parameters},
            },
        }
        auth = httpx.BasicAuth(self.mcp_username, self.mcp_application_password)
        async with httpx.AsyncClient(timeout=15, transport=self.transport, auth=auth) as client:
            response = await client.post(self.mcp_url, json=request)
        if response.status_code >= 400:
            raise WordPressError(f"WordPress MCP HTTP error: {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise WordPressError("WordPress MCP returned invalid JSON") from exc
        if not isinstance(data, dict) or data.get("error"):
            raise WordPressError("WordPress MCP returned an error")
        result = data.get("result")
        if isinstance(result, dict) and result.get("isError"):
            raise WordPressError("WordPress MCP ability failed")
        return data

    async def _sync_access_via_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:
        user_id = int(payload.get("user_id") or 0)
        if user_id <= 0:
            raise WordPressError("WordPress MCP deactivation requires wordpress_user_id")
        action = str(payload.get("action") or "")
        blocked = "1" if action == "deactivate" else "0"
        await self._mcp_execute(
            "mosmcp/update-user-metadata",
            {"id": user_id, "meta_key": "_nero_club_access_blocked", "meta_value": blocked},
        )
        return {
            "user_id": user_id,
            "action": action,
            "access_blocked": blocked == "1",
            "transport": "mcp",
        }

    async def sync_user(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        if self.mcp_enabled and payload.get("action") in {"deactivate", "restore"}:
            return await self._sync_access_via_mcp(payload)

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

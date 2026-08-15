from __future__ import annotations

from typing import Any

import httpx


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, token: str, *, transport: httpx.AsyncBaseTransport | None = None):
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is not configured")
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.transport = transport

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        async with httpx.AsyncClient(timeout=15, transport=self.transport) as client:
            response = await client.post(f"{self.base_url}/{method}", json=payload)
        if response.status_code >= 400:
            raise TelegramError(f"Telegram HTTP error: {response.status_code}")
        body = response.json()
        if not body.get("ok"):
            raise TelegramError(body.get("description", "Telegram API error"))
        return body.get("result")

    async def send_message(
        self, chat_id: int | str, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> Any:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self.call("sendMessage", payload)

    async def set_my_commands(self, commands: list[dict[str, str]]) -> Any:
        return await self.call("setMyCommands", {"commands": commands})

    async def create_chat_invite_link(
        self, chat_id: int | str, *, expire_date: int | None = None, creates_join_request: bool = True
    ) -> Any:
        payload: dict[str, Any] = {"chat_id": chat_id, "creates_join_request": creates_join_request}
        if expire_date is not None:
            payload["expire_date"] = expire_date
        return await self.call("createChatInviteLink", payload)

    async def get_chat_member(self, chat_id: int | str, user_id: int) -> Any:
        return await self.call("getChatMember", {"chat_id": chat_id, "user_id": user_id})

    async def approve_chat_join_request(self, chat_id: int | str, user_id: int) -> Any:
        return await self.call("approveChatJoinRequest", {"chat_id": chat_id, "user_id": user_id})

    async def decline_chat_join_request(self, chat_id: int | str, user_id: int) -> Any:
        return await self.call("declineChatJoinRequest", {"chat_id": chat_id, "user_id": user_id})

    async def ban_chat_member(self, chat_id: int | str, user_id: int) -> Any:
        return await self.call("banChatMember", {"chat_id": chat_id, "user_id": user_id})

    async def unban_chat_member(self, chat_id: int | str, user_id: int) -> Any:
        return await self.call("unbanChatMember", {"chat_id": chat_id, "user_id": user_id, "only_if_banned": True})

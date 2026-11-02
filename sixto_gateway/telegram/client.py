"""aiogram used only as a typed Bot API client (no Dispatcher): the gateway owns the offset."""

from __future__ import annotations

from typing import Protocol

from sixto_gateway.telegram.normalize import IncomingUpdate, normalize


class TelegramClient(Protocol):
    async def get_updates(
        self, offset: int, timeout: int, allowed_updates: list[str]
    ) -> list[IncomingUpdate]: ...


class AiogramTelegramClient:
    def __init__(self, bot):  # type: ignore[no-untyped-def]
        self._bot = bot

    async def get_updates(
        self, offset: int, timeout: int, allowed_updates: list[str]
    ) -> list[IncomingUpdate]:
        updates = await self._bot.get_updates(
            offset=offset, timeout=timeout, allowed_updates=allowed_updates
        )
        return [u for update in updates if (u := normalize(update)) is not None]

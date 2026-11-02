"""aiogram used only as a typed Bot API client (no Dispatcher): the gateway owns the offset."""

from __future__ import annotations

from typing import Protocol

from sixto_gateway.telegram.normalize import IncomingUpdate, normalize


class TelegramClient(Protocol):
    async def get_updates(
        self, offset: int, timeout: int, allowed_updates: list[str]
    ) -> list[IncomingUpdate]: ...

    async def send_message(
        self, chat_id: int, topic_id: int | None, text: str, parse_mode: str | None
    ) -> int:
        """Send a text message (link previews off); returns the Telegram message id."""
        ...

    async def set_reaction(self, chat_id: int, message_id: int, emoji: str) -> None: ...


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

    async def send_message(
        self, chat_id: int, topic_id: int | None, text: str, parse_mode: str | None
    ) -> int:
        from aiogram.types import LinkPreviewOptions

        message = await self._bot.send_message(
            chat_id=chat_id,
            text=text,
            message_thread_id=topic_id,
            parse_mode=parse_mode,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return int(message.message_id)

    async def set_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        from aiogram.types import ReactionTypeEmoji

        await self._bot.set_message_reaction(
            chat_id=chat_id, message_id=message_id, reaction=[ReactionTypeEmoji(emoji=emoji)]
        )

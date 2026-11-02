"""aiogram used only as a typed Bot API client (no Dispatcher): the gateway owns the offset."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol

from sixto_gateway.telegram.errors import (
    PermanentSendError,
    RetryAfterError,
    SendError,
    TransientSendError,
)
from sixto_gateway.telegram.normalize import IncomingUpdate, normalize


class TelegramClient(Protocol):
    async def get_updates(
        self, offset: int, timeout: int, allowed_updates: list[str]
    ) -> list[IncomingUpdate]: ...

    async def send_message(
        self, chat_id: int, topic_id: int | None, text: str, parse_mode: str | None
    ) -> int:
        """Send a text message (link previews off); returns the Telegram message id.

        Failures raise ``SendError`` subclasses (see ``telegram.errors``).
        """
        ...

    async def set_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        """Set one emoji reaction. Failures raise ``SendError`` subclasses, like sends."""
        ...

    async def set_my_commands(self, chat_id: int | None, commands: list[tuple[str, str]]) -> None:
        """Set the ``/`` menu of one private chat (``chat_id``), or the default (None)."""
        ...

    async def delete_my_commands(self, chat_id: int) -> None:
        """Remove one private chat's own menu, so it falls back to the default.

        Failures raise ``SendError`` subclasses, like sends.
        """
        ...


def translate_send_error(exc: Exception) -> SendError | None:
    """Map an aiogram exception to the sender's vocabulary (None: not an aiogram error)."""
    from aiogram.exceptions import (
        TelegramAPIError,
        TelegramNetworkError,
        TelegramRetryAfter,
        TelegramServerError,
    )

    if isinstance(exc, TelegramRetryAfter):
        return RetryAfterError(exc.retry_after, exc.message)
    if isinstance(exc, TelegramServerError | TelegramNetworkError):
        return TransientSendError(f"{type(exc).__name__}: {exc.message}")
    if isinstance(exc, TelegramAPIError):  # every other 4xx can never succeed on a retry
        return PermanentSendError(f"{type(exc).__name__}: {exc.message}")
    return None


@contextmanager
def _translated_errors() -> Iterator[None]:
    """Raise aiogram failures as ``SendError`` subclasses; anything else unchanged."""
    try:
        yield
    except Exception as exc:
        raise translate_send_error(exc) or exc from exc


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

        with _translated_errors():
            message = await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                # Telegram rejects thread id 1 (the General topic); omitting it posts there.
                message_thread_id=None if topic_id == 1 else topic_id,
                parse_mode=parse_mode,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        return int(message.message_id)

    async def set_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        from aiogram.types import ReactionTypeEmoji

        with _translated_errors():
            await self._bot.set_message_reaction(
                chat_id=chat_id, message_id=message_id, reaction=[ReactionTypeEmoji(emoji=emoji)]
            )

    async def set_my_commands(self, chat_id: int | None, commands: list[tuple[str, str]]) -> None:
        from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

        scope = (
            BotCommandScopeDefault() if chat_id is None else BotCommandScopeChat(chat_id=chat_id)
        )
        with _translated_errors():
            await self._bot.set_my_commands(
                commands=[BotCommand(command=n, description=d) for n, d in commands], scope=scope
            )

    async def delete_my_commands(self, chat_id: int) -> None:
        from aiogram.types import BotCommandScopeChat

        with _translated_errors():
            await self._bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=chat_id))

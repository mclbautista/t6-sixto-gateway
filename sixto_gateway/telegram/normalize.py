"""Update normalization: same rules as the watchdog's adapter.

``message`` or ``edited_message`` only; ``text`` falls back to ``caption``; ``raw`` is
``model_dump(mode="json", exclude_unset=True)`` so it is exactly what Telegram sent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class IncomingUpdate:
    update_id: int
    chat_id: int
    topic_id: int | None
    message_id: int
    from_id: int | None
    from_username: str | None
    text: str
    edited: bool
    edit_date: int | None
    raw: dict[str, Any]
    chat_type: str = ""  # "private", "group", "supergroup", ...
    command: str | None = None  # lower-case name when the text starts with a /command
    command_args: str = ""
    from_first_name: str = ""
    from_last_name: str = ""
    callback_id: str | None = None  # set for a button tap; ``message_id`` is then the card's
    callback_data: str = ""
    chat_is_forum: bool = False  # the chat is a group with topics (Telegram's ``is_forum``)


def normalize(update: Any) -> IncomingUpdate | None:
    """Turn an aiogram ``Update`` into an ``IncomingUpdate``; None for unsupported kinds."""
    if getattr(update, "callback_query", None) is not None:
        return _normalize_tap(update, update.callback_query)
    message = update.message or update.edited_message
    if message is None:
        return None
    edited = update.message is None
    raw = update.model_dump(mode="json", exclude_unset=True)
    sender = message.from_user
    command, command_args = _leading_command(message)
    return IncomingUpdate(
        update_id=update.update_id,
        chat_id=message.chat.id,
        topic_id=getattr(message, "message_thread_id", None),
        message_id=message.message_id,
        from_id=sender.id if sender else None,
        from_username=sender.username if sender else None,
        text=message.text or message.caption or "",
        edited=edited,
        edit_date=(raw.get("edited_message") or {}).get("edit_date") if edited else None,
        raw=raw,
        chat_type=message.chat.type,
        command=command,
        command_args=command_args,
        from_first_name=(sender.first_name or "") if sender else "",
        from_last_name=(sender.last_name or "") if sender else "",
        chat_is_forum=bool(getattr(message.chat, "is_forum", None)),
    )


def _normalize_tap(update: Any, query: Any) -> IncomingUpdate | None:
    """A button tap on a message of the bot. Taps on inline-mode messages are not supported."""
    message = query.message
    if message is None:
        return None
    sender = query.from_user
    return IncomingUpdate(
        update_id=update.update_id,
        chat_id=message.chat.id,
        topic_id=None,
        message_id=message.message_id,
        from_id=sender.id,
        from_username=sender.username,
        text="",
        edited=False,
        edit_date=None,
        raw=update.model_dump(mode="json", exclude_unset=True),
        chat_type=message.chat.type,
        from_first_name=sender.first_name or "",
        from_last_name=sender.last_name or "",
        callback_id=query.id,
        callback_data=query.data or "",
    )


def _leading_command(message: Any) -> tuple[str | None, str]:
    """``(name, args)`` when the text starts with a ``bot_command`` entity, else ``(None, "")``.

    The name is lower-cased and drops a ``@botname`` suffix; args are the rest, trimmed.
    """
    text = message.text
    for entity in message.entities or []:
        if entity.type == "bot_command" and entity.offset == 0 and text:
            name = text[1 : entity.length].partition("@")[0].lower()
            return (name or None), text[entity.length :].strip()
    return None, ""

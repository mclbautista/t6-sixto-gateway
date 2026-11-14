"""Update normalization: same rules as the watchdog's adapter.

``message`` or ``edited_message`` only; ``text`` falls back to ``caption``; ``raw`` is
``model_dump(mode="json", exclude_unset=True)`` so it is exactly what Telegram sent.
"""

from __future__ import annotations

import re
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
    reply_to_message_id: int | None = None
    has_file: bool = False
    has_link: bool = False
    topic_name: str | None = None
    events: tuple[dict[str, Any], ...] = ()


def normalize(update: Any) -> IncomingUpdate | None:
    """Turn an aiogram ``Update`` into an ``IncomingUpdate``; None for unsupported kinds."""
    if getattr(update, "callback_query", None) is not None:
        return _normalize_tap(update, update.callback_query)
    if getattr(update, "chat_member", None) is not None:
        return _normalize_member(update)
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
        reply_to_message_id=message.reply_to_message.message_id
        if message.reply_to_message
        else None,
        has_file=any(
            getattr(message, kind, None)
            for kind in ("document", "video", "photo", "audio", "voice", "animation")
        ),
        has_link=any(
            entity.type in ("url", "text_link")
            for entity in (message.entities or []) + (message.caption_entities or [])
        )
        or bool(re.search(r"(?i)\b(?:https?://|www\.)\S+", message.caption or "")),
        topic_name=(
            message.reply_to_message.forum_topic_created.name
            if message.reply_to_message and message.reply_to_message.forum_topic_created
            else None
        ),
        events=_message_events(raw.get("message", {})),
    )


def _message_events(message: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    chat = message.get("chat", {}).get("id")
    if message.get("migrate_to_chat_id") is not None:
        return (
            {"event": "chat_migrated", "old_chat": chat, "new_chat": message["migrate_to_chat_id"]},
        )
    if message.get("migrate_from_chat_id") is not None:
        return (
            {
                "event": "chat_migrated",
                "old_chat": message["migrate_from_chat_id"],
                "new_chat": chat,
            },
        )
    topic = message.get("message_thread_id")
    if message.get("forum_topic_created") and topic is not None:
        return (
            {
                "event": "topic_created",
                "topic": topic,
                "name": message["forum_topic_created"]["name"],
            },
        )
    edited = message.get("forum_topic_edited") or {}
    if edited.get("name") is not None and topic is not None:
        return ({"event": "topic_renamed", "topic": topic, "name": edited["name"]},)
    if message.get("new_chat_members"):
        return tuple(
            {"event": "member_joined", "user": user} for user in message["new_chat_members"]
        )
    if message.get("left_chat_member"):
        return ({"event": "member_left", "user": message["left_chat_member"]},)
    return ()


def _normalize_member(update: Any) -> IncomingUpdate | None:
    raw = update.model_dump(mode="json", exclude_unset=True)
    member = raw["chat_member"]

    def present(value: dict[str, Any]) -> bool:
        return value["status"] in ("creator", "administrator", "member") or (
            value["status"] == "restricted" and value.get("is_member", False)
        )

    old, new = member["old_chat_member"], member["new_chat_member"]
    if present(old) == present(new):
        return None
    event = "member_joined" if present(new) else "member_left"
    return IncomingUpdate(
        update_id=update.update_id,
        chat_id=member["chat"]["id"],
        topic_id=None,
        message_id=0,
        from_id=update.chat_member.from_user.id,
        from_username=update.chat_member.from_user.username,
        text="",
        edited=False,
        edit_date=None,
        raw=raw,
        chat_type=member["chat"]["type"],
        events=({"event": event, "user": new["user"]},),
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

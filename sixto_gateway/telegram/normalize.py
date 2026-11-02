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


def normalize(update: Any) -> IncomingUpdate | None:
    """Turn an aiogram ``Update`` into an ``IncomingUpdate``; None for unsupported kinds."""
    message = update.message or update.edited_message
    if message is None:
        return None
    edited = update.message is None
    raw = update.model_dump(mode="json", exclude_unset=True)
    sender = message.from_user
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
    )

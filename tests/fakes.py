"""Test doubles: a fake Telegram that serves recorded fixtures, and a fake clock."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

from aiogram.types import Update

from sixto_gateway.notion import Contact, NotionError
from sixto_gateway.telegram.normalize import IncomingUpdate, normalize

FIXTURES = Path(__file__).parent / "fixtures" / "telegram_updates.json"
INTAKE_CHAT = -1001234567890
INTAKE_TOPIC = 77
APPROVER = 274983989  # onboarding.approver in test configs
CONTACTS_SOURCE = "77b2a701-71db-4e88-a694-37f07303e77e"
NOTION_YAML = (
    f"notion: {{contacts_data_source_id: {CONTACTS_SOURCE}, version: '2026-03-11'}}\n"
    f"onboarding: {{approver: {APPROVER}}}\n"
)
NOTION_ENV = {"NOTION_API_KEY": "ntn_" + "k" * 40}


def fixture_updates() -> dict[str, dict]:
    return json.loads(FIXTURES.read_text())["updates"]


def valid_fixture_updates() -> dict[str, dict]:
    """The recorded updates the Bot API schema accepts.

    ``forward_channel_missing_ids`` is deliberately malformed (the watchdog tests its raw-dict
    handling); real Telegram never sends it, and aiogram rejects it, so the fake skips it.
    """
    out = {}
    for name, raw in fixture_updates().items():
        try:
            Update.model_validate(raw)
        except ValueError:
            continue
        out[name] = raw
    return out


def moved(raw: dict, *, update_id: int, chat: int | None = None, topic: int | None = None) -> dict:
    """A copy of a fixture update moved to another chat/topic (topic=0 removes the topic)."""
    out = copy.deepcopy(raw)
    out["update_id"] = update_id
    message = out.get("message") or out["edited_message"]
    if chat is not None:
        message["chat"]["id"] = chat
    if topic == 0:
        message.pop("message_thread_id", None)
        message.pop("is_topic_message", None)
    elif topic is not None:
        message["message_thread_id"] = topic
    return out


def dm_message(update_id: int, user_id: int, text: str, *, edited: bool = False) -> dict:
    """A private-chat message from ``user_id``; a leading ``/word`` carries a bot_command entity."""
    message: dict = {
        "message_id": update_id,
        "from": {"id": user_id, "is_bot": False, "first_name": "P", "username": f"u{user_id}"},
        "chat": {"id": user_id, "first_name": "P", "type": "private"},
        "date": 1789700000,
        "text": text,
    }
    if text.startswith("/"):
        length = len(text.split()[0])
        message["entities"] = [{"type": "bot_command", "offset": 0, "length": length}]
    if edited:
        message["edit_date"] = 1789700100
        return {"update_id": update_id, "edited_message": message}
    return {"update_id": update_id, "message": message}


def group_command(update_id: int, user_id: int, text: str, chat: int, topic: int | None) -> dict:
    """The same command typed in a group (and topic)."""
    raw = dm_message(update_id, user_id, text)
    message = raw["message"]
    message["chat"] = {"id": chat, "title": "Group", "type": "supergroup", "is_forum": True}
    if topic is not None:
        message["message_thread_id"] = topic
        message["is_topic_message"] = True
    return raw


class FakeClock:
    def __init__(self, now: float = 1_789_700_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeTelegram:
    """Serves raw updates through the real aiogram models and the real normalizer."""

    def __init__(self, raw_updates: list[dict], clock: FakeClock | None = None):
        self.raw_updates = raw_updates
        self.clock = clock
        self.attempts: list[float] = []  # clock reading of every send/react call, failed or not
        self.fail_always: Exception | None = None  # raised by every send/react until cleared
        self.calls: list[dict] = []
        self.sent: list[dict] = []  # outbound calls in order: messages and reactions
        self.fail_with: Exception | None = None  # raised by the next send/react, then cleared
        self._next_message_id = 9000
        self.menus: dict[int | None, list[tuple[str, str]]] = {}  # scope chat id (None = default)
        self.menu_calls: list[tuple[str, int | None, list[tuple[str, str]]]] = []
        self.menu_failures: set[int] = set()  # user ids Telegram refuses ("chat not found")

    async def send_message(
        self,
        chat_id: int,
        topic_id: int | None,
        text: str,
        parse_mode: str | None,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> int:
        self._maybe_fail()
        self._next_message_id += 1
        self.sent.append(
            {
                "at": self.clock() if self.clock else 0.0,
                "kind": "message",
                "chat_id": chat_id,
                "topic_id": topic_id,
                "text": text,
                "parse_mode": parse_mode,
                "keyboard": keyboard,
                "message_id": self._next_message_id,
            }
        )
        return self._next_message_id

    async def set_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        self._maybe_fail()
        self.sent.append(
            {"kind": "react", "chat_id": chat_id, "message_id": message_id, "emoji": emoji}
        )

    async def set_my_commands(self, chat_id: int | None, commands: list[tuple[str, str]]) -> None:
        """``chat_id`` None is the default scope."""
        if chat_id in self.menu_failures:
            raise RuntimeError("Bad Request: chat not found")
        self.menu_calls.append(("set", chat_id, list(commands)))
        self.menus[chat_id] = list(commands)

    async def delete_my_commands(self, chat_id: int) -> None:
        if chat_id in self.menu_failures:
            raise RuntimeError("Bad Request: chat not found")
        self.menu_calls.append(("delete", chat_id, []))
        self.menus.pop(chat_id, None)

    def _maybe_fail(self) -> None:
        self.attempts.append(self.clock() if self.clock else 0.0)
        if self.fail_always is not None:
            raise self.fail_always
        if self.fail_with is not None:
            exc, self.fail_with = self.fail_with, None
            raise exc

    def push(self, raw: dict) -> None:
        self.raw_updates.append(raw)

    async def get_updates(
        self, offset: int, timeout: int, allowed_updates: list[str]
    ) -> list[IncomingUpdate]:
        self.calls.append(
            {"offset": offset, "timeout": timeout, "allowed_updates": allowed_updates}
        )
        out = []
        for raw in sorted(self.raw_updates, key=lambda r: r["update_id"]):
            if raw["update_id"] < offset:
                continue
            incoming = normalize(Update.model_validate(raw))
            if incoming is not None:
                out.append(incoming)
        return out


def contact(page_id: str, name: str, handle: str = "", aliases: str = "") -> Contact:
    return Contact(
        page_id=page_id,
        name=name,
        url=f"https://www.notion.so/{page_id.replace('-', '')}",
        telegram_handle=handle,
        aliases=aliases,
    )


class FakeNotion:
    """The Contacts directory. ``block`` holds lookups until set; ``fail`` makes them raise."""

    def __init__(self, contacts: list[Contact]):
        self.contacts = contacts
        self.block: asyncio.Event | None = None
        self.fail = False
        self.calls = 0

    async def list_contacts(self) -> list[Contact]:
        self.calls += 1
        if self.block is not None:
            await self.block.wait()
        if self.fail:
            raise NotionError("Notion answered 503 (service_unavailable)")
        return list(self.contacts)

"""Test doubles: a fake Telegram that serves recorded fixtures, and a fake clock."""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path

from aiogram.types import Update
from t6.client import Unavailable
from t6.verbs import Ambiguous, Done, Preview, Refused

from sixto_gateway.people import Contact, PeopleDirectory, PeopleError, Refusal
from sixto_gateway.telegram.normalize import IncomingUpdate, normalize

FIXTURES = Path(__file__).parent / "fixtures" / "telegram_updates.json"
INTAKE_CHAT = -1001234567890
INTAKE_TOPIC = 77
APPROVER = 100000001  # onboarding.approver in test configs
ONBOARDING_YAML = f"onboarding: {{approver: {APPROVER}}}\n"
GATEWAY_ENV = {
    "T6_API_URL": "https://example.invalid",
    "GATEWAY_T6_API_SECRET": "test-secret",
}


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


def group_command(
    update_id: int, user_id: int, text: str, chat: int, topic: int | None, forum: bool = True
) -> dict:
    """The same command typed in a group (and topic)."""
    raw = dm_message(update_id, user_id, text)
    message = raw["message"]
    message["chat"] = {"id": chat, "title": "Group", "type": "supergroup", "is_forum": forum}
    if topic is not None:
        message["message_thread_id"] = topic
        message["is_topic_message"] = True
    return raw


def button_tap(update_id: int, user_id: int, data: str, chat: int, message_id: int) -> dict:
    """A tap on an inline button of a message the bot sent to ``chat``."""
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "chat_instance": "ci",
            "from": {"id": user_id, "is_bot": False, "first_name": "C"},
            "data": data,
            "message": {
                "message_id": message_id,
                "date": 1789700000,
                "chat": {"id": chat, "type": "private", "first_name": "C"},
                "from": {"id": 1, "is_bot": True, "first_name": "Sixto"},
                "text": "card",
            },
        },
    }


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
        self.edits: list[dict] = []  # edit_message calls that Telegram accepted
        self.edit_fail_with: Exception | None = None  # raised by the next edit, then cleared
        self.answers: list[dict] = []  # answer_callback_query calls
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

    async def forward_message(
        self, chat_id: int, topic_id: int | None, from_chat: int, message_id: int
    ) -> int:
        self._maybe_fail()
        self._next_message_id += 1
        self.sent.append(
            {
                "at": self.clock() if self.clock else 0.0,
                "kind": "forward",
                "chat_id": chat_id,
                "topic_id": topic_id,
                "from_chat": from_chat,
                "source_message_id": message_id,
                "message_id": self._next_message_id,
            }
        )
        return self._next_message_id

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: str | None,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> None:
        if self.edit_fail_with is not None:
            exc, self.edit_fail_with = self.edit_fail_with, None
            raise exc
        self.edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": parse_mode,
                "keyboard": keyboard,
            }
        )

    async def answer_callback_query(
        self, callback_id: str, text: str | None = None, alert: bool = False
    ) -> None:
        self.answers.append({"callback_id": callback_id, "text": text, "alert": alert})

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


def contact(
    page_id: str, name: str, handle: str = "", aliases: str = "", user_id: str = ""
) -> Contact:
    return Contact(
        page_id=page_id,
        name=name,
        url=f"https://www.notion.so/{page_id.replace('-', '')}",
        telegram_handle=handle,
        aliases=aliases,
        telegram_user_id=user_id,
    )


class FakeContacts:
    """The Contacts directory. ``block`` holds lookups until set; ``fail`` makes them raise.

    ``refuse`` makes a write raise that ``Refusal``; ``operators`` is who each write was made as.
    """

    def __init__(self, contacts: list[Contact]):
        self.contacts = contacts
        self.block: asyncio.Event | None = None
        self.fail = False
        self.fail_write = False
        self.fail_create = False
        self.refuse: Refusal | None = None
        self.created: list[Contact] = []
        self.notes: list[str] = []  # body line of each created Contact
        self.calls = 0
        self.writes: list[tuple[str, int]] = []  # (page id, user id) in order
        self.operators: list[int] = []  # who each write was made as

    async def get_contact(self, page_id: str) -> Contact:
        if self.fail:
            raise PeopleError("Person API unavailable")
        return next(c for c in self.contacts if c.page_id == page_id)

    async def link_telegram(self, contact: Contact, user_id: int, operator: int) -> Contact:
        if self.fail_write:
            raise PeopleError("Person API unavailable")
        if self.refuse:
            raise self.refuse
        self.writes.append((contact.page_id, user_id))
        self.operators.append(operator)
        saved = replace(contact, telegram_user_id=str(user_id))
        self.contacts = [saved if c.page_id == contact.page_id else c for c in self.contacts]
        return saved

    async def create_contact(
        self, name: str, handle: str, user_id: int, note: str, operator: int
    ) -> Contact:
        if self.fail_create:
            raise PeopleError("Person API unavailable")
        if self.refuse:
            raise self.refuse
        page_id = f"cccccccc-0000-0000-0000-{len(self.created) + 1:012d}"
        made = contact(page_id, name, handle=handle, user_id=str(user_id))
        self.created.append(made)
        self.notes.append(note)
        self.operators.append(operator)
        self.contacts = [*self.contacts, made]
        return made

    async def list_contacts(self) -> list[Contact]:
        self.calls += 1
        if self.block is not None:
            await self.block.wait()
        if self.fail:
            raise PeopleError("Person API unavailable")
        return list(self.contacts)


PERSON_ID = "t6n_" + "1" * 32
PAGE_ID = "aaaaaaaa-0000-0000-0000-000000000001"


class FakeAPI:
    """The t6 API client the people directory calls: reads, and the preview-then-confirm writes.

    It follows the real API's rules for a write: a preview needs the operator's permission and a
    free Telegram ID; a confirm commits once per idempotency key and replays on a repeat.
    ``operators`` is who may write (``None``: anyone); ``lose_answers`` is how many confirms
    commit but answer ``Unavailable``; ``pending_creates`` is how many creates answer
    ``Ambiguous`` and stay pending (the API's answer to a retried, unclear create).
    """

    def __init__(self):
        self.unavailable = False
        self.persons = [
            {
                "t6_id": PERSON_ID,
                "page_id": PAGE_ID,
                "name": "Example Person",
                "telegram_handle": "example_handle",
                "aliases": ["Sample Alias"],
                "telegram_user_id": None,
                "kind": "staff",
            }
        ]
        self.operators: set[int] | None = None
        self.lose_answers = 0
        self.pending_creates = 0
        self.calls: list[tuple[str, int, str | None, str | None]] = []  # verb, as, key, confirm
        self._tokens: dict[str, tuple[str, str]] = {}  # token -> (verb, key)
        self._used: set[str] = set()
        self._done: dict[str, dict] = {}  # idempotency key -> result
        self._pending: set[str] = set()

    @property
    def person(self) -> dict:
        return self.persons[0]

    @person.setter
    def person(self, value: dict) -> None:
        self.persons[0] = value

    def directory(self) -> PeopleDirectory:
        return PeopleDirectory(self, self.as_operator)

    def as_operator(self, telegram_user_id: int) -> _FakeAPIAs:
        return _FakeAPIAs(self, telegram_user_id)

    def person_list(self, inputs):
        return Unavailable("offline") if self.unavailable else Done(result={"people": self.persons})

    def person_get(self, inputs):
        if self.unavailable:
            return Unavailable("offline")
        found = next(p for p in self.persons if p["t6_id"] == inputs.person_id)
        return Done(result={"person": found})

    def _write(self, verb: str, operator: int, inputs):
        key, confirm = inputs.idempotency_key, inputs.confirm
        self.calls.append((verb, operator, key, confirm))
        if self.operators is not None and operator not in self.operators:
            return Refused(rule="permission", reason="needs operator")
        holder = next(
            (p for p in self.persons if p["telegram_user_id"] == inputs.telegram_id), None
        )
        if confirm is None:
            if (
                key not in self._done
                and holder
                and holder["t6_id"] != getattr(inputs, "person_id", None)
            ):
                return Refused(
                    rule="telegram_id", reason=f"Telegram ID is held by {holder['t6_id']}"
                )
            token = f"token-{len(self._tokens)}"
            self._tokens[token] = (verb, key)
            return Preview(plan={}, token=token)
        if self._tokens.get(confirm) != (verb, key):
            return Refused(rule="token", reason="Preview token is not valid")
        if confirm in self._used:
            return Refused(rule="token_used", reason="Preview token has already been used")
        self._used.add(confirm)
        if key in self._pending:
            return Ambiguous(candidates=[{"pending": True}])
        if key in self._done:
            return Done(result=self._done[key])
        if verb == "create" and self.pending_creates:
            self.pending_creates -= 1
            self._pending.add(key)
            return Ambiguous(candidates=[{"pending": True}])
        result = self._commit(verb, inputs)
        self._done[key] = result
        if self.lose_answers:
            self.lose_answers -= 1
            return Unavailable("the answer was lost", key)
        return Done(result=result)

    def _commit(self, verb: str, inputs) -> dict:
        if verb == "set-telegram":
            person = next(p for p in self.persons if p["t6_id"] == inputs.person_id)
            person["telegram_user_id"] = inputs.telegram_id
            return {"person_id": inputs.person_id, "telegram_id": inputs.telegram_id}
        number = len(self.persons) + 1
        person = {
            "t6_id": "t6n_" + f"{number:032x}",
            "page_id": f"cccccccc-0000-0000-0000-{number:012d}",
            "name": inputs.name,
            "telegram_handle": inputs.telegram_handle,
            "aliases": [],
            "telegram_user_id": inputs.telegram_id,
            "kind": inputs.kind,
            "note": inputs.note,
        }
        self.persons.append(person)
        return {"person": person}


class _FakeAPIAs:
    """The API client built for one operator (``Client(telegram_user_id=...)``)."""

    def __init__(self, api: FakeAPI, telegram_user_id: int):
        self._api, self._user = api, telegram_user_id

    def person_set_telegram(self, inputs):
        return self._api._write("set-telegram", self._user, inputs)

    def person_create(self, inputs):
        return self._api._write("create", self._user, inputs)

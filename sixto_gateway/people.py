"""Persons through the t6 API: reads as the gateway, writes as the operator who tapped."""

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from t6.client import APIError, Client, Unavailable
from t6.verbs import (
    Done,
    PersonCreateInput,
    PersonGetInput,
    PersonGetOutput,
    PersonListInput,
    PersonListOutput,
    PersonTelegramInput,
    Preview,
    Refused,
)


class PeopleError(Exception):
    """The t6 API did not answer, or could not confirm a write. Safe to try again."""


class Refusal(Exception):
    """The t6 API refused a write. Trying again with the same input gets the same answer.

    ``rule`` is the API's rule name, for example ``telegram_id`` (held by another Person) or
    ``permission`` (the operator may not do this).
    """

    def __init__(self, rule: str, reason: str):
        super().__init__(f"{rule}: {reason}")
        self.rule = rule
        self.reason = reason


@dataclass(frozen=True)
class Contact:
    page_id: str
    name: str
    url: str
    telegram_handle: str = ""
    aliases: str = ""
    telegram_user_id: str = ""
    notion_page_id: str = ""


class ContactsDirectory(Protocol):
    async def list_contacts(self) -> list[Contact]:
        """Every Contact. Failures raise ``PeopleError``."""
        ...

    async def get_contact(self, page_id: str) -> Contact:
        """One Contact, read fresh. Failures raise ``PeopleError``."""
        ...

    async def link_telegram(self, contact: Contact, user_id: int, operator: int) -> Contact:
        """Set the Contact's Telegram ID as ``operator``; return the Contact read back.

        Raises ``PeopleError`` (try again) or ``Refusal`` (do not).
        """
        ...

    async def create_contact(
        self, name: str, handle: str, user_id: int, note: str, operator: int, kind: str
    ) -> Contact:
        """Create a Contact of ``kind`` as ``operator``; return it read back. Same errors."""
        ...


def write_key(action: str, user_id: int, person_id: str = "") -> str:
    """The idempotency key of one write: the same member and Contact always give the same key."""
    return hashlib.sha256(f"sixto-onboarding:{action}:{user_id}:{person_id}".encode()).hexdigest()[
        :32
    ]


class PeopleDirectory:
    def __init__(self, client: Client, as_operator: Callable[[int], Client]):
        """``client`` reads as the gateway; ``as_operator(id)`` acts as that Telegram user."""
        self._client = client
        self._as_operator = as_operator

    @staticmethod
    def _contact(person):
        if not person.t6_id:
            raise PeopleError("Person has no T6 ID")
        return Contact(
            page_id=person.t6_id,
            name=person.name,
            url=f"https://www.notion.so/{person.page_id.replace('-', '')}",
            telegram_handle=person.telegram_handle or "",
            aliases=", ".join(person.aliases),
            telegram_user_id=str(person.telegram_user_id or ""),
            notion_page_id=person.page_id,
        )

    @staticmethod
    async def _send(method, inputs):
        try:
            return await asyncio.to_thread(method, inputs)
        except APIError:
            raise PeopleError("Person API request failed") from None

    async def _call(self, method, inputs):
        result = await self._send(method, inputs)
        if isinstance(result, Unavailable) or not isinstance(result, Done):
            raise PeopleError("Person API unavailable or refused request")
        return result.result

    async def list_contacts(self) -> list[Contact]:
        result = await self._call(self._client.person_list, PersonListInput())
        return [self._contact(p) for p in PersonListOutput.model_validate(result).people]

    async def get_contact(self, person_id: str) -> Contact:
        result = await self._call(self._client.person_get, PersonGetInput(person_id=person_id))
        return self._contact(PersonGetOutput.model_validate(result).person)

    @staticmethod
    def _answer(result) -> dict[str, Any]:
        if isinstance(result, Done):
            return result.result
        if isinstance(result, Refused):
            raise Refusal(result.rule, result.reason)
        # Unavailable, Ambiguous (the write may or may not have happened) or anything else
        raise PeopleError("Person API unavailable or could not confirm the write")

    async def _confirmed(self, operator: int, verb: str, inputs) -> dict[str, Any]:
        """Preview, then confirm with the same idempotency key, as ``operator``."""
        method = getattr(self._as_operator(operator), verb)
        preview = await self._send(method, inputs)
        if not isinstance(preview, Preview):
            return self._answer(preview)
        confirm = inputs.model_copy(update={"confirm": preview.token})
        return self._answer(await self._send(method, confirm))

    async def _saved(self, person_id: str, user_id: int) -> Contact:
        """The Person read back: the card says "linked" only when the API shows the ID."""
        saved = await self.get_contact(person_id)
        if saved.telegram_user_id != str(user_id):
            raise PeopleError("Person does not show the Telegram ID yet")
        return saved

    async def link_telegram(self, contact: Contact, user_id: int, operator: int) -> Contact:
        inputs = PersonTelegramInput(
            person_id=contact.page_id,
            telegram_id=user_id,
            idempotency_key=write_key("link", user_id, contact.page_id),
        )
        await self._confirmed(operator, "person_set_telegram", inputs)
        return await self._saved(contact.page_id, user_id)

    async def create_contact(
        self, name: str, handle: str, user_id: int, note: str, operator: int, kind: str
    ) -> Contact:
        inputs = PersonCreateInput(
            name=name,
            kind=kind,
            telegram_handle=handle or None,
            telegram_id=user_id,
            note=note,
            idempotency_key=write_key("create", user_id),
        )
        result = await self._confirmed(operator, "person_create", inputs)
        return await self._saved(result["person"]["t6_id"], user_id)

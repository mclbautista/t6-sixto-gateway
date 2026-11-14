"""Contacts, read and written through t6-core.

The gateway keeps no Notion client of its own. ``CoreContacts`` runs t6-core's blocking
``Contacts`` on one worker thread (the t6 client owns a private event loop, so calls must come
from one thread and never from inside the gateway's loop).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol, TypeVar

import httpx
from t6.domain import Contact
from t6.notion import NotionClient, NotionError
from t6.notion.contacts import Contacts
from t6.notion.writes import Ambiguous, Rejected

log = logging.getLogger(__name__)
T = TypeVar("T")


class ContactsError(Exception):
    """A Contacts read or write failed. The text never holds the API key."""


class ContactsDirectory(Protocol):
    async def list_contacts(self) -> list[Contact]:
        """Every Contact. Failures raise ``ContactsError``."""
        ...

    async def get_contact(self, page_id: str) -> Contact:
        """One Contact, read fresh. Failures raise ``ContactsError``."""
        ...

    async def set_telegram_user_id(self, page_id: str, user_id: int) -> Contact:
        """Write the Telegram user id and return the saved Contact. Raises ``ContactsError``."""
        ...

    async def create_contact(self, name: str, handle: str, user_id: int, note: str) -> Contact:
        """Create a Contact with a one-line ``note`` as body. Raises ``ContactsError``."""
        ...


def _saved(result: Contact | Rejected | Ambiguous) -> Contact:
    """A saved Contact, or ``ContactsError``. A write that may have happened is an error too:
    the caller's retry reads the Contact again before writing (onboarding does)."""
    if isinstance(result, Contact):
        return result
    if isinstance(result, Rejected):
        raise ContactsError(f"Notion refused the write ({result.status} {result.code})")
    raise ContactsError(f"Notion write is uncertain ({result.reason})")


class CoreContacts:
    """The Contacts data source through ``t6.notion.contacts``, pinned to one Notion-Version."""

    def __init__(
        self,
        api_key: str,
        data_source_id: str,
        version: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._client = NotionClient(
            api_key, notion_version=version, timeout=timeout, transport=transport
        )
        self._contacts = Contacts(self._client, data_source_id)
        self._thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="t6-contacts")

    async def close(self) -> None:
        await asyncio.get_running_loop().run_in_executor(self._thread, self._client.close)
        self._thread.shutdown(wait=False)

    async def _run(self, call: Callable[[], T]) -> T:
        try:
            return await asyncio.get_running_loop().run_in_executor(self._thread, call)
        except NotionError as exc:
            raise ContactsError(f"Notion answered {exc.status or '-'} ({exc.code})") from None

    async def list_contacts(self) -> list[Contact]:
        return list(await self._run(self._contacts.list))

    async def get_contact(self, page_id: str) -> Contact:
        return await self._run(lambda: self._contacts.get(page_id))

    async def set_telegram_user_id(self, page_id: str, user_id: int) -> Contact:
        return _saved(
            await self._run(lambda: self._contacts.set_telegram_user_id(page_id, user_id))
        )

    async def create_contact(self, name: str, handle: str, user_id: int, note: str) -> Contact:
        return _saved(
            await self._run(
                lambda: self._contacts.create(
                    name, telegram_user_id=user_id, telegram_handle=handle, note=note
                )
            )
        )

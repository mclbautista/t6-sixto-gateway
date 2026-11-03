"""The gateway's own small Notion adapter for the Contacts data source (read only for now).

Matching rules (``normalize_handle``, ``split_aliases``) are ported from ``t6-jo-tg-ntn``
``jo_watchdog/core/resolve.py``; that repo is deliberately not imported.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

BASE_URL = "https://api.notion.com/v1"
DEFAULT_HONORIFICS = ("ate", "kuya", "sir")


class NotionError(Exception):
    """A Notion request failed. The text never holds the API key."""


@dataclass(frozen=True)
class Contact:
    page_id: str
    name: str
    url: str
    telegram_handle: str = ""
    aliases: str = ""
    telegram_user_id: str = ""


class ContactsDirectory(Protocol):
    async def list_contacts(self) -> list[Contact]:
        """Every Contact. Failures raise ``NotionError``."""
        ...


def normalize_handle(handle: str) -> str:
    return handle.strip().lstrip("@").casefold()


def normalize_name(text: str, honorifics: Iterable[str] = DEFAULT_HONORIFICS) -> str:
    """Casefold, trim, collapse whitespace, and drop honorific words."""
    drop = {h.casefold() for h in honorifics}
    words = [w for w in text.casefold().split() if w.strip(".") not in drop]
    return " ".join(words)


def split_aliases(aliases: str, honorifics: Iterable[str] = DEFAULT_HONORIFICS) -> list[str]:
    """Contacts ``Aliases`` is comma-separated; each entry is normalized like a mention."""
    out = []
    for part in aliases.split(","):
        norm = normalize_name(part, honorifics)
        if norm:
            out.append(norm)
    return out


def matches_handle(contact: Contact, handle: str) -> bool:
    """True when ``handle`` (any case, with or without ``@``) is the Contact's handle or alias."""
    wanted = normalize_handle(handle)
    if not wanted:
        return False
    if normalize_handle(contact.telegram_handle) == wanted:
        return True
    return any(alias.lstrip("@") == wanted for alias in split_aliases(contact.aliases))


def _text(prop: dict[str, Any] | None) -> str:
    """The plain text of a title, rich text, number or select property."""
    if not prop:
        return ""
    kind = prop.get("type")
    if kind in ("title", "rich_text"):
        return "".join(part.get("plain_text", "") for part in prop.get(kind) or []).strip()
    if kind == "number":
        value = prop.get("number")
        return "" if value is None else str(int(value))
    if kind == "select":
        return ((prop.get("select") or {}).get("name") or "").strip()
    return ""


def contact_from_page(page: dict[str, Any]) -> Contact:
    props = page.get("properties", {})
    page_id = page["id"]
    return Contact(
        page_id=page_id,
        name=_text(props.get("Name")),
        url=page.get("url") or f"https://www.notion.so/{page_id.replace('-', '')}",
        telegram_handle=_text(props.get("TelegramHandle")),
        aliases=_text(props.get("Aliases")),
        telegram_user_id=_text(props.get("TelegramUserID")),
    )


class NotionContacts:
    """Reads the Contacts data source over HTTPS, pinned to one Notion-Version."""

    def __init__(
        self,
        api_key: str,
        data_source_id: str,
        version: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._data_source_id = data_source_id
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Notion-Version": version,
                "Content-Type": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def list_contacts(self) -> list[Contact]:
        contacts: list[Contact] = []
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            data = await self._post(f"/data_sources/{self._data_source_id}/query", body)
            contacts.extend(contact_from_page(page) for page in data.get("results", []))
            cursor = data.get("next_cursor")
            if not data.get("has_more") or not cursor:
                return contacts

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(path, json=body)
        except httpx.HTTPError as exc:
            raise NotionError(f"Notion request failed: {type(exc).__name__}") from None
        if response.status_code >= 400:
            try:
                code = response.json().get("code", "unknown")
            except ValueError:
                code = "unknown"
            raise NotionError(f"Notion answered {response.status_code} ({code})")
        result: dict[str, Any] = response.json()
        return result

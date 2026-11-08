"""The gateway's own small Notion adapter for the Contacts data source.

Matching rules (``normalize_handle``, ``split_aliases``) are ported from ``t6-sixto-watchdog``
``jo_watchdog/core/resolve.py``; that repo is deliberately not imported.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from rapidfuzz import fuzz

BASE_URL = "https://api.notion.com/v1"
DEFAULT_HONORIFICS = ("ate", "kuya", "sir")
CLOSEST_MIN_SCORE = 60.0  # a candidate on a card only needs to be worth a look
CLOSEST_LIMIT = 3


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
    user_id_kind: str = "number"  # the TelegramUserID property type: "number" or "rich_text"


class ContactsDirectory(Protocol):
    async def list_contacts(self) -> list[Contact]:
        """Every Contact. Failures raise ``NotionError``."""
        ...

    async def get_contact(self, page_id: str) -> Contact:
        """One Contact, read fresh. Failures raise ``NotionError``."""
        ...

    async def set_telegram_user_id(self, contact: Contact, user_id: int) -> None:
        """Write ``TelegramUserID`` on the Contact. Failures raise ``NotionError``."""
        ...

    async def create_contact(self, name: str, handle: str, user_id: int, note: str) -> Contact:
        """Create a Contact with these fields and one body line ``note``. Raises ``NotionError``."""
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


def closest_contacts(
    contacts: Iterable[Contact],
    name: str,
    *,
    limit: int = CLOSEST_LIMIT,
    min_score: float = CLOSEST_MIN_SCORE,
) -> list[Contact]:
    """Contacts whose name or an alias is closest to ``name``, best first.

    Ranks with the same ``WRatio`` scorer and name normalization as t6-sixto-watchdog
    (``fuzzy_ranking``), but with a low bar: these are suggestions for the approver to
    choose from, never picked automatically.
    """
    wanted = normalize_name(name)
    if not wanted:
        return []
    scored: list[tuple[float, Contact]] = []
    for c in contacts:
        titles = [normalize_name(c.name), *split_aliases(c.aliases)]
        best = max((fuzz.WRatio(wanted, t) for t in titles if t), default=0.0)
        if best >= min_score:
            scored.append((best, c))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [c for _, c in scored[:limit]]


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


def _rich(text: str) -> dict[str, Any]:
    return {"type": "text", "text": {"content": text}}


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
        user_id_kind=(props.get("TelegramUserID") or {}).get("type") or "number",
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

    async def get_contact(self, page_id: str) -> Contact:
        return contact_from_page(await self._request("GET", f"/pages/{page_id}"))

    async def set_telegram_user_id(self, contact: Contact, user_id: int) -> None:
        value: dict[str, Any] = (
            {"rich_text": [{"type": "text", "text": {"content": str(user_id)}}]}
            if contact.user_id_kind == "rich_text"
            else {"number": user_id}
        )
        await self._request(
            "PATCH", f"/pages/{contact.page_id}", {"properties": {"TelegramUserID": value}}
        )

    async def create_contact(self, name: str, handle: str, user_id: int, note: str) -> Contact:
        schema = await self._request("GET", f"/data_sources/{self._data_source_id}")
        kinds = {k: (v or {}).get("type") for k, v in (schema.get("properties") or {}).items()}
        id_value: dict[str, Any] = (
            {"rich_text": [_rich(str(user_id))]}
            if kinds.get("TelegramUserID") == "rich_text"
            else {"number": user_id}
        )
        properties: dict[str, Any] = {
            "Name": {"title": [_rich(name)]},
            "TelegramUserID": id_value,
        }
        if handle:
            properties["TelegramHandle"] = {"rich_text": [_rich(handle)]}
        body = {
            "parent": {"type": "data_source_id", "data_source_id": self._data_source_id},
            "properties": properties,
            "children": [
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [_rich(note)]},
                }
            ],
        }
        return contact_from_page(await self._request("POST", "/pages", body))

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", path, body)

    async def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, json=body)
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

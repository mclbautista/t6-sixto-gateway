"""Async Person reads, with the existing Notion writer during the transition."""

import asyncio
from dataclasses import replace

from t6.client import APIError, Client, Unavailable
from t6.verbs import Done, PersonGetInput, PersonGetOutput, PersonListInput, PersonListOutput

from sixto_gateway.notion import Contact, NotionContacts, NotionError


class PeopleDirectory:
    def __init__(self, client: Client, writer: NotionContacts):
        self._client = client
        self._writer = writer

    @staticmethod
    def _contact(person):
        if not person.t6_id:
            raise NotionError("Person has no T6 ID")
        return Contact(
            page_id=person.t6_id,
            name=person.name,
            url=f"https://www.notion.so/{person.page_id.replace('-', '')}",
            telegram_handle=person.telegram_handle or "",
            aliases=", ".join(person.aliases),
            telegram_user_id=str(person.telegram_user_id or ""),
            notion_page_id=person.page_id,
        )

    async def _call(self, method, inputs):
        try:
            result = await asyncio.to_thread(method, inputs)
        except APIError:
            raise NotionError("Person API request failed") from None
        if isinstance(result, Unavailable) or not isinstance(result, Done):
            raise NotionError("Person API unavailable or refused request")
        return result.result

    async def list_contacts(self) -> list[Contact]:
        result = await self._call(self._client.person_list, PersonListInput())
        return [self._contact(p) for p in PersonListOutput.model_validate(result).people]

    async def get_contact(self, person_id: str) -> Contact:
        result = await self._call(self._client.person_get, PersonGetInput(person_id=person_id))
        return self._contact(PersonGetOutput.model_validate(result).person)

    async def set_telegram_user_id(self, contact: Contact, user_id: int) -> None:
        original = await self._writer.get_contact(contact.notion_page_id)
        await self._writer.set_telegram_user_id(original, user_id)

    async def create_contact(self, name: str, handle: str, user_id: int, note: str) -> Contact:
        """Make the Contact in Notion, then return it as a linked one is: keyed by its T6 ID.

        If the t6 API does not list the new page yet, the Notion-keyed Contact comes back and the
        member is stored under the Notion page id only.
        """
        made = await self._writer.create_contact(name, handle, user_id, note)
        try:
            listed = await self.list_contacts()
        except NotionError:  # the page exists now; a retry would make a second one
            listed = []
        same = made.page_id.replace("-", "")
        found = next((c for c in listed if c.notion_page_id.replace("-", "") == same), None)
        return found or replace(made, notion_page_id=made.page_id)

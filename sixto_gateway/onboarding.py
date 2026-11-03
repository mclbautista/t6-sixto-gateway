"""/start onboarding, first slice (spec #17, ticket #20).

A person presses Start in a private chat: the poller saves a ``pending`` member and the
"waiting" reply in the same transaction as the update (``decide_start``). A separate worker then
looks the person up in Notion Contacts and queues an approval card for the approver. The lookup
runs outside the poll loop, so a slow or down Notion never holds up ``getUpdates``.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sixto_gateway import strings
from sixto_gateway.config import Config
from sixto_gateway.decision import Decision, NewMember, NewTap
from sixto_gateway.notifier import Notifier
from sixto_gateway.notion import (
    Contact,
    ContactsDirectory,
    NotionError,
    closest_contacts,
    matches_handle,
)
from sixto_gateway.store import Store
from sixto_gateway.telegram.client import TelegramClient
from sixto_gateway.telegram.normalize import IncomingUpdate

log = logging.getLogger(__name__)
IDLE_WAKE_SECONDS = 5.0  # safety net if a wake-up is ever missed
CALLBACK_PREFIX = "ob"
MANILA = timezone(timedelta(hours=8))
LOOKUP_RETRY_SECONDS = 60.0  # ticket #27 replaces this with 1/5/30 minutes and a failure card


def decide_start(store: Store, update: IncomingUpdate) -> Decision:
    """What happens to a ``/start``. Never goes through the command allow-list."""
    if update.edited or update.from_id is None:
        return Decision(None)  # an edit is never a new Start
    user = update.from_id
    detail = f"user={user}"
    if update.chat_type != "private":
        return Decision(
            None,
            audit=("start", None, f"{detail} in_group"),
            extra_audits=(("reply", None, f"{detail} kind=dm_me"),),
            reply=strings.IN_A_GROUP.format(deep_link=strings.DEEP_LINK),
        )
    existing = store.member(user)
    if existing is not None:
        if existing["status"] == "pending":
            return Decision(
                None,
                audit=("start", None, f"{detail} again"),
                extra_audits=(("reply", None, f"{detail} kind=pending_again"),),
                reply=strings.PENDING_AGAIN,
            )
        # approved, rejected and blocked members are handled by tickets #21, #22 and #26
        return Decision(None, audit=("start", None, f"{detail} status={existing['status']}"))
    reply = strings.WAITING.format(first_name=strings.first_name_or_there(update.from_first_name))
    return Decision(
        None,
        audit=("start", None, detail),
        extra_audits=(("reply", None, f"{detail} kind=waiting"),),
        reply=reply,
        member=NewMember(
            user_id=user,
            handle=update.from_username,
            first_name=update.from_first_name,
            last_name=update.from_last_name,
        ),
    )


def decide_tap(update: IncomingUpdate) -> Decision:
    """A button tap is saved for the worker. It never goes to a module."""
    if update.callback_id is None or update.from_id is None:
        return Decision(None)
    # any tap is saved, so even a payload that is not ours gets answered by the worker
    return Decision(
        None,
        tap=NewTap(
            update.callback_id,
            update.from_id,
            update.chat_id,
            update.message_id,
            update.callback_data,
        ),
    )


def parse_callback(data: str) -> tuple[str, int, str] | None:
    """``(action, user id, page id without dashes)`` from a button payload, else None."""
    parts = data.split(":")
    if len(parts) not in (3, 4) or parts[0] != CALLBACK_PREFIX or not parts[2].isdigit():
        return None
    return parts[1], int(parts[2]), parts[3] if len(parts) == 4 else ""


def find_candidates(contacts: list[Contact], handle: str | None) -> list[Contact]:
    """Contacts whose handle or an alias equals ``handle``. No handle, no candidates."""
    if not handle:
        return []
    return [c for c in contacts if matches_handle(c, handle)]


def find_candidates_for(contacts: list[Contact], member: dict[str, Any]) -> list[Contact]:
    """Handle or alias matches; with none, the Contacts closest by name. Never picks one."""
    exact = find_candidates(contacts, member["handle"])
    if exact:
        return exact
    name = " ".join(p for p in (member["first_name"], member["last_name"]) if p)
    return closest_contacts(contacts, name)


def holders_of_id(contacts: list[Contact], user_id: int) -> list[Contact]:
    """Contacts whose ``TelegramUserID`` is already this user's id."""
    return [c for c in contacts if c.telegram_user_id == str(user_id)]


def callback_data(action: str, user_id: int, page_id: str = "") -> str:
    """Button payload (Telegram allows 64 bytes): ``ob:<action>:<user id>[:<page id>]``."""
    parts = [CALLBACK_PREFIX, action, str(user_id)]
    if page_id:
        parts.append(page_id.replace("-", ""))
    return ":".join(parts)


def _card_header(member: dict[str, Any]) -> list[str]:
    name = " ".join(p for p in (member["first_name"], member["last_name"]) if p) or "(no name)"
    handle = f"@{member['handle']}" if member["handle"] else strings.CARD_NO_HANDLE
    return [
        f"<b>{strings.CARD_TITLE}</b>",
        strings.CARD_TELEGRAM.format(
            name=html.escape(name),
            handle=html.escape(handle),
            user_id=member["telegram_user_id"],
        ),
    ]


def render_outcome(member: dict[str, Any], contact_name: str | None = None) -> str:
    """The card's text once the member is decided: the same header and the outcome, no buttons."""
    lines = _card_header(member)
    status = member["status"]
    if status == "approved":
        page = member["contact_page_id"] or ""
        url = f"https://www.notion.so/{page.replace('-', '')}"
        label = html.escape(contact_name or strings.OUTCOME_CONTACT)
        lines.append(strings.OUTCOME_APPROVED.format(link=f'<a href="{url}">{label}</a>'))
    elif status == "blocked":
        lines.append(strings.OUTCOME_BLOCKED)
    else:
        lines.append(strings.OUTCOME_REJECTED)
    return "\n".join(lines)


def outcome_keyboard(member: dict[str, Any]) -> list[list[dict[str, str]]] | None:
    """A rejected card keeps one Undo button. Every other decided card has no buttons."""
    if member["status"] != "rejected":
        return None
    undo = {
        "text": strings.BUTTON_UNDO,
        "callback_data": callback_data("undo", member["telegram_user_id"]),
    }
    return [[undo]]


def render_card(member: dict[str, Any], candidates: list[Contact]) -> dict[str, Any]:
    """The approver's card as an outbound body (HTML text plus inline buttons)."""
    user_id = member["telegram_user_id"]
    lines = _card_header(member)
    handle = member["handle"]
    by_handle = (
        bool(handle) and bool(candidates) and all(matches_handle(c, handle) for c in candidates)
    )
    single = len(candidates) == 1 and by_handle
    if not candidates:
        lines.append(strings.CARD_NO_MATCH)
    elif single:
        lines.append(strings.CARD_MATCH.format(link=_link(candidates[0])))
    else:  # several matches, or only names to go on: nothing is picked for the approver
        lines.append(strings.CARD_MATCHES if by_handle else strings.CARD_CLOSEST)
    keyboard: list[list[dict[str, str]]] = []
    for contact in candidates:
        if not single:
            lines.append(f"• {_link(contact)}")
        conflict = contact.telegram_user_id not in ("", str(user_id))
        if conflict:
            lines.append(
                strings.CARD_CONFLICT.format(other_id=html.escape(contact.telegram_user_id))
            )
            label = (
                strings.BUTTON_REPLACE
                if len(candidates) == 1
                else strings.BUTTON_REPLACE_NAMED.format(name=contact.name)
            )
            action = "rep"
        else:
            template = strings.BUTTON_APPROVE if single else strings.BUTTON_PICK
            label = template.format(name=contact.name)
            action = "ok"
        keyboard.append(
            [{"text": label, "callback_data": callback_data(action, user_id, contact.page_id)}]
        )
    keyboard.append(
        [
            {"text": strings.BUTTON_CREATE, "callback_data": callback_data("new", user_id)},
            {"text": strings.BUTTON_REJECT, "callback_data": callback_data("no", user_id)},
        ]
    )
    return {"text": "\n".join(lines), "format": "html", "keyboard": keyboard}


def _link(contact: Contact) -> str:
    return f'<a href="{html.escape(contact.url, quote=True)}">{html.escape(contact.name)}</a>'


class OnboardingWorker:
    """Turns each new pending member into an approval card in the approver's DM."""

    def __init__(
        self,
        store: Store,
        contacts: ContactsDirectory,
        config: Config,
        wake: Notifier,
        queue_wake: Notifier,
        clock: Callable[[], float] = time.time,
        *,
        client: TelegramClient,
    ):
        self._client = client
        self._store = store
        self._contacts = contacts
        self._approver = config.onboarding.approver
        self._wake = wake
        self._queue_wake = queue_wake
        self._clock = clock
        self._retry_at: dict[int, float] = {}

    async def process_next(self) -> bool:
        """Handle one button tap, else look up one waiting member and queue their card.

        False when nothing is due.
        """
        if await self.process_tap():
            return True
        now = self._clock()
        for member in self._store.members_awaiting_lookup():
            user = member["telegram_user_id"]
            if self._retry_at.get(user, 0.0) > now:
                continue
            try:
                contacts = await self._contacts.list_contacts()
            except NotionError as exc:
                self._retry_at[user] = self._clock() + LOOKUP_RETRY_SECONDS
                self._store.audit("lookup", None, f"user={user} failed: {exc}"[:300])
                log.warning("contacts lookup for user %s failed: %s", user, exc)
                continue
            self._retry_at.pop(user, None)
            if self._auto_approve(member, contacts):
                self._queue_wake.notify()
                return True
            found = find_candidates_for(contacts, member)
            self._store.queue_approval_card(
                user,
                [c.page_id for c in found],
                self._approver,
                render_card(member, found),
                f"matches={len(found)}",
            )
            self._queue_wake.notify()
            return True
        return False

    def _auto_approve(self, member: dict[str, Any], contacts: list[Contact]) -> bool:
        """A Contact already holds this user's id: onboard them with no card and no write.

        Only when exactly one Contact has the id and no other member is linked to it. In any
        other case the approver decides on a card.
        """
        user = member["telegram_user_id"]
        holders = holders_of_id(contacts, user)
        if len(holders) != 1:
            return False
        holder = holders[0]
        if self._store.approved_user_for_contact(holder.page_id) is not None:
            return False
        reply = {
            "chat": user,
            "topic": None,
            "text": strings.APPROVED.format(
                first_name=strings.first_name_or_there(member["first_name"])
            ),
            "format": "plain",
        }
        name = " ".join(p for p in (member["first_name"], member["last_name"]) if p)
        fyi = {
            "chat": self._approver,
            "topic": None,
            "text": strings.FYI_AUTO.format(
                person=html.escape(name or str(user)), link=_link(holder)
            ),
            "format": "html",
        }
        return self._store.auto_approve_member(user, holder.page_id, reply, self._approver, fyi)

    async def process_tap(self) -> bool:
        """Handle the oldest unhandled button tap. False when there is none."""
        tap = self._store.next_tap()
        if tap is None:
            return False
        try:
            await self._handle_tap(tap)
        except Exception:
            await self._answer(tap)  # the spinner stops even when handling broke
            raise
        finally:  # a tap is never retried: the person taps again
            self._store.finish_tap(tap["update_id"])
        return True

    async def _handle_tap(self, tap: dict[str, Any]) -> None:
        parsed = parse_callback(tap["data"])
        if tap["from_id"] != self._approver or parsed is None:
            await self._answer(tap)  # nobody else's tap counts, but the spinner still stops
            return
        action, user_id, page = parsed
        member = self._store.member(user_id)
        if member is None or action not in ("ok", "rep", "no", "new", "undo"):
            await self._answer(tap)  # ticket #27 adds the Retry buttons
            return
        if member["status"] != ("rejected" if action == "undo" else "pending"):
            await self._answer(tap)  # stale card: show what is true now
            await self._rerender(tap, member)
            return
        if action == "no":
            await self._reject(tap, member)
        elif action == "undo":
            await self._undo(tap, member)
        elif action == "new":
            await self._create(tap, member)
        else:
            chosen = next(
                (p for p in member["candidates"] or [] if p.replace("-", "") == page), None
            )
            if chosen is None:
                await self._answer(tap)
                return
            await self._approve(tap, member, chosen, replace=action == "rep")

    async def _reject(self, tap: dict[str, Any], member: dict[str, Any]) -> None:
        """Nothing is written to Notion. The card keeps one Undo button."""
        user = member["telegram_user_id"]
        reply = {"chat": user, "topic": None, "text": strings.REJECTED, "format": "plain"}
        rejected = self._store.reject_member(user, tap["from_id"], reply)
        await self._answer(tap)
        fresh = self._store.member(user) or member
        if rejected:
            self._queue_wake.notify()
        await self._rerender(tap, fresh)

    async def _undo(self, tap: dict[str, Any], member: dict[str, Any]) -> None:
        """Back to pending, and the card shows the choices it had before the rejection."""
        user = member["telegram_user_id"]
        try:
            known = {c.page_id.replace("-", ""): c for c in await self._contacts.list_contacts()}
        except NotionError as exc:
            self._store.audit("undo", None, f"user={user} failed: {exc}"[:300])
            await self._answer(tap, strings.TAP_NOTION_FAILED, alert=True)
            return
        # a Contact deleted since the card was first sent is simply not offered again
        shown = [
            c
            for p in member["candidates"] or []
            if (c := known.get(p.replace("-", ""))) is not None
        ]
        undone = self._store.undo_rejection(user, tap["from_id"])
        await self._answer(tap)
        fresh = self._store.member(user) or member
        if undone:
            card = render_card(fresh, shown)
            await self._edit_card(tap, card["text"], card["keyboard"])
        else:
            await self._rerender(tap, fresh)

    async def _create(self, tap: dict[str, Any], member: dict[str, Any]) -> None:
        """Create a Contact for the person, then approve them with it (Notion first)."""
        user = member["telegram_user_id"]
        name = " ".join(p for p in (member["first_name"], member["last_name"]) if p)
        handle = member["handle"] or ""
        note = strings.CREATED_NOTE.format(
            date=datetime.fromtimestamp(self._clock(), MANILA).date()
        )
        try:
            created = await self._contacts.create_contact(
                name or handle or str(user), handle, user, note
            )
        except NotionError as exc:
            self._store.audit("notion_write", None, f"user={user} create failed: {exc}"[:300])
            log.warning("creating a Contact for user %s failed: %s", user, exc)
            await self._answer(tap, strings.TAP_NOTION_FAILED, alert=True)
            return
        self._store.audit("notion_write", None, f"user={user} created contact={created.page_id}")
        await self._finish_approval(tap, member, created.page_id, created.name)

    async def _approve(
        self, tap: dict[str, Any], member: dict[str, Any], page_id: str, *, replace: bool = False
    ) -> None:
        """Link the member to a Contact; another id there is overwritten only when ``replace``."""
        user = member["telegram_user_id"]
        owner = self._store.approved_user_for_contact(page_id)
        if owner is not None and owner != user:
            self._store.audit("decision", None, f"user={user} refused: contact has member {owner}")
            await self._answer(tap, strings.TAP_ALREADY_LINKED, alert=True)
            return
        try:
            contact = await self._contacts.get_contact(page_id)
            old_id = contact.telegram_user_id
            if old_id not in ("", str(user)) and not replace:
                # only the Replace ID button overwrites another person's id
                self._store.audit("decision", None, f"user={user} refused: contact has other id")
                await self._answer(tap, strings.TAP_OTHER_ID, alert=True)
                return
            wrote = old_id != str(user)
            if wrote:
                await self._contacts.set_telegram_user_id(contact, user)
        except NotionError as exc:
            self._store.audit("notion_write", None, f"user={user} failed: {exc}"[:300])
            log.warning("writing TelegramUserID for user %s failed: %s", user, exc)
            await self._answer(tap, strings.TAP_NOTION_FAILED, alert=True)
            return
        if wrote:
            replaced = f" replaced_id={old_id}" if old_id else ""
            self._store.audit("notion_write", None, f"user={user} contact={page_id}{replaced}")
        await self._finish_approval(tap, member, page_id, contact.name)

    async def _finish_approval(
        self, tap: dict[str, Any], member: dict[str, Any], page_id: str, contact_name: str
    ) -> None:
        user = member["telegram_user_id"]
        reply = {
            "chat": user,
            "topic": None,
            "text": strings.APPROVED.format(
                first_name=strings.first_name_or_there(member["first_name"])
            ),
            "format": "plain",
        }
        approved = self._store.approve_member(user, page_id, tap["from_id"], reply)
        await self._answer(tap)
        if approved:
            self._queue_wake.notify()
            fresh = self._store.member(user) or member
            await self._edit_card(tap, render_outcome(fresh, contact_name))
        else:  # decided while Notion was being written
            await self._rerender(tap, self._store.member(user) or member)

    async def _rerender(self, tap: dict[str, Any], member: dict[str, Any]) -> None:
        await self._edit_card(tap, render_outcome(member), outcome_keyboard(member))

    async def _answer(
        self, tap: dict[str, Any], text: str | None = None, alert: bool = False
    ) -> None:
        try:
            await self._client.answer_callback_query(tap["callback_id"], text, alert)
        except Exception as exc:  # noqa: BLE001 - an old query id may have expired
            log.warning("answering callback %s failed: %s", tap["callback_id"], exc)

    async def _edit_card(
        self,
        tap: dict[str, Any],
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> None:
        try:
            await self._client.edit_message(
                tap["chat_id"], tap["message_id"], text, "HTML", keyboard
            )
        except Exception as exc:  # noqa: BLE001 - "message is not modified" counts too
            log.warning("editing card %s failed: %s", tap["message_id"], exc)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            wake = self._wake.current()  # before reading, so a commit in between is not missed
            try:
                if await self.process_next():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("onboarding cycle failed")
            try:
                await asyncio.wait_for(wake.wait(), timeout=IDLE_WAKE_SECONDS)
            except TimeoutError:
                pass

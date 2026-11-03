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
from typing import Any

from sixto_gateway import strings
from sixto_gateway.config import Config
from sixto_gateway.decision import Decision, NewMember
from sixto_gateway.notifier import Notifier
from sixto_gateway.notion import Contact, ContactsDirectory, NotionError, matches_handle
from sixto_gateway.store import Store
from sixto_gateway.telegram.normalize import IncomingUpdate

log = logging.getLogger(__name__)
IDLE_WAKE_SECONDS = 5.0  # safety net if a wake-up is ever missed
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


def find_candidates(contacts: list[Contact], handle: str | None) -> list[Contact]:
    """Contacts whose handle or an alias equals ``handle``. No handle, no candidates."""
    if not handle:
        return []
    return [c for c in contacts if matches_handle(c, handle)]


def callback_data(action: str, user_id: int, page_id: str = "") -> str:
    """Button payload (Telegram allows 64 bytes): ``ob:<action>:<user id>[:<page id>]``."""
    parts = ["ob", action, str(user_id)]
    if page_id:
        parts.append(page_id.replace("-", ""))
    return ":".join(parts)


def render_card(member: dict[str, Any], candidates: list[Contact]) -> dict[str, Any]:
    """The approver's card as an outbound body (HTML text plus inline buttons)."""
    name = " ".join(p for p in (member["first_name"], member["last_name"]) if p) or "(no name)"
    handle = f"@{member['handle']}" if member["handle"] else strings.CARD_NO_HANDLE
    user_id = member["telegram_user_id"]
    lines = [
        f"<b>{strings.CARD_TITLE}</b>",
        strings.CARD_TELEGRAM.format(
            name=html.escape(name), handle=html.escape(handle), user_id=user_id
        ),
    ]
    row: list[dict[str, str]] = []
    if not candidates:
        lines.append(strings.CARD_NO_MATCH)
    elif len(candidates) == 1:
        (only,) = candidates
        lines.append(strings.CARD_MATCH.format(link=_link(only)))
        row.append(
            {
                "text": strings.BUTTON_APPROVE.format(name=only.name),
                "callback_data": callback_data("ok", user_id, only.page_id),
            }
        )
    else:  # several matches: nothing is picked for the approver
        lines.append(strings.CARD_MATCHES)
        for contact in candidates:
            lines.append(f"• {_link(contact)}")
            row.append(
                {
                    "text": strings.BUTTON_PICK.format(name=contact.name),
                    "callback_data": callback_data("ok", user_id, contact.page_id),
                }
            )
    keyboard = [[b] for b in row]
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
    ):
        self._store = store
        self._contacts = contacts
        self._approver = config.onboarding.approver
        self._wake = wake
        self._queue_wake = queue_wake
        self._clock = clock
        self._retry_at: dict[int, float] = {}

    async def process_next(self) -> bool:
        """Look up one waiting member and queue their card. False when nothing is due."""
        now = self._clock()
        for member in self._store.members_awaiting_lookup():
            user = member["telegram_user_id"]
            if self._retry_at.get(user, 0.0) > now:
                continue
            try:
                found = find_candidates(await self._contacts.list_contacts(), member["handle"])
            except NotionError as exc:
                self._retry_at[user] = self._clock() + LOOKUP_RETRY_SECONDS
                self._store.audit("lookup", None, f"user={user} failed: {exc}"[:300])
                log.warning("contacts lookup for user %s failed: %s", user, exc)
                continue
            self._retry_at.pop(user, None)
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

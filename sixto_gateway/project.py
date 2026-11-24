"""``/project`` subcommands the gateway answers itself through the t6 API (ticket #125).

Every other ``/project`` goes to its module (t6core) as before. A subcommand here works in a DM
only, and is asked of the t6 API as the person who typed it, so the API decides who may read
what and its refusal is shown as is. The poller saves the request with the update
(``decide_project``); ``ProjectWorker`` asks the API outside the poll loop and queues the reply.

To add a subcommand: recognise it in ``parse_subcommand`` and add its handler to
``ProjectCommands._handlers``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from t6.client import APIError
from t6.verbs import (
    ConcernListInput,
    ConcernReopenInput,
    ConcernResolveInput,
    Done,
    PersonGetInput,
    Preview,
    ProjectGetInput,
    ProjectNotesInput,
    ProjectResolveInput,
    Refused,
)

from sixto_gateway import strings
from sixto_gateway.decision import Decision, ProjectRequest
from sixto_gateway.notifier import Notifier
from sixto_gateway.store import Store
from sixto_gateway.telegram.client import Keyboard, TelegramClient
from sixto_gateway.telegram.normalize import IncomingUpdate

log = logging.getLogger(__name__)
IDLE_WAKE_SECONDS = 5.0  # safety net if a wake-up is ever missed
MESSAGE_LIMIT = 4096  # Telegram's limit, in UTF-16 code units
TAP_PREFIX = "pc"  # callback data of a /project concerns button: pc:<action>:<Concern id>
TOAST_LIMIT = 200  # Telegram refuses a longer answer to a button tap
EXACT_CODE = {"exact_id", "normalized"}  # ``project resolve`` methods that are not a guess

CRM_COMMANDS = ("project", "crm", "leads")  # the commands this module answers

Reply = dict[str, Any]  # an outbound message body without its chat: text, format (, keyboard)


def parse_subcommand(args: str) -> tuple[str, str] | None:
    """``(name, argument)`` for a subcommand the gateway answers, else None.

    ``<code> notes``: the Project's PM Notes history. A code may itself contain spaces.
    ``concerns``: the open Concerns, each with its buttons.
    """
    words = args.split()
    if len(words) == 1 and words[0].casefold() == "concerns":
        return "concerns", ""
    if len(words) >= 2 and words[-1].casefold() == "notes":
        return "notes", " ".join(words[:-1])
    return None


def parse_crm(command: str | None, args: str) -> tuple[str, str] | None:
    """``(name, argument)`` for ``/crm snooze <date>``, ``/crm unsnooze``, ``/leads``, else None."""
    words = args.split(maxsplit=1)
    if command == "leads" and not words:
        return "leads", ""
    if command == "crm" and words and words[0].casefold() == "snooze":
        return "snooze", words[1].strip() if len(words) > 1 else ""
    if command == "crm" and len(words) == 1 and words[0].casefold() == "unsnooze":
        return "unsnooze", ""
    return None


def decide_project(update: IncomingUpdate) -> Decision | None:
    """The decision for a ``/project``, ``/crm`` or ``/leads`` the gateway answers; None leaves
    it to the module (or, for ``/crm`` and ``/leads``, to the command allow-list)."""
    if update.command == "project":
        found = parse_subcommand(update.command_args)
    else:
        found = parse_crm(update.command, update.command_args)
    if found is None:
        return None
    if update.edited or update.from_id is None:  # an edit is never a new command
        return Decision(None)
    name, argument = found
    shown = update.command if name == update.command else f"{update.command} {name}"
    detail = f"command={shown} from={update.from_id}"  # never the argument
    if update.chat_type != "private" and (name == "concerns" or update.command != "project"):
        # a one-line pointer to the DM
        pointer = (
            strings.CONCERNS_DM_ONLY
            if name == "concerns"
            else strings.CRM_DM_ONLY.format(command=update.command)
        )
        return Decision(None, audit=("command_refused", None, f"{detail} not_dm"), reply=pointer)
    if update.chat_type != "private":  # DMs only; a group refusal is silent
        return Decision(None, audit=("command_refused", None, f"{detail} not_dm"))
    return Decision(
        None,
        audit=("command_answered", None, detail),
        project_request=ProjectRequest(update.from_id, update.chat_id, name, argument),
    )


def decide_project_tap(update: IncomingUpdate) -> Decision | None:
    """The decision for a tap on a ``/project concerns`` button; None: not one of ours."""
    if not update.callback_data.startswith(f"{TAP_PREFIX}:"):
        return None
    if update.callback_id is None or update.from_id is None or update.chat_type != "private":
        return Decision(None)  # DMs only: a copy of the buttons in a group does nothing
    return Decision(
        None,
        audit=("command_answered", None, f"command=project tap from={update.from_id}"),
        project_request=ProjectRequest(
            update.from_id,
            update.chat_id,
            "tap",
            update.callback_data[len(TAP_PREFIX) + 1 :],
            update.callback_id,
            update.message_id,
        ),
    )


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def split_message(lines: list[str], limit: int = MESSAGE_LIMIT) -> list[str]:
    """Lines packed into as few messages as fit; a line is never split across two messages,
    unless it is longer than one message on its own."""
    messages: list[str] = []
    for line in lines:
        if _units(line) > limit:
            step = limit // 2  # a character is at most 2 units
            messages.extend(line[i : i + step] for i in range(0, len(line), step))
        elif messages and _units(messages[-1]) + 1 + _units(line) <= limit:
            messages[-1] += "\n" + line
        else:
            messages.append(line)
    return messages


def render_notes(code: str, lines: list[dict[str, Any]]) -> list[str]:
    """The history as the API orders it (newest first), each line with its As-of date."""
    if not lines:
        return [strings.PROJECT_NOTES_NONE.format(code=code)]
    rows = [
        strings.PROJECT_NOTES_LINE.format(as_of=line["as_of"], text=line["text"])
        + (strings.PROJECT_NOTES_REMOVED.format(date=line["removed"]) if line["removed"] else "")
        for line in lines
    ]
    return split_message([strings.PROJECT_NOTES_TITLE.format(code=code), *rows])


class CrmSnoozeInput(BaseModel):
    """``crm snooze`` (t6-core #716); the pinned t6 has no typed method for it yet."""

    until: str | None = None


class CrmUnsnoozeInput(BaseModel):
    """``crm unsnooze`` (t6-core #716)."""


class LeadListInput(BaseModel):
    """``lead list`` (t6-core #709)."""


LEAD_ROLES = (  # the client roles of a Lead, as the API lists them
    ("lead_owner_ids", strings.LEAD_OWNER),
    ("delegate_producer_ids", strings.LEAD_DELEGATE),
    ("line_producer_ids", strings.LEAD_LINE),
    ("prod_coor_supe_ids", strings.LEAD_COOR),
)


class _Answer(Exception):
    """Stop here and send this text instead (a refusal, an unknown code, the API down)."""

    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


async def _ask(method: Callable[[Any], Any], inputs: Any) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(method, inputs)
    except APIError:
        raise _Answer(strings.T6_NOT_ANSWERING) from None
    if isinstance(result, Done):
        return result.result
    if isinstance(result, Refused):
        raise _Answer(result.reason)
    raise _Answer(strings.T6_NOT_ANSWERING)  # Unavailable: a read never previews


class ProjectCommands:
    """Answers each subcommand by asking the t6 API as the person who typed it."""

    def __init__(self, as_user: Callable[[int], Any], store: Store):
        """``as_user(id)`` is a t6 API client that sends that Telegram user's id."""
        self._as_user = as_user
        self._store = store
        self._handlers: dict[str, Callable[[Any, str], Awaitable[list[Reply]]]] = {
            "notes": self._notes,
            "concerns": self._concerns,
            "snooze": self._snooze,
            "unsnooze": self._unsnooze,
            "leads": self._leads,
        }

    async def answer(self, user: int, name: str, argument: str) -> list[Reply]:
        try:
            return await self._handlers[name](self._as_user(user), argument)
        except _Answer as answer:
            return [_plain(answer.text)]

    async def _project(self, api: Any, code: str) -> dict[str, Any]:
        """The one Project the code names, as ``project resolve`` decides it; never a guess."""
        found = await _ask(api.project_resolve, ProjectResolveInput(text=code))
        match = found.get("match")
        if (
            match is None
            or found.get("needs_review")
            or found.get("method") not in EXACT_CODE
            or not match.get("t6_id")
        ):
            raise _Answer(strings.PROJECT_UNKNOWN_CODE.format(code=code))
        return match

    async def _notes(self, api: Any, code: str) -> list[Reply]:
        project = await self._project(api, code)
        # ``project notes-history`` (t6-core #638) takes the same input as ``project notes``.
        # Once the t6 pin has it, this is ``api.project_notes_history(...)``.
        history = await _ask(
            lambda inputs: api.call("project", "notes-history", inputs),
            ProjectNotesInput(project=project["t6_id"]),
        )
        shown = project.get("project_code") or code
        return [_plain(text) for text in render_notes(shown, history["lines"])]

    async def _snooze(self, api: Any, until: str) -> list[Reply]:
        # ``crm snooze`` (t6-core #716): the API checks the date and words every refusal.
        # Once the t6 pin has it, this is ``api.crm_snooze(...)``.
        done = await _ask(
            lambda inputs: api.call("crm", "snooze", inputs), CrmSnoozeInput(until=until or None)
        )
        return [_plain(done["message"])]

    async def _unsnooze(self, api: Any, _: str) -> list[Reply]:
        # Once the t6 pin has ``crm unsnooze``, this is ``api.crm_unsnooze(...)``.
        done = await _ask(lambda inputs: api.call("crm", "unsnooze", inputs), CrmUnsnoozeInput())
        return [_plain(done["message"])]

    async def _leads(self, api: Any, _: str) -> list[Reply]:
        # ``lead list`` (t6-core #709). Once the t6 pin has it, this is ``api.lead_list(...)``.
        found = await _ask(lambda inputs: api.call("lead", "list", inputs), LeadListInput())
        leads = found["leads"]
        if not leads:
            return [_plain(strings.LEADS_NONE)]
        names: dict[str, str | None] = {}
        rows = [strings.LEADS_TITLE.format(count=len(leads))]
        for lead in leads:
            head = strings.LEAD_LINE_HEAD.format(name=lead["name"], stage=lead["stage"])
            if lead.get("days_in_stage") is not None:
                head += strings.LEAD_DAYS.format(days=lead["days_in_stage"])
            rows.append(head)
            for field, label in LEAD_ROLES:
                people = [await self._person(api, names, i) for i in lead.get(field) or []]
                if named := [n for n in people if n]:
                    rows.append(f"{label}: {', '.join(named)}")
        return [_plain(text) for text in split_message(rows)]

    @staticmethod
    async def _person(api: Any, names: dict[str, str | None], person_id: str) -> str | None:
        """A Person's name, asked once per list; None when the API will not say."""
        if person_id not in names:
            found = await _read(api, "person", PersonGetInput(person_id=person_id))
            names[person_id] = found["person"]["name"] if found else None
        return names[person_id]

    async def _concerns(self, api: Any, _: str) -> list[Reply]:
        """Every open Concern in its own message, so a button edits its own Concern only."""
        listed = await _list_concerns(api)
        items = [(c, False) for c in listed["concerns"]] + [(c, True) for c in listed["pending"]]
        if not items:
            return [_plain(strings.CONCERNS_NONE)]
        replies = [_plain(strings.CONCERNS_TITLE.format(count=len(items)))]
        for item, pending in items:
            text, keyboard = await _render(api, item, pending)
            replies.append({**_plain(text), "keyboard": keyboard})
        return replies

    async def tap(self, user: int, payload: str, chat_id: int, message_id: int, key: str) -> Tap:
        """Answer one tap on a ``/project concerns`` button as the person who tapped it.

        ``key`` names the tap (its update id), so a tap handled twice asks the API the same thing.
        """
        api = self._as_user(user)
        action, _, concern = payload.partition(":")
        try:
            if action == "c":  # Close: preview, then wait for Confirm
                return await self._close(api, user, concern, chat_id, message_id, f"close:{key}")
            if action == "y":
                return await self._confirm(api, user, concern, chat_id, message_id)
            if action == "x":  # Cancel: back to the Close button
                self._store.drop_concern_preview(chat_id, message_id, user)
                return await self._show(api, concern)
            if action == "u":
                return await self._undo(api, concern, f"undo:{key}")
        except _Answer as answer:
            return Tap(answer.text, alert=True)
        return Tap(strings.TAP_EXPIRED, alert=True)

    async def _close(
        self, api: Any, user: int, concern: str, chat: int, message: int, key: str
    ) -> Tap:
        result = await _ask_write(
            api, "resolve", ConcernResolveInput(concern=concern, idempotency_key=key)
        )
        if not isinstance(result, Preview):
            raise _Answer(strings.T6_NOT_ANSWERING)
        self._store.save_concern_preview(chat, message, user, concern, result.token, key)
        item, pending = await _find(api, concern)
        text, _ = await _render(api, item, pending)
        keyboard = [[_button(strings.BUTTON_CONFIRM, "y", concern)]]
        keyboard[0].append(_button(strings.BUTTON_CANCEL, "x", concern))
        return Tap(None, f"{text}\n{strings.CONCERN_CONFIRM_QUESTION}", keyboard)

    async def _confirm(self, api: Any, user: int, concern: str, chat: int, message: int) -> Tap:
        saved = self._store.concern_preview(chat, message)
        if saved is None or saved["concern"] != concern or saved["from_id"] != user:
            raise _Answer(strings.TAP_EXPIRED)
        try:
            result = await _ask_write(
                api,
                "resolve",
                ConcernResolveInput(
                    concern=concern,
                    idempotency_key=saved["idempotency_key"],
                    confirm=saved["token"],
                ),
            )
        except _Answer as answer:  # a stale or refused preview is over: show what is true now
            self._store.drop_concern_preview(chat, message, user)
            return await self._show(api, concern, toast=answer.text)
        self._store.drop_concern_preview(chat, message, user)
        if not isinstance(result, Done):
            raise _Answer(strings.T6_NOT_ANSWERING)
        return await self._show(api, concern)

    async def _undo(self, api: Any, concern: str, key: str) -> Tap:
        """Undo is its own confirmation: preview and confirm in one go.

        Once the Monday run has applied the close the Concern is closed and a reopen would reopen
        it for real, so read the state first. An open Concern without a pending close is left to
        the API, which refuses it.
        """
        try:
            try:
                await _find(api, concern)
            except _Answer:  # no longer open: the close was applied
                raise _Answer(strings.CONCERN_CLOSE_APPLIED) from None
            preview = await _ask_write(
                api, "reopen", ConcernReopenInput(concern=concern, idempotency_key=key)
            )
            if not isinstance(preview, Preview):
                raise _Answer(strings.T6_NOT_ANSWERING)
            await _ask_write(
                api,
                "reopen",
                ConcernReopenInput(concern=concern, idempotency_key=key, confirm=preview.token),
            )
        except _Answer as answer:
            return await self._show(api, concern, toast=answer.text)
        return await self._show(api, concern)

    async def _show(self, api: Any, concern: str, toast: str | None = None) -> Tap:
        """The Concern as it is now: Close if open, Undo if it has a pending close."""
        try:
            item, pending = await _find(api, concern)
        except _Answer as answer:
            return Tap(toast or answer.text, alert=True)
        text, keyboard = await _render(api, item, pending)
        return Tap(toast, text, keyboard, alert=toast is not None)


@dataclass(frozen=True)
class Tap:
    """What a tap shows: a toast (a popup if ``alert``) and the message's new text and buttons."""

    toast: str | None = None
    text: str | None = None  # None: the message stays as it is
    keyboard: Keyboard | None = None
    alert: bool = False


def _button(text: str, action: str, concern: str) -> dict[str, str]:
    return {"text": text, "callback_data": f"{TAP_PREFIX}:{action}:{concern}"}


async def _ask_write(api: Any, verb: str, inputs: Any) -> Any:
    """A ``concern`` write as the API answers it: a Preview or Done, a refusal as its reason."""
    try:
        result = await asyncio.to_thread(lambda: api.call("concern", verb, inputs))
    except APIError:
        raise _Answer(strings.T6_NOT_ANSWERING) from None
    if isinstance(result, Refused):
        raise _Answer(result.reason)
    if not isinstance(result, Preview | Done):
        raise _Answer(strings.T6_NOT_ANSWERING)
    return result


async def _list_concerns(api: Any) -> dict[str, Any]:
    found = await _ask(
        lambda inputs: api.call("concern", "list", inputs), ConcernListInput(open=True)
    )
    return {"concerns": found.get("concerns", []), "pending": found.get("pending", [])}


async def _find(api: Any, concern: str) -> tuple[dict[str, Any], bool]:
    """The open Concern and whether a close is pending on it."""
    listed = await _list_concerns(api)
    for pending, items in ((False, listed["concerns"]), (True, listed["pending"])):
        for item in items:
            if item["id"] == concern:
                return item, pending
    raise _Answer(strings.CONCERN_GONE)


async def _read(api: Any, noun: str, inputs: Any) -> dict[str, Any] | None:
    """A name for display: None when the API will not say (the line then leaves it out)."""
    try:
        result = await asyncio.to_thread(lambda: api.call(noun, "get", inputs))
    except APIError:
        return None
    return result.result if isinstance(result, Done) else None


async def _render(api: Any, item: dict[str, Any], pending: bool) -> tuple[str, Keyboard]:
    """A Concern's message: Project, text, owner, age, and Close or (when pending) Undo."""
    found = await _read(api, "project", ProjectGetInput(project=item["project_id"]))
    project = found["project"] if found else {}
    label = project.get("project_code") or project.get("name") or item["project_id"]
    owner = ""
    if item.get("owner_id"):
        person = await _read(api, "person", PersonGetInput(person_id=item["owner_id"]))
        if person:
            owner = strings.CONCERN_OWNER.format(name=person["person"]["name"])
    days = int(item.get("age_seconds") or 0) // 86400
    lines = [
        strings.CONCERN_LINE.format(project=label, text=item["text"]),
        strings.CONCERN_DETAIL.format(owner=owner, days=days),
    ]
    if not pending:
        return "\n".join(lines), [[_button(strings.BUTTON_CLOSE, "c", item["id"])]]
    name = item.get("close_by_name")
    lines.append(
        strings.CONCERN_CLOSING.format(name=name) if name else strings.CONCERN_CLOSING_UNNAMED
    )
    return "\n".join(lines), [[_button(strings.BUTTON_UNDO, "u", item["id"])]]


def _plain(text: str) -> Reply:
    return {"text": text, "format": "plain"}


class ProjectWorker:
    """Answers saved ``/project`` requests one at a time, oldest first."""

    def __init__(
        self,
        store: Store,
        commands: ProjectCommands,
        wake: Notifier,
        queue_wake: Notifier,
        client: TelegramClient,
    ):
        self._store = store
        self._client = client
        self._commands = commands
        self._wake = wake
        self._queue_wake = queue_wake

    async def process_next(self) -> bool:
        """Answer one request and queue its reply. False when none is waiting."""
        request = self._store.next_project_request()
        if request is None:
            return False
        if request["name"] == "tap":
            await self._tap(request)
            return True
        try:
            replies = await self._commands.answer(
                request["from_id"], request["name"], request["argument"]
            )
        except Exception:  # an unexpected API reply: say so once, never retry it forever
            log.exception("project command failed")
            replies = [_plain(strings.T6_NOT_ANSWERING)]
        self._store.answer_project_request(request["update_id"], request["chat_id"], replies)
        self._queue_wake.notify()
        return True

    async def _tap(self, request: dict[str, Any]) -> None:
        """Answer a button tap and edit its message. Never retried: the person taps again."""
        try:
            tap = await self._commands.tap(
                request["from_id"],
                request["argument"],
                request["chat_id"],
                request["message_id"],
                str(request["update_id"]),
            )
        except Exception:
            log.exception("project tap failed")
            tap = Tap(strings.T6_NOT_ANSWERING, alert=True)
        try:
            toast = tap.toast[:TOAST_LIMIT] if tap.toast else tap.toast
            await self._client.answer_callback_query(request["callback_id"], toast, tap.alert)
        except Exception as exc:  # noqa: BLE001 - an old query id may have expired
            log.warning("answering callback %s failed: %s", request["callback_id"], exc)
        if tap.text is not None:
            try:
                await self._client.edit_message(
                    request["chat_id"], request["message_id"], tap.text, None, tap.keyboard
                )
            except Exception as exc:  # noqa: BLE001 - "message is not modified" counts too
                log.warning("editing message %s failed: %s", request["message_id"], exc)
        self._store.answer_project_request(request["update_id"], request["chat_id"], [])

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            wake = self._wake.current()  # before reading, so a commit in between is not missed
            try:
                if await self.process_next():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("project command cycle failed")
            try:
                await asyncio.wait_for(wake.wait(), timeout=IDLE_WAKE_SECONDS)
            except TimeoutError:
                pass

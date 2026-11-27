"""``/project`` subcommands the gateway answers itself through the t6 API (ticket #125).

Every other ``/project`` goes to its module (t6core) as before. A subcommand here works in a DM
only (``links`` also in a group or topic), and is asked of the t6 API as the person who typed
it, so the API decides who may read what and its refusal is shown as is. The poller saves the
request with the update (``decide_project``); ``ProjectWorker`` asks the API outside the poll
loop and queues the reply (a tap on a ``links`` button is answered and its message edited there).

To add a subcommand: recognise it in ``parse_subcommand`` and add its handler to
``ProjectCommands._handlers``.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from t6.client import APIError
from t6.verbs import (
    Done,
    DriveProjectMovementsInput,
    PersonGetInput,
    ProjectNotesInput,
    ProjectResolveInput,
    Refused,
)

from sixto_gateway import strings
from sixto_gateway.decision import Decision, ProjectRequest
from sixto_gateway.notifier import Notifier
from sixto_gateway.store import Store
from sixto_gateway.telegram.client import TelegramClient
from sixto_gateway.telegram.normalize import IncomingUpdate

log = logging.getLogger(__name__)
IDLE_WAKE_SECONDS = 5.0  # safety net if a wake-up is ever missed
MESSAGE_LIMIT = 4096  # Telegram's limit, in UTF-16 code units
DRIVE_MOVEMENT_LIMIT = 10
LINKS_TAP_PREFIX = "pl"  # callback data of a /project links button: pl:<T6 Project ID>
TOAST_LIMIT = 200  # Telegram refuses a longer answer to a button tap
EXACT_CODE = {"exact_id", "normalized"}  # ``project resolve`` methods that are not a guess

CRM_COMMANDS = ("project", "crm", "leads")  # the commands this module answers

Reply = dict[str, Any]  # an outbound message body without its chat: text, format (, keyboard)


def parse_subcommand(args: str) -> tuple[str, str] | None:
    """``(name, argument)`` for a subcommand the gateway answers, else None.

    ``<code> notes`` / ``<code> drives``: PM Notes history / Drive movements.
    ``links``: the links card of this chat's Project; ``<code> links``: of that Project.
    A code may itself contain spaces.
    """
    words = args.split()
    if len(words) == 1 and words[0].casefold() == "links":
        return "links", ""
    if len(words) >= 2 and words[-1].casefold() in {"notes", "drives", "links"}:
        return words[-1].casefold(), " ".join(words[:-1])
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
    if update.chat_type != "private" and update.command != "project":
        # a one-line pointer to the DM
        pointer = strings.CRM_DM_ONLY.format(command=update.command)
        return Decision(None, audit=("command_refused", None, f"{detail} not_dm"), reply=pointer)
    if update.chat_type != "private" and name != "links":  # DMs only; a group refusal is silent
        return Decision(None, audit=("command_refused", None, f"{detail} not_dm"))
    return Decision(
        None,
        audit=("command_answered", None, detail),
        project_request=ProjectRequest(
            update.from_id,
            update.chat_id,
            name,
            argument,
            update.topic_id,
            update.chat_type or "private",
            update.message_id,
            update.topic_name,
        ),
    )


def decide_project_tap(update: IncomingUpdate) -> Decision | None:
    """The decision for a tap on a ``/project links`` button; None: not one of ours.

    Anyone in the chat may tap, so this is not limited to DMs or to operators."""
    if not update.callback_data.startswith(f"{LINKS_TAP_PREFIX}:"):
        return None
    if update.callback_id is None or update.from_id is None:
        return Decision(None)
    return Decision(
        None,
        audit=("command_answered", None, f"command=project links tap from={update.from_id}"),
        project_request=ProjectRequest(
            update.from_id,
            update.chat_id,
            "links_tap",
            update.callback_data[len(LINKS_TAP_PREFIX) + 1 :],
            update.topic_id,
            update.chat_type or "private",
            update.message_id,
            None,
            update.callback_id,
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


LEAD_ROLES = (  # the client roles of a Lead, as the API lists them (names)
    ("lead_owners", strings.LEAD_OWNER),
    ("delegate_producers", strings.LEAD_DELEGATE),
    ("line_producers", strings.LEAD_LINE),
    ("prod_coor_supes", strings.LEAD_COOR),
)


class ProjectLinksInput(BaseModel):
    """``project links`` (t6-core #685). The pinned t6 takes only ``project``; a chat and topic,
    the topic's name and the asker are the chat-scoped read, so this is sent as a plain model."""

    project: str | None = None
    chat: int | None = None
    topic: int | None = None
    topic_name: str | None = None
    asker_telegram_id: int | None = None


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

    def __init__(self, as_user: Callable[[int], Any], service: Any):
        """``as_user(id)`` is a t6 API client that sends that Telegram user's id; ``service`` is
        the gateway's own client, for the chat-scoped ``project links`` read that anyone in a
        chat may ask (a freelancer has no Person record)."""
        self._as_user = as_user
        self._service = service
        self._handlers: dict[str, Callable[[Any, str], Awaitable[list[Reply]]]] = {
            "notes": self._notes,
            "drives": self._drives,
            "snooze": self._snooze,
            "unsnooze": self._unsnooze,
            "leads": self._leads,
        }

    async def answer(
        self,
        user: int,
        name: str,
        argument: str,
        *,
        chat_id: int = 0,
        topic_id: int | None = None,
        chat_type: str = "private",
        topic_name: str | None = None,
    ) -> list[Reply]:
        try:
            if name == "links":
                return await self._links(user, argument, chat_id, topic_id, chat_type, topic_name)
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

    async def _links(
        self,
        user: int,
        code: str,
        chat_id: int,
        topic_id: int | None,
        chat_type: str,
        topic_name: str | None,
    ) -> list[Reply]:
        """The links card. In a DM only an operator may ask, by code. In a group anyone may ask for
        a Project linked to the chat; a code not linked to it is tried once as the person typing,
        so an operator reads any Project anywhere, and the first refusal is shown otherwise."""
        if chat_type == "private":
            if not code:
                raise _Answer(strings.PROJECT_LINKS_DM_CODE)
            return [
                _card_reply(
                    _shown(await _links_call(self._as_user(user), ProjectLinksInput(project=code)))
                )
            ]
        inputs = ProjectLinksInput(
            project=code or None,
            chat=chat_id,
            topic=topic_id,
            topic_name=topic_name,
            asker_telegram_id=None if code else user,
        )
        result = await _links_call(self._service, inputs)
        if isinstance(result, Refused) and code:
            as_person = await _links_call(self._as_user(user), ProjectLinksInput(project=code))
            if isinstance(as_person, Done):
                return [_card_reply(as_person.result)]
        found = _shown(result)
        cards = _cards(found)
        if not cards:  # an unlinked topic; the API says who may be told to /link, and to what
            text = strings.PROJECT_LINKS_UNLINKED
            if found.get("can_link") and found.get("suggested_code"):
                text += "\n" + strings.PROJECT_LINKS_HINT.format(code=found["suggested_code"])
            return [_plain(text)]
        if len(cards) > 1:
            return [_pick_reply(cards, chat_id, topic_id, topic_name)]
        return [_card_reply(cards[0])]

    async def links_tap(self, chat_id: int, topic_id: int | None, project_id: str) -> Tap:
        """A tap on a Project's button: its card, if the chat is still linked to it. Anyone in the
        chat may tap, so this asks with the gateway's own token."""
        result = await _links_call(
            self._service, ProjectLinksInput(project=project_id, chat=chat_id, topic=topic_id)
        )
        if isinstance(result, Refused):
            return Tap(strings.PROJECT_LINKS_UNLINKED_TAP)
        if not isinstance(result, Done):
            return Tap(strings.T6_NOT_ANSWERING, alert=True)
        return Tap(text=render_card(result.result), html=True)

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

    async def _drives(self, api: Any, code: str) -> list[Reply]:
        project = await self._project(api, code)
        found = await _ask(
            api.drive_project_movements, DriveProjectMovementsInput(project=project["t6_id"])
        )
        shown = project.get("project_code") or code
        if not found["movements"]:
            return [_plain(strings.PROJECT_DRIVES_NONE.format(code=shown))]
        lines = [strings.PROJECT_DRIVES_TITLE.format(code=shown)]
        names: dict[str, str] = {}
        # Core supplies effective Movements newest first, with Manila dates and saved coordinators.
        for movement in found["movements"][:DRIVE_MOVEMENT_LIMIT]:
            coordinators = []
            for person_id in movement["coordinators"]:
                if person_id not in names:
                    person = await _read(api, "person", PersonGetInput(person_id=person_id))
                    names[person_id] = person["person"]["name"] if person else person_id
                coordinators.append(names[person_id])
            lines.append(
                strings.PROJECT_DRIVES_LINE.format(
                    date=movement["on"] or "—",
                    direction=movement["direction"],
                    label=movement["label"] or "—",
                    role=(
                        strings.PROJECT_DRIVES_RECIPIENT
                        if movement["direction"] == "OUT"
                        else strings.PROJECT_DRIVES_RECEIVER
                    ),
                    person=movement["person"] or "—",
                    coordinators=", ".join(coordinators) or "—",
                )
                + (strings.PROJECT_DRIVES_REVIEW if movement["to_review"] else "")
            )
        remaining = len(found["movements"]) - DRIVE_MOVEMENT_LIMIT
        if remaining > 0:
            lines.append(strings.PROJECT_DRIVES_MORE.format(count=remaining))
        return [_plain(text) for text in split_message(lines)]

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
        # ``lead list`` (t6-core #709, names #705). Once the t6 pin has it: ``api.lead_list(...)``.
        found = await _ask(lambda inputs: api.call("lead", "list", inputs), LeadListInput())
        leads = found["leads"]
        if not leads:
            return [_plain(strings.LEADS_NONE)]
        rows = [strings.LEADS_TITLE.format(count=len(leads))]
        for lead in leads:
            head = strings.LEAD_LINE_HEAD.format(name=lead["name"], stage=lead["stage"])
            if lead.get("days_in_stage") is not None:
                head += strings.LEAD_DAYS.format(days=lead["days_in_stage"])
            rows.append(head)
            if lead.get("studio"):
                rows.append(f"{strings.LEAD_STUDIO}: {lead['studio']}")
            for field, label in LEAD_ROLES:
                if named := lead.get(field):
                    rows.append(f"{label}: {', '.join(named)}")
        return [_plain(text) for text in split_message(rows)]


async def _links_call(api: Any, inputs: ProjectLinksInput) -> Any:
    """``project links`` as the API answers it; None when it could not be asked."""
    try:
        return await asyncio.to_thread(lambda: api.call("project", "links", inputs))
    except APIError:
        return None


def _shown(result: Any) -> dict[str, Any]:
    if isinstance(result, Done):
        return result.result
    if isinstance(result, Refused):
        raise _Answer(result.reason)
    raise _Answer(strings.T6_NOT_ANSWERING)


def _cards(found: dict[str, Any]) -> list[dict[str, Any]]:
    """The Projects an answer holds: ``projects`` for a chat, else the one card it is itself."""
    if found.get("projects"):
        return list(found["projects"])
    return [found] if found.get("project_id") else []


def _link_line(link: dict[str, Any]) -> str:
    label = link.get("topic_name") or (
        strings.PROJECT_LINKS_WHOLE_CHAT if link.get("topic") is None else f"topic {link['topic']}"
    )
    state = strings.PROJECT_LINK_STATES.get(link["state"], link["state"]).format(
        dead_since=link.get("dead_since")
    )
    if link["state"] == "denied" and link.get("reason"):
        state += f" ({link['reason']})"
    return f"• {html.escape(label)}: {html.escape(state)}"


def render_card(card: dict[str, Any]) -> str:
    """A Project's links card as HTML: name and code, its Telegram links with their states,
    the Notion page and Frame.io links, then the Links by kind."""
    name = " ".join(
        part
        for part in (card.get("name"), f"[{card['code']}]" if card.get("code") else None)
        if part
    )
    lines = [f"<b>{html.escape(name or card['project_id'])}</b>"]
    if card.get("chat_links"):
        lines += [
            strings.PROJECT_LINKS_TELEGRAM,
            *(_link_line(link) for link in card["chat_links"]),
        ]
    for key, label in (
        ("notion_url", strings.PROJECT_LINKS_NOTION),
        ("frameio_url", strings.PROJECT_LINKS_FRAMEIO),
    ):
        if card.get(key):
            lines.append(f'<a href="{html.escape(card[key], quote=True)}">{label}</a>')
    for kind, urls in (card.get("links") or {}).items():
        lines += [f"{html.escape(kind)}: {html.escape(url)}" for url in urls]
    if card.get("links_unavailable"):
        lines.append(strings.PROJECT_LINKS_UNAVAILABLE)
    return "\n".join(lines)


@dataclass(frozen=True)
class Tap:
    """What a tap shows: a toast (a popup if ``alert``) and the message's new text, if any."""

    toast: str | None = None
    text: str | None = None  # None: the message stays as it is
    html: bool = False
    alert: bool = False


def _topic_first(
    cards: list[dict[str, Any]], chat_id: int, topic_id: int | None, topic_name: str | None
) -> list[dict[str, Any]]:
    """Projects in link order, those whose code is in the topic's name first. The name is the
    one the command carried, else one a card's link to this topic has."""
    if topic_name is None:
        topic_name = next(
            (
                link["topic_name"]
                for card in cards
                for link in card.get("chat_links", [])
                if link.get("chat") == chat_id
                and link.get("topic") == topic_id
                and link.get("topic_name")
            ),
            "",
        )

    def named(card: dict[str, Any]) -> bool:
        code = card.get("code")
        if not code:
            return False
        return bool(
            re.search(rf"(?<![A-Za-z0-9]){re.escape(code)}(?![A-Za-z0-9])", topic_name, re.I)
        )

    return sorted(cards, key=lambda card: not named(card))


def _pick_reply(
    cards: list[dict[str, Any]], chat_id: int, topic_id: int | None, topic_name: str | None
) -> Reply:
    keyboard = [
        [
            {
                "text": f"{card['code']} · {card['name']}" if card.get("code") else card["name"],
                "callback_data": f"{LINKS_TAP_PREFIX}:{card['project_id']}",
            }
        ]
        for card in _topic_first(cards, chat_id, topic_id, topic_name)
    ]
    return {**_plain(strings.PROJECT_LINKS_PICK), "keyboard": keyboard}


def _card_reply(card: dict[str, Any]) -> Reply:
    return {"text": render_card(card), "format": "html"}


async def _read(api: Any, noun: str, inputs: Any) -> dict[str, Any] | None:
    """A name for display: None when the API will not say (the line then leaves it out)."""
    try:
        result = await asyncio.to_thread(lambda: api.call(noun, "get", inputs))
    except APIError:
        return None
    return result.result if isinstance(result, Done) else None


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
        if request["name"] == "links_tap":
            await self._tap(request)
            return True
        try:
            replies = await self._commands.answer(
                request["from_id"],
                request["name"],
                request["argument"],
                chat_id=request["chat_id"],
                topic_id=request["topic_id"],
                chat_type=request["chat_type"],
                topic_name=request["topic_name"],
            )
        except Exception:  # an unexpected API reply: say so once, never retry it forever
            log.exception("project command failed")
            replies = [_plain(strings.T6_NOT_ANSWERING)]
        in_group = request["chat_type"] != "private"
        self._store.answer_project_request(
            request["update_id"],
            request["chat_id"],
            replies,
            topic_id=request["topic_id"],
            reply_to_message_id=request["message_id"] if in_group else None,
        )
        self._queue_wake.notify()
        return True

    async def _tap(self, request: dict[str, Any]) -> None:
        """Answer a button tap and edit its message. Never retried: the person taps again."""
        try:
            tap = await self._commands.links_tap(
                request["chat_id"], request["topic_id"], request["argument"]
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
                    request["chat_id"],
                    request["message_id"],
                    tap.text,
                    "HTML" if tap.html else None,
                    None,
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

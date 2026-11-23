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
from typing import Any

from t6.client import APIError
from t6.verbs import Done, ProjectNotesInput, ProjectResolveInput, Refused

from sixto_gateway import strings
from sixto_gateway.decision import Decision, ProjectRequest
from sixto_gateway.notifier import Notifier
from sixto_gateway.store import Store
from sixto_gateway.telegram.normalize import IncomingUpdate

log = logging.getLogger(__name__)
IDLE_WAKE_SECONDS = 5.0  # safety net if a wake-up is ever missed
MESSAGE_LIMIT = 4096  # Telegram's limit, in UTF-16 code units
EXACT_CODE = {"exact_id", "normalized"}  # ``project resolve`` methods that are not a guess

Reply = dict[str, Any]  # an outbound message body without its chat: text, format (, keyboard)


def parse_subcommand(args: str) -> tuple[str, str] | None:
    """``(name, argument)`` for a subcommand the gateway answers, else None.

    ``<code> notes``: the Project's PM Notes history. A code may itself contain spaces.
    """
    words = args.split()
    if len(words) >= 2 and words[-1].casefold() == "notes":
        return "notes", " ".join(words[:-1])
    return None


def decide_project(update: IncomingUpdate) -> Decision | None:
    """The decision for a ``/project`` the gateway answers; None leaves it to the module."""
    found = parse_subcommand(update.command_args)
    if found is None:
        return None
    if update.edited or update.from_id is None:  # an edit is never a new command
        return Decision(None)
    name, argument = found
    detail = f"command=project {name} from={update.from_id}"  # never the argument
    if update.chat_type != "private":  # DMs only; a group refusal is silent
        return Decision(None, audit=("command_refused", None, f"{detail} not_dm"))
    return Decision(
        None,
        audit=("command_answered", None, detail),
        project_request=ProjectRequest(update.from_id, update.chat_id, name, argument),
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

    def __init__(self, as_user: Callable[[int], Any]):
        """``as_user(id)`` is a t6 API client that sends that Telegram user's id."""
        self._as_user = as_user
        self._handlers: dict[str, Callable[[Any, str], Awaitable[list[Reply]]]] = {
            "notes": self._notes,
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


def _plain(text: str) -> Reply:
    return {"text": text, "format": "plain"}


class ProjectWorker:
    """Answers saved ``/project`` requests one at a time, oldest first."""

    def __init__(
        self, store: Store, commands: ProjectCommands, wake: Notifier, queue_wake: Notifier
    ):
        self._store = store
        self._commands = commands
        self._wake = wake
        self._queue_wake = queue_wake

    async def process_next(self) -> bool:
        """Answer one request and queue its reply. False when none is waiting."""
        request = self._store.next_project_request()
        if request is None:
            return False
        replies = await self._commands.answer(
            request["from_id"], request["name"], request["argument"]
        )
        self._store.answer_project_request(request["update_id"], request["chat_id"], replies)
        self._queue_wake.notify()
        return True

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

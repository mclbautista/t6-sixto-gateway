"""Recording-channel photo intake and capture outcomes (ticket #123)."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from t6.client import APIError
from t6.verbs import Done, DriveCaptureInput, DriveCaptureOutput, DriveCaptureReadInput, Refused

from sixto_gateway import strings
from sixto_gateway.config import Config
from sixto_gateway.decision import Decision
from sixto_gateway.notifier import Notifier
from sixto_gateway.store import Store
from sixto_gateway.telegram.client import TelegramClient
from sixto_gateway.telegram.normalize import IncomingUpdate

log = logging.getLogger(__name__)
RETRY_SECONDS = 5.0
QUESTIONS = {
    "missing_direction": strings.DRIVE_ASK_DIRECTION,
    "unreadable_label": strings.DRIVE_ASK_LABEL,
    "no_drive_match": strings.DRIVE_ASK_NO_MATCH,
}
# The same first-word rule as t6: IN or OUT in any case, then space, colon, dash or the end.
_DIRECTION = re.compile(r"(in|out)(?=[\s:\-]|$)", re.IGNORECASE)


def _may_act(config: Config, original: dict[str, Any], user: int | None) -> bool:
    """Only the poster of the photo or a custodian speaks for a capture."""
    return user is not None and (
        user == original["poster_id"] or user in config.groups.get("custodians", [])
    )


def message_has_photo(update: IncomingUpdate) -> bool:
    return bool(update.raw.get("message", {}).get("photo"))


def _undo_request(original: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "undo", "capture": original["capture_id"], "edit_of": original["reply_ref"]}


def _window_ended(store: Store, original: dict[str, Any]) -> bool:
    return original["undo_until"] is not None and store.now() >= original["undo_until"]


def decide_drive(config: Config, update: IncomingUpdate, store: Store) -> Decision | None:
    channel = config.recording_channel
    if channel.chat_id is None or (update.chat_id, update.topic_id) != (
        channel.chat_id,
        channel.topic_id,
    ):
        return None
    if update.edited:
        return _decide_edit(update, store)
    replied_by_member = False
    if update.from_id is not None and update.reply_to_message_id:
        original = store.drive_reply(update.chat_id, update.topic_id, update.reply_to_message_id)
        if original is None:
            original = store.drive_reply_to_photo(
                update.chat_id, update.topic_id, update.reply_to_message_id
            )
        if (
            original is not None
            and update.text.strip().lower() == "undo"
            and original["outcome"] == "resolved"
        ):
            if _may_act(config, original, update.from_id) and not _window_ended(store, original):
                return Decision(None, drive_request=_undo_request(original))
            return None
        asked = (
            original is not None
            and original["kind"] != "text"  # a text-only post's note is not a question
            and original["outcome"] in ("exception", "answered")
        )
        if (
            original is not None
            and asked
            and original["outcome"] == "exception"
            and update.text
            and _may_act(config, original, update.from_id)
            and not store.drive_answer_taken(original["capture_id"])
        ):
            return Decision(
                None,
                drive_request={
                    "kind": "label",
                    "capture": original["capture_id"],
                    "text": update.text,
                    "poster_id": original["poster_id"],
                },
            )
        replied_by_member = original is not None and _may_act(config, original, update.from_id)
        if asked and not replied_by_member and not message_has_photo(update):
            return Decision(None)  # banter under the question: ignored, not even a new post
    if replied_by_member or update.command is not None or update.from_id is None:
        return None  # not subject to the local check
    message = update.raw.get("message", {})
    photos = message.get("photo", [])
    if not photos and not _DIRECTION.match(update.text.strip()):
        return Decision(None, drive_dropped=True)  # chatter: no t6 call, no reply, counted
    if not photos:
        return Decision(
            None,
            drive_request={
                "kind": "text",
                "text": update.text,
                "sender_telegram_id": update.from_id,
                "poster_id": update.from_id,
                "posted_at": message["date"],
            },
        )
    largest = max(photos, key=lambda p: p["width"] * p["height"])
    return Decision(
        None,
        drive_request={
            "file_id": largest["file_id"],
            "text": update.text,
            "sender_telegram_id": update.from_id,
            "poster_id": update.from_id,
            "posted_at": message["date"],
        },
    )


def _decide_edit(update: IncomingUpdate, store: Store) -> Decision | None:
    """A caption edit of a post whose capture is pending or in exception goes to t6."""
    post = store.drive_post(update.chat_id, update.topic_id, update.message_id)
    if post is None:
        return Decision(None)  # an edit of chatter never reaches a module either
    if not store.drive_capture_unresolved(post):
        return None
    return Decision(
        None,
        drive_request={
            "kind": f"edit:{update.update_id}",
            "text": update.text,
            "poster_id": post["poster_id"],
        },
    )


def decide_drive_tap(config: Config, update: IncomingUpdate, store: Store) -> Decision | None:
    if not update.callback_data.startswith("dc:"):
        return None
    channel = config.recording_channel
    original = store.drive_reply(update.chat_id, update.topic_id, update.message_id)
    if (
        channel.chat_id is None
        or (update.chat_id, update.topic_id) != (channel.chat_id, channel.topic_id)
        or original is None
        or original["outcome"] != "resolved"
        or update.callback_data != f"dc:u:{original['update_id']}"
    ):
        return Decision(None, answer_tap=strings.TAP_UNKNOWN_MODULE)
    if not _may_act(config, original, update.from_id):
        return Decision(None, answer_tap=strings.TAP_UNDO_NOT_ALLOWED)
    if _window_ended(store, original):
        return Decision(None, answer_tap=strings.TAP_UNDO_ENDED)
    return Decision(None, answer_tap="", drive_request=_undo_request(original))


class DriveWorker:
    def __init__(
        self,
        store: Store,
        client: TelegramClient,
        api: Any,
        queue_wake: Notifier,
        clock: Callable[[], float] = time.time,
        custodian_id: int | None = None,
    ):
        self._store, self._client, self._api = store, client, api
        self._queue_wake, self._clock = queue_wake, clock
        self._custodian_id = custodian_id  # mentioned when a text-only post is left for review

    async def process_next(self) -> bool:
        self._store.remove_expired_undo_buttons(self._clock())
        request = self._store.next_drive_request(self._clock())
        if request is None:
            return False
        self._store.defer_drive_request(request["update_id"], self._clock() + RETRY_SECONDS)
        try:
            await self._process(request)
        except APIError:
            log.warning("drive API unavailable for update %s", request["update_id"])
        return True

    async def _process(self, request: dict[str, Any]) -> None:
        if request["state"] == "new":
            body = request["body"]
            if request["kind"] == "undo":
                result = await self._call(
                    request,
                    self._api.drive_capture_undo,
                    DriveCaptureReadInput(capture=body["capture"]),
                )
                if isinstance(result, Done):
                    outcome = DriveCaptureOutput.model_validate(result.result)
                    text = (
                        strings.DRIVE_PRACTICE + "\n" if outcome.practice else ""
                    ) + strings.DRIVE_UNDONE
                    self._store.finish_drive_request(request, "undone", {"text": text})
                    self._queue_wake.notify()
                return
            if request["kind"].startswith("edit"):
                inputs = self._edit_input(request)
                if inputs is None:
                    post = self._post(request)
                    if post is not None and post["state"] == "done":
                        self._store.finish_drive_request(request, "ignored", None)
                    return  # else the post is not at t6 yet: try again
            elif request["kind"] == "label":
                inputs = DriveCaptureInput(capture=body["capture"], text=body["text"])
            elif request["kind"] == "text":
                inputs = self._text_input(request)
            else:
                inputs = await self._photo_input(request)
            result = await self._call(request, self._api.drive_capture, inputs)
            if isinstance(result, Done) and request["kind"].startswith("edit"):
                post = self._post(request)
                if post is not None and post["state"] != "done":
                    # the post's own wait still delivers its reply
                    self._store.finish_drive_request(request, "edited", None)
                else:
                    self._store.wait_for_drive(
                        request["update_id"],
                        result.result["capture"],
                        self._clock() + RETRY_SECONDS,
                    )
            elif isinstance(result, Done):
                self._store.wait_for_drive(
                    request["update_id"], result.result["capture"], self._clock() + RETRY_SECONDS
                )
        else:
            result = await self._call(
                request,
                self._api.drive_capture_result,
                DriveCaptureReadInput(capture=request["capture_id"]),
            )
            if isinstance(result, Done):
                outcome = DriveCaptureOutput.model_validate(result.result)
                if outcome.state == "resolved":
                    lines = [
                        strings.DRIVE_RECORDED.format(
                            label=line.label or "",
                            direction=line.direction,
                            person=line.person or "",
                        )
                        for line in outcome.lines
                    ]
                    if outcome.practice:
                        lines.insert(0, strings.DRIVE_PRACTICE)
                    undo_until = (
                        datetime.fromisoformat(outcome.undo_until).timestamp()
                        if outcome.undo_until
                        else None
                    )
                    post = self._post(request) if request["kind"].startswith("edit") else None
                    edit_of = post["reply_ref"] if post is not None else None
                    self._store.finish_drive_request(
                        request,
                        outcome.state,
                        {
                            **({"edit_of": edit_of} if edit_of is not None else {}),
                            "text": "\n".join(lines),
                            "keyboard": [
                                [
                                    {
                                        "text": strings.DRIVE_UNDO,
                                        "callback_data": f"dc:u:{request['update_id']}",
                                    }
                                ]
                            ],
                        },
                        undo_until,
                    )
                    self._queue_wake.notify()
                elif outcome.state == "exception" and request["kind"].startswith("edit"):
                    self._store.finish_drive_request(request, outcome.state, None)
                elif outcome.state == "exception" and outcome.reason == "no_photo":
                    mention = self._custodian_id
                    text = (
                        strings.DRIVE_NO_PHOTO.format(custodian=mention)
                        if mention is not None
                        else strings.DRIVE_NO_PHOTO_NO_CUSTODIAN
                    )
                    self._store.finish_drive_request(
                        request, outcome.state, {"text": text, "format": "html"}
                    )
                    self._queue_wake.notify()
                elif outcome.state == "exception" and not outcome.may_ask:
                    self._store.finish_drive_request(request, outcome.state, None)
                elif outcome.state == "exception":
                    text = QUESTIONS.get(outcome.reason or "", strings.DRIVE_ASK_LABEL)
                    if outcome.practice:
                        text = strings.DRIVE_PRACTICE + "\n" + text
                    self._store.finish_drive_request(request, outcome.state, {"text": text})
                    self._queue_wake.notify()
                elif outcome.state in {"ignored", "undone"}:
                    self._store.finish_drive_request(request, outcome.state, None)
                else:
                    self._store.wait_for_drive(
                        request["update_id"], outcome.capture, self._clock() + RETRY_SECONDS
                    )

    async def _call(self, request: dict[str, Any], method: Callable[..., Any], inputs: Any) -> Any:
        result = await asyncio.to_thread(method, inputs)
        if isinstance(result, Refused):
            self._store.finish_drive_request(request, "refused", None)
            self._store.audit("drive_capture_refused", detail=f"update={request['update_id']}")
        return result

    def _post(self, request: dict[str, Any]) -> dict[str, Any] | None:
        return self._store.drive_post(
            request["chat_id"], request["topic_id"], request["message_id"]
        )

    def _edit_input(self, request: dict[str, Any]) -> DriveCaptureInput | None:
        post = self._post(request)
        if post is None or post["capture_id"] is None:
            return None
        return DriveCaptureInput(
            capture=post["capture_id"], text=request["body"]["text"], edit=True
        )

    def _text_input(self, request: dict[str, Any]) -> DriveCaptureInput:
        body = request["body"]
        return DriveCaptureInput(
            chat_id=request["chat_id"],
            topic_id=request["topic_id"],
            message_id=request["message_id"],
            sender_telegram_id=body["sender_telegram_id"],
            posted_at=datetime.fromtimestamp(body["posted_at"], UTC),
            text=body["text"],
        )

    async def _photo_input(self, request: dict[str, Any]) -> DriveCaptureInput:
        body = request["body"]
        photo = await self._client.download_photo(body["file_id"])
        return DriveCaptureInput(
            chat_id=request["chat_id"],
            topic_id=request["topic_id"],
            message_id=request["message_id"],
            sender_telegram_id=body["sender_telegram_id"],
            posted_at=datetime.fromtimestamp(body["posted_at"], UTC),
            text=body["text"],
            photo=base64.b64encode(photo).decode("ascii"),
            media_type="image/jpeg",
        )

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                if await self.process_next():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("drive capture cycle failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=RETRY_SECONDS)
            except TimeoutError:
                pass

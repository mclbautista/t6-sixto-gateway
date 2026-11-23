"""Recording-channel photo intake and capture outcomes (ticket #123)."""

from __future__ import annotations

import asyncio
import base64
import logging
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


def decide_drive(config: Config, update: IncomingUpdate, store: Store) -> Decision | None:
    channel = config.recording_channel
    if channel.chat_id is None or (update.chat_id, update.topic_id) != (
        channel.chat_id,
        channel.topic_id,
    ):
        return None
    if not update.edited and update.from_id is not None and update.reply_to_message_id:
        original = store.drive_reply(update.chat_id, update.topic_id, update.reply_to_message_id)
        if original is not None and original["outcome"] == "exception" and update.text:
            return Decision(
                None,
                drive_request={
                    "kind": "label",
                    "capture": original["capture_id"],
                    "text": update.text,
                },
            )
    message = update.raw.get("message", {})
    photos = message.get("photo", [])
    if not photos or update.edited or update.from_id is None:
        return None
    largest = max(photos, key=lambda p: p["width"] * p["height"])
    return Decision(
        None,
        drive_request={
            "file_id": largest["file_id"],
            "text": update.text,
            "sender_telegram_id": update.from_id,
            "posted_at": message["date"],
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
    return Decision(
        None,
        answer_tap="",
        drive_request={
            "kind": "undo",
            "capture": original["capture_id"],
            "edit_of": original["reply_ref"],
        },
    )


class DriveWorker:
    def __init__(
        self,
        store: Store,
        client: TelegramClient,
        api: Any,
        queue_wake: Notifier,
        clock: Callable[[], float] = time.time,
    ):
        self._store, self._client, self._api = store, client, api
        self._queue_wake, self._clock = queue_wake, clock

    async def process_next(self) -> bool:
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
            if request["kind"] == "label":
                inputs = DriveCaptureInput(capture=body["capture"], text=body["text"])
            else:
                inputs = await self._photo_input(request)
            result = await self._call(request, self._api.drive_capture, inputs)
            if isinstance(result, Done):
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
                    self._store.finish_drive_request(
                        request,
                        outcome.state,
                        {
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
                    )
                    self._queue_wake.notify()
                elif outcome.state == "exception":
                    text = strings.DRIVE_ASK_LABEL
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

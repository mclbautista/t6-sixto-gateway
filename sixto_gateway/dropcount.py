"""Daily report of the messages the recording channel's local check dropped."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from sixto_gateway.config import Config
from sixto_gateway.notifier import Notifier
from sixto_gateway.store import Store
from sixto_gateway.store.db import SECONDS_PER_DAY

log = logging.getLogger(__name__)
CHECK_EVERY_SECONDS = 60.0


class DropReporter:
    def __init__(
        self,
        store: Store,
        config: Config,
        queue_wake: Notifier,
        clock: Callable[[], float] = time.time,
    ):
        self._store, self._config, self._queue_wake, self._clock = store, config, queue_wake, clock

    def send_due(self) -> None:
        channel = self._config.recording_channel
        if channel.debug_chat_id is None:
            return
        due = self._store.report_drive_drops(
            self._clock(), SECONDS_PER_DAY, channel.debug_chat_id, channel.debug_topic_id
        )
        if due is not None:
            self._queue_wake.notify()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                self.send_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("local-check drop report failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=CHECK_EVERY_SECONDS)
            except TimeoutError:
                pass

"""getUpdates loop: save before advance, then route (spec sections 5 and 6)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from sixto_gateway.config import Config
from sixto_gateway.liveness import Liveness
from sixto_gateway.notifier import Notifier
from sixto_gateway.store import Store
from sixto_gateway.telegram.client import TelegramClient
from sixto_gateway.telegram.normalize import IncomingUpdate

log = logging.getLogger(__name__)
MAX_BACKOFF_SECONDS = 30.0


class Poller:
    def __init__(
        self,
        store: Store,
        client: TelegramClient,
        config: Config,
        notifier: Notifier,
        clock: Callable[[], float] = time.time,
        liveness: Liveness | None = None,
    ):
        self._store = store
        self._client = client
        self._config = config
        self._notifier = notifier
        self._clock = clock
        self.liveness = liveness or Liveness(clock)

    def route(self, update: IncomingUpdate) -> str | None:
        """The owning module, or None (stored, not delivered).

        An edit follows its original message's route when the original is known.
        """
        if update.edited:
            known, original = self._store.original_route(update.chat_id, update.message_id)
            if known:
                return original
        for route in self._config.routes:
            if route.chat == update.chat_id and route.topic in (None, update.topic_id):
                return route.module
        return None

    async def poll_once(self) -> int:
        """One ``getUpdates`` call; each update is committed (with the cursor) before the next.

        Returns how many updates the call returned.
        """
        telegram = self._config.telegram
        updates = await self._client.get_updates(
            self._store.next_offset(), telegram.poll_timeout, telegram.allowed_updates
        )
        self.liveness.record_poll()
        delivered = False
        for update in updates:
            module = self.route(update)
            seq = self._store.save_update(update, module, received_at=self._clock())
            log.info(
                "update %s chat=%s topic=%s module=%s",
                update.update_id,
                update.chat_id,
                update.topic_id,
                module or "-",
            )
            delivered = delivered or seq is not None
        if delivered:
            self._notifier.notify()
        return len(updates)

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                await self.poll_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("getUpdates cycle failed; retrying in %.0fs", backoff)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

"""getUpdates loop: save before advance, then route (spec sections 5 and 6)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from sixto_gateway.commands import decide_builtin, decide_command, is_builtin
from sixto_gateway.config import Config
from sixto_gateway.decision import Decision
from sixto_gateway.liveness import Liveness
from sixto_gateway.notifier import Notifier
from sixto_gateway.onboarding import decide_start
from sixto_gateway.store import Store
from sixto_gateway.taps import decide_tap
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
        queue_wake: Notifier | None = None,
        liveness: Liveness | None = None,
        onboarding_wake: Notifier | None = None,
        push_wake: Notifier | None = None,
    ):
        self._store = store
        self._client = client
        self._config = config
        self._notifier = notifier
        self._clock = clock
        self._queue_wake = queue_wake or Notifier()  # poked when a reply is queued
        self._onboarding_wake = onboarding_wake or Notifier()  # poked when a member is saved
        self._push_wake = push_wake or Notifier()  # poked when a tap is saved for a push module
        self.liveness = liveness or Liveness(clock)

    def decide(self, update: IncomingUpdate) -> Decision:
        """``/start`` is onboarding. Another command in a private chat is checked against the
        allow-list; the rest is routed."""
        if update.callback_id is not None:
            return decide_tap(self._config, update)
        if update.command == "start":
            return decide_start(self._store, update)
        if update.command is not None and is_builtin(self._config, update):
            return decide_builtin(self._config, update)
        if update.command is not None and update.chat_type == "private":
            return decide_command(self._config, update)
        return Decision(self.route(update))

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
        delivered = queued = onboarding = pushed = False
        for update in updates:
            decision = self.decide(update)
            module = decision.module
            seq = self._store.save_update(update, decision, received_at=self._clock())
            queued = queued or decision.reply is not None
            onboarding = onboarding or decision.member is not None or decision.tap is not None
            log.info(
                "update %s chat=%s topic=%s module=%s",
                update.update_id,
                update.chat_id,
                update.topic_id,
                module or "-",
            )
            delivered = delivered or seq is not None
            pushed = pushed or (seq is not None and decision.push)
            if decision.answer_tap is not None:  # after the commit: a lost answer loses no tap
                await self._answer_tap(update, decision.answer_tap)
        if delivered:
            self._notifier.notify()
        if queued:
            self._queue_wake.notify()
        if onboarding:
            self._onboarding_wake.notify()
        if pushed:
            self._push_wake.notify()
        return len(updates)

    async def _answer_tap(self, update: IncomingUpdate, text: str) -> None:
        """Stop the tapper's spinner. Best effort: an old query id may have expired."""
        assert update.callback_id is not None
        try:
            await self._client.answer_callback_query(update.callback_id, text or None, bool(text))
        except Exception as exc:  # noqa: BLE001
            log.warning("answering tap %s failed: %s", update.callback_id, exc)

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

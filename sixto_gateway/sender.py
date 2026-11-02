"""Send queue drain: one task sends ``outbound`` rows (spec section 9).

Rows go out in order of ``next_attempt_at``, then id, paced under Telegram's limits. A network
error or 5xx is retried after 30 s, 2 min, 5 min, 10 min, then the row is ``failed``. A 429
waits ``max(schedule step, retry_after)`` and also pauses all sending for ``retry_after``,
because Telegram limits the bot, not one chat. A request that can never succeed (chat not
found, bad HTML, bot blocked) fails at once.

At-least-once: a row stays ``queued`` until Telegram has accepted it and the ``sent`` mark is
committed, so a crash in between sends it again on restart. That rare duplicate is accepted.
Retry state (``attempts``, ``next_attempt_at``) is in SQLite, so it survives a restart too.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from sixto_gateway.notifier import Notifier
from sixto_gateway.pacing import Pacer
from sixto_gateway.store import Store
from sixto_gateway.telegram.client import TelegramClient
from sixto_gateway.telegram.errors import PermanentSendError, RetryAfterError

log = logging.getLogger(__name__)
IDLE_WAKE_SECONDS = 5.0  # safety net if a wake-up is ever missed
PARSE_MODES = {"html": "HTML", "plain": None}
RETRY_SCHEDULE_SECONDS = (30.0, 120.0, 300.0, 600.0)  # the watchdog's schedule


class Sender:
    def __init__(
        self,
        store: Store,
        client: TelegramClient,
        queue_wake: Notifier,
        clock: Callable[[], float] = time.time,
    ):
        self._store = store
        self._client = client
        self._wake = queue_wake
        self._clock = clock
        self._pacer = Pacer()
        self._paused_until = 0.0
        self.next_wake_in: float | None = None
        """After ``send_next`` returns False: seconds until it could act (None: queue empty)."""

    async def send_next(self) -> bool:
        """Attempt the next row that is due and allowed by pacing.

        Returns False when nothing can be sent right now; ``next_wake_in`` then says when to
        look again.
        """
        now = self._clock()
        row = self._pick(now)
        if row is None:
            return False
        telegram_id: int | None
        ref, module, body = row["id"], row["module"], row["body"]
        self._pacer.record(body["chat"], now)
        self._store.audit("send_attempt", module, f"ref={ref} kind={row['kind']}")
        try:
            if row["kind"] == "react":
                await self._client.set_reaction(body["chat"], body["message_id"], body["emoji"])
                telegram_id = None
            else:
                telegram_id = await self._client.send_message(
                    body["chat"], body.get("topic"), body["text"], PARSE_MODES[body["format"]]
                )
        except Exception as exc:  # noqa: BLE001 - any failure is recorded, never raised
            self._handle_failure(row, exc)
        else:
            self._store.mark_outbound_sent(ref, telegram_id)
            self._store.audit("send_ok", module, f"ref={ref} telegram_message_id={telegram_id}")
        return True

    def _pick(self, now: float) -> dict | None:
        waits: list[float] = []
        if now < self._paused_until:
            waits.append(self._paused_until - now)
        else:
            for row in self._store.due_outbound(now):
                wait = self._pacer.wait(row["body"]["chat"], now)
                if wait <= 0:
                    return row
                waits.append(wait)
        later = self._store.next_outbound_at(now)
        if later is not None:
            waits.append(later - now)
        self.next_wake_in = max(min(waits), 0.0) if waits else None
        return None

    def _handle_failure(self, row: dict, exc: Exception) -> None:
        ref, module = row["id"], row["module"]
        error = f"{type(exc).__name__}: {exc}"[:300]
        step = row["attempts"]  # failed attempts so far, before this one
        if isinstance(exc, PermanentSendError) or step >= len(RETRY_SCHEDULE_SECONDS):
            self._store.mark_outbound_failed(ref, error)
            self._store.audit("send_failed", module, f"ref={ref} attempts={step + 1} {error}")
            log.warning(
                "send %s for %s failed after %d attempt(s): %s", ref, module, step + 1, error
            )
            return
        delay = RETRY_SCHEDULE_SECONDS[step]
        if isinstance(exc, RetryAfterError):
            delay = max(delay, exc.retry_after)
            self._paused_until = max(self._paused_until, self._clock() + exc.retry_after)
        self._store.schedule_outbound_retry(ref, self._clock() + delay, error)
        self._store.audit("send_retry", module, f"ref={ref} in={delay:.0f}s {error}")
        log.info("send %s for %s will retry in %.0f s: %s", ref, module, delay, error)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            wake = self._wake.current()  # before reading, so a commit in between is not missed
            try:
                if await self.send_next():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("sender cycle failed")
            timeout = IDLE_WAKE_SECONDS
            if self.next_wake_in is not None:
                timeout = min(timeout, self.next_wake_in)
            try:
                await asyncio.wait_for(wake.wait(), timeout=max(timeout, 0.01))
            except TimeoutError:
                pass

"""Send queue drain: one task sends ``outbound`` rows (spec section 9).

Rows go out in order of ``next_attempt_at``, then id, paced under Telegram's limits. A network
error or 5xx is retried after 30 s, 2 min, 5 min, 10 min, then the row is ``failed``. A 429
waits ``max(schedule step, retry_after)`` and also pauses all sending for ``retry_after``,
because Telegram limits the bot, not one chat. A request that can never succeed (chat not
found, bad HTML, bot blocked) fails at once.

Pacing windows are rebuilt from stored attempt times at boot, and the 429 pause is stored too,
so a restart can neither burst nor send early.

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
from sixto_gateway.pacing import LONGEST_WINDOW_SECONDS, Pacer
from sixto_gateway.store import Store
from sixto_gateway.telegram.client import TelegramClient
from sixto_gateway.telegram.errors import (
    BlockedByUserError,
    MessageNotModifiedError,
    PermanentSendError,
    RetryAfterError,
)

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
        scrub: Callable[[str], str] = lambda text: text,
    ):
        self._scrub = scrub  # error text is stored and served back, so secrets must not be in it
        self._store = store
        self._client = client
        self._wake = queue_wake
        self._clock = clock
        self._pacer = Pacer()
        since = clock() - LONGEST_WINDOW_SECONDS
        for at, chat in store.send_attempts_since(since):  # windows survive a restart
            self._pacer.record(chat, at)
        self._paused_until = store.paused_until()  # a 429 pause outlives the process
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
        edit_target: int | None = None
        if row["kind"] == "edit":
            original = self._store.outbound_row(body["edit_of"])
            if original is None or original["state"] != "sent":  # failed, or purged
                error = f"message_ref {body['edit_of']} was not sent, so it cannot be edited"
                self._store.mark_outbound_failed(ref, error)
                self._store.audit("send_failed", module, f"ref={ref} {error}")
                return True
            edit_target = original["telegram_message_id"]
        # stored before the request: Telegram may accept it even if this process never hears
        self._store.record_send_attempt(body["chat"], now, LONGEST_WINDOW_SECONDS)
        self._pacer.record(body["chat"], now)
        self._store.audit("send_attempt", module, f"ref={ref} kind={row['kind']}")
        try:
            if row["kind"] == "react":
                await self._client.set_reaction(body["chat"], body["message_id"], body["emoji"])
                telegram_id = None
            elif row["kind"] == "forward":
                telegram_id = await self._client.forward_message(
                    body["chat"], body.get("topic"), body["from_chat"], body["message_id"]
                )
            elif edit_target is not None:
                try:
                    await self._client.edit_message(
                        body["chat"],
                        edit_target,
                        body["text"],
                        PARSE_MODES[body["format"]],
                        body.get("keyboard"),
                    )
                except MessageNotModifiedError:  # it already shows exactly this: done
                    pass
                telegram_id = edit_target
            else:
                telegram_id = await self._client.send_message(
                    body["chat"],
                    body.get("topic"),
                    body["text"],
                    PARSE_MODES[body["format"]],
                    body.get("keyboard"),
                )
        except Exception as exc:  # noqa: BLE001 - any failure is recorded, never raised
            self._handle_failure(row, exc)
        else:
            self._store.mark_outbound_sent(ref, telegram_id)
            self._store.audit("send_ok", module, f"ref={ref} telegram_message_id={telegram_id}")
            log.info("send %s for %s ok kind=%s", ref, module, row["kind"])
        return True

    def _pick(self, now: float) -> dict | None:
        waits: list[float] = []
        if now < self._paused_until:
            waits.append(self._paused_until - now)
        else:
            for row in self._store.due_outbound(now):
                if row["kind"] == "edit" and self._waits_for_original(row):
                    continue  # the original's own row decides when the queue looks again
                wait = self._pacer.wait(row["body"]["chat"], now)
                if wait <= 0:
                    return row
                waits.append(wait)
        later = self._store.next_outbound_at(now)
        if later is not None:
            waits.append(later - now)
        self.next_wake_in = max(min(waits), 0.0) if waits else None
        return None

    def _waits_for_original(self, row: dict) -> bool:
        """An edit whose message is still queued waits for that message to be sent."""
        original = self._store.outbound_row(row["body"]["edit_of"])
        return original is not None and original["state"] == "queued"

    def _handle_failure(self, row: dict, exc: Exception) -> None:
        ref, module = row["id"], row["module"]
        error = self._scrub(f"{type(exc).__name__}: {exc}")[:300]
        step = row["attempts"]  # failed attempts so far, before this one
        if isinstance(exc, RetryAfterError):  # Telegram limits the bot: even a last try pauses all
            self._paused_until = max(self._paused_until, self._clock() + exc.retry_after)
            self._store.extend_pause(self._paused_until)
        if isinstance(exc, PermanentSendError) or step >= len(RETRY_SCHEDULE_SECONDS):
            self._store.mark_outbound_failed(ref, error)
            self._store.audit("send_failed", module, f"ref={ref} attempts={step + 1} {error}")
            if isinstance(exc, BlockedByUserError):
                self._store.block_member(row["body"]["chat"])
            log.warning(
                "send %s for %s failed after %d attempt(s): %s", ref, module, step + 1, error
            )
            return
        delay = RETRY_SCHEDULE_SECONDS[step]
        if isinstance(exc, RetryAfterError):
            delay = max(delay, exc.retry_after)
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

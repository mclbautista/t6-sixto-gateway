"""Send queue drain: one task sends ``outbound`` rows in order (spec section 9, basic form).

Pacing, retries and 429 handling arrive in the next slice; here a failed send is recorded as
``failed`` with the error and the audit log gets one row per attempt and one per outcome.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from sixto_gateway.notifier import Notifier
from sixto_gateway.store import Store
from sixto_gateway.telegram.client import TelegramClient

log = logging.getLogger(__name__)
IDLE_WAKE_SECONDS = 5.0  # safety net if a wake-up is ever missed
PARSE_MODES = {"html": "HTML", "plain": None}


class Sender:
    def __init__(
        self,
        store: Store,
        client: TelegramClient,
        queue_wake: Notifier,
        scrub: Callable[[str], str] = lambda text: text,
    ):
        self._scrub = scrub  # error text is stored and served back, so secrets must not be in it
        self._store = store
        self._client = client
        self._wake = queue_wake

    async def send_next(self) -> bool:
        """Send the next queued row. Returns False when the queue is empty."""
        row = self._store.next_outbound()
        if row is None:
            return False
        telegram_id: int | None
        ref, module, body = row["id"], row["module"], row["body"]
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
            error = self._scrub(f"{type(exc).__name__}: {exc}")[:300]
            self._store.mark_outbound_failed(ref, error)
            self._store.audit("send_failed", module, f"ref={ref} {error}")
            log.warning("send %s for %s failed: %s", ref, module, error)
        else:
            self._store.mark_outbound_sent(ref, telegram_id)
            self._store.audit("send_ok", module, f"ref={ref} telegram_message_id={telegram_id}")
        return True

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
            try:
                await asyncio.wait_for(wake.wait(), timeout=IDLE_WAKE_SECONDS)
            except TimeoutError:
                pass

"""Push delivery of taps (ticket #50): post each tap to its module's URL until it is accepted.

A module in ``delivery: push`` mode gets every tap on its buttons as an HTTP POST to its
``push_url`` with ``Authorization: Bearer <its secret>``. The body is the tap item (the same
shape ``GET /updates`` serves) plus ``idempotency_key``, which is also sent as an
``Idempotency-Key`` header and is the same on every retry, so the receiver can drop a repeat.

Any 2xx answer accepts the tap. Anything else (an error status, a timeout, no connection) is
retried with a growing wait that stops growing at ten minutes; the tap is never given up on.
Retry state is in SQLite, so it survives a restart. The VPS never calls into the LAN: this
worker is the gateway calling out.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

import httpx

from sixto_gateway.config import Config
from sixto_gateway.notifier import Notifier
from sixto_gateway.store import Store

log = logging.getLogger(__name__)
IDLE_WAKE_SECONDS = 5.0  # safety net if a wake-up is ever missed
RETRY_SCHEDULE_SECONDS = (5.0, 30.0, 120.0, 600.0)  # the last step repeats
REQUEST_TIMEOUT_SECONDS = 10.0


def idempotency_key(update_id: int) -> str:
    return f"tap-{update_id}"


class PushWorker:
    def __init__(
        self,
        store: Store,
        config: Config,
        wake: Notifier,
        clock: Callable[[], float] = time.time,
        client: httpx.AsyncClient | None = None,
        scrub: Callable[[str], str] = lambda text: text,
    ):
        self._store = store
        self._config = config
        self._wake = wake
        self._clock = clock
        self._client = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
        self._scrub = scrub

    async def push_due(self) -> int:
        """Attempt every push that is due now. Returns how many were attempted."""
        due = self._store.due_pushes(self._clock())
        for row in due:
            await self._push(row)
        return len(due)

    async def _push(self, row: dict) -> None:
        seq, module, item = row["seq"], row["module"], row["item"]
        key = idempotency_key(item["update_id"])
        module_config = self._config.modules[module]
        assert module_config.push_url is not None  # validated at boot
        try:
            response = await self._client.post(
                module_config.push_url,
                json={"idempotency_key": key, **item},
                headers={
                    "Authorization": f"Bearer {self._config.secrets.secret_for(module)}",
                    "Idempotency-Key": key,
                },
            )
            if not response.is_success:
                raise PushRejected(f"{module} answered {response.status_code}")
        except Exception as exc:  # noqa: BLE001 - every failure is retried, never raised
            error = self._scrub(f"{type(exc).__name__}: {exc}")[:300]
            step = min(row["attempts"], len(RETRY_SCHEDULE_SECONDS) - 1)
            delay = RETRY_SCHEDULE_SECONDS[step]
            self._store.schedule_push_retry(seq, self._clock() + delay, error)
            self._store.audit(
                "tap_push_retry", module, f"seq={seq} attempt={row['attempts'] + 1} {error}"
            )
            log.warning("tap push %s to %s failed, retry in %.0f s: %s", seq, module, delay, error)
        else:
            self._store.mark_push_done(seq)
            self._store.audit("tap_push_ok", module, f"seq={seq} key={key}")
            log.info("tap push %s to %s accepted", seq, module)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            wake = self._wake.current()  # before reading, so a commit in between is not missed
            try:
                if await self.push_due():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("tap push cycle failed")
            timeout = IDLE_WAKE_SECONDS
            later = self._store.next_push_at()
            if later is not None:
                timeout = min(timeout, max(later - self._clock(), 0.01))
            try:
                await asyncio.wait_for(wake.wait(), timeout=timeout)
            except TimeoutError:
                pass


class PushRejected(Exception):
    """The module's URL answered with a non-2xx status."""

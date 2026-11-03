"""Poll liveness: feeds ``/healthz`` and the deadman (spec sections 7 and 10)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

HEALTHY_POLL_SECONDS = 5 * 60  # /healthz is 503 when the last completed poll is older
DEADMAN_SECONDS = 10 * 60  # no completed poll for this long: the process exits
DEADMAN_CHECK_SECONDS = 15.0


class Liveness:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self.started_at = clock()
        self.last_poll_at: float | None = None

    def now(self) -> float:
        return self._clock()

    def record_poll(self) -> None:
        """A ``getUpdates`` call completed (with or without updates)."""
        self.last_poll_at = self._clock()

    def poll_is_fresh(self) -> bool:
        return (
            self.last_poll_at is not None
            and self._clock() - self.last_poll_at <= HEALTHY_POLL_SECONDS
        )

    def stalled_for(self) -> float:
        """Seconds since the last completed poll (since boot if none has completed yet)."""
        return self._clock() - (self.last_poll_at or self.started_at)

    def deadman_expired(self) -> bool:
        return self.stalled_for() >= DEADMAN_SECONDS


async def watch_deadman(
    liveness: Liveness, stop: asyncio.Event, interval: float = DEADMAN_CHECK_SECONDS
) -> bool:
    """Returns True when polling has stalled too long (the caller then exits non-zero)."""
    while not stop.is_set():
        if liveness.deadman_expired():
            return True
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass
    return False

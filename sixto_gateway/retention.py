"""Daily retention job (spec section 5)."""

from __future__ import annotations

import asyncio
import logging

from sixto_gateway.store import Store

log = logging.getLogger(__name__)
RUN_EVERY_SECONDS = 24 * 60 * 60


def run_retention_once(store: Store, retention_days: int) -> dict[str, int]:
    counts = store.purge(retention_days)
    store.audit("retention", detail=" ".join(f"{k}={v}" for k, v in counts.items()))
    log.info("retention: deleted %s", counts)
    return counts


async def run_retention(
    store: Store,
    retention_days: int,
    stop: asyncio.Event,
    interval: float = RUN_EVERY_SECONDS,
) -> None:
    """Runs once at boot, then every ``interval`` seconds."""
    while not stop.is_set():
        try:
            run_retention_once(store, retention_days)
        except Exception:
            log.exception("retention run failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass

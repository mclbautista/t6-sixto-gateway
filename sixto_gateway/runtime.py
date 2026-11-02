"""Starts the background tasks that run beside the HTTP server (spec sections 6, 9 and 10)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from sixto_gateway.config import Config
from sixto_gateway.liveness import Liveness, watch_deadman
from sixto_gateway.notifier import Notifier
from sixto_gateway.poller import Poller
from sixto_gateway.retention import run_retention
from sixto_gateway.sender import Sender
from sixto_gateway.store import Store
from sixto_gateway.telegram.client import TelegramClient

log = logging.getLogger(__name__)


def start_workers(
    config: Config,
    store: Store,
    client: TelegramClient,
    notifier: Notifier,
    queue_wake: Notifier,
    liveness: Liveness,
    stop: asyncio.Event,
    scrub: Callable[[str], str] = lambda text: text,
) -> dict[str, asyncio.Task]:
    """Retention always runs. Polling, sending and the deadman run only when polling is on.

    With ``telegram.polling: false`` nothing here calls Telegram: ``/notify`` rows are saved and
    stay ``queued``.
    """
    tasks: dict[str, asyncio.Task] = {
        "retention": asyncio.create_task(run_retention(store, config.retention_days, stop))
    }
    if config.telegram.polling:
        poller = Poller(store, client, config, notifier, liveness=liveness)
        tasks["poller"] = asyncio.create_task(poller.run(stop))
        tasks["sender"] = asyncio.create_task(Sender(store, client, queue_wake, scrub).run(stop))
        tasks["deadman"] = asyncio.create_task(watch_deadman(liveness, stop))
    else:
        log.warning("telegram.polling is false: serving the API only (no polling, no sending)")
    return tasks

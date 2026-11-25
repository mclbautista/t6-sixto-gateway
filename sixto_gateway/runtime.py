"""Starts the background tasks that run beside the HTTP server (spec sections 6, 9 and 10)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from t6.client import Client

from sixto_gateway.config import Config
from sixto_gateway.drives import DriveWorker
from sixto_gateway.dropcount import DropReporter
from sixto_gateway.liveness import Liveness, watch_deadman
from sixto_gateway.notifier import Notifier
from sixto_gateway.onboarding import OnboardingWorker
from sixto_gateway.people import ContactsDirectory
from sixto_gateway.poller import Poller
from sixto_gateway.project import ProjectCommands, ProjectWorker
from sixto_gateway.push import PushWorker
from sixto_gateway.retention import run_retention
from sixto_gateway.sender import Sender
from sixto_gateway.store import Store
from sixto_gateway.summary import SummaryWorker
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
    *,
    contacts: ContactsDirectory,
    projects: ProjectCommands,
    onboarding_wake: Notifier,
    clock: Callable[[], float] = time.time,
) -> dict[str, asyncio.Task]:
    """Retention always runs. Polling, sending and the deadman run only when polling is on.

    With ``telegram.polling: false`` nothing here calls Telegram: ``/notify`` rows are saved and
    stay ``queued``.
    """
    tasks: dict[str, asyncio.Task] = {
        "retention": asyncio.create_task(run_retention(store, config.retention_days, stop))
    }
    if config.telegram.polling:
        push_wake = Notifier()
        project_wake = Notifier()
        poller = Poller(
            store,
            client,
            config,
            notifier,
            queue_wake=queue_wake,
            liveness=liveness,
            onboarding_wake=onboarding_wake,
            push_wake=push_wake,
            project_wake=project_wake,
        )
        tasks["poller"] = asyncio.create_task(poller.run(stop))
        if config.recording_channel.chat_id is not None:
            tasks["drives"] = asyncio.create_task(
                DriveWorker(
                    store,
                    client,
                    Client(config.t6_api_url, config.secrets.t6_api_secret),
                    queue_wake,
                    clock,
                ).run(stop)
            )
            if config.recording_channel.debug_chat_id is not None:
                tasks["drive_drops"] = asyncio.create_task(
                    DropReporter(store, config, queue_wake, clock).run(stop)
                )
        worker = OnboardingWorker(
            store, contacts, config, onboarding_wake, queue_wake, client=client
        )
        tasks["onboarding"] = asyncio.create_task(worker.run(stop))
        tasks["projects"] = asyncio.create_task(
            ProjectWorker(store, projects, project_wake, queue_wake, client).run(stop)
        )
        tasks["summary"] = asyncio.create_task(
            SummaryWorker(store, config, queue_wake, clock).run(stop)
        )
        tasks["sender"] = asyncio.create_task(
            Sender(store, client, queue_wake, scrub=scrub).run(stop)
        )
        if any(m.delivery == "push" for m in config.modules.values()):
            tasks["push"] = asyncio.create_task(
                PushWorker(store, config, push_wake, clock, scrub=scrub).run(stop)
            )
        tasks["deadman"] = asyncio.create_task(watch_deadman(liveness, stop))
    else:
        log.warning("telegram.polling is false: serving the API only (no polling, no sending)")
    return tasks

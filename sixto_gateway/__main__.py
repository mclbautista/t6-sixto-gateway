"""Entry point: runs the poller and the HTTP API in one process.

``python -m sixto_gateway [--config PATH]`` serves; ``set-offset N`` sets (or rewinds) the Telegram
cursor, and ``show-offset`` prints it with any routed updates that are missing a delivery.
``backup DEST`` writes a consistent copy of the database to a new file while the gateway runs.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Awaitable, Coroutine
from typing import TextIO

import uvicorn
from aiogram import Bot
from t6.privacy import Scrubber, ScrubbingFormatter

from sixto_gateway.api import create_app
from sixto_gateway.commands import register_menus
from sixto_gateway.config import Config, ConfigError, load_config
from sixto_gateway.contacts import ContactsDirectory, CoreContacts
from sixto_gateway.liveness import Liveness
from sixto_gateway.notifier import Notifier
from sixto_gateway.runtime import start_workers
from sixto_gateway.store import Store
from sixto_gateway.store.db import StoreCorrupt, backup
from sixto_gateway.telegram.client import AiogramTelegramClient, TelegramClient

log = logging.getLogger("sixto_gateway")
EXIT_DEADMAN = 1
MENU_SETUP_SECONDS = 120.0  # all boot-time menu calls together
SHUTDOWN_GRACE_SECONDS = 5.0  # to stop on request, then again after cancelling


def scrubber_for(config: Config) -> Scrubber:
    return Scrubber(config.secrets.redaction_values())


def configure_logging(
    scrubber: Scrubber, stream: TextIO | None = None, level: int = logging.INFO
) -> logging.Handler:
    """Send every log record through the scrubber. Replaces any earlier root handlers."""
    handler = logging.StreamHandler(stream)
    handler.setFormatter(ScrubbingFormatter(scrubber))
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)
    return handler


async def serve(
    config: Config,
    client: TelegramClient | None = None,
    liveness: Liveness | None = None,
    contacts: ContactsDirectory | None = None,
) -> int:
    """Run until any task ends; returns the process exit code.

    ``client``, ``liveness`` and ``contacts`` are for tests; by default a real Bot, the wall
    clock and the real Notion Contacts are used.
    """
    store = Store(config.database_path)
    store.audit("config_loaded", detail=f"modules={sorted(config.modules)}")
    notifier = Notifier()
    queue_wake = Notifier()
    liveness = liveness or Liveness()
    app = create_app(config, store, notifier, queue_wake, liveness)
    server = uvicorn.Server(
        uvicorn.Config(app, host=config.listen.host, port=config.listen.port, log_level="warning")
    )
    stop = asyncio.Event()
    bot = Bot(token=config.secrets.bot_token) if client is None else None
    client = client or AiogramTelegramClient(bot)
    core_contacts = None
    if contacts is None:
        core_contacts = CoreContacts(
            config.secrets.notion_api_key,
            config.notion.contacts_data_source_id,
            config.notion.version,
        )
        contacts = core_contacts
    if config.telegram.polling:
        await _register_menus_bounded(store, client, config)
    workers = start_workers(
        config,
        store,
        client,
        notifier,
        queue_wake,
        liveness,
        stop,
        scrubber_for(config).scrub,
        contacts=contacts,
        onboarding_wake=Notifier(),
    )
    tasks = {"server": asyncio.create_task(server.serve()), **workers}
    exit_code = 0
    try:
        # If any task ends, the process ends; the container restart policy takes over.
        await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.set()
        server.should_exit = True
        stuck = await _shut_down(tasks)
        if bot is not None:
            await _bounded(bot.session.close(), SHUTDOWN_GRACE_SECONDS)
        if core_contacts is not None:
            await _bounded(core_contacts.close(), SHUTDOWN_GRACE_SECONDS)
    deadman = tasks.get("deadman")
    if deadman and deadman.done() and not deadman.cancelled() and deadman.exception() is None:
        if deadman.result():
            log.critical("no getUpdates call completed for 10 minutes: exiting for a restart")
            store.audit("deadman")
            exit_code = EXIT_DEADMAN
    for name, task in tasks.items():
        if task.done() and not task.cancelled() and task.exception() is not None:
            log.error("task %s crashed: %r", name, task.exception())
            exit_code = exit_code or 1
    if stuck:
        log.critical("tasks %s did not stop when cancelled", sorted(stuck))
        exit_code = exit_code or 1
    store.close()
    return exit_code


def run_serving(main_task: Coroutine[object, object, int]) -> int:
    """Run ``serve`` and return its exit code; a non-zero code leaves at once.

    ``asyncio.run`` would wait for leftover tasks and for executor threads (aiohttp resolves
    names in one), and a stuck one would keep a deadman exit from ever happening. A non-zero
    exit is always a restart, so nothing is lost by skipping that cleanup: the store is already
    closed.
    """
    loop = asyncio.new_event_loop()
    code = loop.run_until_complete(main_task)
    if code != 0:
        logging.shutdown()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    loop.close()
    return code


async def _register_menus_bounded(store: Store, client: TelegramClient, config: Config) -> None:
    """Menus are best effort: a Telegram call that hangs must not hold up polling and the deadman.

    A cancelled run is safe, because a person is recorded before their menu is set (see
    ``register_menus``), so the next boot finishes the job.
    """
    task = asyncio.create_task(register_menus(store, client, config))
    done, _ = await asyncio.wait({task}, timeout=MENU_SETUP_SECONDS)
    if task in done:
        task.result()  # register_menus handles Telegram errors itself; anything else is a bug
    else:
        task.cancel()  # not awaited: a call that ignores cancelling must not hold up boot
        log.warning("menu setup timed out after %s s: carrying on without it", MENU_SETUP_SECONDS)
        store.audit("menu_failed", detail=f"setup timed out after {MENU_SETUP_SECONDS} s")


async def _shut_down(tasks: dict[str, asyncio.Task]) -> set[str]:
    """Give tasks a moment to stop on ``stop``, then cancel the rest (a hung await cannot see
    ``stop``). Returns the names of tasks that still have not finished."""
    pending = {t for t in tasks.values() if not t.done()}
    if pending:
        _, pending = await asyncio.wait(pending, timeout=SHUTDOWN_GRACE_SECONDS)
    for task in pending:
        task.cancel()
    if pending:
        _, pending = await asyncio.wait(pending, timeout=SHUTDOWN_GRACE_SECONDS)
    return {name for name, task in tasks.items() if task in pending}


async def _bounded(awaitable: Awaitable[object], seconds: float) -> None:
    try:
        await asyncio.wait_for(awaitable, seconds)
    except Exception:  # noqa: BLE001 - shutting down anyway
        log.warning("shutdown step did not finish cleanly", exc_info=True)


def set_offset(config: Config, last_update_id: int) -> int:
    store = Store(config.database_path)
    try:
        store.set_offset(last_update_id)
    finally:
        store.close()
    print(f"poll cursor set: the next getUpdates will use offset {last_update_id + 1}")
    return 0


def show_offset(config: Config) -> int:
    store = Store(config.database_path)
    try:
        last = store.last_update_id()
        missing = store.missing_deliveries()
    finally:
        store.close()
    if last is None:
        print("poll cursor not set: the next getUpdates will use offset 0")
    else:
        print(f"poll cursor: last_update_id={last}, next getUpdates offset {last + 1}")
    if missing:
        print(f"routed updates missing a delivery: {', '.join(map(str, missing))}")
    else:
        print("routed updates missing a delivery: none")
    return 0


def run_backup(config: Config, dest: str) -> int:
    try:
        result = backup(config.database_path, dest)
    except FileExistsError:
        print(f"backup: {dest} already exists; nothing written", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"backup: {exc}", file=sys.stderr)
        return 2
    if result != "ok":
        print(f"backup: {dest} was written but failed integrity_check: {result}", file=sys.stderr)
        return 1
    print(f"backup written to {dest} (integrity_check ok)")
    return 0


def _non_negative(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sixto_gateway")
    parser.add_argument("--config", default="/app/config.yaml")
    commands = parser.add_subparsers(dest="command")
    offset = commands.add_parser(
        "set-offset",
        help="set the Telegram cursor to N; the next poll uses N + 1 (cutover step 3)",
    )
    offset.add_argument("n", type=_non_negative, metavar="N")
    commands.add_parser(
        "show-offset",
        help="print the Telegram cursor and any routed updates missing a delivery",
    )
    copy = commands.add_parser(
        "backup",
        help="write a consistent copy of the database to the new file DEST (safe while serving)",
    )
    copy.add_argument("dest", metavar="DEST")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config, os.environ)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    configure_logging(scrubber_for(config))
    if args.command == "backup":
        return run_backup(config, args.dest)
    try:
        if args.command == "set-offset":
            return set_offset(config, args.n)
        if args.command == "show-offset":
            return show_offset(config)
        return run_serving(serve(config))
    except StoreCorrupt as exc:
        # Still non-zero: the container restarts and repeats this line until someone acts.
        print(f"database: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

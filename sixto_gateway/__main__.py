"""Entry point: runs the poller and the HTTP API in one process.

``python -m sixto_gateway [--config PATH]`` serves; ``set-offset N`` is the one-off cutover command.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Awaitable, Coroutine

import uvicorn
from aiogram import Bot

from sixto_gateway.api import create_app
from sixto_gateway.commands import register_menus
from sixto_gateway.config import Config, ConfigError, load_config
from sixto_gateway.liveness import Liveness
from sixto_gateway.logsafe import Redactor, configure_logging
from sixto_gateway.notifier import Notifier
from sixto_gateway.notion import ContactsDirectory, NotionContacts
from sixto_gateway.runtime import start_workers
from sixto_gateway.store import Store
from sixto_gateway.telegram.client import AiogramTelegramClient, TelegramClient

log = logging.getLogger("sixto_gateway")
EXIT_DEADMAN = 1
MENU_SETUP_SECONDS = 120.0  # all boot-time menu calls together
SHUTDOWN_GRACE_SECONDS = 5.0  # to stop on request, then again after cancelling


def redactor_for(config: Config) -> Redactor:
    return Redactor(config.secrets.redaction_values())


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
    notion = None
    if contacts is None:
        notion = NotionContacts(
            config.secrets.notion_api_key,
            config.notion.contacts_data_source_id,
            config.notion.version,
        )
        contacts = notion
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
        redactor_for(config).scrub,
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
        if notion is not None:
            await _bounded(notion.close(), SHUTDOWN_GRACE_SECONDS)
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
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config, os.environ)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    configure_logging(redactor_for(config))
    if args.command == "set-offset":
        return set_offset(config, args.n)
    return run_serving(serve(config))


if __name__ == "__main__":
    sys.exit(main())

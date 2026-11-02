"""Entry point: runs the poller and the HTTP API in one process.

``python -m sixto_gateway [--config PATH]`` serves; ``set-offset N`` is the one-off cutover command.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import uvicorn
from aiogram import Bot

from sixto_gateway.api import create_app
from sixto_gateway.config import Config, ConfigError, load_config
from sixto_gateway.liveness import Liveness
from sixto_gateway.logsafe import Redactor, configure_logging
from sixto_gateway.notifier import Notifier
from sixto_gateway.runtime import start_workers
from sixto_gateway.store import Store
from sixto_gateway.telegram.client import AiogramTelegramClient

log = logging.getLogger("sixto_gateway")
EXIT_DEADMAN = 1


def redactor_for(config: Config) -> Redactor:
    return Redactor(config.secrets.redaction_values())


async def serve(config: Config) -> int:
    store = Store(config.database_path)
    store.audit("config_loaded", detail=f"modules={sorted(config.modules)}")
    notifier = Notifier()
    queue_wake = Notifier()
    liveness = Liveness()
    app = create_app(config, store, notifier, queue_wake, liveness)
    server = uvicorn.Server(
        uvicorn.Config(app, host=config.listen.host, port=config.listen.port, log_level="warning")
    )
    stop = asyncio.Event()
    bot = Bot(token=config.secrets.bot_token)
    workers = start_workers(
        config,
        store,
        AiogramTelegramClient(bot),
        notifier,
        queue_wake,
        liveness,
        stop,
        redactor_for(config).scrub,
    )
    tasks = {"server": asyncio.create_task(server.serve()), **workers}
    exit_code = 0
    try:
        # If any task ends, the process ends; the container restart policy takes over.
        await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.set()
        server.should_exit = True
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        await bot.session.close()
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
    store.close()
    return exit_code


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
    return asyncio.run(serve(config))


if __name__ == "__main__":
    sys.exit(main())

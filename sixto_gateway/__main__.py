"""Entry point: runs the poller and the HTTP API in one process."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import uvicorn
from aiogram import Bot

from sixto_gateway.api import create_app
from sixto_gateway.config import ConfigError, load_config
from sixto_gateway.notifier import Notifier
from sixto_gateway.poller import Poller
from sixto_gateway.store import Store
from sixto_gateway.telegram.client import AiogramTelegramClient

log = logging.getLogger("sixto_gateway")


async def serve(config_path: str) -> None:
    config = load_config(config_path, os.environ)
    store = Store(config.database_path)
    store.audit("config_loaded", detail=f"modules={sorted(config.modules)}")
    notifier = Notifier()
    app = create_app(config, store, notifier)
    server = uvicorn.Server(
        uvicorn.Config(app, host=config.listen.host, port=config.listen.port, log_level="warning")
    )
    stop = asyncio.Event()
    tasks = [asyncio.create_task(server.serve())]
    bot = Bot(token=config.secrets.bot_token)
    if config.telegram.polling:
        poller = Poller(store, AiogramTelegramClient(bot), config, notifier)
        tasks.append(asyncio.create_task(poller.run(stop)))
    else:
        log.warning("telegram.polling is false: serving the API only")
    try:
        # If either task ends, the process ends; the container restart policy takes over.
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.set()
        server.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.session.close()
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="sixto_gateway")
    parser.add_argument("--config", default="/app/config.yaml")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        asyncio.run(serve(args.config))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

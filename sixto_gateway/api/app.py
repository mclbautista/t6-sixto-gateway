"""Module HTTP API. Every endpoint here requires ``Authorization: Bearer <module secret>``."""

import asyncio
import logging
import time
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request

from sixto_gateway.config import Config
from sixto_gateway.notifier import Notifier
from sixto_gateway.store import Store

log = logging.getLogger(__name__)
MAX_ITEMS = 100


def create_app(config: Config, store: Store, notifier: Notifier) -> FastAPI:
    app = FastAPI(title="Sixto gateway", docs_url=None, redoc_url=None, openapi_url=None)

    async def calling_module(request: Request) -> str:
        header = request.headers.get("authorization", "")
        scheme, _, presented = header.partition(" ")
        module = config.module_for_secret(presented.strip()) if scheme.lower() == "bearer" else None
        if module is None:
            # Never log or store what was presented; only that it failed and from where.
            client = request.client.host if request.client else "?"
            log.warning("auth failure from %s on %s", client, request.url.path)
            store.audit("auth_failure", detail=f"{client} {request.url.path}")
            raise HTTPException(
                401, "invalid or missing module secret", {"WWW-Authenticate": "Bearer"}
            )
        return module

    # async so the SQLite connection is only touched on the event-loop thread
    Module = Annotated[str, Depends(calling_module)]

    @app.get("/updates")
    async def get_updates(
        module: Module,
        after: Annotated[int, Query(ge=0)] = 0,
        timeout: Annotated[float, Query(ge=0, le=50)] = 0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            # Take the wake-up event before reading, so a commit in between is not missed.
            wake = notifier.current()
            items = store.deliveries_after(module, after, MAX_ITEMS)
            remaining = deadline - time.monotonic()
            if items or remaining <= 0:
                return {"items": items}
            try:
                await asyncio.wait_for(wake.wait(), timeout=remaining)
            except TimeoutError:
                pass

    return app

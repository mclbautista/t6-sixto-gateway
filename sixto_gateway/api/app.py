"""Module HTTP API. Every endpoint requires ``Authorization: Bearer <module secret>``,
except ``GET /healthz``."""

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from sixto_gateway.config import Config
from sixto_gateway.liveness import Liveness
from sixto_gateway.notifier import Notifier
from sixto_gateway.store import Store
from sixto_gateway.store.db import SECONDS_PER_DAY

log = logging.getLogger(__name__)
MAX_ITEMS = 100
INT64_MAX = 2**63 - 1
BigInt = Annotated[int, Field(ge=-INT64_MAX, le=INT64_MAX)]


class NotifyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: BigInt
    topic: BigInt | None = None
    text: str = Field(min_length=1, max_length=4096)
    format: Literal["html", "plain"] = "html"
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class ReactBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chat: BigInt
    message_id: BigInt
    emoji: str = Field(min_length=1, max_length=32)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


def create_app(
    config: Config,
    store: Store,
    notifier: Notifier,
    queue_wake: Notifier | None = None,
    liveness: Liveness | None = None,
) -> FastAPI:
    """``queue_wake`` is poked after each save to the send queue (the sender waits on it).

    ``liveness`` is the poller's heartbeat, read by ``/healthz``.
    """
    queue_wake = queue_wake or Notifier()
    liveness = liveness or Liveness()
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

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """No auth. 200 when polling is fresh (if enabled) and the database is writable."""
        polling = config.telegram.polling
        writable = store.is_writable()
        # With polling off (cutover step 1) there is no poll to wait for: only the database counts.
        healthy = writable and (liveness.poll_is_fresh() if polling else True)
        last = liveness.last_poll_at
        body = {
            "status": "ok" if healthy else "unhealthy",
            "polling": polling,
            "database_writable": writable,
            "last_poll_at": datetime.fromtimestamp(last, UTC).isoformat() if last else None,
            "queued": store.queued_count(),
            "failed_last_24h": store.failed_sends_since(liveness.now() - SECONDS_PER_DAY),
        }
        return JSONResponse(body, status_code=200 if healthy else 503)

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

    @app.post("/notify", status_code=202)
    async def post_notify(module: Module, body: NotifyBody) -> dict[str, int]:
        ref = store.enqueue_outbound(
            module,
            "message",
            str(body.to),
            {
                "chat": body.to,
                "topic": body.topic,
                "text": body.text,
                "format": body.format,
            },
            body.idempotency_key,
        )
        queue_wake.notify()
        return {"message_ref": ref}

    @app.post("/react", status_code=202)
    async def post_react(module: Module, body: ReactBody) -> dict[str, int]:
        ref = store.enqueue_outbound(
            module,
            "react",
            str(body.chat),
            {"chat": body.chat, "message_id": body.message_id, "emoji": body.emoji},
            body.idempotency_key,
        )
        queue_wake.notify()
        return {"message_ref": ref}

    @app.get("/notify/{message_ref}")
    async def get_notify(
        module: Module, message_ref: Annotated[int, Path(ge=1, le=INT64_MAX)]
    ) -> dict[str, Any]:
        status = store.outbound_status(message_ref, module)
        if status is None:
            raise HTTPException(404, "unknown message_ref")
        return status

    return app

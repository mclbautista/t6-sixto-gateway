"""Module HTTP API. Every endpoint requires ``Authorization: Bearer <module secret>``,
except ``GET /healthz``."""

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, UrlConstraints, model_validator

from sixto_gateway.config import Config
from sixto_gateway.decision import module_callback_data
from sixto_gateway.liveness import Liveness
from sixto_gateway.notifier import Notifier
from sixto_gateway.store import Store
from sixto_gateway.store.db import SECONDS_PER_DAY

log = logging.getLogger(__name__)
MAX_ITEMS = 100
MAX_TEXT = 4096
MAX_CALLBACK_BYTES = 64
INT64_MAX = 2**63 - 1
BigInt = Annotated[int, Field(ge=-INT64_MAX, le=INT64_MAX)]


HANDLE_PATTERN = r"^@?[A-Za-z0-9_]{1,64}$"
CONTACT_ID_PATTERN = r"^[0-9A-Fa-f-]{32,36}$"


class Fallback(BaseModel):
    """Where a Contact's message goes when they have no approved Telegram member."""

    model_config = ConfigDict(extra="forbid")
    to: BigInt
    topic: BigInt | None = None
    handle: str = Field(pattern=HANDLE_PATTERN)


class CallbackButton(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1)
    payload: str = Field(min_length=1)


class UrlButton(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1)
    url: Annotated[AnyHttpUrl, UrlConstraints(allowed_schemes=["https"])]


Buttons = list[Annotated[list[CallbackButton | UrlButton], Field(min_length=1)]]


class NotifyBody(BaseModel):
    """Either ``to`` (a chat id) or ``contact`` with its ``fallback``; never both, never neither."""

    model_config = ConfigDict(extra="forbid")
    to: BigInt | None = None
    topic: BigInt | None = None
    contact: str | None = Field(default=None, pattern=CONTACT_ID_PATTERN)
    fallback: Fallback | None = None
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    format: Literal["html", "plain"] = "html"
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    buttons: Buttons | None = None

    @model_validator(mode="after")
    def to_or_contact_with_fallback(self) -> Self:
        by_chat = self.to is not None and self.contact is None and self.fallback is None
        by_contact = (
            self.to is None
            and self.topic is None
            and self.contact is not None
            and self.fallback is not None
        )
        if not (by_chat or by_contact):
            raise ValueError("send either 'to', or 'contact' with 'fallback'")
        return self


class EditBody(BaseModel):
    """New text and buttons for a message the caller sent with ``/notify``.

    Omitted, null or empty ``buttons`` removes the message's buttons.
    """

    model_config = ConfigDict(extra="forbid")
    message_ref: Annotated[int, Field(ge=1, le=INT64_MAX)]
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    format: Literal["html", "plain"] = "html"
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    buttons: Buttons | None = None


class Subscription(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chat: BigInt
    topic: BigInt | None


class ReactBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chat: BigInt
    message_id: BigInt
    emoji: str = Field(min_length=1, max_length=32)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class ForwardBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_chat: BigInt
    message_id: BigInt
    to: BigInt
    topic: BigInt | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)


def _keyboard(module: str, rows: Buttons | None) -> list[list[dict[str, str]]] | None:
    """Telegram inline rows for a module's buttons (None: no buttons); 422 if one is too long."""
    if not rows:
        return None
    keyboard = []
    for row in rows:
        buttons = []
        for button in row:
            if isinstance(button, CallbackButton):
                # Escape module delimiters; payload is always opaque and caller-owned.
                data = module_callback_data(module, button.payload)
                if len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
                    raise HTTPException(
                        422,
                        f"callback payload with module prefix exceeds {MAX_CALLBACK_BYTES} bytes",
                    )
                buttons.append({"text": button.text, "callback_data": data})
            else:
                buttons.append({"text": button.text, "url": str(button.url)})
        keyboard.append(buttons)
    return keyboard


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

    @app.get("/subscriptions")
    async def get_subscriptions(module: Module) -> list[dict[str, int | None]]:
        return store.subscriptions(module)

    @app.put("/subscriptions")
    async def put_subscriptions(
        module: Module, body: list[Subscription]
    ) -> list[dict[str, int | None]]:
        store.replace_subscriptions(module, [(item.chat, item.topic) for item in body])
        return store.subscriptions(module)

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
        keyboard = _keyboard(module, body.buttons)
        chat, topic, text = body.to, body.topic, body.text
        if body.contact is not None:
            assert body.fallback is not None
            # Local members table only: no Notion call on the send path.
            user_id = store.approved_user_for_contact(body.contact)
            if user_id is not None:
                chat, topic = user_id, None
            else:
                fallback = body.fallback
                chat, topic = fallback.to, fallback.topic
                text = f"@{fallback.handle.lstrip('@')} {text}"
                if len(text) > MAX_TEXT:  # the mention must not push it past Telegram's limit
                    raise HTTPException(422, f"text with the @handle is over {MAX_TEXT} characters")
        assert chat is not None
        ref = store.enqueue_outbound(
            module,
            "message",
            str(chat),
            {"chat": chat, "topic": topic, "text": text, "format": body.format}
            | ({"keyboard": keyboard} if keyboard is not None else {}),
            body.idempotency_key,
        )
        queue_wake.notify()
        return {"message_ref": ref}

    @app.post("/edit", status_code=202)
    async def post_edit(module: Module, body: EditBody) -> dict[str, int]:
        """Queue new text and buttons for a message this module sent with ``/notify``.

        An edit of a message that is still queued waits for it to be sent; if that send fails,
        the edit fails too.
        """
        keyboard = _keyboard(module, body.buttons)
        original = store.outbound_row(body.message_ref)
        if original is None or original["module"] != module:
            raise HTTPException(404, "unknown message_ref")  # another module's ref looks unknown
        if original["kind"] != "message":
            raise HTTPException(422, "message_ref must be a message sent with /notify")
        if original["state"] == "failed":
            raise HTTPException(409, "that message failed to send, so it cannot be edited")
        chat = original["body"]["chat"]
        ref = store.enqueue_outbound(
            module,
            "edit",
            str(chat),
            {
                "chat": chat,
                "edit_of": body.message_ref,
                "text": body.text,
                "format": body.format,
            }
            | ({"keyboard": keyboard} if keyboard is not None else {}),
            body.idempotency_key,
        )
        queue_wake.notify()
        return {"message_ref": ref}

    @app.post("/forward", status_code=202)
    async def post_forward(module: Module, body: ForwardBody) -> dict[str, int]:
        ref = store.enqueue_outbound(
            module,
            "forward",
            str(body.to),
            {
                "chat": body.to,
                "topic": body.topic,
                "from_chat": body.from_chat,
                "message_id": body.message_id,
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

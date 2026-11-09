"""POST /edit: a module updates the text and buttons of a message it sent (ticket #51)."""

from types import SimpleNamespace

import httpx
import pytest
from aiogram.exceptions import TelegramBadRequest

from sixto_gateway.api import create_app
from sixto_gateway.config import ConfigError, load_config
from sixto_gateway.decision import GATEWAY_MODULE
from sixto_gateway.notifier import Notifier
from sixto_gateway.sender import RETRY_SCHEDULE_SECONDS, Sender
from sixto_gateway.store import Store
from sixto_gateway.telegram.client import AiogramTelegramClient
from sixto_gateway.telegram.errors import (
    MessageNotModifiedError,
    PermanentSendError,
    TransientSendError,
)
from tests.fakes import NOTION_ENV, NOTION_YAML, FakeClock, FakeTelegram

WATCHDOG = "w" * 40
PSR = "p" * 40
CHAT = -1003261335368
ENV = {"TELEGRAM_BOT_TOKEN": "1:tok", "WATCHDOG_SECRET": WATCHDOG, "PSR_SECRET": PSR, **NOTION_ENV}


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "modules:\n  watchdog: {secret_env: WATCHDOG_SECRET}\n  psr: {secret_env: PSR_SECRET}\n"
        "routes: []\n"
        f"database_path: {tmp_path / 'gateway.db'}\n" + NOTION_YAML
    )
    config = load_config(path, ENV)
    clock = FakeClock()
    store = Store(config.database_path, clock=clock)
    wake = Notifier()
    telegram = FakeTelegram([], clock)
    sender = Sender(store, telegram, wake, clock=clock)
    return SimpleNamespace(
        store=store,
        telegram=telegram,
        sender=sender,
        clock=clock,
        app=create_app(config, store, Notifier(), wake),
    )


@pytest.fixture
async def client(env):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://gateway"
    ) as c:
        yield c


def auth(secret=WATCHDOG):
    return {"Authorization": f"Bearer {secret}"}


async def drain(sender):
    while await sender.send_next():
        pass


async def notify(client, **extra):
    body = {
        "to": CHAT,
        "text": "Approve job?",
        "buttons": [[{"text": "Approve", "payload": "a:1"}]],
    }
    r = await client.post("/notify", json=body | extra, headers=auth())
    assert r.status_code == 202
    return r.json()["message_ref"]


async def edit(client, ref, secret=WATCHDOG, **extra):
    return await client.post(
        "/edit",
        json={"message_ref": ref, "text": "Approved by Alex"} | extra,
        headers=auth(secret),
    )


async def test_edit_replaces_text_and_removes_buttons(client, env):
    ref = await notify(client)
    await drain(env.sender)
    sent_id = env.telegram.sent[0]["message_id"]

    r = await edit(client, ref)
    assert r.status_code == 202
    edit_ref = r.json()["message_ref"]
    await drain(env.sender)

    assert env.telegram.edits == [
        {
            "chat_id": CHAT,
            "message_id": sent_id,
            "text": "Approved by Alex",
            "parse_mode": "HTML",
            "keyboard": None,
        }
    ]
    status = await client.get(f"/notify/{edit_ref}", headers=auth())
    assert status.json() == {"state": "sent", "telegram_message_id": sent_id, "error": None}


async def test_edit_can_set_new_buttons_owned_by_caller(client, env):
    ref = await notify(client)
    await drain(env.sender)
    r = await edit(
        client,
        ref,
        format="plain",
        buttons=[
            [
                {"text": "Retry", "payload": "retry:1"},
                {"text": "Open", "url": "https://example.com/x"},
            ]
        ],
    )
    assert r.status_code == 202
    await drain(env.sender)
    assert env.telegram.edits[0]["parse_mode"] is None
    assert env.telegram.edits[0]["keyboard"] == [
        [
            {"text": "Retry", "callback_data": "mod:watchdog:retry:1"},
            {"text": "Open", "url": "https://example.com/x"},
        ]
    ]


async def test_edit_reaches_telegram_through_real_adapter(client, env):
    class RecordingBot:
        def __init__(self):
            self.edits = []

        async def send_message(self, **kwargs):
            return SimpleNamespace(message_id=42)

        async def edit_message_text(self, **kwargs):
            self.edits.append(kwargs)

    bot = RecordingBot()
    sender = Sender(env.store, AiogramTelegramClient(bot), Notifier(), clock=env.clock)
    ref = await notify(client)
    await drain(sender)
    await edit(client, ref)
    await drain(sender)
    assert bot.edits[0]["chat_id"] == CHAT
    assert bot.edits[0]["message_id"] == 42
    assert bot.edits[0]["text"] == "Approved by Alex"
    assert bot.edits[0]["reply_markup"] is None


async def test_module_cannot_edit_another_modules_message(client, env):
    ref = await notify(client)
    await drain(env.sender)
    r = await edit(client, ref, secret=PSR)
    assert r.status_code == 404
    assert env.store.queued_count() == 0


async def test_module_cannot_edit_a_gateway_message(client, env):
    ref = env.store.enqueue_outbound(
        GATEWAY_MODULE, "message", str(CHAT), {"chat": CHAT, "text": "x", "format": "html"}
    )
    r = await edit(client, ref)
    assert r.status_code == 404


async def test_gateway_is_a_reserved_module_name(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "modules:\n  gateway: {secret_env: WATCHDOG_SECRET}\nroutes: []\n"
        f"database_path: {tmp_path / 'gateway.db'}\n" + NOTION_YAML
    )
    with pytest.raises(ConfigError, match="reserved"):
        load_config(path, ENV)


async def test_unknown_ref_is_404_and_reaction_ref_is_422(client, env):
    assert (await edit(client, 999)).status_code == 404
    react = await client.post(
        "/react", json={"chat": CHAT, "message_id": 5, "emoji": "👍"}, headers=auth()
    )
    assert (await edit(client, react.json()["message_ref"])).status_code == 422


async def test_edit_of_an_edit_is_422(client, env):
    ref = await notify(client)
    edit_ref = (await edit(client, ref)).json()["message_ref"]
    assert (await edit(client, edit_ref)).status_code == 422


@pytest.mark.parametrize(
    "extra",
    [
        {"text": ""},
        {"buttons": [[]]},
        {"buttons": [[{"text": "x", "payload": "p", "url": "https://e.com"}]]},
        {"buttons": [[{"text": "x", "payload": "p" * 60}]]},
        {"surprise": 1},
    ],
)
async def test_invalid_edit_bodies_are_422_and_queue_nothing(client, env, extra):
    ref = await notify(client)
    await drain(env.sender)
    r = await edit(client, ref, **extra)
    assert r.status_code == 422
    assert env.store.queued_count() == 0


async def test_idempotency_key_queues_one_edit(client, env):
    ref = await notify(client)
    await drain(env.sender)
    first = await edit(client, ref, idempotency_key="approved:1")
    second = await edit(client, ref, idempotency_key="approved:1", text="something else")
    assert first.json() == second.json()
    await drain(env.sender)
    assert [e["text"] for e in env.telegram.edits] == ["Approved by Alex"]


async def test_edit_of_a_queued_message_waits_for_the_send(client, env):
    ref = await notify(client)
    env.telegram.fail_with = TransientSendError("network down")
    await drain(env.sender)  # the send fails once and is scheduled for a retry
    assert env.telegram.sent == []

    edit_ref = (await edit(client, ref)).json()["message_ref"]
    await drain(env.sender)
    assert env.telegram.edits == []  # waits: the message has no Telegram id yet
    assert (await client.get(f"/notify/{edit_ref}", headers=auth())).json()["state"] == "queued"
    assert env.sender.next_wake_in == pytest.approx(RETRY_SCHEDULE_SECONDS[0])

    env.clock.advance(RETRY_SCHEDULE_SECONDS[0])
    await drain(env.sender)
    sent_id = env.telegram.sent[0]["message_id"]
    assert env.telegram.edits[0]["message_id"] == sent_id  # sent first, then edited


async def test_edit_fails_when_the_original_send_fails(client, env):
    ref = await notify(client)
    edit_ref = (await edit(client, ref)).json()["message_ref"]
    env.telegram.fail_with = PermanentSendError("Bad Request: chat not found")
    await drain(env.sender)
    status = (await client.get(f"/notify/{edit_ref}", headers=auth())).json()
    assert status["state"] == "failed"
    assert f"message_ref {ref} was not sent" in status["error"]
    assert env.telegram.edits == []


async def test_edit_of_an_already_failed_message_is_409(client, env):
    ref = await notify(client)
    env.telegram.fail_with = PermanentSendError("Bad Request: chat not found")
    await drain(env.sender)
    r = await edit(client, ref)
    assert r.status_code == 409
    assert env.store.queued_count() == 0


async def test_transient_edit_errors_are_retried(client, env):
    ref = await notify(client)
    await drain(env.sender)
    env.telegram.edit_fail_with = TransientSendError("502 Bad Gateway")
    edit_ref = (await edit(client, ref)).json()["message_ref"]
    await drain(env.sender)
    status = (await client.get(f"/notify/{edit_ref}", headers=auth())).json()
    assert status["state"] == "queued" and "502" in status["error"]

    env.clock.advance(RETRY_SCHEDULE_SECONDS[0])
    await drain(env.sender)
    assert len(env.telegram.edits) == 1
    assert (await client.get(f"/notify/{edit_ref}", headers=auth())).json()["state"] == "sent"


async def test_permanent_edit_error_fails_at_once(client, env):
    ref = await notify(client)
    await drain(env.sender)
    env.telegram.edit_fail_with = PermanentSendError("Bad Request: message to edit not found")
    edit_ref = (await edit(client, ref)).json()["message_ref"]
    await drain(env.sender)
    assert (await client.get(f"/notify/{edit_ref}", headers=auth())).json()["state"] == "failed"


async def test_message_not_modified_counts_as_success(client, env):
    ref = await notify(client)
    await drain(env.sender)
    env.telegram.edit_fail_with = MessageNotModifiedError("Bad Request: message is not modified")
    edit_ref = (await edit(client, ref)).json()["message_ref"]
    await drain(env.sender)
    status = (await client.get(f"/notify/{edit_ref}", headers=auth())).json()
    assert status["state"] == "sent" and status["error"] is None


async def test_adapter_maps_message_not_modified():
    class Bot:
        async def edit_message_text(self, **kwargs):
            raise TelegramBadRequest(
                method=SimpleNamespace(),  # type: ignore[arg-type]
                message="Bad Request: message is not modified: specified new message content "
                "and reply markup are exactly the same",
            )

    with pytest.raises(MessageNotModifiedError):
        await AiogramTelegramClient(Bot()).edit_message(CHAT, 1, "t", None)


async def test_queued_edit_survives_a_restart(client, env, tmp_path):
    ref = await notify(client)
    await drain(env.sender)
    await edit(client, ref)
    restarted = Sender(env.store, env.telegram, Notifier(), clock=env.clock)
    await drain(restarted)
    assert len(env.telegram.edits) == 1

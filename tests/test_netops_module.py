"""The netops module (ticket #66): registered from the example config with its own secret."""

from pathlib import Path

import httpx
import pytest

from sixto_gateway.api import create_app
from sixto_gateway.config import load_config
from sixto_gateway.notifier import Notifier
from sixto_gateway.sender import Sender
from sixto_gateway.store import Store
from tests.fakes import NOTION_ENV, FakeClock, FakeTelegram

EXAMPLE = Path(__file__).parent.parent / "config.example.yaml"
NETOPS = "n" * 40
WATCHDOG = "w" * 40
CHAT = -1000000000000


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(EXAMPLE.read_text().replace("./data/gateway.db", str(tmp_path / "gateway.db")))
    config = load_config(
        path,
        {
            "TELEGRAM_BOT_TOKEN": "1:tok",
            "WATCHDOG_SECRET": WATCHDOG,
            "SHIFT_SECRET": "s" * 40,
            "T6CORE_SECRET": "t" * 40,
            "NETOPS_SECRET": NETOPS,
            **NOTION_ENV,
        },
    )
    clock = FakeClock()
    store = Store(config.database_path, clock=clock)
    wake = Notifier()
    telegram = FakeTelegram([], clock)
    sender = Sender(store, telegram, wake, clock=clock)
    app = create_app(config, store, Notifier(), wake)
    return config, store, telegram, sender, app


@pytest.fixture
async def client(env):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env[-1]), base_url="http://gateway"
    ) as c:
        yield c


def auth(secret=NETOPS):
    return {"Authorization": f"Bearer {secret}"}


async def drain(sender):
    while await sender.send_next():
        pass


def test_netops_is_its_own_module_with_its_own_secret(env):
    config = env[0]
    assert config.module_for_secret(NETOPS) == "netops"
    assert config.module_for_secret(WATCHDOG) == "watchdog"
    assert config.modules["netops"].secret_env == "NETOPS_SECRET"


async def test_netops_notification_is_sent(client, env):
    telegram, sender = env[2], env[3]
    r = await client.post("/notify", json={"to": CHAT, "text": "tunnel down"}, headers=auth())
    assert r.status_code == 202
    await drain(sender)
    assert [m["text"] for m in telegram.sent] == ["tunnel down"]


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}, auth("n" * 41)])
async def test_wrong_or_missing_secret_is_rejected_and_audited(client, env, headers):
    store, telegram, sender = env[1], env[2], env[3]
    r = await client.post("/notify", json={"to": CHAT, "text": "x"}, headers=headers)
    assert r.status_code == 401
    assert [e[0] for e in store.audit_events()] == ["auth_failure"]
    await drain(sender)
    assert telegram.sent == []


async def test_telegram_bot_token_is_not_accepted_as_a_secret(client):
    r = await client.post("/notify", json={"to": CHAT, "text": "x"}, headers=auth("1:tok"))
    assert r.status_code == 401


async def test_retry_with_same_key_queues_once(client, env):
    telegram, sender = env[2], env[3]
    body = {"to": CHAT, "text": "tunnel down", "idempotency_key": "netops-tunnel-down-1"}
    first = await client.post("/notify", json=body, headers=auth())
    again = await client.post("/notify", json=body, headers=auth())
    assert first.status_code == again.status_code == 202
    assert first.json() == again.json()
    await drain(sender)
    assert len(telegram.sent) == 1


async def test_same_key_from_another_module_is_not_a_retry(client, env):
    body = {"to": CHAT, "text": "hi", "idempotency_key": "shared-key"}
    mine = await client.post("/notify", json=body, headers=auth())
    other = await client.post("/notify", json=body, headers=auth(WATCHDOG))
    assert mine.json()["message_ref"] != other.json()["message_ref"]


async def test_netops_cannot_read_another_modules_notification(client):
    r = await client.post("/notify", json={"to": CHAT, "text": "hi"}, headers=auth(WATCHDOG))
    ref = r.json()["message_ref"]
    assert (await client.get(f"/notify/{ref}", headers=auth())).status_code in (403, 404)

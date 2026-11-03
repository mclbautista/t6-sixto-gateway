import asyncio
import logging
import time

import httpx
import pytest

from sixto_gateway.api import create_app
from sixto_gateway.config import load_config
from sixto_gateway.notifier import Notifier
from sixto_gateway.poller import Poller
from sixto_gateway.store import Store
from tests.fakes import (
    INTAKE_CHAT,
    INTAKE_TOPIC,
    NOTION_ENV,
    NOTION_YAML,
    FakeClock,
    FakeTelegram,
    fixture_updates,
    moved,
)

WATCHDOG = "w" * 40
PSR = "p" * 40


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "modules:\n  watchdog: {secret_env: WATCHDOG_SECRET}\n  psr: {secret_env: PSR_SECRET}\n"
        "routes:\n"
        f"  - {{chat: {INTAKE_CHAT}, topic: {INTAKE_TOPIC}, module: watchdog}}\n"
        f"  - {{chat: {INTAKE_CHAT}, topic: 5, module: psr}}\n"
        f"database_path: {tmp_path / 'gateway.db'}\n" + NOTION_YAML
    )
    config = load_config(
        path,
        {
            "TELEGRAM_BOT_TOKEN": "1:tok",
            "WATCHDOG_SECRET": WATCHDOG,
            "PSR_SECRET": PSR,
            **NOTION_ENV,
        },
    )
    clock = FakeClock()
    store = Store(config.database_path, clock=clock)
    notifier = Notifier()
    telegram = FakeTelegram([])
    poller = Poller(store, telegram, config, notifier, clock)
    app = create_app(config, store, notifier)
    return config, store, notifier, telegram, poller, app


@pytest.fixture
async def client(env):
    app = env[-1]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as c:
        yield c


def auth(secret):
    return {"Authorization": f"Bearer {secret}"}


async def feed(env, raws):
    _, _, _, telegram, poller, _ = env
    for raw in raws:
        telegram.push(raw)
    while await poller.poll_once():
        pass


def intake(update_id, **kw):
    return moved(fixture_updates()["text"], update_id=update_id, **kw)


async def test_updates_returns_only_the_callers_items_in_order(env, client):
    await feed(env, [intake(1), intake(2, topic=5), intake(3), intake(4, topic=5), intake(5)])

    response = await client.get("/updates", headers=auth(WATCHDOG))
    assert response.status_code == 200
    items = response.json()["items"]
    assert [i["update_id"] for i in items] == [1, 3, 5]
    assert [i["seq"] for i in items] == sorted(i["seq"] for i in items)
    assert items[0]["from"] == {"id": 111, "username": "anareyes", "contact_id": None}

    other = await client.get("/updates", headers=auth(PSR))
    assert [i["update_id"] for i in other.json()["items"]] == [2, 4]


async def test_after_is_exclusive_and_a_module_can_reread(env, client):
    await feed(env, [intake(1), intake(2), intake(3)])
    seqs = [
        i["seq"] for i in (await client.get("/updates", headers=auth(WATCHDOG))).json()["items"]
    ]

    after_first = await client.get("/updates", params={"after": seqs[0]}, headers=auth(WATCHDOG))
    assert [i["seq"] for i in after_first.json()["items"]] == seqs[1:]
    again = await client.get("/updates", params={"after": 0}, headers=auth(WATCHDOG))
    assert [i["seq"] for i in again.json()["items"]] == seqs  # nothing is acked away
    done = await client.get("/updates", params={"after": seqs[-1]}, headers=auth(WATCHDOG))
    assert done.json() == {"items": []}


async def test_at_most_100_items_per_response(env, client):
    await feed(env, [intake(i) for i in range(1, 121)])
    first = (await client.get("/updates", headers=auth(WATCHDOG))).json()["items"]
    assert len(first) == 100
    rest = await client.get("/updates", params={"after": first[-1]["seq"]}, headers=auth(WATCHDOG))
    assert len(rest.json()["items"]) == 20


async def test_empty_poll_waits_for_the_timeout(env, client):
    started = time.monotonic()
    response = await client.get("/updates", params={"timeout": 0.3}, headers=auth(WATCHDOG))
    assert response.json() == {"items": []}
    assert 0.25 <= time.monotonic() - started < 2


async def test_poll_returns_early_when_a_new_item_arrives(env, client):
    task = asyncio.create_task(
        client.get("/updates", params={"timeout": 30}, headers=auth(WATCHDOG))
    )
    await asyncio.sleep(0.1)
    assert not task.done()

    started = time.monotonic()
    await feed(env, [intake(7)])
    response = await asyncio.wait_for(task, timeout=5)
    assert time.monotonic() - started < 2
    assert [i["update_id"] for i in response.json()["items"]] == [7]


async def test_poll_is_not_woken_by_another_modules_item(env, client):
    task = asyncio.create_task(
        client.get("/updates", params={"timeout": 0.4}, headers=auth(WATCHDOG))
    )
    await asyncio.sleep(0.05)
    await feed(env, [intake(8, topic=5)])  # routed to psr
    response = await asyncio.wait_for(task, timeout=5)
    assert response.json() == {"items": []}


@pytest.mark.parametrize(
    "params", [{"timeout": 51}, {"timeout": -1}, {"after": -1}, {"after": "x"}]
)
async def test_bad_query_values_are_rejected(client, params):
    response = await client.get("/updates", params=params, headers=auth(WATCHDOG))
    assert response.status_code == 422


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer nope"}, {"Authorization": WATCHDOG}, auth(WATCHDOG + "x")],
)
async def test_wrong_or_missing_secret_gets_401_and_is_audited(env, client, headers, caplog):
    store = env[1]
    with caplog.at_level(logging.WARNING):
        response = await client.get("/updates", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert [e[0] for e in store.audit_events()] == ["auth_failure"]
    assert "auth failure" in caplog.text
    assert WATCHDOG not in caplog.text and "nope" not in caplog.text
    assert all("nope" not in (d or "") for _, _, d in store.audit_events())


async def test_401_does_not_depend_on_whether_items_exist(env, client):
    await feed(env, [intake(1)])
    assert (await client.get("/updates", headers=auth("x" * 40))).status_code == 401


async def test_edited_flag_is_served(env, client):
    await feed(env, [fixture_updates()["edit"]])
    (item,) = (await client.get("/updates", headers=auth(WATCHDOG))).json()["items"]
    assert item["edited"] is True and item["edit_date"] is not None

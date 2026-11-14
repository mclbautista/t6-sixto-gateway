import json
from pathlib import Path

import pytest

from tests.fakes import fixture_updates, moved
from tests.test_api import PSR, WATCHDOG, auth, feed

pytest_plugins = ["tests.test_api"]


def migrations():
    return json.loads((Path(__file__).parent / "fixtures/chat_migration.json").read_text())


@pytest.mark.parametrize("order", [("to", "from"), ("from", "to")])
async def test_migration_rewrites_and_delivers_once(env, client, order):
    old, new = -12345, -10012345
    await client.put(
        "/subscriptions",
        headers=auth(PSR),
        json=[{"chat": old, "topic": 77}, {"chat": new, "topic": 77}],
    )
    await client.put("/subscriptions", headers=auth(WATCHDOG), json=[{"chat": old, "topic": None}])
    fixtures = migrations()
    updates = [fixtures[key].copy() for key in order]
    # Telegram update ids are monotonic even when the service messages arrive in reverse order.
    updates[0]["update_id"], updates[1]["update_id"] = 201, 202
    await feed(env, updates + [moved(fixture_updates()["text"], update_id=203, chat=new, topic=77)])
    for secret in (PSR, WATCHDOG):
        subscriptions = (await client.get("/subscriptions", headers=auth(secret))).json()
        assert subscriptions == [{"chat": new, "topic": 77 if secret == PSR else None}]
        items = (await client.get("/updates", headers=auth(secret))).json()["items"]
        assert [item["kind"] for item in items] == ["event", "update"]
        assert (items[0]["event"], items[0]["old_chat"], items[0]["new_chat"]) == (
            "chat_migrated",
            old,
            new,
        )
        assert items[1]["chat"] == new


async def test_migration_rolls_back_with_update(env, monkeypatch):
    _, store, _, telegram, poller, _ = env
    store.replace_subscriptions("psr", [(-12345, None)])
    telegram.push(migrations()["to"])
    real_commit = store._commit

    def crash():
        raise RuntimeError("crash before commit")

    monkeypatch.setattr(store, "_commit", crash)
    with pytest.raises(RuntimeError, match="crash"):
        await poller.poll_once()
    assert store.subscriptions("psr") == [{"chat": -12345, "topic": None}]
    assert store.count_updates() == 0
    assert store.last_update_id() is None
    monkeypatch.setattr(store, "_commit", real_commit)
    await poller.poll_once()
    assert store.subscriptions("psr") == [{"chat": -10012345, "topic": None}]
    assert [item["event"] for item in store.deliveries_after("psr", 0, 100)] == ["chat_migrated"]

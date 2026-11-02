import json

import pytest

from sixto_gateway.config import load_config
from sixto_gateway.notifier import Notifier
from sixto_gateway.poller import Poller
from sixto_gateway.store import Store
from tests.fakes import (
    INTAKE_CHAT,
    INTAKE_TOPIC,
    FakeClock,
    FakeTelegram,
    fixture_updates,
    moved,
    valid_fixture_updates,
)

SECRET = "w" * 40


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "modules: {watchdog: {secret_env: WATCHDOG_SECRET}}\n"
        f"routes: [{{chat: {INTAKE_CHAT}, topic: {INTAKE_TOPIC}, module: watchdog}}]\n"
        f"database_path: {tmp_path / 'gateway.db'}\n"
    )
    return load_config(path, {"TELEGRAM_BOT_TOKEN": "1:tok", "WATCHDOG_SECRET": SECRET})


def make(config, telegram, clock=None):
    store = Store(config.database_path, clock=clock or FakeClock())
    return store, Poller(store, telegram, config, Notifier(), clock=clock or FakeClock())


def all_fixture_updates():
    return list(valid_fixture_updates().values())


async def drain(poller):
    while await poller.poll_once():
        pass


async def test_fixtures_are_stored_once_and_routed_to_watchdog(config):
    telegram = FakeTelegram(all_fixture_updates())
    store, poller = make(config, telegram)

    await drain(poller)
    await drain(poller)  # a second pass finds nothing new

    expected = len(valid_fixture_updates())
    assert store.count_updates() == expected
    items = store.deliveries_after("watchdog", 0, limit=100)
    assert len(items) == expected  # every fixture is in the intake topic
    assert [i["update_id"] for i in items] == sorted(i["update_id"] for i in items)
    assert all(i["kind"] == "update" and i["chat"] == INTAKE_CHAT for i in items)
    assert telegram.calls[0]["offset"] == 0  # nothing committed yet: no offset constraint
    assert telegram.calls[-1]["offset"] == max(i["update_id"] for i in items) + 1
    assert telegram.calls[0]["allowed_updates"] == ["message", "edited_message"]


async def test_updates_outside_the_intake_topic_are_kept_but_not_delivered(config):
    text = fixture_updates()["text"]
    telegram = FakeTelegram(
        [
            moved(text, update_id=10, topic=999),  # other topic, same chat
            moved(text, update_id=11, chat=-100777, topic=INTAKE_TOPIC),  # other chat
            moved(text, update_id=12, topic=0),  # no topic at all
            moved(text, update_id=13),  # the intake topic
        ]
    )
    store, poller = make(config, telegram)

    await drain(poller)

    assert store.count_updates() == 4
    assert store.unrouted_update_ids() == [10, 11, 12]
    assert [i["update_id"] for i in store.deliveries_after("watchdog", 0, 100)] == [13]


async def test_update_fields_are_normalized(config):
    telegram = FakeTelegram([fixture_updates()["caption"]])
    store, poller = make(config, telegram)
    await drain(poller)

    (item,) = store.deliveries_after("watchdog", 0, 100)
    assert item["seq"] >= 1
    assert item["update_id"] == 9002
    assert item["topic"] == INTAKE_TOPIC and item["message_id"] == 502
    assert item["text"] == "MFG - TOC - JO #074\nTask: cut 30s teaser"  # caption fallback
    assert item["from"] == {"id": 111, "username": "anareyes", "contact_id": None}
    assert item["edited"] is False and item["edit_date"] is None
    assert item["raw"]["message"]["photo"][0]["file_id"] == "F"
    json.dumps(item)


async def test_edited_message_follows_the_route_and_is_flagged(config):
    edit = fixture_updates()["edit"]
    telegram = FakeTelegram([edit])
    store, poller = make(config, telegram)
    await drain(poller)

    (item,) = store.deliveries_after("watchdog", 0, 100)
    assert item["edited"] is True
    assert item["edit_date"] == edit["edited_message"]["edit_date"]
    assert item["raw"]["edited_message"]["message_id"] == edit["edited_message"]["message_id"]


async def test_edit_without_topic_follows_the_original_message(config):
    text = fixture_updates()["text"]
    edit = moved(fixture_updates()["edit"], update_id=9100, topic=0)
    edit["edited_message"]["message_id"] = text["message"]["message_id"]
    store, poller = make(config, FakeTelegram([text, edit]))
    await drain(poller)

    assert [i["update_id"] for i in store.deliveries_after("watchdog", 0, 100)] == [9001, 9100]


async def test_stored_timestamps_use_the_clock(config):
    clock = FakeClock(1000.0)
    store, poller = make(config, FakeTelegram([fixture_updates()["text"]]), clock)
    await drain(poller)
    assert store.received_at(9001) == 1000.0


class Crash(Exception):
    pass


async def test_crash_between_fetch_and_commit_loses_and_duplicates_nothing(config, monkeypatch):
    telegram = FakeTelegram(all_fixture_updates())
    total = len(valid_fixture_updates())

    # First process: dies while committing the 4th update (before COMMIT).
    store, poller = make(config, telegram)
    real_commit, commits = store._commit, []

    def crashing_commit():
        commits.append(1)
        if len(commits) == 4:
            raise Crash
        real_commit()

    monkeypatch.setattr(store, "_commit", crashing_commit)
    with pytest.raises(Crash):
        await poller.poll_once()
    store.close()  # the process is gone; SQLite rolls the open transaction back

    # Restart on the same file.
    store2, poller2 = make(config, telegram)
    assert store2.count_updates() == 3
    assert store2.last_update_id() == 9003  # the cursor did not pass the unsaved update
    await drain(poller2)

    assert store2.count_updates() == total
    ids = [i["update_id"] for i in store2.deliveries_after("watchdog", 0, 100)]
    assert ids == sorted(set(ids)) and len(ids) == total
    assert ids == sorted(u["update_id"] for u in all_fixture_updates())  # no gaps


async def test_refetched_updates_are_idempotent(config):
    telegram = FakeTelegram(all_fixture_updates())
    store, poller = make(config, telegram)
    await drain(poller)
    before = store.deliveries_after("watchdog", 0, 100)

    # Telegram redelivers everything (e.g. the cursor was lost): nothing doubles.
    for incoming in await telegram.get_updates(0, 0, []):
        assert store.save_update(incoming, "watchdog", received_at=1.0) is None
    assert store.deliveries_after("watchdog", 0, 100) == before
    assert store.count_updates() == len(before)


async def test_notifier_wakes_waiters_when_a_routed_update_is_saved(config):
    notifier = Notifier()
    store = Store(config.database_path, clock=FakeClock())
    poller = Poller(store, FakeTelegram([fixture_updates()["text"]]), config, notifier, FakeClock())
    waiter = notifier.current()
    assert not waiter.is_set()
    await poller.poll_once()
    assert waiter.is_set()


async def test_edit_of_an_unrouted_original_stays_unrouted(config):
    text = moved(fixture_updates()["text"], update_id=20, topic=999)  # unrouted topic
    edit = moved(fixture_updates()["edit"], update_id=21)  # edit arrives tagged with intake topic
    edit["edited_message"]["message_id"] = text["message"]["message_id"]
    store, poller = make(config, FakeTelegram([text, edit]))
    await drain(poller)

    assert store.unrouted_update_ids() == [20, 21]

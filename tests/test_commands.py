"""Commands and the allow-list (spec sections 6 step 3 and 8), with a fake ``jo`` command."""

import pytest

from sixto_gateway.commands import register_menus
from sixto_gateway.config import load_config
from sixto_gateway.notifier import Notifier
from sixto_gateway.poller import Poller
from sixto_gateway.sender import Sender
from sixto_gateway.store import Store
from tests.fakes import FakeClock, FakeTelegram, dm_message, group_command

PSR_CHAT = -1003261335368
ALLOWED, OTHER = 5001, 5002
REFUSAL = "That command isn't available to you."
BASE = (
    "modules: {watchdog: {secret_env: WATCHDOG_SECRET}, jo: {secret_env: JO_SECRET}}\n"
    f"routes: [{{chat: {PSR_CHAT}, topic: 1, module: watchdog}}]\n"
    f"groups: {{psr: [{ALLOWED}, 5003]}}\n"
)
ENV = {"TELEGRAM_BOT_TOKEN": "1:tok", "WATCHDOG_SECRET": "w" * 40, "JO_SECRET": "j" * 40}


def make_config(
    tmp_path, commands="commands: {jo: {module: jo, allow: [psr], description: JOs}}\n"
):
    path = tmp_path / "config.yaml"
    path.write_text(BASE + commands + f"database_path: {tmp_path / 'gateway.db'}\n")
    return load_config(path, ENV)


@pytest.fixture
def config(tmp_path):
    return make_config(tmp_path)


def build(config, raw_updates):
    clock = FakeClock()
    telegram = FakeTelegram(list(raw_updates))
    store = Store(config.database_path, clock=clock)
    wake = Notifier()
    poller = Poller(store, telegram, config, Notifier(), clock=clock, queue_wake=wake)
    return store, telegram, poller, Sender(store, telegram, wake)


async def run(poller, sender):
    while await poller.poll_once():
        pass
    while await sender.send_next():
        pass


def replies(telegram):
    return [(m["chat_id"], m["text"]) for m in telegram.sent if m["kind"] == "message"]


async def test_allowed_user_command_reaches_the_owning_module(config):
    store, telegram, poller, sender = build(config, [dm_message(1, ALLOWED, "/jo some args")])
    await run(poller, sender)

    (item,) = store.deliveries_after("jo", 0, 100)
    assert item["kind"] == "command"
    assert item["command"] == "jo" and item["args"] == "some args"
    assert item["from"]["id"] == ALLOWED and item["chat"] == ALLOWED
    assert item["text"] == "/jo some args" and item["raw"]["message"]["entities"]
    assert store.deliveries_after("watchdog", 0, 100) == []
    assert replies(telegram) == []
    assert ("command_routed", "jo", f"command=jo from={ALLOWED}") in store.audit_events()


async def test_refused_user_gets_one_reply_and_nothing_is_delivered(config):
    store, telegram, poller, sender = build(config, [dm_message(1, OTHER, "/jo some args")])
    await run(poller, sender)
    await run(poller, sender)  # nothing new on a second pass

    assert store.deliveries_after("jo", 0, 100) == []
    assert store.unrouted_update_ids() == [1]
    assert replies(telegram) == [(OTHER, REFUSAL)]
    assert telegram.sent[0]["topic_id"] is None and telegram.sent[0]["parse_mode"] is None
    assert ("command_refused", None, f"command=jo from={OTHER}") in store.audit_events()


async def test_unknown_command_in_a_dm_gets_the_same_refusal(config):
    store, telegram, poller, sender = build(config, [dm_message(1, ALLOWED, "/nope")])
    await run(poller, sender)

    assert store.unrouted_update_ids() == [1]
    assert replies(telegram) == [(ALLOWED, REFUSAL)]
    assert ("command_refused", None, f"command=nope from={ALLOWED}") in store.audit_events()


async def test_commands_in_groups_get_no_reply(config):
    updates = [
        group_command(1, OTHER, "/jo x", PSR_CHAT, 1),  # not allowed, intake route
        group_command(2, ALLOWED, "/jo x", PSR_CHAT, 1),  # allowed, still a group
        group_command(3, OTHER, "/nope", PSR_CHAT, 9),  # unknown, unrouted topic
    ]
    store, telegram, poller, sender = build(config, updates)
    await run(poller, sender)

    assert replies(telegram) == []
    assert store.deliveries_after("jo", 0, 100) == []
    # group messages keep their normal routing
    assert [i["update_id"] for i in store.deliveries_after("watchdog", 0, 100)] == [1, 2]
    assert store.unrouted_update_ids() == [3]


async def test_command_forms(config):
    updates = [
        dm_message(1, ALLOWED, "/JO"),  # case-insensitive, no args
        dm_message(2, ALLOWED, "/jo@sixto_t6bot  a  b "),  # bot suffix, args trimmed
        dm_message(3, ALLOWED, "hello /jo"),  # not the first entity: plain text
    ]
    store, telegram, poller, sender = build(config, updates)
    await run(poller, sender)

    items = store.deliveries_after("jo", 0, 100)
    assert [(i["command"], i["args"]) for i in items] == [("jo", ""), ("jo", "a  b")]
    assert store.unrouted_update_ids() == [3] and replies(telegram) == []


async def test_edit_of_a_command_is_not_delivered_or_answered(config):
    updates = [dm_message(1, ALLOWED, "/jo x"), dm_message(2, OTHER, "/jo y", edited=True)]
    store, telegram, poller, sender = build(config, updates)
    await run(poller, sender)

    assert [i["update_id"] for i in store.deliveries_after("jo", 0, 100)] == [1]
    assert store.unrouted_update_ids() == [2] and replies(telegram) == []


async def test_redelivered_refused_command_is_not_answered_twice(config):
    store, telegram, poller, sender = build(config, [dm_message(1, OTHER, "/jo")])
    await run(poller, sender)
    for incoming in await telegram.get_updates(0, 0, []):  # Telegram redelivers it
        decision = poller.decide(incoming)
        store.save_update(incoming, decision, received_at=1.0)
    await run(poller, sender)

    assert replies(telegram) == [(OTHER, REFUSAL)]


async def test_reply_is_queued_in_the_same_commit_as_the_update(config, monkeypatch):
    store, telegram, poller, sender = build(config, [dm_message(1, OTHER, "/jo")])

    class Crash(Exception):
        pass

    def boom():
        raise Crash

    monkeypatch.setattr(store, "_commit", boom)
    with pytest.raises(Crash):
        await poller.poll_once()
    store.close()

    store2, telegram2, poller2, sender2 = build(config, [dm_message(1, OTHER, "/jo")])
    assert store2.count_updates() == 0 and store2.next_outbound() is None  # all or nothing
    await run(poller2, sender2)
    assert replies(telegram2) == [(OTHER, REFUSAL)]


async def test_boot_sets_each_allowed_users_menu_and_an_empty_default(config):
    store = Store(config.database_path, clock=FakeClock())
    telegram = FakeTelegram([])
    await register_menus(store, telegram, config)

    assert telegram.menus == {None: [], ALLOWED: [("jo", "JOs")], 5003: [("jo", "JOs")]}
    assert OTHER not in telegram.menus


async def test_each_person_sees_only_their_commands(tmp_path):
    config = make_config(
        tmp_path,
        "commands:\n"
        "  jo: {module: jo, allow: [psr]}\n"
        "  secret: {module: jo, allow: [5003, 5004]}\n",
    )
    store = Store(config.database_path, clock=FakeClock())
    telegram = FakeTelegram([])
    await register_menus(store, telegram, config)

    assert telegram.menus[ALLOWED] == [("jo", "jo")]  # no description falls back to the name
    assert telegram.menus[5003] == [("jo", "jo"), ("secret", "secret")]
    assert telegram.menus[5004] == [("secret", "secret")]


async def test_removed_commands_and_people_disappear_on_the_next_boot(tmp_path):
    config = make_config(tmp_path)
    store = Store(config.database_path, clock=FakeClock())
    telegram = FakeTelegram([])
    await register_menus(store, telegram, config)

    empty = make_config(tmp_path, "commands: {}\n")
    await register_menus(store, telegram, empty)
    assert telegram.menus == {None: []}  # no per-person menu remains

    narrower = make_config(tmp_path, "commands: {jo: {module: jo, allow: [5003]}}\n")
    await register_menus(store, telegram, narrower)
    assert telegram.menus == {None: [], 5003: [("jo", "jo")]}


async def test_a_refused_menu_call_does_not_stop_boot_and_is_retried(config):
    store = Store(config.database_path, clock=FakeClock())
    telegram = FakeTelegram([])
    telegram.menu_failures = {5003}  # never opened a chat with the bot
    await register_menus(store, telegram, config)
    assert set(telegram.menus) == {None, ALLOWED}
    assert any(e == "menu_failed" for e, _, _ in store.audit_events())

    telegram.menu_failures = set()
    await register_menus(store, telegram, config)
    assert telegram.menus[5003] == [("jo", "JOs")]


async def test_phase_1_with_no_commands_sets_nothing_but_the_empty_default(tmp_path):
    config = make_config(tmp_path, "commands: {}\n")
    store, telegram, poller, sender = build(config, [dm_message(1, ALLOWED, "/jo")])
    await register_menus(store, telegram, config)
    await run(poller, sender)

    assert telegram.menus == {None: []}
    assert replies(telegram) == [(ALLOWED, REFUSAL)]

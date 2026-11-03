"""Commands and the allow-list (spec sections 6 step 3 and 8), with a fake ``jo`` command."""

import pytest

from sixto_gateway.commands import register_menus
from sixto_gateway.config import load_config
from sixto_gateway.notifier import Notifier
from sixto_gateway.poller import Poller
from sixto_gateway.sender import Sender
from sixto_gateway.store import Store
from tests.fakes import (
    NOTION_ENV,
    NOTION_YAML,
    FakeClock,
    FakeTelegram,
    dm_message,
    group_command,
)

PSR_CHAT = -1003261335368
ALLOWED, OTHER = 5001, 5002
REFUSAL = "That command isn't available to you."
BASE = (
    "modules: {watchdog: {secret_env: WATCHDOG_SECRET}, jo: {secret_env: JO_SECRET}}\n"
    f"routes: [{{chat: {PSR_CHAT}, topic: 1, module: watchdog}}]\n"
    f"groups: {{psr: [{ALLOWED}, 5003]}}\n" + NOTION_YAML
)
ENV = {
    "TELEGRAM_BOT_TOKEN": "1:tok",
    "WATCHDOG_SECRET": "w" * 40,
    "JO_SECRET": "j" * 40,
    **NOTION_ENV,
}


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
    assert store2.count_updates() == 0 and store2.due_outbound(1e18) == []  # all or nothing
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


class CrashAfterTelegramAccepts(FakeTelegram):
    """Telegram sets the menu, then the process dies before the gateway records it."""

    async def set_my_commands(self, chat_id, commands):
        await super().set_my_commands(chat_id, commands)
        if chat_id is not None:
            raise SystemExit("crash")


async def test_a_crash_after_telegram_set_a_menu_cannot_leave_it_behind(tmp_path):
    config = make_config(tmp_path, "commands: {jo: {module: jo, allow: [5003]}}\n")
    store = Store(config.database_path, clock=FakeClock())
    crashing = CrashAfterTelegramAccepts([])
    with pytest.raises(SystemExit):
        await register_menus(store, crashing, config)
    assert 5003 in crashing.menus  # Telegram has it
    store.close()

    telegram = FakeTelegram([])
    telegram.menus = crashing.menus  # same bot, next boot
    store = Store(config.database_path, clock=FakeClock())
    await register_menus(store, telegram, make_config(tmp_path, "commands: {}\n"))
    assert telegram.menus == {None: []}
    assert store.menu_users() == set()


async def test_a_removed_person_telegram_refuses_is_forgotten_not_retried_forever(tmp_path):
    from sixto_gateway.telegram.errors import PermanentSendError, TransientSendError

    class Refusing(FakeTelegram):
        def __init__(self, error):
            super().__init__([])
            self.error = error

        async def delete_my_commands(self, chat_id):
            self.menu_calls.append(("delete", chat_id, []))
            raise self.error

    config = make_config(tmp_path, "commands: {jo: {module: jo, allow: [5003]}}\n")
    store = Store(config.database_path, clock=FakeClock())
    await register_menus(store, FakeTelegram([]), config)
    empty = make_config(tmp_path, "commands: {}\n")

    await register_menus(store, Refusing(TransientSendError("timeout")), empty)
    assert store.menu_users() == {5003}  # maybe still there: try again next boot

    await register_menus(store, Refusing(PermanentSendError("chat not found")), empty)
    assert store.menu_users() == set()  # Telegram will never take a call for this chat
    assert any(e == "menu_failed" for e, _, _ in store.audit_events())


async def test_menus_wait_out_a_stored_429_pause(config):
    clock = FakeClock()
    store = Store(config.database_path, clock=clock)
    store.extend_pause(clock() + 30)
    telegram = FakeTelegram([])
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)
        clock.advance(seconds)

    await register_menus(store, telegram, config, clock=clock, sleep=sleep)
    assert slept == [30]  # no menu call before Telegram allows it
    assert telegram.menus[ALLOWED] == [("jo", "JOs")]


async def test_a_429_on_a_menu_call_pauses_the_sender_too(config):
    from sixto_gateway.telegram.errors import RetryAfterError

    class Limited(FakeTelegram):
        async def set_my_commands(self, chat_id, commands):
            if chat_id is not None:
                raise RetryAfterError(90)
            await super().set_my_commands(chat_id, commands)

    clock = FakeClock()
    store = Store(config.database_path, clock=clock)
    start = clock()

    async def sleep(seconds):
        clock.advance(seconds)

    await register_menus(store, Limited([]), config, clock=clock, sleep=sleep)
    # the first refusal pauses 90 s; the next person's call waits it out, then is refused again
    assert store.paused_until() == start + 180


# --- /whereami: a built-in command the gateway answers itself (spec #37) ------------------

WHEREAMI = "commands: {whereami: {builtin: true, allow: [psr], description: Where am I}}\n"
TOPIC = 77


@pytest.fixture
def whereami_config(tmp_path):
    return make_config(tmp_path, WHEREAMI)


async def test_allowed_user_gets_chat_and_topic_ids_in_the_same_topic(whereami_config):
    raw = group_command(1, ALLOWED, "/whereami", PSR_CHAT, TOPIC)
    store, telegram, poller, sender = build(whereami_config, [raw])
    await run(poller, sender)

    (sent,) = telegram.sent
    assert sent["chat_id"] == PSR_CHAT and sent["topic_id"] == TOPIC
    assert sent["text"] == f"Chat id: <code>{PSR_CHAT}</code>\nTopic id: <code>{TOPIC}</code>"
    assert sent["parse_mode"] == "HTML"
    assert (
        "command_answered",
        None,
        f"command=whereami chat={PSR_CHAT} from={ALLOWED}",
    ) in store.audit_events()


async def test_whereami_is_never_delivered_to_a_module(whereami_config):
    raw = [
        group_command(1, ALLOWED, "/whereami", PSR_CHAT, 1),  # routed to the watchdog
        dm_message(2, ALLOWED, "/whereami"),
    ]
    store, _, poller, sender = build(whereami_config, raw)
    await run(poller, sender)

    for module in ("watchdog", "jo"):
        assert store.deliveries_after(module, 0, 100) == []


async def test_non_allowed_user_in_a_group_gets_no_reply_but_is_audited(whereami_config):
    raw = group_command(1, OTHER, "/whereami", PSR_CHAT, TOPIC)
    store, telegram, poller, sender = build(whereami_config, [raw])
    await run(poller, sender)

    assert telegram.sent == []
    assert (
        "command_refused",
        None,
        f"command=whereami chat={PSR_CHAT} from={OTHER}",
    ) in store.audit_events()


async def test_dm_hint_for_allowed_and_refusal_for_others(whereami_config):
    raw = [dm_message(1, ALLOWED, "/whereami"), dm_message(2, OTHER, "/whereami")]
    store, telegram, poller, sender = build(whereami_config, raw)
    await run(poller, sender)

    assert sorted(replies(telegram)) == sorted(
        [(ALLOWED, "Send this in the group topic you want."), (OTHER, REFUSAL)]
    )
    events = [e for e, *_ in store.audit_events()]
    assert events.count("command_answered") == 1 and events.count("command_refused") == 1


async def test_an_edited_whereami_gets_no_second_reply(whereami_config):
    original = group_command(1, ALLOWED, "/whereami", PSR_CHAT, TOPIC)
    edit = {"update_id": 2, "edited_message": {**original["message"], "edit_date": 1789700100}}
    store, telegram, poller, sender = build(whereami_config, [original, edit])
    await run(poller, sender)

    assert len(telegram.sent) == 1
    answered = [e for e in store.audit_events() if e[0] == "command_answered"]
    assert len(answered) == 1


async def test_general_topic_says_1_general(whereami_config):
    raw = group_command(1, ALLOWED, "/whereami", PSR_CHAT, None)  # forum, no topic id sent
    _, telegram, poller, sender = build(whereami_config, [raw])
    await run(poller, sender)

    (sent,) = telegram.sent
    assert sent["text"].endswith("Topic id: <code>1</code> (General)")


async def test_group_without_topics_says_none(whereami_config):
    raw = group_command(1, ALLOWED, "/whereami", -1009999999999, None, forum=False)
    _, telegram, poller, sender = build(whereami_config, [raw])
    await run(poller, sender)

    (sent,) = telegram.sent
    assert sent["text"].endswith("Topic id: none (this group has no topics)")


async def test_whereami_is_in_each_allowed_dm_menu_and_never_group_scoped(whereami_config):
    store, telegram, _, _ = build(whereami_config, [])
    await register_menus(store, telegram, whereami_config)

    menu = [("whereami", "Where am I")]
    assert telegram.menus == {None: [], ALLOWED: menu, 5003: menu}
    assert all(chat is None or chat > 0 for chat in telegram.menus)  # no group-scoped menu


async def test_removing_whereami_from_config_removes_the_menu_next_boot(whereami_config, tmp_path):
    store, telegram, _, _ = build(whereami_config, [])
    await register_menus(store, telegram, whereami_config)
    removed = make_config(tmp_path, "commands: {}\n")
    await register_menus(store, telegram, removed)

    assert telegram.menus == {None: []}


def test_builtin_command_config_is_validated(tmp_path):
    from sixto_gateway.config import ConfigError

    for commands in (
        "commands: {jo: {builtin: true, allow: [psr]}}\n",  # not a known built-in
        "commands: {whereami: {builtin: true, module: jo}}\n",  # built-in names no module
        "commands: {jo: {allow: [psr]}}\n",  # a normal command needs a module
    ):
        with pytest.raises(ConfigError):
            make_config(tmp_path, commands)

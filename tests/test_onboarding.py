"""/start onboarding, first slice (#20): a Start becomes a pending member and an approval card."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from sixto_gateway.config import ConfigError, load_config
from sixto_gateway.notifier import Notifier
from sixto_gateway.notion import NotionContacts, NotionError, matches_handle
from sixto_gateway.onboarding import OnboardingWorker
from sixto_gateway.poller import Poller
from sixto_gateway.sender import Sender
from sixto_gateway.store import Store
from tests.fakes import (
    APPROVER,
    CONTACTS_SOURCE,
    INTAKE_CHAT,
    INTAKE_TOPIC,
    NOTION_ENV,
    NOTION_YAML,
    FakeClock,
    FakeNotion,
    FakeTelegram,
    button_tap,
    contact,
    dm_message,
    group_command,
)

USER = 6001
JUAN = contact("aaaaaaaa-0000-0000-0000-000000000001", "Juan Dela Cruz", handle="@JuanDC")
MARIA = contact(
    "aaaaaaaa-0000-0000-0000-000000000002", "Maria Santos", aliases="Ate Mia, @mariasan"
)
ENV = {"TELEGRAM_BOT_TOKEN": "1:tok", "WATCHDOG_SECRET": "w" * 40, **NOTION_ENV}


def start_from(
    update_id, user=USER, username="juandc", text="/start", first="Juan", last="Dela Cruz"
):
    raw = dm_message(update_id, user, "/start")
    sender = raw["message"]["from"]
    sender.update(first_name=first, last_name=last)
    if username is None:
        sender.pop("username")
    else:
        sender["username"] = username
    return raw


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "modules: {watchdog: {secret_env: WATCHDOG_SECRET}}\n"
        f"routes: [{{chat: {INTAKE_CHAT}, topic: {INTAKE_TOPIC}, module: watchdog}}]\n"
        f"database_path: {tmp_path / 'gateway.db'}\n" + NOTION_YAML
    )
    return load_config(path, ENV)


class Rig:
    def __init__(self, config, raw_updates, contacts):
        self.clock = FakeClock()
        self.telegram = FakeTelegram(list(raw_updates), self.clock)
        self.store = Store(config.database_path, clock=self.clock)
        self.notion = FakeNotion(contacts)
        self.queue_wake, self.onboarding_wake = Notifier(), Notifier()
        self.poller = Poller(
            self.store,
            self.telegram,
            config,
            Notifier(),
            clock=self.clock,
            queue_wake=self.queue_wake,
            onboarding_wake=self.onboarding_wake,
        )
        self.worker = OnboardingWorker(
            self.store,
            self.notion,
            config,
            self.onboarding_wake,
            self.queue_wake,
            self.clock,
            client=self.telegram,
        )
        self.sender = Sender(self.store, self.telegram, self.queue_wake, clock=self.clock)

    async def settle(self):
        while await self.poller.poll_once():
            pass
        while await self.worker.process_next():
            pass
        while await self.sender.send_next():
            pass

    def texts_to(self, chat):
        return [m["text"] for m in self.telegram.sent if m["chat_id"] == chat]

    def card(self):
        (card,) = [m for m in self.telegram.sent if m["chat_id"] == APPROVER]
        return card


def buttons(card):
    return [b for row in card["keyboard"] for b in row]


async def test_start_from_a_matching_handle_gives_a_waiting_reply_and_a_card(config):
    rig = Rig(config, [start_from(1)], [MARIA, JUAN])
    await rig.settle()

    member = rig.store.member(USER)
    assert member["status"] == "pending" and member["handle"] == "juandc"
    assert member["first_name"] == "Juan" and member["last_name"] == "Dela Cruz"
    assert member["candidates"] == [JUAN.page_id]
    assert rig.texts_to(USER) == [
        "Hi Juan! Thanks for pressing Start. Cyril will confirm who you are soon. "
        "After that, you will get your briefings here."
    ]
    card = rig.card()
    assert card["parse_mode"] == "HTML"
    assert "Telegram: Juan Dela Cruz · @juandc · id 6001" in card["text"]
    assert f'<a href="{JUAN.url}">Juan Dela Cruz</a>' in card["text"]
    assert [b["text"] for b in buttons(card)] == [
        "Approve Juan Dela Cruz",
        "Create new Contact",
        "Reject",
    ]
    assert all(len(b["callback_data"].encode()) <= 64 for b in buttons(card))
    assert member["card_message_id"] == card["message_id"]
    events = [e for e, _, _ in rig.store.audit_events()]
    for wanted in ("start", "reply", "lookup", "card_sent"):
        assert wanted in events
    assert "command_refused" not in events
    assert "That command isn't available to you." not in rig.texts_to(USER)


async def test_handle_matching_ignores_case_and_at_sign_and_reads_aliases(config):
    assert matches_handle(JUAN, "JUANDC") and matches_handle(JUAN, "@juandc")
    assert not matches_handle(JUAN, "juan") and not matches_handle(JUAN, "")
    assert matches_handle(MARIA, "MariaSan") and matches_handle(MARIA, "mia")

    rig = Rig(config, [start_from(1, username="MariaSAN")], [JUAN, MARIA])
    await rig.settle()
    assert rig.store.member(USER)["candidates"] == [MARIA.page_id]
    assert "Approve Maria Santos" in [b["text"] for b in buttons(rig.card())]


async def test_no_handle_means_no_match_and_nothing_is_picked(config):
    rig = Rig(config, [start_from(1, username=None)], [JUAN, MARIA])
    await rig.settle()
    assert rig.store.member(USER)["candidates"] == []
    card = rig.card()
    assert "Match: none found" in card["text"] and "(no @handle)" in card["text"]
    assert [b["text"] for b in buttons(card)] == ["Create new Contact", "Reject"]


async def test_several_matches_get_one_button_each_and_no_single_approve(config):
    twin = contact("aaaaaaaa-0000-0000-0000-000000000003", "Juan D.", aliases="juandc")
    rig = Rig(config, [start_from(1)], [JUAN, twin])
    await rig.settle()
    assert [b["text"] for b in buttons(rig.card())] == [
        "Pick Juan Dela Cruz",
        "Pick Juan D.",
        "Create new Contact",
        "Reject",
    ]


async def test_start_in_a_group_or_topic_gets_the_dm_me_reply_and_no_row(config):
    updates = [
        group_command(1, USER, "/start", INTAKE_CHAT, INTAKE_TOPIC),
        group_command(2, USER, "/start@sixto_t6bot", INTAKE_CHAT, None),
    ]
    rig = Rig(config, updates, [JUAN])
    await rig.settle()

    expected = "Please message me directly to set up your briefings: https://t.me/sixto_t6bot?start=onboard"
    assert [(m["chat_id"], m["text"]) for m in rig.telegram.sent] == [
        (INTAKE_CHAT, expected),
        (INTAKE_CHAT, expected),
    ]
    assert [m["topic_id"] for m in rig.telegram.sent] == [INTAKE_TOPIC, None]  # answered in place
    assert rig.store.member(USER) is None
    assert rig.notion.calls == 0


async def test_pressing_start_again_while_pending_sends_no_second_card(config):
    rig = Rig(config, [start_from(1)], [JUAN])
    await rig.settle()
    rig.clock.advance(10)  # past the per-chat send pacing
    rig.telegram.push(start_from(2))
    await rig.settle()

    assert (
        rig.texts_to(USER)[-1] == "Thanks, we already have your request. Cyril will confirm soon."
    )
    assert len(rig.texts_to(APPROVER)) == 1


async def test_an_edited_start_is_not_a_new_start(config):
    raw = dm_message(1, USER, "/start", edited=True)
    rig = Rig(config, [raw], [JUAN])
    await rig.settle()
    assert rig.store.member(USER) is None and rig.telegram.sent == []


async def test_polling_keeps_going_while_a_notion_lookup_is_slow(config):
    rig = Rig(config, [start_from(1)], [JUAN])
    rig.notion.block = asyncio.Event()  # Notion is hanging
    stop = asyncio.Event()
    worker = asyncio.create_task(rig.worker.run(stop))

    await rig.poller.poll_once()
    await asyncio.sleep(0.05)  # the worker is now stuck inside the lookup
    assert rig.notion.calls == 1 and rig.store.member(USER)["candidates"] is None

    watchdog_message = dm_message(2, USER, "hello")
    watchdog_message["message"]["chat"] = {"id": INTAKE_CHAT, "type": "supergroup", "title": "g"}
    watchdog_message["message"]["message_thread_id"] = INTAKE_TOPIC
    rig.telegram.push(watchdog_message)
    assert await rig.poller.poll_once() == 1  # polling is not held up
    assert len(rig.store.deliveries_after("watchdog", 0)) == 1

    rig.notion.block.set()
    await asyncio.sleep(0.05)
    stop.set()
    rig.onboarding_wake.notify()
    await asyncio.wait_for(worker, 2)
    assert rig.store.member(USER)["candidates"] == [JUAN.page_id]


async def test_a_failed_lookup_leaves_the_member_pending_and_is_retried(config):
    rig = Rig(config, [start_from(1)], [JUAN])
    rig.notion.fail = True
    await rig.settle()
    assert rig.store.member(USER)["candidates"] is None and rig.texts_to(APPROVER) == []
    assert len(rig.texts_to(USER)) == 1  # the waiting reply went out anyway

    rig.notion.fail = False
    await rig.settle()
    assert rig.notion.calls == 1  # not retried before the delay
    rig.clock.advance(61)
    await rig.settle()
    assert len(rig.texts_to(APPROVER)) == 1


# --- approving (#21) ------------------------------------------------------------------------


async def approved_rig(config, contacts=(JUAN, MARIA), extra=()):
    rig = Rig(config, [start_from(1), *extra], list(contacts))
    await rig.settle()
    return rig


def tap(rig, update_id, data, user=APPROVER):
    rig.telegram.push(button_tap(update_id, user, data, APPROVER, rig.card()["message_id"]))


def approve_data(rig, contact_=JUAN):
    return next(b["callback_data"] for b in buttons(rig.card()) if b["text"].startswith("Approve"))


async def test_approve_writes_notion_then_marks_the_row_approved_and_replies(config):
    rig = await approved_rig(config)
    rig.clock.advance(10)
    tap(rig, 2, approve_data(rig))
    await rig.settle()

    assert rig.notion.writes == [(JUAN.page_id, USER)]
    member = rig.store.member(USER)
    assert member["status"] == "approved" and member["contact_page_id"] == JUAN.page_id
    assert member["decided_by"] == APPROVER and member["decided_at"] is not None
    assert rig.texts_to(USER)[-1] == (
        "You are all set, Juan. Your briefings will come to this chat."
    )
    (edit,) = rig.telegram.edits
    assert edit["message_id"] == rig.card()["message_id"] and not edit["keyboard"]
    assert "✅ Approved:" in edit["text"] and "Juan Dela Cruz" in edit["text"]
    assert rig.telegram.answers == [{"callback_id": "cb2", "text": None, "alert": False}]
    events = [(e, d) for e, _, d in rig.store.audit_events()]
    assert any(e == "decision" and "approved" in d for e, d in events)
    assert any(e == "notion_write" and d.endswith(f"contact={JUAN.page_id}") for e, d in events)


async def test_a_second_tap_changes_nothing_and_rerenders_the_card(config):
    rig = await approved_rig(config)
    data = approve_data(rig)
    tap(rig, 2, data)
    await rig.settle()
    rig.clock.advance(10)
    tap(rig, 3, data)
    await rig.settle()

    assert rig.notion.writes == [(JUAN.page_id, USER)]
    assert len(rig.telegram.answers) == 2  # both taps were answered
    assert len(rig.telegram.edits) == 2 and "✅ Approved:" in rig.telegram.edits[1]["text"]
    assert (
        rig.texts_to(USER).count("You are all set, Juan. Your briefings will come to this chat.")
        == 1
    )


async def test_a_tap_from_anyone_but_the_approver_changes_nothing(config):
    rig = await approved_rig(config)
    tap(rig, 2, approve_data(rig), user=555)
    await rig.settle()

    assert rig.store.member(USER)["status"] == "pending"
    assert rig.notion.writes == [] and rig.telegram.edits == []
    assert [a["callback_id"] for a in rig.telegram.answers] == ["cb2"]  # spinner stops


async def test_a_failed_notion_write_leaves_the_row_pending_and_the_buttons_usable(config):
    rig = await approved_rig(config)
    rig.notion.fail_write = True
    tap(rig, 2, approve_data(rig))
    await rig.settle()

    assert rig.store.member(USER)["status"] == "pending" and rig.telegram.edits == []
    assert rig.telegram.answers[0]["alert"] is True
    assert any(e == "notion_write" and "failed" in d for e, _, d in rig.store.audit_events())

    rig.notion.fail_write = False
    tap(rig, 3, approve_data(rig))
    await rig.settle()
    assert rig.store.member(USER)["status"] == "approved"


async def test_a_contact_with_a_different_telegram_id_is_not_overwritten(config):
    taken = contact(JUAN.page_id, JUAN.name, handle="@JuanDC", user_id="987")
    rig = await approved_rig(config, contacts=(taken,))
    tap(rig, 2, approve_data(rig))
    await rig.settle()

    assert rig.notion.writes == [] and rig.store.member(USER)["status"] == "pending"
    assert rig.telegram.answers[0]["alert"] is True


async def test_a_contact_that_already_has_this_id_is_approved_without_a_second_write(config):
    same = contact(JUAN.page_id, JUAN.name, handle="@JuanDC", user_id=str(USER))
    rig = await approved_rig(config, contacts=(same,))
    tap(rig, 2, approve_data(rig))
    await rig.settle()
    assert rig.notion.writes == [] and rig.store.member(USER)["status"] == "approved"


async def test_one_contact_cannot_be_linked_to_two_users(config):
    rig = await approved_rig(config)
    tap(rig, 2, approve_data(rig))
    await rig.settle()
    rig.clock.advance(10)
    rig.telegram.push(start_from(3, user=6002, username="juandc", first="Other"))
    await rig.settle()
    (second,) = [
        m for m in rig.telegram.sent if m["chat_id"] == APPROVER and "id 6002" in m["text"]
    ]
    approve = next(b for b in second["keyboard"][0] if b["text"].startswith("Approve"))
    rig.telegram.push(
        button_tap(4, APPROVER, approve["callback_data"], APPROVER, second["message_id"])
    )
    await rig.settle()

    assert rig.store.member(6002)["status"] == "pending"
    assert rig.notion.writes == [(JUAN.page_id, USER)]
    assert rig.telegram.answers[-1]["alert"] is True


async def test_the_contact_to_user_lookup_returns_only_approved_members(config):
    rig = await approved_rig(config)
    assert rig.store.approved_user_for_contact(JUAN.page_id) is None  # pending
    tap(rig, 2, approve_data(rig))
    await rig.settle()
    assert rig.store.approved_user_for_contact(JUAN.page_id) == USER
    assert rig.store.approved_user_for_contact(JUAN.page_id.replace("-", "")) == USER
    assert rig.store.approved_user_for_contact(MARIA.page_id) is None


async def test_taps_are_stored_and_never_routed_to_a_module(config):
    rig = await approved_rig(config)
    tap(rig, 2, "something:else")
    await rig.poller.poll_once()
    assert rig.store.deliveries_after("watchdog", 0) == []
    assert rig.store.count_updates() == 2


async def test_callback_query_is_polled_by_default(config):
    assert "callback_query" in config.telegram.allowed_updates


async def test_the_adapter_writes_a_number_or_text_property_as_the_contact_has_it():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.read()))
        return httpx.Response(200, json=page("p-1", "Juan"))

    notion = notion_with(handler)
    await notion.set_telegram_user_id(contact("p-1", "Juan"), 42)
    await notion.set_telegram_user_id(replace(contact("p-2", "Mia"), user_id_kind="rich_text"), 7)
    await notion.close()

    assert seen[0][:2] == ("PATCH", "/v1/pages/p-1")
    assert json.loads(seen[0][2]) == {"properties": {"TelegramUserID": {"number": 42}}}
    assert json.loads(seen[1][2]) == {
        "properties": {
            "TelegramUserID": {"rich_text": [{"type": "text", "text": {"content": "7"}}]}
        }
    }


# --- config ---------------------------------------------------------------------------------


def test_boot_fails_naming_the_key_when_notion_api_key_is_missing(config, tmp_path):
    env = {k: v for k, v in ENV.items() if k != "NOTION_API_KEY"}
    with pytest.raises(ConfigError, match="NOTION_API_KEY is not set"):
        load_config(tmp_path / "config.yaml", env)


@pytest.mark.parametrize("section", ["notion", "onboarding"])
def test_boot_fails_when_a_config_section_is_missing(config, tmp_path, section):
    path = tmp_path / "config.yaml"
    kept = [line for line in path.read_text().splitlines() if not line.startswith(section)]
    path.write_text("\n".join(kept) + "\n")
    with pytest.raises(ConfigError, match=f"{section}: Field required"):
        load_config(path, ENV)


def test_a_bad_notion_value_is_reported_without_echoing_it(config, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(path.read_text().replace("2026-03-11", "leaky-secret-version"))
    with pytest.raises(ConfigError) as caught:
        load_config(path, ENV)
    assert "notion.version" in str(caught.value)
    assert "leaky-secret-version" not in str(caught.value)


def test_the_notion_key_is_scrubbed_from_redaction_and_never_shown(config):
    assert NOTION_ENV["NOTION_API_KEY"] in config.secrets.redaction_values()
    assert NOTION_ENV["NOTION_API_KEY"] not in repr(config.secrets)


# --- the Notion adapter ---------------------------------------------------------------------


def page(page_id, name, handle="", aliases="", user_id=None):
    def rich(text):
        return {"type": "rich_text", "rich_text": [{"plain_text": text}] if text else []}

    return {
        "id": page_id,
        "url": f"https://www.notion.so/{page_id.replace('-', '')}",
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": name}]},
            "TelegramHandle": rich(handle),
            "Aliases": rich(aliases),
            "TelegramUserID": {"type": "number", "number": user_id},
        },
    }


def notion_with(handler):
    transport = httpx.MockTransport(handler)
    return NotionContacts("ntn_secret", CONTACTS_SOURCE, "2026-03-11", transport=transport)


async def test_the_adapter_reads_every_page_of_contacts_with_the_pinned_version():
    seen = []

    def handler(request):
        seen.append(request)
        body = request.read()
        if b"start_cursor" not in body:
            return httpx.Response(
                200,
                json={
                    "results": [page("p-1", "Juan", "@juandc", user_id=42)],
                    "has_more": True,
                    "next_cursor": "c2",
                },
            )
        return httpx.Response(
            200, json={"results": [page("p-2", "Maria", aliases="mia")], "has_more": False}
        )

    notion = NotionContacts(
        "ntn_secret", CONTACTS_SOURCE, "2026-03-11", transport=httpx.MockTransport(handler)
    )
    contacts = await notion.list_contacts()
    await notion.close()

    assert [(c.name, c.telegram_handle, c.aliases, c.telegram_user_id) for c in contacts] == [
        ("Juan", "@juandc", "", "42"),
        ("Maria", "", "mia", ""),
    ]
    assert seen[0].url.path == f"/v1/data_sources/{CONTACTS_SOURCE}/query"
    assert seen[0].headers["Notion-Version"] == "2026-03-11"
    assert seen[0].headers["Authorization"] == "Bearer ntn_secret"


@pytest.mark.parametrize(
    "response",
    [httpx.Response(503, json={"code": "service_unavailable"}), httpx.ConnectError("boom")],
)
async def test_adapter_errors_never_carry_the_key(response):
    def handler(request):
        if isinstance(response, Exception):
            raise response
        return response

    notion = NotionContacts(
        "ntn_secret", CONTACTS_SOURCE, "2026-03-11", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(NotionError) as caught:
        await notion.list_contacts()
    assert "ntn_secret" not in str(caught.value)
    await notion.close()

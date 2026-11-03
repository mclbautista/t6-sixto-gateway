"""/start onboarding, first slice (#20): a Start becomes a pending member and an approval card."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from sixto_gateway import strings
from sixto_gateway.config import ConfigError, load_config
from sixto_gateway.notifier import Notifier
from sixto_gateway.notion import NotionContacts, NotionError, closest_contacts, matches_handle
from sixto_gateway.onboarding import OnboardingWorker, callback_data
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
        self.sleeps: list[float] = []  # waits the worker asked for between Notion write tries

        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        self.config = config
        self.worker = OnboardingWorker(
            self.store,
            self.notion,
            config,
            self.onboarding_wake,
            self.queue_wake,
            self.clock,
            client=self.telegram,
            sleep=sleep,
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


async def test_no_handle_and_no_close_name_means_no_candidates(config):
    rig = Rig(config, [start_from(1, username=None, first="Zed", last="Quill")], [JUAN, MARIA])
    await rig.settle()
    assert rig.store.member(USER)["candidates"] == []
    card = rig.card()
    assert "Match: none found" in card["text"] and "(no @handle)" in card["text"]
    assert [b["text"] for b in buttons(card)] == ["Create new Contact", "Reject"]


async def test_no_handle_lists_the_closest_names_and_aliases_and_picks_nothing(config):
    rig = Rig(config, [start_from(1, username=None, first="Mia", last="")], [JUAN, MARIA])
    await rig.settle()
    card = rig.card()
    assert "Closest by name:" in card["text"] and "Maria Santos" in card["text"]
    assert [b["text"] for b in buttons(card)] == [
        "Pick Maria Santos",
        "Create new Contact",
        "Reject",
    ]
    assert rig.store.member(USER)["status"] == "pending" and rig.notion.writes == []


async def test_a_handle_with_no_match_falls_back_to_the_closest_names(config):
    rig = Rig(config, [start_from(1, username="somebody_else")], [JUAN, MARIA])
    await rig.settle()
    card = rig.card()
    assert "Closest by name:" in card["text"] and "Juan Dela Cruz" in card["text"]
    assert "Pick Juan Dela Cruz" in [b["text"] for b in buttons(card)]
    assert not any(b["text"].startswith("Approve") for b in buttons(card))


async def test_picking_a_closest_name_links_that_contact(config):
    rig = Rig(config, [start_from(1, username=None, first="Mia", last="")], [JUAN, MARIA])
    await rig.settle()
    pick = next(b for b in buttons(rig.card()) if b["text"].startswith("Pick"))
    tap(rig, 2, pick["callback_data"])
    await rig.settle()
    assert rig.notion.writes == [(MARIA.page_id, USER)]
    assert rig.store.member(USER)["contact_page_id"] == MARIA.page_id


def test_closest_contacts_ranks_names_and_aliases_and_ignores_honorifics():
    kuya = contact("aaaaaaaa-0000-0000-0000-000000000004", "Pio Reyes", aliases="Kuya Pio")
    ranked = closest_contacts([JUAN, MARIA, kuya], "ate mia")
    assert [c.name for c in ranked] == ["Maria Santos"]
    assert [c.name for c in closest_contacts([JUAN, MARIA, kuya], "Pio")] == ["Pio Reyes"]
    assert closest_contacts([JUAN, MARIA], "") == []
    many = [contact(f"aaaaaaaa-0000-0000-0000-{i:012d}", f"Juan {i}") for i in range(10)]
    assert len(closest_contacts(many, "Juan")) == 3


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


async def test_a_failed_notion_write_is_retried_then_the_card_offers_retry(config):
    rig = await approved_rig(config)
    rig.notion.fail_write = True
    tap(rig, 2, approve_data(rig))
    await rig.settle()

    assert rig.sleeps == [1.0, 5.0, 15.0]  # three retries after the first try
    assert rig.store.member(USER)["status"] == "pending"
    edit = rig.telegram.edits[-1]
    assert "Write failed" in edit["text"]
    assert [b["text"] for b in buttons(edit)] == ["Retry"]
    assert any(e == "notion_write" and "failed" in d for e, _, d in rig.store.audit_events())

    rig.notion.fail_write = False
    retry = buttons(edit)[0]["callback_data"]
    tap(rig, 3, retry)
    await rig.settle()
    assert rig.store.member(USER)["status"] == "approved"
    assert "✅ Approved:" in rig.telegram.edits[-1]["text"]


async def test_a_write_that_works_on_a_retry_needs_no_retry_button(config):
    rig = await approved_rig(config)
    real = rig.notion.set_telegram_user_id
    calls = []

    async def flaky(contact, user_id):
        calls.append(1)
        if len(calls) < 3:
            raise NotionError("Notion answered 503 (service_unavailable)")
        await real(contact, user_id)

    rig.notion.set_telegram_user_id = flaky
    tap(rig, 2, approve_data(rig))
    await rig.settle()
    assert rig.sleeps == [1.0, 5.0]
    assert rig.store.member(USER)["status"] == "approved"
    assert rig.notion.writes == [(JUAN.page_id, USER)]


def replace_id(c, new_id):
    return replace(c, telegram_user_id=new_id)


TAKEN = contact(JUAN.page_id, JUAN.name, handle="@JuanDC", user_id="987")


async def test_a_conflicting_id_shows_a_warning_and_a_replace_id_button(config):
    rig = await approved_rig(config, contacts=(TAKEN,))
    card = rig.card()
    assert "⚠ This Contact already has Telegram id 987" in card["text"]
    assert [b["text"] for b in buttons(card)] == ["Replace ID", "Create new Contact", "Reject"]
    assert rig.notion.writes == [] and rig.store.member(USER)["status"] == "pending"


async def test_only_the_replace_id_tap_overwrites_the_old_id(config):
    rig = await approved_rig(config, contacts=(TAKEN,))
    # a plain Approve payload for the same Contact (an old or forged card) changes nothing
    tap(rig, 2, callback_data("ok", USER, TAKEN.page_id))
    await rig.settle()
    assert rig.notion.writes == [] and rig.store.member(USER)["status"] == "pending"
    assert rig.telegram.answers[-1]["alert"] is True

    replace = next(b for b in buttons(rig.card()) if b["text"] == "Replace ID")
    tap(rig, 3, replace["callback_data"])
    await rig.settle()
    assert rig.notion.writes == [(JUAN.page_id, USER)]
    assert rig.store.member(USER)["status"] == "approved"
    assert any(
        e == "notion_write" and "replaced_id=987" in d for e, _, d in rig.store.audit_events()
    )
    assert "✅ Approved:" in rig.telegram.edits[-1]["text"]


async def test_replace_id_refuses_when_the_id_changed_since_the_card_was_sent(config):
    rig = await approved_rig(config, contacts=(TAKEN,))
    replace = next(b for b in buttons(rig.card()) if b["text"] == "Replace ID")
    rig.notion.contacts = [replace_id(TAKEN, "555")]  # someone edited Notion meanwhile
    tap(rig, 2, replace["callback_data"])
    await rig.settle()
    assert rig.notion.writes == [] and rig.store.member(USER)["status"] == "pending"
    assert rig.telegram.answers[-1]["alert"] is True


async def test_replace_id_names_the_candidate_when_there_are_several(config):
    twin = contact("aaaaaaaa-0000-0000-0000-000000000003", "Juan D.", aliases="juandc")
    rig = await approved_rig(config, contacts=(TAKEN, twin))
    assert [b["text"] for b in buttons(rig.card())] == [
        "Replace ID: Juan Dela Cruz",
        "Pick Juan D.",
        "Create new Contact",
        "Reject",
    ]
    assert rig.card()["text"].count("⚠") == 1


async def test_replace_id_cannot_take_a_contact_linked_to_another_user(config):
    rig = await approved_rig(config)
    tap(rig, 2, approve_data(rig))
    await rig.settle()
    rig.clock.advance(10)
    rig.telegram.push(start_from(3, user=6002, username="juandc", first="Other"))
    await rig.settle()
    (second,) = [
        m for m in rig.telegram.sent if m["chat_id"] == APPROVER and "id 6002" in m["text"]
    ]
    assert f"already has Telegram id {USER}" in second["text"]
    replace = next(b for b in buttons(second) if b["text"] == "Replace ID")
    rig.telegram.push(
        button_tap(4, APPROVER, replace["callback_data"], APPROVER, second["message_id"])
    )
    await rig.settle()
    assert rig.store.member(6002)["status"] == "pending"
    assert rig.notion.writes == [(JUAN.page_id, USER)]
    assert rig.telegram.answers[-1]["alert"] is True


async def test_a_contact_that_already_has_this_id_is_approved_with_no_card_and_no_write(config):
    holder = contact("aaaaaaaa-0000-0000-0000-000000000009", "Juan Dela Cruz", user_id=str(USER))
    rig = Rig(config, [start_from(1, username="renamed")], [holder, MARIA])
    await rig.settle()
    rig.clock.advance(10)  # the sender paces messages to one chat
    await rig.settle()

    member = rig.store.member(USER)
    assert member["status"] == "approved" and member["contact_page_id"] == holder.page_id
    assert member["decided_by"] is None
    assert rig.notion.writes == [] and rig.notion.created == []
    assert rig.texts_to(USER) == [
        strings.WAITING.format(first_name="Juan"),
        strings.APPROVED.format(first_name="Juan"),
    ]
    (fyi,) = rig.texts_to(APPROVER)  # one line, and no buttons
    assert "Juan Dela Cruz" in fyi and "already had this Telegram id" in fyi
    assert not any(m.get("keyboard") for m in rig.telegram.sent if m["chat_id"] == APPROVER)
    assert any(e == "decision" for e, _, _ in rig.store.audit_events())


async def test_two_contacts_with_this_id_are_left_to_the_approver(config):
    one = contact("aaaaaaaa-0000-0000-0000-000000000009", "Juan A", user_id=str(USER))
    two = contact("aaaaaaaa-0000-0000-0000-000000000010", "Juan B", user_id=str(USER))
    rig = Rig(config, [start_from(1)], [one, two, JUAN])
    await rig.settle()
    assert rig.store.member(USER)["status"] == "pending"
    assert rig.card()["keyboard"]


async def test_a_contact_with_this_id_that_belongs_to_another_member_is_left_to_the_approver(
    config,
):
    rig = await approved_rig(config)
    tap(rig, 2, approve_data(rig))
    await rig.settle()
    rig.telegram.push(start_from(3, user=6002, username="juandc", first="Other"))
    rig.notion.contacts = [
        replace(c, telegram_user_id="6002") if c.page_id == JUAN.page_id else c
        for c in rig.notion.contacts
    ]
    await rig.settle()
    assert rig.store.member(6002)["status"] == "pending"


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


async def test_a_tap_with_a_foreign_payload_is_still_answered(config):
    rig = await approved_rig(config)
    tap(rig, 2, "something:else")
    await rig.settle()
    assert [a["callback_id"] for a in rig.telegram.answers] == ["cb2"]
    assert rig.store.member(USER)["status"] == "pending"


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


# --- rejecting, undoing and creating (#22) --------------------------------------------------


def data_of(card, label):
    return next(b["callback_data"] for b in buttons(card) if b["text"].startswith(label))


async def test_reject_marks_the_row_replies_neutrally_and_leaves_notion_alone(config):
    rig = await approved_rig(config)
    rig.clock.advance(10)  # past the pacing gap after the waiting reply
    tap(rig, 2, data_of(rig.card(), "Reject"))
    await rig.settle()

    member = rig.store.member(USER)
    assert member["status"] == "rejected" and member["decided_by"] == APPROVER
    assert rig.texts_to(USER)[-1] == strings.REJECTED
    assert rig.notion.writes == [] and rig.notion.created == []
    (edit,) = rig.telegram.edits
    assert "Rejected" in edit["text"]
    assert [b["text"] for row in edit["keyboard"] for b in row] == ["Undo"]
    assert any(e == "decision" and "rejected" in d for e, _, d in rig.store.audit_events())


async def test_undo_puts_the_row_back_to_pending_with_the_original_buttons(config):
    rig = await approved_rig(config, contacts=(JUAN, MARIA, contact("p-3", "Juan Two", "@juandc")))
    original = rig.card()
    tap(rig, 2, data_of(original, "Reject"))
    await rig.settle()
    rig.clock.advance(10)
    tap(rig, 3, callback_for(rig, "undo"))
    await rig.settle()

    assert rig.store.member(USER)["status"] == "pending"
    undone = rig.telegram.edits[-1]
    assert undone["text"] == original["text"] and undone["keyboard"] == original["keyboard"]
    assert any(e == "undo" for e, _, _ in rig.store.audit_events())
    assert rig.notion.calls >= 2  # the card is rebuilt from the stored candidates

    tap(rig, 4, data_of(undone, "Pick Juan Dela"))  # a decision works again after Undo
    await rig.settle()
    assert rig.store.member(USER)["status"] == "approved"


async def test_undo_then_approve_works_as_in_approving(config):
    rig = await approved_rig(config)
    rig.clock.advance(10)  # past the pacing gap after the waiting reply
    tap(rig, 2, data_of(rig.card(), "Reject"))
    tap(rig, 3, callback_for(rig, "undo"))
    tap(rig, 4, approve_data(rig))
    await rig.settle()
    rig.clock.advance(10)  # the sender paces messages to one chat
    await rig.settle()

    assert rig.notion.writes == [(JUAN.page_id, USER)]
    assert rig.store.member(USER)["status"] == "approved"
    # rejecting never repeats: the person was told once, then told they are all set
    assert rig.texts_to(USER)[-1].startswith("You are all set")


def callback_for(rig, action):
    return f"ob:{action}:{USER}"


async def test_rejecting_twice_with_an_undo_between_tells_the_person_once(config):
    rig = await approved_rig(config)
    rig.clock.advance(10)  # past the pacing gap after the waiting reply
    reject = data_of(rig.card(), "Reject")
    tap(rig, 2, reject)
    tap(rig, 3, callback_for(rig, "undo"))
    tap(rig, 4, reject)
    await rig.settle()

    assert rig.store.member(USER)["status"] == "rejected"
    assert rig.texts_to(USER).count(strings.REJECTED) == 1


async def test_undo_with_notion_down_changes_nothing_and_can_be_retried(config):
    rig = await approved_rig(config)
    tap(rig, 2, data_of(rig.card(), "Reject"))
    await rig.settle()
    rig.notion.fail = True
    tap(rig, 3, callback_for(rig, "undo"))
    await rig.settle()
    assert rig.store.member(USER)["status"] == "rejected" and rig.telegram.answers[-1]["alert"]

    rig.notion.fail = False
    tap(rig, 4, callback_for(rig, "undo"))
    await rig.settle()
    assert rig.store.member(USER)["status"] == "pending"


async def test_create_new_makes_a_contact_with_exactly_the_listed_fields(config):
    rig = await approved_rig(config)
    rig.clock.advance(10)  # past the pacing gap after the waiting reply
    tap(rig, 2, data_of(rig.card(), "Create new"))
    await rig.settle()

    (made,) = rig.notion.created
    assert (made.name, made.telegram_handle, made.telegram_user_id) == (
        "Juan Dela Cruz",
        "juandc",
        str(USER),
    )
    assert rig.notion.writes == []
    assert rig.notion.notes == ["Created by Sixto /start on 2026-09-18."]
    member = rig.store.member(USER)
    assert member["status"] == "approved" and member["contact_page_id"] == made.page_id
    assert rig.texts_to(USER)[-1].startswith("You are all set, Juan.")
    assert "✅ Approved:" in rig.telegram.edits[-1]["text"]
    assert any(e == "notion_write" and "created" in d for e, _, d in rig.store.audit_events())


async def test_a_failed_create_is_retried_then_the_card_offers_retry(config):
    rig = await approved_rig(config)
    rig.notion.fail_create = True
    tap(rig, 2, data_of(rig.card(), "Create new"))
    await rig.settle()
    assert rig.sleeps == [1.0, 5.0, 15.0]
    assert rig.store.member(USER)["status"] == "pending"
    edit = rig.telegram.edits[-1]
    assert "Write failed" in edit["text"] and [b["text"] for b in buttons(edit)] == ["Retry"]

    rig.notion.fail_create = False
    tap(rig, 3, buttons(edit)[0]["callback_data"])
    await rig.settle()
    assert rig.store.member(USER)["status"] == "approved"
    assert len(rig.notion.created) == 1


async def test_create_reuses_a_contact_left_by_an_earlier_create_instead_of_making_a_second(
    config,
):
    rig = await approved_rig(config)
    orphan = contact("bbbbbbbb-0000-0000-0000-000000000009", "Juan Dela Cruz", user_id=str(USER))
    rig.notion.contacts = [*rig.notion.contacts, orphan]  # made, but the approval never finished
    tap(rig, 2, data_of(rig.card(), "Create new"))
    await rig.settle()

    assert rig.notion.created == []
    member = rig.store.member(USER)
    assert member["status"] == "approved" and member["contact_page_id"] == orphan.page_id


async def test_reject_undo_and_create_ignore_taps_from_anyone_but_the_approver(config):
    rig = await approved_rig(config)
    for n, data in enumerate((data_of(rig.card(), "Reject"), data_of(rig.card(), "Create new"))):
        tap(rig, 2 + n, data, user=555)
    await rig.settle()
    assert rig.store.member(USER)["status"] == "pending"
    assert rig.notion.created == [] and rig.telegram.edits == []

    tap(rig, 5, data_of(rig.card(), "Reject"))
    await rig.settle()
    tap(rig, 6, callback_for(rig, "undo"), user=555)
    await rig.settle()
    assert rig.store.member(USER)["status"] == "rejected"


async def test_a_stale_reject_tap_after_approval_changes_nothing(config):
    rig = await approved_rig(config)
    reject = data_of(rig.card(), "Reject")
    tap(rig, 2, approve_data(rig))
    await rig.settle()
    tap(rig, 3, reject)
    await rig.settle()
    assert rig.store.member(USER)["status"] == "approved"
    assert rig.texts_to(USER).count(strings.REJECTED) == 0


async def test_the_adapter_creates_a_contact_page_in_the_data_source():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.read()))
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "properties": {
                        "TelegramUserID": {"type": "rich_text"},
                        "TelegramHandle": {"type": "rich_text"},
                    }
                },
            )
        return httpx.Response(200, json=page("p-9", "Juan", "juandc", user_id="42"))

    notion = notion_with(handler)
    made = await notion.create_contact("Juan", "juandc", 42, "Created by Sixto /start on X.")
    await notion.close()

    assert made.page_id == "p-9"
    assert seen[0][:2] == ("GET", f"/v1/data_sources/{CONTACTS_SOURCE}")
    assert seen[1][:2] == ("POST", "/v1/pages")
    body = json.loads(seen[1][2])
    assert body["parent"] == {"type": "data_source_id", "data_source_id": CONTACTS_SOURCE}
    assert body["properties"] == {
        "Name": {"title": [{"type": "text", "text": {"content": "Juan"}}]},
        "TelegramUserID": {"rich_text": [{"type": "text", "text": {"content": "42"}}]},
        "TelegramHandle": {"rich_text": [{"type": "text", "text": {"content": "juandc"}}]},
    }
    (block,) = body["children"]
    assert block["paragraph"]["rich_text"][0]["text"]["content"] == "Created by Sixto /start on X."


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


# --- repeat presses and blocked users (#26) ---------------------------------------------------


async def press_again(rig, update_id, **kwargs):
    rig.clock.advance(10)  # past the per-chat send pacing
    rig.telegram.push(start_from(update_id, **kwargs))
    await rig.settle()


async def onboarded_rig(config):
    rig = await approved_rig(config)
    rig.clock.advance(10)
    tap(rig, 2, approve_data(rig))
    await rig.settle()
    assert rig.store.member(USER)["status"] == "approved"
    return rig


async def test_start_again_when_approved_says_already_onboarded_with_no_card_and_no_lookup(config):
    rig = await onboarded_rig(config)
    calls, cards = rig.notion.calls, len(rig.texts_to(APPROVER))
    await press_again(rig, 3)

    assert rig.texts_to(USER)[-1] == strings.ALREADY_ONBOARDED
    assert len(rig.texts_to(APPROVER)) == cards and rig.notion.calls == calls
    assert rig.store.member(USER)["status"] == "approved"


async def test_a_handle_change_after_approval_still_gets_already_onboarded(config):
    rig = await onboarded_rig(config)
    await press_again(rig, 3, username="juan_new_name")

    assert rig.texts_to(USER)[-1] == strings.ALREADY_ONBOARDED
    member = rig.store.member(USER)
    assert member["status"] == "approved" and member["contact_page_id"] == JUAN.page_id
    assert len(rig.texts_to(APPROVER)) == 1


async def test_start_again_when_rejected_gets_the_neutral_reply_and_no_card(config):
    rig = await approved_rig(config)
    rig.clock.advance(10)
    tap(rig, 2, data_of(rig.card(), "Reject"))
    await rig.settle()
    cards = len(rig.texts_to(APPROVER))
    await press_again(rig, 3)

    assert rig.texts_to(USER)[-2:] == [strings.REJECTED, strings.REJECTED]
    assert len(rig.texts_to(APPROVER)) == cards
    assert rig.store.member(USER)["status"] == "rejected"


async def test_a_permanent_block_error_on_a_send_to_a_member_marks_them_blocked(config):
    from sixto_gateway.telegram.errors import BlockedByUserError

    rig = await onboarded_rig(config)
    rig.clock.advance(10)
    rig.telegram.fail_with = BlockedByUserError("bot was blocked by the user")
    rig.store.enqueue_outbound(
        "watchdog",
        "message",
        str(USER),
        {"chat": USER, "topic": None, "text": "hi", "format": "plain"},
    )
    await rig.settle()

    assert rig.store.member(USER)["status"] == "blocked"
    assert any(e == "blocked" and f"user={USER}" in d for e, _, d in rig.store.audit_events())


async def test_a_block_error_for_a_pending_member_or_a_group_changes_no_row(config):
    rig = Rig(config, [start_from(1)], [JUAN])
    await rig.settle()
    assert rig.store.block_member(USER) is False  # pending: the approver still decides
    assert rig.store.block_member(INTAKE_CHAT) is False  # no such member
    assert rig.store.member(USER)["status"] == "pending"


async def test_a_blocked_member_pressing_start_goes_back_to_approved_with_no_card(config):
    rig = await onboarded_rig(config)
    rig.store.block_member(USER)
    cards, calls = len(rig.texts_to(APPROVER)), rig.notion.calls
    await press_again(rig, 3)

    assert rig.store.member(USER)["status"] == "approved"
    assert rig.texts_to(USER)[-1] == strings.ALREADY_ONBOARDED
    assert len(rig.texts_to(APPROVER)) == cards and rig.notion.calls == calls


async def test_pressing_start_many_times_while_pending_gives_the_approver_one_card(config):
    rig = Rig(config, [start_from(1)], [JUAN])
    await rig.settle()
    for n in (2, 3, 4):
        await press_again(rig, n)

    assert len(rig.texts_to(APPROVER)) == 1
    assert rig.texts_to(USER).count(strings.PENDING_AGAIN) == 3


def test_a_telegram_403_is_translated_to_a_block_error():
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendMessage

    from sixto_gateway.telegram.client import translate_send_error
    from sixto_gateway.telegram.errors import BlockedByUserError, PermanentSendError

    error = translate_send_error(
        TelegramForbiddenError(SendMessage(chat_id=1, text="x"), "blocked")
    )
    assert isinstance(error, BlockedByUserError) and isinstance(error, PermanentSendError)


# --- surviving failures (#27) -----------------------------------------------------------------


async def test_lookups_are_retried_after_1_5_and_30_minutes_then_a_failure_card_is_sent(config):
    rig = Rig(config, [start_from(1)], [JUAN])
    rig.notion.fail = True
    await rig.settle()
    assert rig.notion.calls == 1
    for minutes, calls in ((0.5, 1), (1, 2), (4.9, 2), (5, 3), (29, 3)):
        rig.clock.now = 1_789_700_000.0 + minutes * 60
        await rig.settle()
        assert rig.notion.calls == calls, minutes
    assert rig.texts_to(APPROVER) == []

    rig.clock.now = 1_789_700_000.0 + 30 * 60
    await rig.settle()
    assert rig.notion.calls == 4
    card = rig.card()
    assert "Lookup failed" in card["text"]
    assert [b["text"] for b in buttons(card)] == ["Retry", "Create new Contact", "Reject"]
    assert len(rig.texts_to(USER)) == 1  # only the waiting reply, sent at the first Start
    assert rig.store.member(USER)["status"] == "pending"

    rig.clock.advance(3600)
    await rig.settle()
    assert rig.notion.calls == 4  # no more automatic tries


async def test_lookup_retries_resume_after_a_restart(config):
    rig = Rig(config, [start_from(1)], [JUAN])
    rig.notion.fail = True
    await rig.settle()
    rig.clock.advance(61)
    await rig.settle()
    assert rig.notion.calls == 2
    rig.store.close()

    again = Rig(config, [], [JUAN])  # a new process on the same database
    again.clock.now = rig.clock.now
    again.notion.fail = True
    await again.settle()
    assert again.notion.calls == 0  # the 5-minute step is not due yet
    again.clock.advance(5 * 60)
    await again.settle()
    assert again.notion.calls == 1
    assert again.store.member(USER)["lookup_attempts"] == 3


async def exhausted_rig(config, contacts=(JUAN,)):
    rig = Rig(config, [start_from(1)], list(contacts))
    rig.notion.fail = True
    for offset in (0, 60, 300, 1800):
        rig.clock.now = 1_789_700_000.0 + offset
        await rig.settle()
    assert "Lookup failed" in rig.card()["text"]
    return rig


async def test_retry_on_the_failure_card_looks_the_person_up_again(config):
    rig = await exhausted_rig(config)
    retry = data_of(rig.card(), "Retry")

    tap(rig, 2, retry)  # Notion is still down
    await rig.settle()
    assert rig.telegram.answers[-1]["alert"] is True
    assert rig.telegram.edits == [] and rig.store.member(USER)["candidates"] == []

    rig.notion.fail = False
    tap(rig, 3, retry)
    await rig.settle()
    edit = rig.telegram.edits[-1]
    assert "Lookup failed" not in edit["text"] and "Juan Dela Cruz" in edit["text"]
    assert [b["text"] for b in buttons(edit)][0] == "Approve Juan Dela Cruz"
    assert rig.store.member(USER)["candidates"] == [JUAN.page_id]

    tap(rig, 4, data_of(edit, "Approve Juan Dela Cruz"))
    await rig.settle()
    assert rig.store.member(USER)["status"] == "approved"


async def test_create_new_and_reject_work_on_the_failure_card(config):
    rig = await exhausted_rig(config)
    rig.notion.fail = False
    tap(rig, 2, data_of(rig.card(), "Create new Contact"))
    await rig.settle()
    assert rig.store.member(USER)["status"] == "approved" and len(rig.notion.created) == 1


async def test_reject_works_on_the_failure_card(config):
    rig = await exhausted_rig(config)
    tap(rig, 2, data_of(rig.card(), "Reject"))
    await rig.settle()
    assert rig.store.member(USER)["status"] == "rejected"
    assert [b["text"] for b in buttons(rig.telegram.edits[-1])] == ["Undo"]


async def test_a_card_that_cannot_be_sent_leaves_the_row_pending_and_is_audited(config):
    rig = Rig(config, [start_from(1)], [JUAN])
    await rig.poller.poll_once()
    while await rig.worker.process_next():
        pass
    rig.telegram.fail_always = RuntimeError("network down")
    for _ in range(12):  # both queued messages: first try plus the whole retry schedule
        rig.clock.advance(700)
        await rig.sender.send_next()
    assert rig.store.member(USER)["status"] == "pending"
    failures = rig.store.send_failures_since(0)
    assert any("network down" in f for f in failures)

"""Daily onboarding summary for the approver (#25)."""

from datetime import datetime, timedelta, timezone

import pytest

from sixto_gateway.config import ConfigError, load_config
from sixto_gateway.summary import SummaryWorker
from tests.fakes import (
    APPROVER,
    CONTACTS_SOURCE,
    INTAKE_CHAT,
    INTAKE_TOPIC,
    contact,
)
from tests.test_onboarding import ENV, USER, Rig, start_from

MANILA = timezone(timedelta(hours=8))
EIGHT = datetime(2026, 11, 4, 8, 59, tzinfo=MANILA).timestamp()
NINE = datetime(2026, 11, 4, 9, 0, tzinfo=MANILA).timestamp()
JUAN = contact("aaaaaaaa-0000-0000-0000-000000000001", "Juan Dela Cruz", handle="@JuanDC")


def make_config(tmp_path, onboarding="", name="config.yaml"):
    path = tmp_path / name
    path.write_text(
        "modules: {watchdog: {secret_env: WATCHDOG_SECRET}}\n"
        f"routes: [{{chat: {INTAKE_CHAT}, topic: {INTAKE_TOPIC}, module: watchdog}}]\n"
        f"database_path: {tmp_path / 'gateway.db'}\n"
        f"notion: {{contacts_data_source_id: {CONTACTS_SOURCE}, version: '2026-03-11'}}\n"
        f"onboarding: {{approver: {APPROVER}{onboarding}}}\n"
    )
    return path


@pytest.fixture
def config(tmp_path):
    expected = ", expected: ['@ALICE_DEMO', juandc, carol_demo]"
    return load_config(make_config(tmp_path, expected), ENV)


def worker_for(rig, config):
    return SummaryWorker(rig.store, config, rig.queue_wake, rig.clock)


async def test_at_nine_manila_one_summary_is_queued_with_counts_pending_failures_and_missing(
    config,
):
    rig = Rig(config, [start_from(1)], [JUAN])
    rig.clock.now = EIGHT - 3 * 86400
    await rig.settle()  # JUAN is pending with a sent card
    rig.store.audit("send_failed", "watchdog", "ref=9 attempts=5 TimeoutError: <slow>")
    rig.clock.now = NINE
    rig.store.audit("send_failed", "gateway", "ref=10 attempts=1 Forbidden: blocked")
    summary = worker_for(rig, config)

    assert summary.send_due() is True
    (row,) = [r for r in rig.store.due_outbound(NINE) if r["body"]["chat"] == APPROVER]
    text = row["body"]["text"]
    assert row["body"]["format"] == "html"
    assert "Onboarding summary, 2026-11-04" in text
    assert "Approved: 0 · Pending: 1 · Rejected: 0 · Blocked: 0" in text
    assert "Juan Dela Cruz @juandc, waiting 3 days" in text
    card_id = rig.store.member(USER)["card_message_id"]
    assert f'<a href="tg://openmessage?user_id=1&message_id={card_id}">open card</a>' in text
    assert "failed in the last 24 hours: 1" in text  # the 3-day-old one is out of the window
    assert "gateway: ref=10 attempts=1 Forbidden: blocked" in text
    assert "ref=9" not in text
    assert "Still missing, no approved member yet: @alice_demo, @juandc, @carol_demo" in text


async def test_nothing_is_queued_before_the_time(config):
    rig = Rig(config, [], [])
    rig.clock.now = EIGHT
    assert worker_for(rig, config).send_due() is False
    assert not rig.store.has_outbound("gateway", "summary:2026-11-04")


async def test_an_empty_day_says_so_and_approved_members_are_not_missing(config):
    rig = Rig(config, [start_from(1)], [JUAN])
    await rig.settle()
    rig.store.audit("start")  # unrelated rows do not matter
    # approve Juan through the store, as a tap would
    reply = {"chat": USER, "topic": None, "text": "ok", "format": "plain"}
    assert rig.store.approve_member(USER, JUAN.page_id, APPROVER, reply)
    rig.clock.now = NINE
    worker_for(rig, config).send_due()
    (row,) = [r for r in rig.store.due_outbound(NINE) if "summary" in r["body"]["text"].lower()]
    text = row["body"]["text"]
    assert "Approved: 1 · Pending: 0" in text
    assert "No one is waiting for you." in text
    assert "No messages failed in the last 24 hours." in text
    assert "@juandc" not in text and "@alice_demo, @carol_demo" in text


async def test_without_an_expected_list_there_is_no_missing_section(tmp_path):
    config = load_config(make_config(tmp_path), ENV)
    rig = Rig(config, [], [])
    rig.clock.now = NINE
    worker_for(rig, config).send_due()
    (row,) = rig.store.due_outbound(NINE)
    assert "missing" not in row["body"]["text"].lower()


async def test_failures_are_capped_and_html_escaped(config):
    rig = Rig(config, [], [])
    rig.clock.now = NINE
    for i in range(7):
        rig.store.audit("send_failed", "watchdog", f"ref={i} <b>boom</b>")
    worker_for(rig, config).send_due()
    (row,) = rig.store.due_outbound(NINE)
    text = row["body"]["text"]
    assert "failed in the last 24 hours: 7" in text
    assert "&lt;b&gt;boom&lt;/b&gt;" in text and "<b>boom" not in text
    assert "and 2 more" in text


async def test_no_second_summary_the_same_day_even_after_a_restart(config):
    rig = Rig(config, [], [])
    rig.clock.now = NINE
    assert worker_for(rig, config).send_due() is True
    rig.clock.advance(3600)
    assert worker_for(rig, config).send_due() is False  # a restarted worker, same store
    again = worker_for(rig, config)
    assert again.send_due() is False and again.send_due() is False
    assert len(rig.store.due_outbound(rig.clock.now)) == 1


async def test_the_next_day_gets_its_own_summary(config):
    rig = Rig(config, [], [])
    rig.clock.now = NINE
    summary = worker_for(rig, config)
    summary.send_due()
    rig.clock.advance(86400)
    assert summary.send_due() is True
    assert len(rig.store.due_outbound(rig.clock.now)) == 2


async def test_the_summary_reaches_the_approver_through_the_sender(config):
    rig = Rig(config, [], [])
    rig.clock.now = NINE
    worker_for(rig, config).send_due()
    await rig.settle()
    (sent,) = [m for m in rig.telegram.sent if m["chat_id"] == APPROVER]
    assert sent["parse_mode"] == "HTML" and "Onboarding summary" in sent["text"]


async def test_a_summary_time_in_the_config_moves_the_schedule(tmp_path):
    config = load_config(make_config(tmp_path, ", summary_time: '07:30'"), ENV)
    rig = Rig(config, [], [])
    rig.clock.now = datetime(2026, 11, 4, 7, 29, tzinfo=MANILA).timestamp()
    summary = worker_for(rig, config)
    assert summary.send_due() is False
    rig.clock.advance(60)
    assert summary.send_due() is True


# --- config ---------------------------------------------------------------------------------


def test_defaults_are_nine_manila_and_nobody_expected(tmp_path):
    config = load_config(make_config(tmp_path), ENV)
    assert config.onboarding.summary_time == "09:00"
    assert config.onboarding.expected == []


@pytest.mark.parametrize("bad", ["9am", "25:00", "09:60", "9:00", "", "0900"])
def test_a_bad_summary_time_fails_boot_naming_the_key(tmp_path, bad):
    path = make_config(tmp_path, f", summary_time: '{bad}'")
    with pytest.raises(ConfigError, match="onboarding.summary_time.*HH:MM"):
        load_config(path, ENV)


def test_summary_time_must_be_text(tmp_path):
    with pytest.raises(ConfigError, match="onboarding.summary_time"):
        load_config(make_config(tmp_path, ", summary_time: 900"), ENV)


@pytest.mark.parametrize(
    "bad",
    [
        "[abc]",
        "['not a handle']",
        "[12345678]",
        "[alice_demo, ALICE_DEMO]",
        "[alice_demo, '@alice_demo']",
        "[null]",
    ],
)
def test_a_bad_expected_list_fails_boot_naming_the_key(tmp_path, bad):
    with pytest.raises(ConfigError, match="onboarding.expected"):
        load_config(make_config(tmp_path, f", expected: {bad}"), ENV)


def test_a_bad_expected_entry_is_reported_by_position_not_value(tmp_path):
    path = make_config(tmp_path, ", expected: [alice_demo, 'leaky secret']")
    with pytest.raises(ConfigError) as caught:
        load_config(path, ENV)
    assert "entry 2" in str(caught.value) and "leaky secret" not in str(caught.value)


def test_the_example_config_lists_the_psr_handles_and_a_placeholder_for_pio():
    from pathlib import Path

    example = Path(__file__).parent.parent / "config.example.yaml"
    config = load_config(example, {**ENV, "SHIFT_SECRET": "s" * 40, "T6CORE_SECRET": "t" * 40})
    assert config.onboarding.summary_time == "09:00"
    assert config.onboarding.expected[:7] == [
        "alice_demo",
        "eyadane",
        "jamagne",
        "dave_demo",
        "erin_demo",
        "dentiamson",
        "carol_demo",
    ]
    assert len(config.onboarding.expected) == 8


async def test_a_long_pending_list_is_capped_to_fit_one_telegram_message(config):
    rig = Rig(
        config, [start_from(i, user=7000 + i, username=f"person{i}") for i in range(1, 26)], []
    )
    await rig.settle()
    rig.clock.now = NINE
    worker_for(rig, config).send_due()
    (row,) = [r for r in rig.store.due_outbound(NINE) if "summary" in r["body"]["text"].lower()]
    assert "Pending: 25" in row["body"]["text"] and "and 5 more" in row["body"]["text"]
    assert len(row["body"]["text"]) < 4096


async def test_a_failed_attempt_is_retried_at_the_next_check(config, monkeypatch):
    rig = Rig(config, [], [])
    rig.clock.now = NINE
    summary = worker_for(rig, config)
    real = rig.store.enqueue_outbound
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("disk")
        return real(*args, **kwargs)

    monkeypatch.setattr(rig.store, "enqueue_outbound", flaky)
    with pytest.raises(RuntimeError):
        summary.send_due()
    assert summary.send_due() is True

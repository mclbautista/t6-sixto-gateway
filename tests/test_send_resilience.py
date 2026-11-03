"""Pacing, retries, 429, permanent errors and crash recovery (spec section 9)."""

import logging

import pytest

from sixto_gateway.notifier import Notifier
from sixto_gateway.sender import Sender
from sixto_gateway.store import Store
from sixto_gateway.telegram.errors import PermanentSendError, RetryAfterError, TransientSendError
from tests.fakes import FakeClock, FakeTelegram

PRIVATE = 4242  # positive chat ids are private chats
GROUP = -1003261335368


class Rig:
    def __init__(self, tmp_path, client=None):
        self.path = tmp_path / "gateway.db"
        self.clock = FakeClock()
        self.telegram = FakeTelegram([], self.clock)
        self.client = client or self.telegram
        self.open()

    def open(self):
        """(Re)open the store and sender on the same database file: a process start."""
        self.store = Store(self.path, clock=self.clock)
        self.sender = Sender(self.store, self.client, Notifier(), clock=self.clock)

    def queue(self, chat, text="hi", module="watchdog", kind="message", **extra):
        if kind == "react":
            body = {"chat": chat, "message_id": 5, "emoji": "👍"}
        else:
            body = {"chat": chat, "topic": None, "text": text, "format": "html"}
        return self.store.enqueue_outbound(module, kind, str(chat), body | extra)

    async def drain_now(self):
        while await self.sender.send_next():
            pass

    async def run_until_idle(self, step_limit=10_000):
        """Send, then jump the fake clock to the next moment the sender can act."""
        for _ in range(step_limit):
            await self.drain_now()
            wait = self.sender.next_wake_in
            if wait is None:
                return
            self.clock.advance(max(wait, 0.001))
        raise AssertionError("sender never went idle")

    def state(self, ref):
        return self.store.outbound_status(ref, "watchdog")


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# --- pacing -----------------------------------------------------------------------------------


async def test_private_chat_gets_at_most_one_message_per_second(rig):
    for i in range(5):
        rig.queue(PRIVATE, f"m{i}")
    await rig.drain_now()
    assert len(rig.telegram.sent) == 1  # the rest wait
    await rig.run_until_idle()
    times = [m["at"] for m in rig.telegram.sent]
    assert len(times) == 5
    assert all(b - a >= 1.0 for a, b in zip(times, times[1:], strict=False))
    assert [m["text"] for m in rig.telegram.sent] == [f"m{i}" for i in range(5)]


async def test_group_gets_at_most_twenty_messages_per_minute(rig):
    for i in range(45):
        rig.queue(GROUP, f"m{i}")
    await rig.drain_now()
    assert len(rig.telegram.sent) == 20  # a burst up to the limit goes out at once
    await rig.run_until_idle()
    times = [m["at"] for m in rig.telegram.sent]
    assert len(times) == 45
    for i, t in enumerate(times):
        in_window = [x for x in times if t <= x < t + 60]
        assert len(in_window) <= 20, f"window starting at message {i}"


async def test_overall_rate_is_at_most_25_per_second(rig):
    for chat in range(1, 81):  # 80 different private chats
        rig.queue(chat)
    await rig.drain_now()
    assert len(rig.telegram.sent) == 25
    await rig.run_until_idle()
    times = [m["at"] for m in rig.telegram.sent]
    assert len(times) == 80
    for t in times:
        assert len([x for x in times if t <= x < t + 1.0]) <= 25


async def test_a_restart_inside_the_group_window_keeps_the_limit(rig):
    for i in range(40):
        rig.queue(GROUP, f"m{i}")
    await rig.drain_now()
    assert len(rig.telegram.sent) == 20
    rig.open()  # crash and restart at once, same fake second
    await rig.drain_now()
    assert len(rig.telegram.sent) == 20  # the new process remembers the first 20
    rig.clock.advance(59.9)
    await rig.drain_now()
    assert len(rig.telegram.sent) == 20
    rig.clock.advance(0.2)
    await rig.drain_now()
    assert len(rig.telegram.sent) == 40


async def test_a_restart_keeps_the_private_and_overall_limits(rig):
    for chat in range(1, 31):
        rig.queue(chat)
    rig.queue(1, "second to chat 1")
    await rig.drain_now()
    assert len(rig.telegram.sent) == 25
    rig.open()
    await rig.drain_now()
    assert len(rig.telegram.sent) == 25  # overall 25/s still applies after the restart
    rig.clock.advance(1.01)
    await rig.drain_now()
    assert len(rig.telegram.sent) == 31
    assert rig.telegram.sent[-1]["text"] == "second to chat 1"  # 1/s per private chat


async def test_a_failed_attempt_counts_towards_the_window_after_a_restart(rig):
    """Telegram may have accepted a request whose outcome the gateway never saw."""
    rig.queue(PRIVATE, "first")
    rig.telegram.fail_with = TransientSendError("timeout")
    await rig.drain_now()
    rig.queue(PRIVATE, "second")
    rig.open()
    rig.clock.advance(0.5)
    await rig.drain_now()
    assert rig.telegram.sent == []
    rig.clock.advance(0.51)
    await rig.drain_now()
    assert [m["text"] for m in rig.telegram.sent] == ["second"]


async def test_paced_chat_does_not_hold_up_other_chats(rig):
    for i in range(3):
        rig.queue(PRIVATE, f"slow{i}")
    rig.queue(GROUP, "other")
    await rig.drain_now()
    assert [m["text"] for m in rig.telegram.sent] == ["slow0", "other"]


async def test_reactions_count_towards_chat_pacing(rig):
    rig.queue(PRIVATE, kind="react")
    rig.queue(PRIVATE, "after")
    await rig.drain_now()
    assert len(rig.telegram.sent) == 1


# --- retries ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error", [TransientSendError("502"), OSError("connection reset"), TimeoutError()]
)
async def test_transient_errors_follow_the_schedule_then_fail(rig, error, caplog):
    rig.telegram.fail_always = error
    ref = rig.queue(GROUP)
    start = rig.clock.now
    with caplog.at_level(logging.WARNING):
        await rig.run_until_idle()
    offsets = [t - start for t in rig.telegram.attempts]
    assert offsets == pytest.approx([0, 30, 150, 450, 1050], abs=0.01)
    state = rig.state(ref)
    assert state["state"] == "failed" and state["error"]
    events = [e for e, _, _ in rig.store.audit_events()]
    assert events.count("send_retry") == 4 and events.count("send_failed") == 1
    assert [r.levelno for r in caplog.records if "failed" in r.getMessage()] == [logging.WARNING]


async def test_message_that_recovers_after_a_failure_is_sent(rig):
    rig.telegram.fail_with = TransientSendError("503")
    ref = rig.queue(GROUP)
    await rig.drain_now()
    assert rig.state(ref)["state"] == "queued"
    assert rig.sender.next_wake_in == pytest.approx(30, abs=0.01)
    await rig.run_until_idle()
    assert rig.state(ref)["state"] == "sent"
    assert len(rig.telegram.sent) == 1


async def test_a_retrying_message_does_not_block_other_chats(rig):
    rig.telegram.fail_with = TransientSendError("503")
    first = rig.queue(GROUP, "flaky")
    second = rig.queue(PRIVATE, "fine")
    await rig.drain_now()
    assert rig.state(first)["state"] == "queued"
    assert rig.state(second)["state"] == "sent"


# --- 429 --------------------------------------------------------------------------------------


async def test_429_waits_at_least_retry_after(rig):
    rig.telegram.fail_with = RetryAfterError(120)
    ref = rig.queue(GROUP)
    await rig.drain_now()
    assert rig.sender.next_wake_in == pytest.approx(120, abs=0.01)
    rig.clock.advance(119)
    await rig.drain_now()
    assert rig.state(ref)["state"] == "queued"
    rig.clock.advance(1.01)
    await rig.drain_now()
    assert rig.state(ref)["state"] == "sent"


async def test_429_with_a_short_retry_after_still_waits_the_schedule_step(rig):
    rig.telegram.fail_with = RetryAfterError(3)
    ref = rig.queue(GROUP)
    await rig.drain_now()
    rig.clock.advance(29)
    await rig.drain_now()
    assert rig.state(ref)["state"] == "queued"
    rig.clock.advance(1.01)
    await rig.drain_now()
    assert rig.state(ref)["state"] == "sent"


async def test_429_pauses_all_sending_for_retry_after(rig):
    rig.telegram.fail_with = RetryAfterError(60)
    rig.queue(GROUP, "limited")
    other = rig.queue(PRIVATE, "other chat")
    await rig.drain_now()
    assert rig.state(other)["state"] == "queued"  # Telegram limits the bot, so everyone waits
    rig.clock.advance(60.01)
    await rig.drain_now()
    assert rig.state(other)["state"] == "sent"


async def test_429_uses_a_schedule_step_and_fails_after_the_last(rig):
    rig.telegram.fail_always = RetryAfterError(1)
    ref = rig.queue(GROUP)
    await rig.run_until_idle()
    assert len(rig.telegram.attempts) == 5
    assert rig.state(ref)["state"] == "failed"


# --- permanent errors -------------------------------------------------------------------------


async def test_permanent_error_fails_at_once_without_retries(rig, caplog):
    ref = rig.queue(GROUP)
    other = rig.queue(PRIVATE)
    rig.telegram.fail_with = PermanentSendError("Bad Request: chat not found")
    with caplog.at_level(logging.WARNING):
        await rig.run_until_idle()
    assert len(rig.telegram.attempts) == 2  # one try for the doomed message, one for the other
    assert rig.state(ref)["state"] == "failed"
    assert "chat not found" in rig.state(ref)["error"]
    assert rig.state(other)["state"] == "sent"
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert "send_failed" in [e for e, _, _ in rig.store.audit_events()]


# --- crash recovery ---------------------------------------------------------------------------


class Crash(BaseException):
    """Stands in for the process dying: not an Exception, so nothing in the sender swallows it."""


async def test_crash_before_marked_sent_resends_once_and_loses_nothing(rig):
    refs = [rig.queue(GROUP, f"m{i}") for i in range(3)]
    await rig.sender.send_next()  # m0 is fully sent
    real_mark = rig.store.mark_outbound_sent

    def die(*args, **kwargs):
        raise Crash

    rig.store.mark_outbound_sent = die  # type: ignore[method-assign]
    with pytest.raises(Crash):
        await rig.sender.send_next()  # m1 reaches Telegram, then the process dies
    assert [m["text"] for m in rig.telegram.sent] == ["m0", "m1"]
    assert real_mark  # the real method is untouched on disk

    rig.open()  # restart
    await rig.run_until_idle()
    assert [m["text"] for m in rig.telegram.sent] == ["m0", "m1", "m1", "m2"]  # m1 once more
    assert [rig.state(r)["state"] for r in refs] == ["sent"] * 3


async def test_retry_state_survives_a_restart(rig):
    rig.telegram.fail_with = TransientSendError("503")
    ref = rig.queue(GROUP)
    await rig.drain_now()
    rig.open()
    await rig.drain_now()
    assert rig.state(ref)["state"] == "queued"  # still waiting out its 30 s
    rig.clock.advance(30.01)
    await rig.drain_now()
    assert rig.state(ref)["state"] == "sent"


# --- aiogram translation ----------------------------------------------------------------------


def test_aiogram_errors_map_to_retry_or_fail():
    from aiogram.exceptions import (
        TelegramBadRequest,
        TelegramForbiddenError,
        TelegramNetworkError,
        TelegramRetryAfter,
        TelegramServerError,
    )
    from aiogram.methods import SendMessage

    from sixto_gateway.telegram.client import translate_send_error

    method = SendMessage(chat_id=1, text="x")
    retry = translate_send_error(TelegramRetryAfter(method, "Too Many Requests", 17))
    assert isinstance(retry, RetryAfterError) and retry.retry_after == 17
    for transient in (
        TelegramServerError(method, "Bad Gateway"),
        TelegramNetworkError(method, "t"),
    ):
        mapped = translate_send_error(transient)
        assert isinstance(mapped, TransientSendError) and not isinstance(mapped, PermanentSendError)
    for permanent in (
        TelegramBadRequest(method, "chat not found"),
        TelegramBadRequest(method, "can't parse entities"),
        TelegramForbiddenError(method, "bot was blocked by the user"),
    ):
        assert isinstance(translate_send_error(permanent), PermanentSendError)
    assert translate_send_error(OSError("reset")) is None  # unknown: the sender retries it


async def test_a_429_on_the_last_attempt_still_pauses_other_chats(rig):
    ref = rig.queue(GROUP, "doomed")
    for _ in range(4):  # use up the four retries with plain network errors
        rig.telegram.fail_with = TransientSendError("503")
        await rig.drain_now()
        rig.clock.advance(601)
    rig.telegram.fail_with = RetryAfterError(60)
    await rig.drain_now()
    assert rig.state(ref)["state"] == "failed"
    other = rig.queue(PRIVATE, "other")
    await rig.drain_now()
    assert rig.state(other)["state"] == "queued"
    rig.clock.advance(60.01)
    await rig.drain_now()
    assert rig.state(other)["state"] == "sent"


class RaisingBot:
    """Stands in for aiogram's ``Bot``: every send or reaction raises ``exc`` (aiogram-native)."""

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    async def send_message(self, **kwargs):
        self.calls += 1
        raise self.exc

    async def set_message_reaction(self, **kwargs):
        self.calls += 1
        raise self.exc


def aiogram_rig(tmp_path, exc):
    from sixto_gateway.telegram.client import AiogramTelegramClient

    bot = RaisingBot(exc)
    rig = Rig(tmp_path, client=AiogramTelegramClient(bot))
    rig.bot = bot
    return rig


@pytest.mark.parametrize("kind", ["message", "react"])
async def test_an_aiogram_429_waits_at_least_retry_after_through_the_real_client(tmp_path, kind):
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import SetMessageReaction

    method = SetMessageReaction(chat_id=GROUP, message_id=5)
    rig = aiogram_rig(tmp_path, TelegramRetryAfter(method, "Too Many Requests", 120))
    ref = rig.queue(GROUP, kind=kind)
    await rig.drain_now()
    assert rig.state(ref)["state"] == "queued"
    rig.clock.advance(119)  # the 30 s schedule step is not enough: Telegram said 120
    await rig.drain_now()
    assert rig.bot.calls == 1
    rig.clock.advance(1.01)
    await rig.drain_now()
    assert rig.bot.calls == 2


@pytest.mark.parametrize("kind", ["message", "react"])
@pytest.mark.parametrize("error", ["bad_request", "forbidden"])
async def test_a_permanent_aiogram_error_fails_at_once_and_is_audited(tmp_path, kind, error):
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
    from aiogram.methods import SetMessageReaction

    method = SetMessageReaction(chat_id=GROUP, message_id=5)
    exc = (
        TelegramBadRequest(method, "chat not found")
        if error == "bad_request"
        else TelegramForbiddenError(method, "bot was blocked by the user")
    )
    rig = aiogram_rig(tmp_path, exc)
    ref = rig.queue(GROUP, kind=kind)
    await rig.drain_now()
    row = rig.state(ref)
    assert row["state"] == "failed" and rig.bot.calls == 1
    assert row["error"].startswith(("PermanentSendError", "BlockedByUserError"))
    assert any(e == "send_failed" for e, _, _ in rig.store.audit_events())


# --- the bot-wide 429 pause survives a restart -------------------------------------------------


@pytest.mark.parametrize("final_attempt", [False, True])
async def test_a_429_pause_survives_a_restart(tmp_path, final_attempt):
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import SetMessageReaction

    method = SetMessageReaction(chat_id=GROUP, message_id=5)
    bot = RaisingBot(TelegramRetryAfter(method, "Too Many Requests", 120))
    from sixto_gateway.telegram.client import AiogramTelegramClient

    rig = Rig(tmp_path, client=AiogramTelegramClient(bot))
    ref = rig.queue(GROUP, kind="react")
    if final_attempt:  # four plain failures first, so the 429 ends the row
        rig.client = rig.telegram
        rig.open()
        for _ in range(4):
            rig.telegram.fail_with = TransientSendError("503")
            await rig.drain_now()
            rig.clock.advance(601)
        rig.client = AiogramTelegramClient(bot)
        rig.open()
    await rig.drain_now()
    assert bot.calls == 1
    if final_attempt:
        assert rig.state(ref)["state"] == "failed"

    other = rig.queue(PRIVATE, "other chat")
    rig.client = rig.telegram  # Telegram is fine again; only the pause should hold
    rig.open()  # restart
    sent_before = len(rig.telegram.sent)
    rig.clock.advance(1.01)
    await rig.drain_now()
    assert len(rig.telegram.sent) == sent_before  # still inside Telegram's 120 s
    assert rig.sender.next_wake_in == pytest.approx(118.99, abs=0.01)
    rig.clock.advance(118.99)
    await rig.drain_now()
    assert rig.state(other)["state"] == "sent"

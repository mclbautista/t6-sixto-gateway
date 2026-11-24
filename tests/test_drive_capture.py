"""Recording-channel captures through fake Telegram and the t6 API boundary."""

import pytest

from sixto_gateway.config import ConfigError
from tests.test_config import load


def test_recording_channel_is_optional_and_validated_at_boot(tmp_path):
    assert load(tmp_path).recording_channel.chat_id is None
    configured = load(
        tmp_path,
        lambda d: d.update(recording_channel={"chat_id": -900001, "topic_id": 4801}),
    )
    assert configured.recording_channel.chat_id == -900001
    assert configured.recording_channel.topic_id == 4801


@pytest.mark.parametrize(
    "channel",
    [
        {"chat_id": -900001},
        {"topic_id": 4801},
        {"chat_id": "-900001", "topic_id": 4801},
        {"chat_id": True, "topic_id": 4801},
        {"chat_id": 0, "topic_id": 4801},
        {"chat_id": -900001, "topic_id": 0},
    ],
)
def test_invalid_recording_channel_stops_boot(tmp_path, channel):
    with pytest.raises(ConfigError, match="recording_channel"):
        load(tmp_path, lambda d: d.update(recording_channel=channel))


class FakeDriveAPI:
    def __init__(self):
        self.calls = []
        self.reads = []
        self.outcome = {
            "capture": "cap-example",
            "state": "pending",
            "lines": [],
            "differences": [],
            "photo_reference": None,
            "practice": True,
        }

    def drive_capture(self, inputs):
        from t6.verbs import Done

        self.calls.append(inputs)
        return Done(result={**self.outcome, "state": "pending"})

    def drive_capture_result(self, inputs):
        from t6.verbs import Done

        self.reads.append(inputs)
        return Done(result=self.outcome)


class Rig:
    def __init__(self, tmp_path):
        from sixto_gateway.drives import DriveWorker
        from sixto_gateway.notifier import Notifier
        from sixto_gateway.poller import Poller
        from sixto_gateway.sender import Sender
        from sixto_gateway.store import Store
        from tests.fakes import FakeClock, FakeTelegram

        self.clock = FakeClock()
        self.config = load(
            tmp_path,
            lambda d: (
                d.update(recording_channel={"chat_id": -900001, "topic_id": 4801}, routes=[]),
                d["groups"].update(custodians=[7003]),
            ),
        )
        self.database = tmp_path / "gateway.db"
        self.store = Store(self.database, self.clock)
        self.telegram = FakeTelegram([], self.clock)
        self.api = FakeDriveAPI()
        self.poller = Poller(self.store, self.telegram, self.config, Notifier(), self.clock)
        self.worker = DriveWorker(
            self.store, self.telegram, self.api, Notifier(), self.clock, custodian_id=7003
        )
        self.sender = Sender(self.store, self.telegram, Notifier(), self.clock)

    async def send(self, raw):
        self.telegram.push(raw)
        await self.poller.poll_once()
        await self.worker.process_next()

    async def resolve(self):
        self.clock.advance(5)
        await self.worker.process_next()
        while await self.sender.send_next():
            self.clock.advance(4)


def photo(update_id=10, chat=-900001, topic=4801):
    from tests.fakes import group_command

    raw = group_command(update_id, 7001, "", chat, topic)
    raw["message"].pop("text")
    raw["message"].update(
        caption=" OUT Example Person\nlabel as typed ",
        photo=[
            {"file_id": "small", "file_unique_id": "s", "width": 20, "height": 20},
            {"file_id": "large", "file_unique_id": "l", "width": 400, "height": 400},
        ],
    )
    return raw


@pytest.fixture
def rig(tmp_path):
    made = Rig(tmp_path)
    yield made
    made.store.close()


async def test_only_recording_topic_photos_capture_largest_bytes_and_unparsed_text(rig):
    import base64
    from datetime import UTC, datetime

    from tests.fakes import group_command

    await rig.send(photo())
    await rig.send(photo(11, chat=-900002))
    await rig.send(photo(12, topic=4802))
    await rig.send(photo(13, topic=None))
    await rig.send(group_command(14, 7001, "just text", -900001, 4801))
    assert len(rig.api.calls) == 1
    sent = rig.api.calls[0]
    assert (sent.chat_id, sent.topic_id, sent.message_id, sent.sender_telegram_id) == (
        -900001,
        4801,
        10,
        7001,
    )
    assert sent.posted_at == datetime.fromtimestamp(1789700000, UTC)
    assert sent.text == " OUT Example Person\nlabel as typed "
    assert base64.b64decode(sent.photo) == b"largest photo bytes"
    assert sent.media_type == "image/jpeg"
    assert rig.telegram.downloads == ["large"]


async def test_replayed_update_and_repeated_post_do_not_capture_twice(rig):
    await rig.send(photo())
    rig.store.set_offset(0)
    await rig.send(photo())
    repeated = photo(11)
    repeated["message"]["message_id"] = 10
    await rig.send(repeated)
    assert len(rig.api.calls) == 1
    assert rig.telegram.downloads == ["large"]


async def test_pending_capture_then_resolved_reply_has_lines_practice_and_one_undo(rig):
    await rig.send(photo())
    await rig.resolve()
    assert rig.telegram.sent == []
    rig.api.outcome.update(
        state="resolved",
        lines=[
            {"label": "EX-01", "direction": "OUT", "person": "Example Person"},
            {"label": "EX-02", "direction": "IN", "person": "SHELF"},
        ],
    )
    await rig.resolve()
    assert len(rig.telegram.sent) == 1
    reply = rig.telegram.sent[0]
    assert reply["text"] == (
        "Practice\nRecorded: EX-01, OUT, Example Person\nRecorded: EX-02, IN, SHELF"
    )
    assert (reply["chat_id"], reply["topic_id"], reply["reply_to_message_id"]) == (
        -900001,
        4801,
        10,
    )
    assert len(reply["keyboard"]) == 1 and len(reply["keyboard"][0]) == 1
    assert reply["keyboard"][0][0]["text"] == "UNDO"
    assert len(reply["keyboard"][0][0]["callback_data"].encode()) <= 64
    await rig.resolve()
    assert len(rig.telegram.sent) == 1 and len(rig.api.calls) == 1


async def test_ignored_capture_stays_silent_and_stops_polling(rig):
    await rig.send(photo())
    rig.api.outcome.update(state="ignored")
    await rig.resolve()
    reads = len(rig.api.reads)
    await rig.resolve()
    assert rig.telegram.sent == []
    assert len(rig.api.reads) == reads


async def test_live_capture_reply_has_no_practice_prefix(rig):
    await rig.send(photo())
    rig.api.outcome.update(
        state="resolved",
        practice=False,
        lines=[
            {"label": "EX-01", "direction": "IN", "person": None},
        ],
    )
    await rig.resolve()
    assert rig.telegram.sent[0]["text"] == "Recorded: EX-01, IN, "


async def test_ask_for_label_and_follow_up_resolve_the_same_capture(rig):
    from tests.fakes import group_command

    await rig.send(photo())
    rig.api.outcome.update(state="exception", reason="unreadable_label", may_ask=True)
    await rig.resolve()
    assert rig.telegram.sent[0]["text"] == "Practice\nPlease reply with the drive label or serial."
    ask_id = rig.telegram.sent[0]["message_id"]
    assert rig.telegram.sent[0]["keyboard"] is None
    followup = group_command(20, 7001, " EX-01 ", -900001, 4801)
    followup["message"]["reply_to_message"] = {
        "message_id": ask_id,
        "date": 1789700000,
        "chat": followup["message"]["chat"],
        "from": {"id": 1, "is_bot": True, "first_name": "Example Bot"},
        "text": "Please reply with the drive label or serial.",
    }
    await rig.send(followup)
    assert len(rig.api.calls) == 2
    sent = rig.api.calls[-1]
    assert (sent.capture, sent.text, sent.photo) == ("cap-example", " EX-01 ", None)
    rig.api.outcome.update(
        state="resolved",
        lines=[
            {"label": "EX-01", "direction": "OUT", "person": "Example Person"},
        ],
    )
    await rig.resolve()
    assert "Recorded: EX-01, OUT, Example Person" in rig.telegram.sent[-1]["text"]
    assert rig.telegram.downloads == ["large"]


async def test_undo_tap_calls_api_once_and_edits_the_recorded_reply(rig):
    from tests.fakes import button_tap

    rig.api.undos = []

    def undo(inputs):
        from t6.verbs import Done

        rig.api.undos.append(inputs)
        return Done(result={**rig.api.outcome, "state": "undone"})

    rig.api.drive_capture_undo = undo
    await rig.send(photo())
    rig.api.outcome.update(
        state="resolved",
        lines=[
            {"label": "EX-01", "direction": "OUT", "person": "Example Person"},
        ],
    )
    await rig.resolve()
    reply = rig.telegram.sent[0]
    data = reply["keyboard"][0][0]["callback_data"]
    tap = button_tap(30, 7001, data, -900001, reply["message_id"], "supergroup")
    tap["callback_query"]["message"]["message_thread_id"] = 4801
    await rig.send(tap)
    await rig.resolve()
    assert [item.capture for item in rig.api.undos] == ["cap-example"]
    assert rig.telegram.edits[-1]["text"] == "Practice\nUndone."
    assert rig.telegram.edits[-1]["message_id"] == reply["message_id"]
    assert rig.telegram.edits[-1]["keyboard"] is None
    assert rig.telegram.answers[-1]["callback_id"] == "cb30"
    rig.store.set_offset(0)
    await rig.send(tap)
    tap["update_id"] = 31
    tap["callback_query"]["id"] = "cb31"
    await rig.send(tap)
    assert len(rig.api.undos) == 1


async def test_worker_restarts_with_pending_capture_without_resubmitting(rig):
    from sixto_gateway.drives import DriveWorker
    from sixto_gateway.notifier import Notifier
    from sixto_gateway.store import Store

    await rig.send(photo())
    # Reopen the same database, as a restarted gateway does.
    rig.store.close()
    rig.store = Store(rig.database, rig.clock)
    rig.worker = DriveWorker(rig.store, rig.telegram, rig.api, Notifier(), rig.clock)
    rig.api.outcome.update(state="ignored")
    rig.clock.advance(5)
    await rig.worker.process_next()
    assert len(rig.api.calls) == 1
    assert len(rig.api.reads) == 1


async def test_other_chat_text_never_enters_capture_storage_or_downloads(rig):
    from tests.fakes import group_command

    await rig.send(photo(10, chat=-900002))
    await rig.send(group_command(11, 7001, "outside sentinel text", -900002, 4801))
    assert rig.store.next_drive_request(rig.clock()) is None
    assert rig.api.calls == [] and rig.telegram.downloads == []


async def test_unavailable_api_retries_later_without_holding_up_other_updates(rig):
    from t6.client import Unavailable

    rig.api.drive_capture_result = lambda inputs: Unavailable("offline")
    await rig.send(photo())
    rig.clock.advance(5)
    assert await rig.worker.process_next()
    assert not await rig.worker.process_next()  # deferred, not a busy retry loop


async def test_refused_capture_is_not_retried_forever(rig):
    from t6.verbs import Refused

    attempts = []

    def refuse(inputs):
        attempts.append(inputs)
        return Refused(rule="recording_channel", reason="Recording channel does not match")

    rig.api.drive_capture = refuse
    await rig.send(photo())
    await rig.resolve()
    assert len(attempts) == 1
    assert rig.telegram.sent == []


async def test_followups_from_another_topic_or_to_another_message_are_not_captured(rig):
    from tests.fakes import group_command

    await rig.send(photo())
    rig.api.outcome.update(state="exception", reason="unreadable_label", may_ask=True)
    await rig.resolve()
    ask_id = rig.telegram.sent[0]["message_id"]
    for update_id, topic, reply_id in [(20, 4802, ask_id), (21, 4801, 8888)]:
        raw = group_command(update_id, 7001, "EX-01", -900001, topic)
        raw["message"]["reply_to_message"] = {
            "message_id": reply_id,
            "date": 1789700000,
            "chat": raw["message"]["chat"],
        }
        await rig.send(raw)
    assert len(rig.api.calls) == 1


async def test_disabled_channel_and_photo_edits_do_not_capture(rig):
    raw = photo()
    raw["edited_message"] = raw.pop("message")
    raw["edited_message"]["edit_date"] = 1789700100
    await rig.send(raw)
    assert rig.api.calls == []
    from sixto_gateway.notifier import Notifier
    from sixto_gateway.poller import Poller

    rig.poller = Poller(
        rig.store,
        rig.telegram,
        rig.config.model_copy(
            update={
                "recording_channel": type(rig.config.recording_channel)(),
            }
        ),
        Notifier(),
        rig.clock,
    )
    await rig.send(photo(11))
    assert rig.api.calls == []


async def test_telegram_adapter_sets_reply_and_topic_and_downloads_in_memory():
    from types import SimpleNamespace

    from sixto_gateway.telegram.client import AiogramTelegramClient

    class Bot:
        async def send_message(self, **kwargs):
            self.sent = kwargs
            return SimpleNamespace(message_id=9001)

        async def download(self, file_id, destination):
            assert file_id == "large"
            destination.write(b"photo")

    bot = Bot()
    client = AiogramTelegramClient(bot)
    assert await client.download_photo("large") == b"photo"
    assert (
        await client.send_message(-900001, 4801, "Recorded", None, reply_to_message_id=10) == 9001
    )
    assert bot.sent["chat_id"] == -900001
    assert bot.sent["message_thread_id"] == 4801
    assert bot.sent["reply_parameters"].message_id == 10


def reply_to(raw, message_id):
    raw["message"]["reply_to_message"] = {
        "message_id": message_id,
        "date": 1789700000,
        "chat": raw["message"]["chat"],
    }
    return raw


def tap_of(rig, update_id, user, reply):
    from tests.fakes import button_tap

    data = reply["keyboard"][0][0]["callback_data"]
    tap = button_tap(update_id, user, data, -900001, reply["message_id"], "supergroup")
    tap["callback_query"]["message"]["message_thread_id"] = 4801
    return tap


def undoable(rig):
    from t6.verbs import Done, Refused

    rig.api.undos = []
    rig.api.undo_refusal = None

    def undo(inputs):
        rig.api.undos.append(inputs)
        if rig.api.undo_refusal:
            return Refused(rule="undo_window", reason="Undo window has ended")
        return Done(result={**rig.api.outcome, "state": "undone"})

    rig.api.drive_capture_undo = undo


async def resolved_reply(rig, **extra):
    await rig.send(photo())
    rig.api.outcome.update(
        state="resolved",
        lines=[{"label": "EX-01", "direction": "OUT", "person": "Example Person"}],
        **extra,
    )
    await rig.resolve()
    return rig.telegram.sent[0]


@pytest.mark.parametrize(
    ("reason", "text"),
    [
        (
            "missing_direction",
            "Practice\nPlease reply with IN or OUT and the person, like OUT <person>.",
        ),
        ("unreadable_label", "Practice\nPlease reply with the drive label or serial."),
        (
            "no_drive_match",
            "Practice\nI could not find that label or serial. Please reply with the correct one.",
        ),
    ],
)
async def test_question_text_follows_the_exception_reason(rig, reason, text):
    await rig.send(photo())
    rig.api.outcome.update(state="exception", reason=reason, may_ask=True)
    await rig.resolve()
    assert [m["text"] for m in rig.telegram.sent] == [text]


async def test_exception_that_may_not_ask_gets_no_question(rig):
    await rig.send(photo())
    rig.api.outcome.update(state="exception", reason="no_drive_match", may_ask=False)
    await rig.resolve()
    assert rig.telegram.sent == []


async def test_only_poster_or_custodian_answers_count_and_an_answer_goes_once(rig):
    from tests.fakes import group_command

    await rig.send(photo())
    rig.api.outcome.update(state="exception", reason="unreadable_label", may_ask=True)
    await rig.resolve()
    ask_id = rig.telegram.sent[0]["message_id"]
    await rig.send(reply_to(group_command(20, 7002, "EX-01", -900001, 4801), ask_id))
    assert len(rig.api.calls) == 1 and rig.telegram.sent[1:] == []
    # The custodian answers; t6 still cannot resolve it, so nothing more is said.
    rig.api.outcome.update(reason="no_drive_match", may_ask=False)
    await rig.send(reply_to(group_command(21, 7003, "EX-01", -900001, 4801), ask_id))
    assert len(rig.api.calls) == 2
    await rig.resolve()
    assert len(rig.telegram.sent) == 1
    await rig.send(reply_to(group_command(22, 7001, "EX-02", -900001, 4801), ask_id))
    assert len(rig.api.calls) == 2


async def test_undo_tap_from_someone_else_gets_a_popup_and_nothing_else(rig):
    undoable(rig)
    reply = await resolved_reply(rig)
    await rig.send(tap_of(rig, 30, 7002, reply))
    await rig.resolve()
    assert rig.api.undos == []
    assert rig.telegram.answers[-1]["text"] == "Only the poster or the custodian can undo"
    assert rig.telegram.edits == []
    await rig.send(tap_of(rig, 31, 7003, reply))
    await rig.resolve()
    assert len(rig.api.undos) == 1


@pytest.mark.parametrize("target", ["reply", "photo"])
@pytest.mark.parametrize("user", [7001, 7003])
async def test_text_undo_acts_as_the_button(rig, target, user):
    from tests.fakes import group_command

    undoable(rig)
    reply = await resolved_reply(rig)
    target_id = reply["message_id"] if target == "reply" else 10
    await rig.send(reply_to(group_command(40, user, " Undo ", -900001, 4801), target_id))
    await rig.resolve()
    assert [u.capture for u in rig.api.undos] == ["cap-example"]
    assert rig.telegram.edits[-1]["text"] == "Practice\nUndone."
    assert rig.telegram.edits[-1]["message_id"] == reply["message_id"]
    assert rig.telegram.edits[-1]["keyboard"] is None


async def test_text_undo_from_others_or_other_wording_is_ignored(rig):
    from tests.fakes import group_command

    undoable(rig)
    reply = await resolved_reply(rig)
    mid = reply["message_id"]
    await rig.send(reply_to(group_command(40, 7002, "undo", -900001, 4801), mid))
    await rig.send(reply_to(group_command(41, 7001, "please undo", -900001, 4801), mid))
    await rig.send(group_command(42, 7001, "undo", -900001, 4801))
    await rig.resolve()
    assert rig.api.undos == [] and rig.telegram.edits == []
    assert len(rig.telegram.sent) == 1


async def test_button_is_removed_when_the_undo_window_ends_and_late_undo_is_silent(rig):
    from datetime import UTC, datetime

    from tests.fakes import group_command

    undoable(rig)
    until = datetime.fromtimestamp(rig.clock() + 3600, UTC).isoformat()
    reply = await resolved_reply(rig, undo_until=until)
    assert rig.telegram.edits == []
    rig.clock.advance(3500)
    await rig.resolve()
    assert rig.telegram.edits == []
    rig.clock.advance(200)
    await rig.resolve()
    assert len(rig.telegram.edits) == 1
    edit = rig.telegram.edits[0]
    assert edit["message_id"] == reply["message_id"]
    assert edit["text"] == reply["text"] and edit["keyboard"] is None
    rig.clock.advance(60)
    await rig.resolve()
    assert len(rig.telegram.edits) == 1
    await rig.send(reply_to(group_command(40, 7001, "undo", -900001, 4801), reply["message_id"]))
    await rig.send(tap_of(rig, 41, 7001, reply))
    await rig.resolve()
    assert rig.api.undos == [] and len(rig.telegram.sent) == 1
    assert rig.telegram.answers[-1]["text"] == "Undo window has ended"


async def test_undo_window_refusal_from_t6_produces_no_message(rig):
    undoable(rig)
    rig.api.undo_refusal = True
    reply = await resolved_reply(rig)
    await rig.send(tap_of(rig, 30, 7001, reply))
    await rig.resolve()
    assert len(rig.api.undos) == 1
    assert rig.telegram.edits == [] and len(rig.telegram.sent) == 1


def text_post(text, update_id=40, user=7001, message_id=None):
    from tests.fakes import group_command

    raw = group_command(update_id, user, text, -900001, 4801)
    if message_id is not None:
        raw["message"]["message_id"] = message_id
    return raw


def edited(raw, text):
    raw["edited_message"] = raw.pop("message")
    raw["edited_message"].update(text=text, edit_date=1789700100)
    raw["update_id"] += 1000
    return raw


@pytest.mark.parametrize("text", ["hello", "INSIDE job", "outbound", "", "please OUT"])
async def test_chatter_is_dropped_and_counted_without_t6_call_or_reply(rig, text):
    await rig.send(text_post(text))
    await rig.resolve()
    assert rig.api.calls == [] and rig.telegram.sent == []
    assert rig.store._db.execute("SELECT dropped FROM drive_drops").fetchone()[0] == 1
    rig.store.set_offset(0)
    await rig.send(text_post(text))
    assert rig.store._db.execute("SELECT dropped FROM drive_drops").fetchone()[0] == 1


@pytest.mark.parametrize("text", ["OUT Angel D-123", "in", "In: shelf", "out-Angel", "IN\nx"])
async def test_in_out_text_without_photo_reaches_t6_as_text_only(rig, text):
    await rig.send(text_post(text))
    assert len(rig.api.calls) == 1
    sent = rig.api.calls[0]
    assert sent.photo is None and sent.text == text
    assert (sent.chat_id, sent.topic_id, sent.sender_telegram_id) == (-900001, 4801, 7001)
    assert rig.telegram.downloads == []
    assert rig.store._db.execute("SELECT dropped FROM drive_drops").fetchone()[0] == 0


async def test_member_reply_to_sixto_is_not_subject_to_the_check(rig):
    await rig.send(photo())
    rig.api.outcome.update(state="exception", reason="unreadable_label", may_ask=True)
    await rig.resolve()
    question = rig.telegram.sent[0]
    await rig.send(reply_to(text_post("EX-01", 41), question["message_id"]))
    assert rig.store._db.execute("SELECT dropped FROM drive_drops").fetchone()[0] == 0
    assert len(rig.api.calls) == 2
    await rig.send(text_post("thanks", 42))  # not a reply: chatter
    assert rig.store._db.execute("SELECT dropped FROM drive_drops").fetchone()[0] == 1


async def test_no_photo_exception_gets_one_mention_reply_without_undo(rig):
    await rig.send(text_post("OUT Angel D-123"))
    rig.api.outcome.update(state="exception", reason="no_photo", may_ask=False)
    await rig.resolve()
    await rig.resolve()
    assert len(rig.telegram.sent) == 1
    reply = rig.telegram.sent[0]
    assert reply["text"] == (
        '<a href="tg://user?id=7003">Ate Angel</a> Placed in the Drives app for your review.'
    )
    assert reply["parse_mode"] == "HTML"
    assert reply["reply_to_message_id"] == 40
    assert not reply.get("keyboard")


async def test_no_photo_reply_without_custodian_has_no_mention(rig):
    rig.worker._custodian_id = None
    await rig.send(text_post("IN"))
    rig.api.outcome.update(state="exception", reason="no_photo", may_ask=False)
    await rig.resolve()
    assert [m["text"] for m in rig.telegram.sent] == ["Placed in the Drives app for review."]


async def test_edit_of_exception_post_goes_to_t6_and_resolution_edits_the_question(rig):
    from t6.verbs import Done

    await rig.send(photo())
    rig.api.outcome.update(state="exception", reason="missing_direction", may_ask=True)
    await rig.resolve()
    question = rig.telegram.sent[0]
    edit = edited(photo(), "OUT Example Person")
    rig.api.outcome["state"] = "resolved"
    rig.api.drive_capture = lambda inputs: (
        rig.api.calls.append(inputs) or Done(result={**rig.api.outcome, "state": "pending"})
    )
    rig.api.outcome.update(state="pending")
    await rig.send(edit)
    sent = rig.api.calls[-1]
    assert (sent.capture, sent.text, sent.edit, sent.photo) == (
        "cap-example",
        "OUT Example Person",
        True,
        None,
    )
    rig.api.outcome.update(
        state="resolved", lines=[{"label": "EX-01", "direction": "OUT", "person": "Example Person"}]
    )
    await rig.resolve()
    assert len(rig.telegram.sent) == 1
    last = rig.telegram.edits[-1]
    assert last["message_id"] == question["message_id"]
    assert last["text"] == "Practice\nRecorded: EX-01, OUT, Example Person"
    assert last["keyboard"][0][0]["text"] == "UNDO"


async def test_edit_that_stays_exception_sends_no_new_message(rig):
    await rig.send(photo())
    rig.api.outcome.update(state="exception", reason="unreadable_label", may_ask=True)
    await rig.resolve()
    await rig.send(edited(photo(), "OUT"))
    await rig.resolve()
    assert len(rig.api.calls) == 2 and rig.api.calls[-1].edit is True
    assert len(rig.telegram.sent) == 1 and rig.telegram.edits == []


async def test_edit_of_pending_post_reaches_t6_and_the_post_replies_once(rig):
    await rig.send(photo())
    await rig.send(edited(photo(), "OUT Someone"))
    assert rig.api.calls[-1].edit is True
    rig.api.outcome.update(
        state="resolved", lines=[{"label": "EX-01", "direction": "OUT", "person": "Someone"}]
    )
    await rig.resolve()
    await rig.resolve()
    assert len(rig.telegram.sent) == 1 and rig.telegram.edits == []


@pytest.mark.parametrize("state", ["resolved", "ignored"])
async def test_edit_of_closed_capture_is_ignored(rig, state):
    await rig.send(photo())
    rig.api.outcome.update(state=state, lines=[])
    await rig.resolve()
    await rig.send(edited(photo(), "OUT Someone Else"))
    assert len(rig.api.calls) == 1


async def test_edit_of_unknown_or_chatter_post_is_ignored(rig):
    await rig.send(text_post("hello", 50))
    await rig.send(edited(text_post("OUT Angel", 50), "OUT Angel"))
    assert rig.api.calls == []


async def test_daily_drop_count_is_posted_once_to_the_debug_topic_and_resets(tmp_path):
    from sixto_gateway.dropcount import DropReporter
    from sixto_gateway.notifier import Notifier

    rig = Rig(tmp_path)
    try:
        rig.config = rig.config.model_copy(
            update={
                "recording_channel": rig.config.recording_channel.model_copy(
                    update={"debug_chat_id": -900009, "debug_topic_id": 12}
                )
            }
        )
        reporter = DropReporter(rig.store, rig.config, Notifier(), rig.clock)
        reporter.send_due()  # starts the day
        await rig.send(text_post("hello", 60))
        await rig.send(text_post("hi", 61))
        rig.clock.advance(86399)
        reporter.send_due()
        await rig.resolve()
        assert rig.telegram.sent == []
        rig.clock.advance(2)
        reporter.send_due()
        reporter.send_due()
        await rig.resolve()
        assert [(m["chat_id"], m["topic_id"], m["text"]) for m in rig.telegram.sent] == [
            (-900009, 12, "TRANSMITTAL local check: 2 messages dropped in the last 24 h")
        ]
        rig.clock.advance(86400)
        reporter.send_due()
        await rig.resolve()
        assert rig.telegram.sent[-1]["text"].startswith("TRANSMITTAL local check: 0 messages")
    finally:
        rig.store.close()


async def test_drop_count_survives_restart_and_unset_debug_topic_posts_nothing(rig):
    from sixto_gateway.dropcount import DropReporter
    from sixto_gateway.notifier import Notifier
    from sixto_gateway.store import Store

    await rig.send(text_post("hello", 70))
    rig.store.close()
    rig.store = Store(rig.database, rig.clock)
    assert rig.store._db.execute("SELECT dropped FROM drive_drops").fetchone()[0] == 1
    reporter = DropReporter(rig.store, rig.config, Notifier(), rig.clock)
    reporter.send_due()
    rig.clock.advance(200000)
    reporter.send_due()
    assert rig.store._db.execute("SELECT count(*) FROM outbound").fetchone()[0] == 0

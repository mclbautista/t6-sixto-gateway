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
        self.approvals = []
        self.outcome = {
            "capture": "cap-example",
            "state": "pending",
            "lines": [],
            "differences": [],
            "photo_reference": None,
            "practice": True,
            "grade": None,
            "problem": None,
            "items": [],
        }

    def drive_capture(self, inputs):
        from t6.verbs import Done

        self.calls.append(inputs)
        return Done(result={**self.outcome, "state": "pending"})

    def drive_capture_result(self, inputs):
        from t6.verbs import Done

        self.reads.append(inputs)
        return Done(result=self.outcome)

    def as_user(self, user):
        from types import SimpleNamespace

        from t6.verbs import Done

        def approve(inputs):
            self.approvals.append((user, inputs.model_dump()))
            return Done(result={"state": "approved"})

        return SimpleNamespace(drive_approve=approve)


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
            self.store, self.telegram, self.api, Notifier(), self.clock, as_user=self.api.as_user
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


@pytest.mark.parametrize(("grade", "emoji"), [("complete", "👍"), ("thin", "🤔")])
@pytest.mark.parametrize("practice", [True, False])
@pytest.mark.parametrize("post", [photo, lambda: text_post("IN EX-01")])
async def test_capture_grade_reacts_once_after_write_without_text(
    rig, grade, emoji, practice, post
):
    await rig.send(post())
    # Even if a pending result carries a grade, the write is not yet confirmed.
    rig.api.outcome.update(grade=grade, practice=practice)
    await rig.resolve()
    assert rig.telegram.sent == []
    rig.api.outcome.update(state="resolved")
    await rig.resolve()
    await rig.resolve()
    assert rig.telegram.sent == [
        {
            "kind": "react",
            "chat_id": -900001,
            "message_id": post()["message"]["message_id"],
            "emoji": emoji,
        }
    ]
    assert rig.telegram.edits == []


async def test_ignored_capture_stays_silent_and_stops_polling(rig):
    await rig.send(photo())
    rig.api.outcome.update(state="ignored")
    await rig.resolve()
    reads = len(rig.api.reads)
    await rig.resolve()
    assert rig.telegram.sent == []
    assert len(rig.api.reads) == reads


@pytest.mark.parametrize("reason", ["unreadable_label", "no_drive_match", "no_photo"])
async def test_old_label_and_review_exceptions_stay_silent(rig, reason):
    await rig.send(photo())
    rig.api.outcome.update(state="exception", reason=reason, may_ask=True)
    await rig.resolve()
    await rig.resolve()
    assert rig.telegram.sent == []


@pytest.mark.parametrize("user", [7001, 7003])
@pytest.mark.parametrize(("grade", "emoji"), [("complete", "👍"), ("thin", "🤔")])
async def test_undo_reply_removes_post_reaction_and_reacts_on_undo_once(rig, user, grade, emoji):
    undoable(rig)
    await resolved_capture(rig, grade=grade)
    undo = reply_to(text_post(" Undo ", 40, user=user), 10)
    await rig.send(undo)
    await rig.resolve()
    assert [u.capture for u in rig.api.undos] == ["cap-example"]
    assert rig.telegram.sent == [
        {"kind": "react", "chat_id": -900001, "message_id": 10, "emoji": emoji},
        {"kind": "react", "chat_id": -900001, "message_id": 10, "emoji": ""},
        {"kind": "react", "chat_id": -900001, "message_id": 40, "emoji": "🙈"},
    ]
    assert rig.telegram.edits == []
    rig.store.set_offset(0)
    await rig.send(undo)
    await rig.send(reply_to(text_post("undo", 41, user=user), 10))
    await rig.resolve()
    assert len(rig.api.undos) == 1 and len(rig.telegram.sent) == 3


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
    rig.api.outcome.update(state="resolved", grade="problem", problem="missing_direction")
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


async def test_telegram_adapter_handles_capture_feedback_and_in_memory_download():
    from types import SimpleNamespace

    from sixto_gateway.telegram.client import AiogramTelegramClient

    class Bot:
        async def send_message(self, **kwargs):
            self.sent = kwargs
            return SimpleNamespace(message_id=9001)

        async def delete_message(self, **kwargs):
            self.deleted = kwargs

        async def set_message_reaction(self, **kwargs):
            self.reacted = kwargs

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
    await client.delete_message(-900001, 9001)
    assert bot.deleted == {"chat_id": -900001, "message_id": 9001}
    await client.set_reaction(-900001, 10, "👍")
    assert bot.reacted["reaction"][0].emoji == "👍"
    await client.set_reaction(-900001, 10, "")
    assert bot.reacted == {"chat_id": -900001, "message_id": 10, "reaction": []}


def reply_to(raw, message_id):
    message = raw.get("message") or raw["edited_message"]
    message["reply_to_message"] = {
        "message_id": message_id,
        "date": 1789700000,
        "chat": message["chat"],
    }
    return raw


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


async def resolved_capture(rig, **extra):
    from datetime import UTC, datetime

    await rig.send(photo())
    until = datetime.fromtimestamp(rig.clock() + 3600, UTC).isoformat()
    rig.api.outcome.update(
        **{"state": "resolved", "grade": "complete", "undo_until": until, **extra},
    )
    await rig.resolve()
    return rig.telegram.sent[0]


@pytest.mark.parametrize(
    ("problem", "text"),
    [
        ("missing_direction", "Please edit the caption to add IN or OUT."),
        ("missing_recipient", "Please edit the caption to add R: <recipient>."),
    ],
)
@pytest.mark.parametrize("practice", [True, False])
async def test_problem_gets_one_scoped_reply(rig, problem, text, practice):
    await rig.send(photo())
    rig.api.outcome.update(state="resolved", grade="problem", problem=problem, practice=practice)
    await rig.resolve()
    await rig.resolve()
    assert len(rig.telegram.sent) == 1
    reply = rig.telegram.sent[0]
    assert reply["kind"] == "message"
    assert reply["text"] == ("Practice\n" if practice else "") + text
    assert (reply["chat_id"], reply["topic_id"], reply["reply_to_message_id"]) == (
        -900001,
        4801,
        10,
    )
    assert reply["keyboard"] is None


async def test_exception_that_may_not_ask_gets_no_question(rig):
    await rig.send(photo())
    rig.api.outcome.update(state="exception", reason="no_drive_match", may_ask=False)
    await rig.resolve()
    assert rig.telegram.sent == []


@pytest.mark.parametrize("user", [7001, 7002, 7003])
@pytest.mark.parametrize("target", ["post", "question"])
async def test_banter_and_label_replies_are_not_capture_answers(rig, user, target):
    question = await question_rig(rig)
    target_id = 10 if target == "post" else question
    for n, text in enumerate(["IN a meeting", "OUT of curiosity", "EX-01", "sorry typo"]):
        await rig.send(reply_to(text_post(text, 40 + n, user=user), target_id))
    await rig.resolve()
    assert len(rig.api.calls) == 1
    assert len(rig.telegram.sent) == 1


async def test_text_undo_from_others_or_other_wording_is_ignored(rig):
    from tests.fakes import group_command

    undoable(rig)
    reply = await resolved_capture(rig)
    mid = reply["message_id"]
    await rig.send(reply_to(group_command(40, 7002, "undo", -900001, 4801), mid))
    await rig.send(reply_to(group_command(41, 7001, "please undo", -900001, 4801), mid))
    await rig.send(group_command(42, 7001, "undo", -900001, 4801))
    await rig.resolve()
    assert rig.api.undos == [] and rig.telegram.edits == []
    assert len(rig.telegram.sent) == 1


@pytest.mark.parametrize("late_by", [0, 1, 3600])
@pytest.mark.parametrize("user", [7001, 7003])
async def test_undo_at_or_after_window_is_ignored_without_removing_reaction(rig, late_by, user):
    from datetime import UTC, datetime

    undoable(rig)
    end = rig.clock() + 3600
    await resolved_capture(rig, undo_until=datetime.fromtimestamp(end, UTC).isoformat())
    rig.clock.advance(end - rig.clock() + late_by)
    await rig.send(reply_to(text_post("undo", 40, user=user), 10))
    await rig.resolve()
    assert rig.api.undos == []
    assert rig.telegram.sent == [
        {"kind": "react", "chat_id": -900001, "message_id": 10, "emoji": "👍"}
    ]
    assert rig.telegram.edits == []


async def test_undo_window_refusal_from_t6_produces_no_message(rig):
    undoable(rig)
    rig.api.undo_refusal = True
    await resolved_capture(rig)
    await rig.send(reply_to(text_post("undo", 40), 10))
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
    question = await question_rig(rig)
    await rig.send(reply_to(text_post("EX-01", 41), question))
    assert rig.store._db.execute("SELECT dropped FROM drive_drops").fetchone()[0] == 0
    assert len(rig.api.calls) == 1
    await rig.send(text_post("thanks", 42))  # not a reply: chatter
    assert rig.store._db.execute("SELECT dropped FROM drive_drops").fetchone()[0] == 1


async def test_text_only_no_photo_exception_stays_silent(rig):
    await rig.send(text_post("OUT Example Person D-123"))
    rig.api.outcome.update(state="exception", reason="no_photo", may_ask=False)
    await rig.resolve()
    await rig.resolve()
    assert rig.telegram.sent == []


@pytest.mark.parametrize("problem", ["missing_direction", "missing_recipient"])
@pytest.mark.parametrize(("grade", "emoji"), [("complete", "👍"), ("thin", "🤔")])
async def test_fixing_edit_clears_problem_reply_and_reacts(rig, problem, grade, emoji):
    await rig.send(photo())
    rig.api.outcome.update(state="resolved", grade="problem", problem=problem)
    await rig.resolve()
    question = rig.telegram.sent[0]
    rig.api.outcome.update(state="pending", grade=None, problem=None)
    await rig.send(edited(photo(), "OUT EX-01\nR: Example Person"))
    assert len(rig.api.calls) == 2
    sent = rig.api.calls[-1]
    assert (sent.capture, sent.text, sent.edit, sent.photo) == (
        "cap-example",
        "OUT EX-01\nR: Example Person",
        True,
        None,
    )
    await rig.resolve()
    assert len(rig.telegram.sent) == 1
    rig.api.outcome.update(state="resolved", grade=grade)
    await rig.resolve()
    await rig.resolve()
    assert rig.telegram.deleted == [{"chat_id": -900001, "message_id": question["message_id"]}]
    assert rig.telegram.sent[1:] == [
        {"kind": "react", "chat_id": -900001, "message_id": 10, "emoji": emoji}
    ]
    assert rig.telegram.edits == []


async def test_successive_caption_fixes_update_one_prompt_then_allow_undo(rig):
    from datetime import UTC, datetime

    undoable(rig)
    question_id = await question_rig(rig)
    first_edit = photo(11)
    first_edit["message"]["message_id"] = 10
    await rig.send(edited(first_edit, "OUT EX-01"))
    rig.api.outcome.update(problem="missing_recipient")
    await rig.resolve()
    assert len(rig.telegram.sent) == 1
    assert rig.telegram.edits[-1] == {
        "chat_id": -900001,
        "message_id": question_id,
        "text": "Practice\nPlease edit the caption to add R: <recipient>.",
        "parse_mode": None,
        "keyboard": None,
    }
    second_edit = photo(12)
    second_edit["message"]["message_id"] = 10
    await rig.send(edited(second_edit, "OUT EX-01\nR: Example Person"))
    rig.api.outcome.update(
        grade="complete",
        problem=None,
        undo_until=datetime.fromtimestamp(rig.clock() + 3600, UTC).isoformat(),
    )
    await rig.resolve()
    assert rig.telegram.deleted == [{"chat_id": -900001, "message_id": question_id}]
    await rig.send(reply_to(text_post("undo", 2000), 10))
    await rig.resolve()
    assert [u.capture for u in rig.api.undos] == ["cap-example"]
    assert rig.telegram.sent[1:] == [
        {"kind": "react", "chat_id": -900001, "message_id": 10, "emoji": "👍"},
        {"kind": "react", "chat_id": -900001, "message_id": 10, "emoji": ""},
        {"kind": "react", "chat_id": -900001, "message_id": 2000, "emoji": "🙈"},
    ]


async def test_edit_that_stays_exception_is_silent(rig):
    await rig.send(photo())
    rig.api.outcome.update(state="exception", reason="unreadable_label", may_ask=True)
    await rig.resolve()
    await rig.send(edited(photo(), "OUT"))
    await rig.resolve()
    assert len(rig.api.calls) == 2 and rig.api.calls[-1].edit is True
    assert rig.telegram.sent == [] and rig.telegram.edits == []


async def test_edit_of_pending_post_reaches_t6_and_the_post_reacts_once(rig):
    await rig.send(photo())
    await rig.send(edited(photo(), "OUT Someone"))
    assert rig.api.calls[-1].edit is True
    rig.api.outcome.update(state="resolved", grade="complete")
    await rig.resolve()
    await rig.resolve()
    assert len(rig.telegram.sent) == 1 and rig.telegram.edits == []


@pytest.mark.parametrize("state", ["resolved", "ignored"])
async def test_edit_of_closed_capture_is_ignored(rig, state):
    await rig.send(photo())
    rig.api.outcome.update(state=state, grade="complete")
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


async def question_rig(rig):
    await rig.send(photo())
    rig.api.outcome.update(state="resolved", grade="problem", problem="missing_direction")
    await rig.resolve()
    return rig.telegram.sent[0]["message_id"]


async def test_banter_under_the_question_from_others_is_ignored_even_with_in_or_out_first(rig):
    ask_id = await question_rig(rig)
    for n, text in enumerate(["in a meeting, sorry", "OUT of curiosity: who is this?", "lol"]):
        await rig.send(reply_to(text_post(text, 60 + n, user=7002), ask_id))
    await rig.resolve()
    assert len(rig.api.calls) == 1 and len(rig.telegram.sent) == 1
    assert rig.store._db.execute("SELECT dropped FROM drive_drops").fetchone()[0] == 0


async def test_two_quick_poster_replies_send_no_answer_to_t6(rig):
    ask_id = await question_rig(rig)
    for n in (61, 62):  # both arrive before the worker has handled the first
        rig.telegram.push(reply_to(text_post("wait what", n), ask_id))
    await rig.poller.poll_once()
    await rig.worker.process_next()
    rig.clock.advance(5)
    await rig.worker.process_next()
    assert len(rig.api.calls) == 1  # only the original post
    assert len(rig.telegram.sent) == 1


async def test_reply_to_the_text_only_post_is_not_an_answer(rig):
    await rig.send(text_post("OUT Angel D-123"))
    rig.api.outcome.update(state="exception", reason="no_photo", may_ask=False)
    await rig.resolve()
    await rig.send(reply_to(text_post("ok po", 63, user=7003), 40))
    assert len(rig.api.calls) == 1 and rig.telegram.sent == []


async def test_incident_sequence_gives_one_question_and_no_further_messages(rig):
    ask_id = await question_rig(rig)
    await rig.send(reply_to(text_post("in a bit", 64, user=7002), ask_id))
    await rig.send(reply_to(text_post("haha", 65, user=7004), ask_id))
    await rig.send(reply_to(text_post("sorry typo", 66), ask_id))
    rig.api.outcome.update(reason="missing_direction", may_ask=False)
    await rig.resolve()
    await rig.send(edited(photo(), "OUT Example Person"))
    rig.api.outcome.update(state="exception", may_ask=False)
    await rig.resolve()
    assert len(rig.telegram.sent) == 1 and rig.telegram.edits == []


async def test_edit_of_chatter_in_the_recording_channel_reaches_no_module(rig):
    from aiogram.types import Update

    from sixto_gateway.config import RouteConfig
    from sixto_gateway.telegram.normalize import normalize

    module = next(iter(rig.config.modules))
    rig.config.routes.append(RouteConfig(chat=-900001, module=module))
    decision = rig.poller.decide(
        normalize(Update.model_validate(edited(text_post("hello", 70), "hello again")))
    )
    assert decision.module is None and decision.additional_modules == ()


def test_migration_backfills_poster_of_in_flight_photo_requests(tmp_path):
    import json
    import sqlite3

    from sixto_gateway.store import Store
    from sixto_gateway.store.migrations import MIGRATIONS

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    for script in MIGRATIONS[:14]:
        db.executescript(script)
    db.execute("PRAGMA user_version = 14")
    for update_id, state, body in [(1, "waiting", {"sender_telegram_id": 7001}), (2, "done", {})]:
        db.execute(
            "INSERT INTO drive_requests (update_id, chat_id, topic_id, message_id, kind, body,"
            " state, next_attempt_at) VALUES (?, -1, 1, ?, 'photo', ?, ?, 0)",
            (update_id, update_id, json.dumps(body), state),
        )
    db.commit()
    db.close()
    store = Store(path, lambda: 0.0)
    rows = store._db.execute(
        "SELECT update_id, poster_id FROM drive_requests ORDER BY 1"
    ).fetchall()
    assert [tuple(r) for r in rows] == [(1, 7001), (2, None)]
    assert store._db.execute("SELECT dropped FROM drive_drops").fetchone()[0] == 0
    store.close()


async def notify_item(rig, **extra):
    import httpx

    from sixto_gateway.api.app import create_app
    from sixto_gateway.notifier import Notifier
    from tests.test_config import SECRET

    body = {
        "to": 7003,
        "text": 'Item: EX-01\n<a href="https://t.me/c/900001/10">Post</a>',
        "photo": "aXRlbSBwaG90bw==",
        "idempotency_key": "drive-item:capture_" + "a" * 32 + ":0",
        "buttons": [
            [{"text": label, "payload": "drive:" + "a" * 32 + ":0:" + choice}]
            for label, choice in [
                ("EX-01 — Example Project", "g1"),
                ("EX-02 — Example Project", "g2"),
                ("EX-03 — Example Project", "g3"),
                ("New drive", "new"),
                ("Not a drive", "skip"),
                ("Type it", "type"),
                ("None", "none"),
            ]
        ],
        **extra,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(rig.config, rig.store, Notifier())),
        base_url="http://gateway",
    ) as client:
        response = await client.post(
            "/notify", json=body, headers={"Authorization": f"Bearer {SECRET}"}
        )
    assert response.status_code == 202, response.text
    return response.json()["message_ref"]


async def test_item_dm_sends_photo_caption_post_link_and_all_buttons_once(rig):
    ref = await notify_item(rig)
    assert await notify_item(rig) == ref
    await rig.resolve()
    assert len(rig.telegram.sent) == 1
    sent = rig.telegram.sent[0]
    assert sent["kind"] == "photo"
    assert sent["photo"] == b"item photo"
    assert sent["chat_id"] == 7003 and sent["topic_id"] is None
    assert sent["text"] == 'Item: EX-01\n<a href="https://t.me/c/900001/10">Post</a>'
    assert sent["parse_mode"] == "HTML"
    assert [row[0]["text"] for row in sent["keyboard"]] == [
        "EX-01 — Example Project",
        "EX-02 — Example Project",
        "EX-03 — Example Project",
        "New drive",
        "Not a drive",
        "Type it",
        "None",
    ]


@pytest.mark.parametrize("with_photo", [True, False])
@pytest.mark.parametrize("before_send", [True, False])
async def test_inbox_approval_edits_item_outcome_and_removes_buttons(rig, with_photo, before_send):
    ref = await notify_item(rig, photo="aXRlbSBwaG90bw==" if with_photo else None)
    if not before_send:
        await rig.resolve()
    await edit_item(rig, ref)
    await rig.resolve()
    assert len(rig.telegram.sent) == 1
    assert rig.telegram.edits == [
        {
            "chat_id": 7003,
            "message_id": rig.telegram.sent[0]["message_id"],
            "text": "Approved: EX-02",
            "parse_mode": "HTML",
            "keyboard": None,
            **({"kind": "caption"} if with_photo else {}),
        }
    ]


@pytest.fixture
async def approval_rig(rig):
    import httpx

    from sixto_gateway.notifier import Notifier
    from sixto_gateway.push import PushWorker

    rig.config.modules["watchdog"] = rig.config.modules["watchdog"].model_copy(
        update={"delivery": "push", "push_url": "https://core.invalid/tap"}
    )
    rig.tap_posts = []

    def endpoint(request):
        import json

        body = json.loads(request.content)
        rig.tap_posts.append((dict(request.headers), body))
        if body["data"].endswith(":type"):
            return httpx.Response(
                200,
                json={
                    "outcome": "needs_person",
                    "reason": "Type a Drive label or serial",
                    "verb": "drive approve",
                    "inputs": {"capture": "capture_" + "a" * 32, "item": 0, "choice": "drive"},
                },
            )
        return httpx.Response(200, json={"outcome": "done", "result": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        rig.pusher = PushWorker(rig.store, rig.config, Notifier(), rig.clock, client=client)
        yield rig


async def test_only_custodian_can_tap_item_buttons_in_practice_chat(approval_rig):
    from tests.fakes import button_tap

    rig = approval_rig
    await notify_item(rig, to=-900009, topic=12)
    await rig.resolve()
    sent = rig.telegram.sent[0]
    # Even the original poster, onboarding approver and another member cannot settle Items.
    for update_id, user in [(100, 7001), (101, 100000001), (102, 7002)]:
        await rig.send(
            button_tap(
                update_id,
                user,
                sent["keyboard"][0][0]["callback_data"],
                -900009,
                sent["message_id"],
                "supergroup",
            )
        )
        await rig.pusher.push_due()
    assert rig.tap_posts == []
    assert len(rig.telegram.sent) == 1 and rig.telegram.edits == []
    assert len(rig.telegram.answers) == 3
    assert all(a["text"] and a["alert"] for a in rig.telegram.answers)


@pytest.mark.parametrize(
    ("row", "choice"),
    [
        (0, "g1"),
        (1, "g2"),
        (2, "g3"),
        (3, "new"),
        (4, "skip"),
        (5, "type"),
        (6, "none"),
    ],
)
async def test_item_button_choices_reach_core_tap_once_as_custodian(approval_rig, row, choice):
    from tests.fakes import button_tap

    rig = approval_rig
    await notify_item(rig)
    await rig.resolve()
    sent = rig.telegram.sent[0]
    tap = button_tap(100, 7003, sent["keyboard"][row][0]["callback_data"], 7003, sent["message_id"])
    await rig.send(tap)
    await rig.pusher.push_due()
    rig.store.set_offset(0)
    await rig.send(tap)
    await rig.pusher.push_due()
    assert len(rig.tap_posts) == 1
    headers, body = rig.tap_posts[0]
    assert headers["x-telegram-user-id"] == "7003"
    assert headers["idempotency-key"] == "tap-100"
    assert (body["data"], body["chat"], body["message_id"], body["from"]["id"]) == (
        "drive:" + "a" * 32 + ":0:" + choice,
        7003,
        sent["message_id"],
        7003,
    )
    if choice == "type":
        await rig.resolve()
        assert len(rig.telegram.sent) == 2
        prompt = rig.telegram.sent[-1]
        assert "label or serial" in prompt["text"]
        assert prompt["chat_id"] == 7003
        assert prompt["reply_to_message_id"] == sent["message_id"]


async def ask_to_type(rig, *, chat=7003, topic=None):
    from tests.fakes import button_tap

    await notify_item(rig, to=chat, topic=topic)
    await rig.resolve()
    item = rig.telegram.sent[0]
    tap = button_tap(
        100,
        7003,
        item["keyboard"][5][0]["callback_data"],
        chat,
        item["message_id"],
        "private" if chat > 0 else "supergroup",
    )
    if topic is not None:
        tap["callback_query"]["message"]["message_thread_id"] = topic
    await rig.send(tap)
    await rig.pusher.push_due()
    await rig.resolve()
    return item, rig.telegram.sent[-1]


@pytest.mark.parametrize("reply_target", ["item", "prompt"])
@pytest.mark.parametrize(("chat", "topic"), [(7003, None), (-900009, 12)])
async def test_type_it_approves_only_first_text_reply_as_custodian(
    approval_rig,
    reply_target,
    chat,
    topic,
):
    from tests.fakes import dm_message, group_command

    rig = approval_rig
    item, prompt = await ask_to_type(rig, chat=chat, topic=topic)
    target = item if reply_target == "item" else prompt
    for update_id, label in [(101, "  EX-02  "), (102, "EX-03")]:
        raw = (
            dm_message(update_id, 7003, label)
            if chat > 0
            else group_command(update_id, 7003, label, chat, topic)
        )
        rig.telegram.push(reply_to(raw, target["message_id"]))
    # Both replies arrive before the worker runs: claiming the first must be atomic.
    await rig.poller.poll_once()
    await rig.worker.process_next()
    rig.clock.advance(5)
    await rig.worker.process_next()
    await rig.resolve()
    assert rig.api.approvals == [
        (
            7003,
            {
                "capture": "capture_" + "a" * 32,
                "item": 0,
                "choice": "drive",
                "typed": "EX-02",
                "drive": None,
                "label": None,
                "serial": None,
            },
        )
    ]
    # The gateway leaves the outcome edit to core, which also handles Inbox approvals.
    assert rig.telegram.edits == []


async def edit_item(rig, ref, text="Approved: EX-02"):
    import httpx

    from sixto_gateway.api.app import create_app
    from sixto_gateway.notifier import Notifier
    from tests.test_config import SECRET

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(rig.config, rig.store, Notifier())),
        base_url="http://gateway",
    ) as client:
        response = await client.post(
            "/edit",
            json={"message_ref": ref, "text": text, "buttons": []},
            headers={"Authorization": f"Bearer {SECRET}"},
        )
    assert response.status_code == 202


async def test_inbox_approval_cancels_pending_type_it_reply(approval_rig):
    from tests.fakes import dm_message

    rig = approval_rig
    item, prompt = await ask_to_type(rig)
    ref = await notify_item(rig)  # same idempotency key: core's original message_ref
    await edit_item(rig, ref)
    await rig.resolve()
    await rig.send(reply_to(dm_message(101, 7003, "EX-03"), prompt["message_id"]))
    await rig.send(reply_to(dm_message(102, 7003, "EX-03"), item["message_id"]))
    assert rig.api.approvals == []
    assert rig.telegram.edits[-1]["text"] == "Approved: EX-02"
    assert rig.telegram.edits[-1]["keyboard"] is None


async def test_repeated_type_it_taps_still_accept_only_one_reply_to_item(approval_rig):
    from tests.fakes import button_tap, dm_message

    rig = approval_rig
    item, _ = await ask_to_type(rig)
    await rig.send(
        button_tap(
            101,
            7003,
            item["keyboard"][5][0]["callback_data"],
            7003,
            item["message_id"],
        )
    )
    await rig.pusher.push_due()
    await rig.resolve()
    await rig.send(reply_to(dm_message(102, 7003, "EX-02"), item["message_id"]))
    await rig.send(reply_to(dm_message(103, 7003, "EX-03"), item["message_id"]))
    assert len(rig.api.approvals) == 1
    assert rig.api.approvals[0][1]["typed"] == "EX-02"


async def test_type_it_ignores_other_people_chats_topics_and_non_text_replies(approval_rig):
    from tests.fakes import group_command

    rig = approval_rig
    item, _ = await ask_to_type(rig, chat=-900009, topic=12)
    ignored = [
        group_command(101, 7002, "EX-03", -900009, 12),
        group_command(102, 7003, "EX-03", -900008, 12),
        group_command(103, 7003, "EX-03", -900009, 13),
        photo(104, chat=-900009, topic=12),  # a caption is not a typed answer
        edited(group_command(105, 7003, "EX-03", -900009, 12), "EX-02"),
        group_command(106, 7003, "   ", -900009, 12),
        group_command(107, 7003, "/project", -900009, 12),
    ]
    for raw in ignored:
        if "edited_message" in raw:
            raw["update_id"] = 105
        await rig.send(reply_to(raw, item["message_id"]))
    await rig.send(group_command(200, 7003, "EX-03", -900009, 12))  # not a reply
    assert rig.api.approvals == []
    await rig.send(reply_to(group_command(201, 7003, "EX-02", -900009, 12), item["message_id"]))
    assert len(rig.api.approvals) == 1
    assert rig.api.approvals[0][1]["typed"] == "EX-02"


async def test_type_it_prompt_and_claimed_reply_survive_gateway_restarts(approval_rig):
    from sixto_gateway.drives import DriveWorker
    from sixto_gateway.notifier import Notifier
    from sixto_gateway.poller import Poller
    from sixto_gateway.store import Store
    from tests.fakes import dm_message

    rig = approval_rig
    item, prompt = await ask_to_type(rig)

    def restart():
        rig.store.close()
        rig.store = Store(rig.database, rig.clock)
        rig.poller = Poller(rig.store, rig.telegram, rig.config, Notifier(), rig.clock)
        rig.worker = DriveWorker(
            rig.store, rig.telegram, rig.api, Notifier(), rig.clock, as_user=rig.api.as_user
        )

    restart()
    reply = reply_to(dm_message(101, 7003, "EX-02"), prompt["message_id"])
    rig.telegram.push(reply)
    await rig.poller.poll_once()  # reply persisted, API not called yet
    restart()
    await rig.worker.process_next()
    restart()
    rig.store.set_offset(101)  # replay the claimed reply after another restart
    await rig.poller.poll_once()
    await rig.send(reply_to(dm_message(102, 7003, "EX-03"), item["message_id"]))
    assert len(rig.api.approvals) == 1
    assert rig.api.approvals[0][1]["typed"] == "EX-02"

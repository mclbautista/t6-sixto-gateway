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
            lambda d: d.update(recording_channel={"chat_id": -900001, "topic_id": 4801}, routes=[]),
        )
        self.database = tmp_path / "gateway.db"
        self.store = Store(self.database, self.clock)
        self.telegram = FakeTelegram([], self.clock)
        self.api = FakeDriveAPI()
        self.poller = Poller(self.store, self.telegram, self.config, Notifier(), self.clock)
        self.worker = DriveWorker(self.store, self.telegram, self.api, Notifier(), self.clock)
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
    rig.api.outcome.update(state="exception")
    await rig.resolve()
    assert rig.telegram.sent[0]["text"] == "Practice\nPlease reply with the drive label or serial."
    ask_id = rig.telegram.sent[0]["message_id"]
    assert rig.telegram.sent[0]["keyboard"] is None
    followup = group_command(20, 7002, " EX-01 ", -900001, 4801)
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
    tap = button_tap(30, 7002, data, -900001, reply["message_id"], "supergroup")
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
    rig.api.outcome.update(state="exception")
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

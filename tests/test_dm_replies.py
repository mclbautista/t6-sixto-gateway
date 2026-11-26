"""A DM reply to a module's /notify message is pushed to that module (issue #149)."""

import json
import re
from pathlib import Path

import httpx
import pytest

from sixto_gateway.config import load_config
from sixto_gateway.notifier import Notifier
from sixto_gateway.poller import Poller
from sixto_gateway.push import RETRY_SCHEDULE_SECONDS, PushWorker
from sixto_gateway.store import Store
from tests.fakes import GATEWAY_ENV, ONBOARDING_YAML, FakeClock, FakeTelegram, dm_message

CORE_SECRET = "c" * 40
OTHER_SECRET = "o" * 40
SHIFT_SECRET = "s" * 40
ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:" + "t" * 35,
    "CORE_SECRET": CORE_SECRET,
    "OTHER_SECRET": OTHER_SECRET,
    "SHIFT_SECRET": SHIFT_SECRET,
    **GATEWAY_ENV,
}
CORE_URL = "http://198.51.100.9:8790/tap"
OTHER_URL = "http://198.51.100.10:8790/tap"
OPERATOR, STRANGER = 555, 999


def write_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "modules: {"
        f"t6core: {{secret_env: CORE_SECRET, delivery: push, push_url: {CORE_URL}}},"
        f" other: {{secret_env: OTHER_SECRET, delivery: push, push_url: {OTHER_URL}}},"
        " shift: {secret_env: SHIFT_SECRET}}\n"
        "groups: {operators: [555]}\n"
        "commands: {jo: {module: t6core, allow: [operators]},"
        " ping: {module: other, allow: [555, 999]}, report: {module: shift, allow: [555]}}\n"
        f"database_path: {tmp_path / 'gateway.db'}\n" + ONBOARDING_YAML
    )
    return path


class Rig:
    def __init__(self, tmp_path):
        self.clock = FakeClock()
        self.config = load_config(write_config(tmp_path), ENV)
        self.store = Store(self.config.database_path, clock=self.clock)
        self.telegram = FakeTelegram([], self.clock)
        push_wake = Notifier()
        self.poller = Poller(
            self.store,
            self.telegram,
            self.config,
            Notifier(),
            clock=self.clock,
            push_wake=push_wake,
        )
        self.posts: list[httpx.Request] = []
        self.status = 200

        def endpoint(request: httpx.Request) -> httpx.Response:
            self.posts.append(request)
            return httpx.Response(self.status)

        self.pusher = PushWorker(
            self.store,
            self.config,
            push_wake,
            self.clock,
            client=httpx.AsyncClient(transport=httpx.MockTransport(endpoint)),
        )

    def notified(self, module, chat, telegram_message_id):
        """A message ``module`` sent with /notify that Telegram accepted."""
        ref = self.store.enqueue_outbound(
            module, "message", str(chat), {"chat": chat, "topic": None, "text": "Deadline?"}
        )
        self.store.mark_outbound_sent(ref, telegram_message_id)
        return ref

    async def reply(self, update_id, user, text, reply_to, edited=False):
        raw = dm_message(update_id, user, text, edited=edited)
        message = raw["edited_message" if edited else "message"]
        message["reply_to_message"] = {
            "message_id": reply_to,
            "date": 1789700000,
            "chat": {"id": user, "type": "private", "first_name": "P"},
            "from": {"id": 1, "is_bot": True, "first_name": "Sixto"},
            "text": "Deadline?",
        }
        self.telegram.push(raw)
        await self.poller.poll_once()

    def bodies(self):
        return [httpx.Response(200, content=r.content).json() for r in self.posts]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


async def test_a_dm_reply_to_a_modules_notify_message_is_pushed_as_a_reply_item(rig):
    ref = rig.notified("t6core", OPERATOR, 9001)
    await rig.reply(20, OPERATOR, "6pm", 9001)

    assert await rig.pusher.push_due() == 1
    (request,) = rig.posts
    assert str(request.url) == CORE_URL
    assert request.headers["authorization"] == f"Bearer {CORE_SECRET}"
    assert request.headers["idempotency-key"] == "reply-20"
    assert request.headers["x-telegram-user-id"] == str(OPERATOR)
    (body,) = rig.bodies()
    assert body["kind"] == "reply"
    assert body["idempotency_key"] == "reply-20"
    assert (body["text"], body["chat"], body["message_id"]) == ("6pm", OPERATOR, 20)
    assert (body["reply_to_message_id"], body["reply_to_message_ref"]) == (9001, ref)
    assert await rig.pusher.push_due() == 0


async def test_a_reply_after_the_draft_was_edited_still_names_the_notify_ref(rig):
    ref = rig.notified("t6core", OPERATOR, 9001)
    edit = rig.store.enqueue_outbound(
        "t6core", "edit", str(OPERATOR), {"chat": OPERATOR, "edit_of": ref, "text": "Purpose?"}
    )
    rig.store.mark_outbound_sent(edit, 9001)  # Telegram keeps the message id on an edit
    await rig.reply(32, OPERATOR, "none", 9001)
    await rig.pusher.push_due()
    assert [b["reply_to_message_ref"] for b in rig.bodies()] == [ref]


async def test_a_reply_to_another_modules_message_goes_only_to_that_module(rig):
    rig.notified("other", OPERATOR, 9002)
    await rig.reply(21, OPERATOR, "none", 9002)
    await rig.pusher.push_due()
    assert [str(r.url) for r in rig.posts] == [OTHER_URL]


async def test_a_reply_to_a_message_no_module_sent_is_not_pushed(rig):
    await rig.reply(22, OPERATOR, "6pm", 4242)
    assert await rig.pusher.push_due() == 0
    assert rig.posts == []


async def test_a_reply_to_a_message_in_another_chat_is_not_pushed(rig):
    rig.notified("t6core", 777, 9001)  # same Telegram message id, someone else's DM
    await rig.reply(23, OPERATOR, "6pm", 9001)
    assert await rig.pusher.push_due() == 0


async def test_a_reply_from_someone_the_modules_commands_do_not_allow_is_not_pushed(rig):
    rig.notified("t6core", STRANGER, 9003)
    await rig.reply(24, STRANGER, "6pm", 9003)
    assert await rig.pusher.push_due() == 0
    assert rig.posts == []


async def test_a_reply_from_someone_allowed_only_another_modules_command_is_not_pushed(rig):
    rig.notified("t6core", STRANGER, 9005)  # STRANGER may use /ping (other), not /jo (t6core)
    await rig.reply(31, STRANGER, "6pm", 9005)
    assert await rig.pusher.push_due() == 0
    assert ("reply_refused", "t6core") in [(e, m) for e, m, _ in rig.store.audit_events()]


async def test_an_edited_reply_is_not_pushed_again(rig):
    rig.notified("t6core", OPERATOR, 9001)
    await rig.reply(25, OPERATOR, "6pm", 9001)
    await rig.reply(26, OPERATOR, "7pm", 9001, edited=True)
    assert await rig.pusher.push_due() == 1
    assert [b["text"] for b in rig.bodies()] == ["6pm"]


async def test_a_refetched_reply_is_pushed_once(rig):
    rig.notified("t6core", OPERATOR, 9001)
    await rig.reply(27, OPERATOR, "6pm", 9001)
    await rig.reply(27, OPERATOR, "6pm", 9001)
    assert await rig.pusher.push_due() == 1


async def test_a_failed_reply_push_is_retried_under_the_same_key(rig):
    rig.status = 503
    ref = rig.notified("t6core", OPERATOR, 9001)
    await rig.reply(28, OPERATOR, "6pm", 9001)
    await rig.pusher.push_due()
    rig.clock.advance(RETRY_SCHEDULE_SECONDS[0])
    rig.status = 200
    assert await rig.pusher.push_due() == 1
    assert [r.headers["idempotency-key"] for r in rig.posts] == ["reply-28"] * 2
    assert [b["reply_to_message_ref"] for b in rig.bodies()] == [ref] * 2


async def test_a_reply_to_a_pull_modules_message_is_not_pushed(rig):
    rig.notified("shift", OPERATOR, 9004)
    await rig.reply(29, OPERATOR, "ok", 9004)
    assert await rig.pusher.push_due() == 0


async def test_the_reply_text_is_not_written_to_the_audit_log(rig):
    rig.notified("t6core", OPERATOR, 9001)
    await rig.reply(30, OPERATOR, "secret purpose words", 9001)
    assert all("secret purpose" not in (d or "") for _, _, d in rig.store.audit_events())


async def test_the_pushed_reply_matches_the_readme(rig):
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    pattern = re.compile(r"<!-- pushed: dm-reply -->\n```json\n(.*?)\n```", re.DOTALL)
    documented = json.loads(pattern.search(readme).group(1))
    ref = rig.notified("t6core", OPERATOR, 9101)
    await rig.reply(9002, OPERATOR, "6pm", 9101)
    await rig.pusher.push_due()
    (body,) = rig.bodies()
    documented.update(reply_to_message_ref=ref, chat=OPERATOR, message_id=9002)
    documented["from"] = {**body["from"], "id": OPERATOR}
    assert {key: body[key] for key in documented} == documented

"""Taps on a module's buttons reach that module (ticket #50)."""

import httpx
import pytest

from sixto_gateway.api.app import create_app
from sixto_gateway.config import ConfigError, load_config
from sixto_gateway.decision import module_callback_data, parse_module_callback
from sixto_gateway.notifier import Notifier
from sixto_gateway.poller import Poller
from sixto_gateway.push import RETRY_SCHEDULE_SECONDS, PushWorker
from sixto_gateway.store import Store
from tests.fakes import NOTION_ENV, NOTION_YAML, FakeClock, FakeTelegram, button_tap

CARDS_SECRET = "c" * 40
SHIFT_SECRET = "s" * 40
ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:" + "t" * 35,
    "CARDS_SECRET": CARDS_SECRET,
    "SHIFT_SECRET": SHIFT_SECRET,
    **NOTION_ENV,
}
PUSH_URL = "http://100.64.0.9:8790/tap"
USER, CHAT = 555, 555


def write_config(tmp_path, modules=None):
    modules = modules or (
        "{cards: {secret_env: CARDS_SECRET, delivery: push, push_url: " + PUSH_URL + "},"
        " shift: {secret_env: SHIFT_SECRET}}"
    )
    path = tmp_path / "config.yaml"
    path.write_text(f"modules: {modules}\ndatabase_path: {tmp_path / 'gateway.db'}\n" + NOTION_YAML)
    return path


class Rig:
    def __init__(self, tmp_path, handler=None):
        self.clock = FakeClock()
        self.config = load_config(write_config(tmp_path), ENV)
        self.store = Store(self.config.database_path, clock=self.clock)
        self.telegram = FakeTelegram([], self.clock)
        self.push_wake = Notifier()
        self.poller = Poller(
            self.store,
            self.telegram,
            self.config,
            Notifier(),
            clock=self.clock,
            push_wake=self.push_wake,
        )
        self.posts: list[httpx.Request] = []
        self.status = 200

        def fake_endpoint(request: httpx.Request) -> httpx.Response:
            self.posts.append(request)
            return httpx.Response(self.status)

        self.pusher = PushWorker(
            self.store,
            self.config,
            self.push_wake,
            self.clock,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler or fake_endpoint)),
        )
        self.app = create_app(self.config, self.store, Notifier())

    def tap(self, update_id, data, user=USER):
        self.telegram.push(button_tap(update_id, user, data, CHAT, 40))

    async def pull(self, module, secret, after=0):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://gateway"
        ) as client:
            response = await client.get(
                f"/updates?after={after}&timeout=0", headers={"Authorization": f"Bearer {secret}"}
            )
        assert response.status_code == 200
        return response.json()["items"]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def audits(rig):
    return [(e, m) for e, m, _ in rig.store.audit_events()]


# --- callback data -------------------------------------------------------------------------


def test_callback_data_round_trips_with_colons_in_the_payload_and_module():
    data = module_callback_data("we:ird", "approve:42:x")
    assert parse_module_callback(data) == ("we:ird", "approve:42:x")


@pytest.mark.parametrize("data", ["ob:ok:1", "mod:", "mod:cards", "mod:cards:", "mod::x", ""])
def test_data_without_a_module_and_payload_is_not_a_module_tap(data):
    assert parse_module_callback(data) is None


# --- pull ---------------------------------------------------------------------------------


async def test_a_pull_module_gets_the_tap_once_with_the_payload_and_tapper(rig):
    rig.tap(1, module_callback_data("shift", "approve:42:now"), user=777)
    await rig.poller.poll_once()
    await rig.poller.poll_once()  # nothing new: the cursor moved

    items = await rig.pull("shift", SHIFT_SECRET)

    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "tap"
    assert item["data"] == "approve:42:now"  # the module's own payload, prefix stripped
    assert item["from"]["id"] == 777
    assert item["chat"] == CHAT and item["message_id"] == 40
    assert await rig.pull("shift", SHIFT_SECRET, after=item["seq"]) == []
    assert await rig.pull("cards", CARDS_SECRET) == []  # not another module's


async def test_a_refetched_tap_is_not_delivered_twice(rig):
    rig.tap(1, module_callback_data("shift", "go"))
    await rig.poller.poll_once()
    rig.store.set_offset(0)  # Telegram sends it again after a crash before the cursor moved
    await rig.poller.poll_once()
    assert len(await rig.pull("shift", SHIFT_SECRET)) == 1


async def test_the_gateway_answers_a_routed_tap(rig):
    rig.tap(1, module_callback_data("shift", "go"))
    await rig.poller.poll_once()
    assert rig.telegram.answers == [{"callback_id": "cb1", "text": None, "alert": False}]


async def test_a_tap_is_saved_even_if_answering_it_fails(rig):
    async def broken(*args, **kwargs):
        raise RuntimeError("query is too old")

    rig.telegram.answer_callback_query = broken
    rig.tap(1, module_callback_data("shift", "go"))
    await rig.poller.poll_once()
    assert len(await rig.pull("shift", SHIFT_SECRET)) == 1
    assert rig.store.last_update_id() == 1


async def test_the_tap_is_saved_and_delivered_before_the_cursor_moves(rig):
    seen = []
    real = rig.store._move_cursor

    def spy(update_id):
        seen.append(rig.store.deliveries_after("shift", 0))
        real(update_id)

    rig.store._move_cursor = spy
    rig.tap(1, module_callback_data("shift", "go"))
    await rig.poller.poll_once()
    assert len(seen[0]) == 1  # its delivery was already in the transaction


# --- unknown module and onboarding ----------------------------------------------------------


async def test_a_tap_for_an_unknown_module_is_audited_and_answered_not_delivered(rig):
    rig.tap(1, module_callback_data("ghost", "go"), user=9)
    await rig.poller.poll_once()

    assert ("tap_unknown_module", None) in audits(rig)
    assert [a["callback_id"] for a in rig.telegram.answers] == ["cb1"]
    assert rig.telegram.answers[0]["text"]
    assert await rig.pull("shift", SHIFT_SECRET) == []
    assert rig.store.due_pushes(rig.clock()) == []
    assert rig.store.next_tap() is None  # not onboarding's either
    assert rig.store.last_update_id() == 1


async def test_an_onboarding_tap_still_goes_to_the_onboarding_worker(rig):
    rig.tap(1, "ob:ok:555:abc")
    await rig.poller.poll_once()
    assert rig.store.next_tap()["data"] == "ob:ok:555:abc"
    assert await rig.pull("shift", SHIFT_SECRET) == []
    assert rig.telegram.answers == []  # the onboarding worker answers it


async def test_a_module_payload_that_looks_like_onboarding_stays_the_modules(rig):
    rig.tap(1, module_callback_data("shift", "ob:ok:555:abc"))
    await rig.poller.poll_once()
    assert rig.store.next_tap() is None
    assert (await rig.pull("shift", SHIFT_SECRET))[0]["data"] == "ob:ok:555:abc"


# --- push ---------------------------------------------------------------------------------


async def test_a_push_module_gets_a_post_with_its_secret_and_an_idempotency_key(rig):
    rig.tap(7, module_callback_data("cards", "approve:42"), user=777)
    await rig.poller.poll_once()
    assert await rig.pull("cards", CARDS_SECRET) == []  # pushed, not pulled

    assert await rig.pusher.push_due() == 1

    (request,) = rig.posts
    assert str(request.url) == PUSH_URL
    assert request.headers["authorization"] == f"Bearer {CARDS_SECRET}"
    assert request.headers["idempotency-key"] == "tap-7"
    body = httpx.Response(200, content=request.content).json()
    assert body["idempotency_key"] == "tap-7"
    assert body["kind"] == "tap" and body["data"] == "approve:42"
    assert body["from"]["id"] == 777
    assert rig.telegram.answers[0]["callback_id"] == "cb7"
    assert await rig.pusher.push_due() == 0  # accepted: not posted again


async def test_a_failed_push_is_retried_with_backoff_under_the_same_key(rig):
    rig.status = 503
    rig.tap(7, module_callback_data("cards", "approve:42"))
    await rig.poller.poll_once()

    assert await rig.pusher.push_due() == 1
    assert await rig.pusher.push_due() == 0  # not due yet
    rig.clock.advance(RETRY_SCHEDULE_SECONDS[0])
    assert await rig.pusher.push_due() == 1  # second try after 5 s
    rig.clock.advance(RETRY_SCHEDULE_SECONDS[1])
    rig.status = 200
    assert await rig.pusher.push_due() == 1  # accepted on the third

    assert [r.headers["idempotency-key"] for r in rig.posts] == ["tap-7"] * 3
    assert rig.store.push_status(1)["state"] == "done"
    assert ("tap_push_retry", "cards") in audits(rig)
    rig.clock.advance(10_000)
    assert await rig.pusher.push_due() == 0


async def test_the_backoff_stops_growing_and_never_gives_up(rig):
    rig.status = 500
    rig.tap(7, module_callback_data("cards", "x"))
    await rig.poller.poll_once()
    waits = []
    for _ in range(7):
        before = rig.clock()
        await rig.pusher.push_due()
        waits.append(rig.store.push_status(1)["next_attempt_at"] - before)
        rig.clock.advance(waits[-1])
    assert waits == [5.0, 30.0, 120.0, 600.0, 600.0, 600.0, 600.0]
    assert rig.store.push_status(1)["state"] == "pending"


async def test_a_network_error_is_retried_and_its_text_is_scrubbed(tmp_path):
    def down(request):
        raise httpx.ConnectError(f"cannot reach {CARDS_SECRET}")

    rig = Rig(tmp_path, handler=down)
    rig.pusher._scrub = lambda text: text.replace(CARDS_SECRET, "[redacted]")
    rig.tap(7, module_callback_data("cards", "x"))
    await rig.poller.poll_once()
    await rig.pusher.push_due()
    error = rig.store.push_status(1)["last_error"]
    assert "[redacted]" in error and CARDS_SECRET not in error


async def test_a_pending_push_survives_a_restart(tmp_path):
    rig = Rig(tmp_path)
    rig.status = 502
    rig.tap(7, module_callback_data("cards", "x"))
    await rig.poller.poll_once()
    await rig.pusher.push_due()
    rig.store.close()

    again = Rig(tmp_path)
    again.clock.now = rig.clock.now + 60
    assert await again.pusher.push_due() == 1
    assert len(again.posts) == 1
    assert again.store.push_status(1)["state"] == "done"


async def test_purge_keeps_a_pending_push_and_drops_a_finished_one(rig):
    rig.status = 500
    rig.tap(1, module_callback_data("cards", "a"))
    rig.tap(2, module_callback_data("cards", "b"))
    await rig.poller.poll_once()
    rig.store.mark_push_done(2)
    rig.clock.advance(40 * 86400)
    rig.store.purge(30)
    assert rig.store.push_status(1)["state"] == "pending"
    assert rig.store.push_status(2) is None
    rig.status = 200
    assert await rig.pusher.push_due() == 1


# --- config -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "modules, message",
    [
        ("{cards: {secret_env: CARDS_SECRET, delivery: push}}", "needs a push_url"),
        (
            "{cards: {secret_env: CARDS_SECRET, push_url: http://h/tap}}",
            "only for delivery: push",
        ),
        (
            "{cards: {secret_env: CARDS_SECRET, delivery: push, push_url: ftp://h/tap}}",
            "push_url",
        ),
        ("{cards: {secret_env: CARDS_SECRET, delivery: poke}}", "delivery"),
    ],
)
def test_bad_push_config_fails_the_boot(tmp_path, modules, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(tmp_path, modules), ENV)


def test_delivery_defaults_to_pull(tmp_path):
    config = load_config(write_config(tmp_path, "{shift: {secret_env: SHIFT_SECRET}}"), ENV)
    assert config.modules["shift"].delivery == "pull"

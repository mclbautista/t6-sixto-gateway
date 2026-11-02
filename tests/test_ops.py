"""Health, deadman, retention, polling-off, set-offset and log safety (issue #6)."""

import asyncio
import io

import httpx
import pytest

from sixto_gateway.__main__ import main, redactor_for
from sixto_gateway.api import create_app
from sixto_gateway.config import load_config
from sixto_gateway.liveness import DEADMAN_SECONDS, Liveness, watch_deadman
from sixto_gateway.logsafe import configure_logging
from sixto_gateway.notifier import Notifier
from sixto_gateway.poller import Poller
from sixto_gateway.retention import run_retention
from sixto_gateway.runtime import start_workers
from sixto_gateway.sender import Sender
from sixto_gateway.store import Store
from tests.fakes import INTAKE_CHAT, INTAKE_TOPIC, FakeClock, FakeTelegram, fixture_updates, moved

WATCHDOG = "w" * 40
TOKEN = "123456789:AAH-secret_token_value_0123456789abcdef"
DAY = 86400.0


def write_config(tmp_path, polling=True):
    path = tmp_path / "config.yaml"
    path.write_text(
        "modules: {watchdog: {secret_env: WATCHDOG_SECRET}}\n"
        f"routes: [{{chat: {INTAKE_CHAT}, topic: {INTAKE_TOPIC}, module: watchdog}}]\n"
        f"telegram: {{polling: {str(polling).lower()}}}\n"
        f"database_path: {tmp_path / 'gateway.db'}\n"
    )
    return path


ENV = {"TELEGRAM_BOT_TOKEN": TOKEN, "WATCHDOG_SECRET": WATCHDOG}


@pytest.fixture
def clock():
    return FakeClock()


def build(tmp_path, clock, polling=True):
    config = load_config(write_config(tmp_path, polling), ENV)
    store = Store(config.database_path, clock=clock)
    liveness = Liveness(clock)
    telegram = FakeTelegram([])
    notifier, wake = Notifier(), Notifier()
    app = create_app(config, store, notifier, wake, liveness)
    return config, store, liveness, telegram, notifier, wake, app


def http(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway")


# --- /healthz and the deadman ---------------------------------------------------------------


async def test_healthz_flips_to_503_when_polling_stalls_and_back_when_it_resumes(tmp_path, clock):
    config, store, liveness, telegram, notifier, _, app = build(tmp_path, clock)
    poller = Poller(store, telegram, config, notifier, clock, liveness)
    async with http(app) as client:
        assert (await client.get("/healthz")).status_code == 503  # nothing polled yet

        await poller.poll_once()
        response = await client.get("/healthz")  # no Authorization header
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

        clock.advance(5 * 60)
        assert (await client.get("/healthz")).status_code == 200  # exactly 5 minutes is fine
        clock.advance(1)
        stalled = await client.get("/healthz")
        assert stalled.status_code == 503
        assert stalled.json()["status"] == "unhealthy"

        await poller.poll_once()
        assert (await client.get("/healthz")).status_code == 200


async def test_healthz_reports_queue_and_recent_failures(tmp_path, clock):
    config, store, liveness, telegram, notifier, wake, app = build(tmp_path, clock)
    liveness.record_poll()
    store.enqueue_outbound("watchdog", "message", "1", {"chat": 1, "text": "a", "format": "plain"})
    store.enqueue_outbound("watchdog", "message", "1", {"chat": 1, "text": "b", "format": "plain"})
    store.audit("send_failed", "watchdog", "ref=1 old")
    clock.advance(DAY + 1)
    liveness.record_poll()
    store.audit("send_failed", "watchdog", "ref=2 recent")
    async with http(app) as client:
        body = (await client.get("/healthz")).json()
    assert body["queued"] == 2
    assert body["failed_last_24h"] == 1
    assert body["last_poll_at"].endswith("+00:00")
    assert body["database_writable"] is True


async def test_healthz_is_503_when_the_database_is_not_writable(tmp_path, clock, monkeypatch):
    _, store, liveness, _, _, _, app = build(tmp_path, clock)
    liveness.record_poll()
    monkeypatch.setattr(store, "is_writable", lambda: False)
    async with http(app) as client:
        response = await client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["database_writable"] is False


def test_store_is_writable_for_a_normal_database(tmp_path, clock):
    assert Store(tmp_path / "db.sqlite", clock=clock).is_writable() is True


async def test_healthz_with_polling_off_only_checks_the_database(tmp_path, clock):
    *_, app = build(tmp_path, clock, polling=False)
    async with http(app) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["polling"] is False
    assert response.json()["last_poll_at"] is None


async def test_deadman_trips_after_ten_minutes_without_a_poll(clock):
    liveness = Liveness(clock)
    stop = asyncio.Event()
    watcher = asyncio.create_task(watch_deadman(liveness, stop, interval=0.01))

    clock.advance(DEADMAN_SECONDS - 1)
    await asyncio.sleep(0.05)
    assert not watcher.done()

    liveness.record_poll()  # a completed poll resets the countdown
    clock.advance(DEADMAN_SECONDS - 1)
    await asyncio.sleep(0.05)
    assert not watcher.done()

    clock.advance(1)
    assert await asyncio.wait_for(watcher, timeout=2) is True


async def test_deadman_counts_from_boot_when_no_poll_ever_completes(clock):
    liveness = Liveness(clock)
    clock.advance(DEADMAN_SECONDS)
    assert await watch_deadman(liveness, asyncio.Event(), interval=0.01) is True


async def test_deadman_returns_false_when_stopped(clock):
    stop = asyncio.Event()
    stop.set()
    assert await watch_deadman(Liveness(clock), stop) is False


# --- retention ------------------------------------------------------------------------------


def seed(store, clock, age_days, update_id, outbound_state="sent"):
    """One update + delivery, one outbound row and one audit row, all `age_days` old."""
    clock.now = BASE - age_days * DAY
    raw = moved(fixture_updates()["text"], update_id=update_id)
    from aiogram.types import Update

    from sixto_gateway.telegram.normalize import normalize

    incoming = normalize(Update.model_validate(raw))
    store.save_update(incoming, "watchdog", received_at=clock.now)
    ref = store.enqueue_outbound("watchdog", "message", "1", {"chat": 1, "text": "x"}, None)
    if outbound_state == "sent":
        store.mark_outbound_sent(ref, 5)
    elif outbound_state == "failed":
        store.mark_outbound_failed(ref, "boom")
    store.audit("send_ok", "watchdog", f"age={age_days}")
    return ref


BASE = 1_789_700_000.0


def counts(store):
    db = store._db
    return {
        t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("updates", "deliveries", "outbound", "audit")
    }


def test_retention_deletes_exactly_the_rows_past_their_limits(tmp_path, clock):
    store = Store(tmp_path / "db.sqlite", clock=clock)
    seed(store, clock, 31, 1)  # past 30 days: updates, deliveries, finished sends go
    seed(store, clock, 30, 2, "failed")  # exactly 30 days: kept (not *older* than the limit)
    seed(store, clock, 29, 3)
    queued = seed(store, clock, 40, 4, "queued")  # old but unsent: never deleted
    seed(store, clock, 181, 5, "failed")
    # audit: ages 31, 30, 29, 40 and 181 days plus the seed's own; set one at exactly 180
    clock.now = BASE - 180 * DAY
    store.audit("send_ok", detail="age=180")
    cursor_before = store.last_update_id()
    clock.now = BASE

    deleted = store.purge(retention_days=30)

    assert deleted == {"updates": 3, "deliveries": 3, "outbound": 2, "audit": 1}
    assert [r[0] for r in store._db.execute("SELECT update_id FROM updates ORDER BY 1")] == [2, 3]
    assert store._db.execute("SELECT id FROM outbound WHERE state='queued'").fetchone()[0] == queued
    assert counts(store) == {"updates": 2, "deliveries": 2, "outbound": 3, "audit": 5}
    assert store.last_update_id() == cursor_before  # the polling cursor is never purged


def test_retention_follows_the_configured_days(tmp_path, clock):
    store = Store(tmp_path / "db.sqlite", clock=clock)
    seed(store, clock, 8, 1)
    seed(store, clock, 6, 2)
    clock.now = BASE
    assert store.purge(retention_days=7)["updates"] == 1


async def test_retention_job_runs_at_boot_and_audits(tmp_path, clock):
    store = Store(tmp_path / "db.sqlite", clock=clock)
    seed(store, clock, 31, 1)
    clock.now = BASE
    stop = asyncio.Event()
    job = asyncio.create_task(run_retention(store, 30, stop, interval=0.01))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(job, timeout=2)
    assert counts(store)["updates"] == 0
    assert any(e == "retention" for e, _, _ in store.audit_events())


# --- polling off ----------------------------------------------------------------------------


async def test_with_polling_off_no_telegram_call_is_made_and_the_api_still_works(tmp_path, clock):
    config, store, liveness, telegram, notifier, wake, app = build(tmp_path, clock, polling=False)
    stop = asyncio.Event()
    tasks = start_workers(config, store, telegram, notifier, wake, liveness, stop)
    assert set(tasks) == {"retention"}  # no poller, sender or deadman
    async with http(app) as client:
        headers = {"Authorization": f"Bearer {WATCHDOG}"}
        accepted = await client.post(
            "/notify", headers=headers, json={"to": 1, "text": "hi", "format": "plain"}
        )
        assert accepted.status_code == 202
        assert (await client.get("/updates", headers=headers)).status_code == 200
        status = await client.get(f"/notify/{accepted.json()['message_ref']}", headers=headers)
        assert status.json()["state"] == "queued"
        assert (await client.get("/healthz")).status_code == 200
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.gather(*tasks.values())
    assert telegram.calls == [] and telegram.sent == []


class SlowTelegram(FakeTelegram):
    """Like the network: a poll takes a moment, so the event loop is not starved."""

    async def get_updates(self, offset, timeout, allowed_updates):
        await asyncio.sleep(0.01)
        return await super().get_updates(offset, timeout, allowed_updates)


async def test_with_polling_on_all_workers_start(tmp_path, clock):
    config, store, liveness, _, notifier, wake, _ = build(tmp_path, clock)
    telegram = SlowTelegram([])
    stop = asyncio.Event()
    tasks = start_workers(config, store, telegram, notifier, wake, liveness, stop)
    assert set(tasks) == {"retention", "poller", "sender", "deadman"}
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait(tasks.values(), timeout=5)
    assert telegram.calls  # it did poll


# --- set-offset -----------------------------------------------------------------------------


async def test_set_offset_makes_the_next_poll_use_n_plus_one(tmp_path, clock, monkeypatch, capsys):
    path = write_config(tmp_path)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)

    assert main(["--config", str(path), "set-offset", "4242"]) == 0
    assert "4243" in capsys.readouterr().out

    config = load_config(path, ENV)
    store = Store(config.database_path, clock=clock)
    telegram = FakeTelegram([])
    await Poller(store, telegram, config, Notifier(), clock).poll_once()
    assert telegram.calls[0]["offset"] == 4243
    assert ("set_offset", None, "last_update_id=4242") in store.audit_events()


def test_set_offset_can_move_the_cursor_backwards(tmp_path, clock):
    store = Store(tmp_path / "db.sqlite", clock=clock)
    store.set_offset(500)
    store.set_offset(100)
    assert store.next_offset() == 101


def test_set_offset_rejects_a_negative_or_missing_number(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    for argv in (["set-offset", "-1"], ["set-offset"], ["set-offset", "abc"]):
        with pytest.raises(SystemExit) as exc:
            main(["--config", str(path), *argv])
        assert exc.value.code == 2


# --- logs never carry secrets ---------------------------------------------------------------


class LeakyTelegram(FakeTelegram):
    """Every call fails with an error that carries the bot token, as a network error can."""

    async def get_updates(self, offset, timeout, allowed_updates):
        raise ConnectionError(f"cannot reach https://api.telegram.org/bot{TOKEN}/getUpdates")

    async def send_message(self, chat_id, topic_id, text, parse_mode):
        raise ConnectionError(f"cannot reach https://api.telegram.org/bot{TOKEN}/sendMessage")


async def test_no_log_line_contains_a_secret_or_the_token(tmp_path, clock):
    config = load_config(write_config(tmp_path), ENV)
    stream = io.StringIO()
    configure_logging(redactor_for(config), stream)
    store = Store(config.database_path, clock=clock)
    telegram = LeakyTelegram([])
    notifier, wake = Notifier(), Notifier()
    app = create_app(config, store, notifier, wake, Liveness(clock))

    # a failing poll (logged with a traceback), a failing send, and an auth failure
    poller = Poller(store, telegram, config, notifier, clock)
    stop = asyncio.Event()
    run = asyncio.create_task(poller.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(run, timeout=5)

    async with http(app) as client:
        wrong = "x" * 40 + WATCHDOG
        assert (
            await client.get("/updates", headers={"Authorization": f"Bearer {wrong}"})
        ).status_code == 401
        ref = (
            await client.post(
                "/notify",
                headers={"Authorization": f"Bearer {WATCHDOG}"},
                json={"to": 1, "text": "SECRET BODY TEXT", "format": "plain"},
            )
        ).json()["message_ref"]
        await Sender(store, telegram, wake, redactor_for(config).scrub).send_next()
        status = await client.get(f"/notify/{ref}", headers={"Authorization": f"Bearer {WATCHDOG}"})

    output = stream.getvalue()
    assert "auth failure" in output and "send" in output and "getUpdates cycle failed" in output
    for forbidden in (TOKEN, TOKEN.split(":")[1], WATCHDOG, wrong, "SECRET BODY TEXT"):
        assert forbidden not in output
    # the stored/served error text is scrubbed too
    assert TOKEN not in status.text
    assert TOKEN not in " ".join(str(e) for e in store.audit_events())


async def test_a_successful_send_logs_one_line_without_the_text(tmp_path, clock):
    config = load_config(write_config(tmp_path), ENV)
    stream = io.StringIO()
    configure_logging(redactor_for(config), stream)
    store = Store(config.database_path, clock=clock)
    store.enqueue_outbound(
        "watchdog",
        "message",
        "1",
        {"chat": 1, "topic": None, "text": "PRIVATE WORDS", "format": "plain"},
    )
    await Sender(store, FakeTelegram([]), Notifier()).send_next()
    output = stream.getvalue()
    assert "send 1 for watchdog ok" in output
    assert "PRIVATE WORDS" not in output


def test_redactor_scrubs_configured_secrets_tokens_and_bearer_values():
    from sixto_gateway.logsafe import Redactor

    redactor = Redactor(["supersecretvalue"])
    text = "a supersecretvalue b bot111111:ABCDEFGHIJKLMNOPQRSTUVWX c Authorization: Bearer abc.def"
    out = redactor.scrub(text)
    assert "supersecretvalue" not in out and "ABCDEFGH" not in out and "abc.def" not in out

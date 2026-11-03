import httpx
import pytest

from sixto_gateway.api import create_app
from sixto_gateway.config import load_config
from sixto_gateway.notifier import Notifier
from sixto_gateway.sender import Sender
from sixto_gateway.store import Store
from sixto_gateway.telegram.errors import PermanentSendError
from tests.fakes import NOTION_ENV, NOTION_YAML, FakeClock, FakeTelegram

WATCHDOG = "w" * 40
PSR = "p" * 40
CHAT = -1003261335368


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "modules:\n  watchdog: {secret_env: WATCHDOG_SECRET}\n  psr: {secret_env: PSR_SECRET}\n"
        "routes: []\n"
        f"database_path: {tmp_path / 'gateway.db'}\n" + NOTION_YAML
    )
    config = load_config(
        path,
        {
            "TELEGRAM_BOT_TOKEN": "1:tok",
            "WATCHDOG_SECRET": WATCHDOG,
            "PSR_SECRET": PSR,
            **NOTION_ENV,
        },
    )
    clock = FakeClock()
    store = Store(config.database_path, clock=clock)
    wake = Notifier()
    telegram = FakeTelegram([], clock)
    sender = Sender(store, telegram, wake, clock=clock)
    app = create_app(config, store, Notifier(), wake)
    return store, telegram, sender, app


@pytest.fixture
async def client(env):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env[-1]), base_url="http://gateway"
    ) as c:
        yield c


def auth(secret=WATCHDOG):
    return {"Authorization": f"Bearer {secret}"}


async def drain(sender):
    while await sender.send_next():
        pass


async def test_notify_saves_then_202_and_status_is_queued(client, env):
    r = await client.post("/notify", json={"to": CHAT, "text": "hi"}, headers=auth())
    assert r.status_code == 202
    ref = r.json()["message_ref"]
    s = await client.get(f"/notify/{ref}", headers=auth())
    assert s.json() == {"state": "queued", "telegram_message_id": None, "error": None}
    assert env[1].sent == []  # the request does not send; the queue does


async def test_wrong_secret_401_and_invalid_body_422(client):
    body = {"to": CHAT, "text": "hi"}
    assert (await client.post("/notify", json=body)).status_code == 401
    assert (await client.post("/notify", json=body, headers=auth("x" * 40))).status_code == 401
    assert (await client.post("/react", json={}, headers=auth("x" * 40))).status_code == 401
    assert (await client.get("/notify/1", headers=auth("x" * 40))).status_code == 401
    for bad in (
        {"to": CHAT},
        {"to": CHAT, "text": ""},
        {"to": "abc", "text": "hi"},
        {"to": CHAT, "text": "hi", "format": "markdown"},
    ):
        assert (await client.post("/notify", json=bad, headers=auth())).status_code == 422
    for bad in ({"chat": CHAT, "message_id": 1}, {"chat": CHAT, "emoji": "👍"}):
        assert (await client.post("/react", json=bad, headers=auth())).status_code == 422


async def test_delivered_in_order_with_ids_then_sent(client, env):
    _, telegram, sender, _ = env
    a = await client.post("/notify", json={"to": CHAT, "topic": 1, "text": "one"}, headers=auth())
    b = await client.post(
        "/react", json={"chat": CHAT, "message_id": 5, "emoji": "👍"}, headers=auth()
    )
    c = await client.post("/notify", json={"to": CHAT, "text": "three"}, headers=auth())
    await drain(sender)
    assert [x["kind"] for x in telegram.sent] == ["message", "react", "message"]
    assert [x.get("text") for x in telegram.sent] == ["one", None, "three"]
    first = await client.get(f"/notify/{a.json()['message_ref']}", headers=auth())
    assert first.json() == {
        "state": "sent",
        "telegram_message_id": telegram.sent[0]["message_id"],
        "error": None,
    }
    reacted = await client.get(f"/notify/{b.json()['message_ref']}", headers=auth())
    assert reacted.json()["state"] == "sent"
    assert c.json()["message_ref"] > a.json()["message_ref"]


async def test_formats_parse_mode_and_topic(client, env):
    _, telegram, sender, _ = env
    await client.post("/notify", json={"to": CHAT, "topic": 7, "text": "<b>x</b>"}, headers=auth())
    await client.post(
        "/notify", json={"to": CHAT, "text": "<b>x</b>", "format": "plain"}, headers=auth()
    )
    await drain(sender)
    assert telegram.sent[0]["parse_mode"] == "HTML"
    assert telegram.sent[0]["topic_id"] == 7
    assert telegram.sent[1]["parse_mode"] is None
    assert telegram.sent[1]["topic_id"] is None


async def test_idempotency_key_returns_same_ref_and_queues_once(client, env):
    _, telegram, sender, _ = env
    body = {"to": CHAT, "text": "hi", "idempotency_key": "watchdog-outbox-381"}
    r1 = await client.post("/notify", json=body, headers=auth())
    r2 = await client.post("/notify", json=body, headers=auth())
    assert r1.status_code == r2.status_code == 202
    assert r1.json() == r2.json()
    await drain(sender)
    assert len(telegram.sent) == 1
    # another module may reuse the same key
    r3 = await client.post("/notify", json=body, headers=auth(PSR))
    assert r3.json()["message_ref"] != r1.json()["message_ref"]


async def test_failed_send_is_recorded_with_error_and_audited(client, env):
    store, telegram, sender, _ = env
    telegram.fail_with = PermanentSendError("chat not found")
    r = await client.post("/notify", json={"to": CHAT, "text": "hi"}, headers=auth())
    ok = await client.post("/notify", json={"to": CHAT, "text": "next"}, headers=auth())
    await drain(sender)
    s = (await client.get(f"/notify/{r.json()['message_ref']}", headers=auth())).json()
    assert s["state"] == "failed" and "chat not found" in s["error"]
    assert s["telegram_message_id"] is None
    # a failure does not block the queue
    assert (await client.get(f"/notify/{ok.json()['message_ref']}", headers=auth())).json()[
        "state"
    ] == "sent"
    events = [e for e, _, _ in store.audit_events()]
    assert events.count("send_attempt") == 2
    assert "send_failed" in events and "send_ok" in events


async def test_audit_records_each_attempt_and_outcome(client, env):
    store, _, sender, _ = env
    await client.post("/notify", json={"to": CHAT, "text": "hi"}, headers=auth())
    await drain(sender)
    assert [e for e, m, _ in store.audit_events()] == ["send_attempt", "send_ok"]


async def test_status_of_unknown_or_other_modules_ref_is_404(client):
    r = await client.post("/notify", json={"to": CHAT, "text": "hi"}, headers=auth())
    ref = r.json()["message_ref"]
    assert (await client.get(f"/notify/{ref}", headers=auth(PSR))).status_code == 404
    assert (await client.get("/notify/9999", headers=auth())).status_code == 404


async def test_out_of_range_ids_are_422_not_500(client):
    assert (await client.get("/notify/99999999999999999999", headers=auth())).status_code == 422
    big = {"to": 2**70, "text": "hi"}
    assert (await client.post("/notify", json=big, headers=auth())).status_code == 422

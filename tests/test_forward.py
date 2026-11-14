from types import SimpleNamespace

import pytest

import tests.test_send as send_fixtures
from sixto_gateway.telegram.client import AiogramTelegramClient
from sixto_gateway.telegram.errors import RetryAfterError, TransientSendError
from tests.test_send import CHAT, auth, drain

env = send_fixtures.env
client = send_fixtures.client


async def test_forward_is_idempotent_and_audited(client, env):
    body = {
        "from_chat": -123,
        "message_id": 42,
        "to": CHAT,
        "topic": 7,
        "idempotency_key": "forward-42",
    }
    first = await client.post("/forward", headers=auth(), json=body)
    again = await client.post("/forward", headers=auth(), json=body | {"message_id": 43})
    assert first.status_code == again.status_code == 202
    ref = first.json()["message_ref"]
    assert again.json() == first.json()
    assert (await client.get(f"/notify/{ref}", headers=auth())).json()["state"] == "queued"
    await drain(env[2])
    assert env[1].sent == [
        {
            "at": env[1].clock(),
            "kind": "forward",
            "chat_id": CHAT,
            "topic_id": 7,
            "from_chat": -123,
            "source_message_id": 42,
            "message_id": 9001,
        }
    ]
    assert (await client.get(f"/notify/{ref}", headers=auth())).json() == {
        "state": "sent",
        "telegram_message_id": 9001,
        "error": None,
    }
    assert [event for event, _, _ in env[0].audit_events()] == ["send_attempt", "send_ok"]


async def test_forward_validation(client):
    body = {"from_chat": 1, "message_id": 2, "to": 3, "idempotency_key": "key"}
    assert (await client.post("/forward", json=body)).status_code == 401
    for invalid in (
        {k: v for k, v in body.items() if k != "idempotency_key"},
        body | {"idempotency_key": ""},
        body | {"reply_to": 5},
    ):
        assert (await client.post("/forward", headers=auth(), json=invalid)).status_code == 422


async def test_forward_shares_pacing_and_429_pause(client, env):
    store, telegram, sender, _ = env
    body = {"from_chat": 1, "message_id": 2, "to": CHAT, "idempotency_key": "0"}
    for i in range(21):
        await client.post("/forward", headers=auth(), json=body | {"idempotency_key": str(i)})
    await drain(sender)
    assert len(telegram.sent) == 20
    telegram.clock.advance(60)
    telegram.fail_with = RetryAfterError(60)
    await drain(sender)
    other = await client.post("/notify", headers=auth(), json={"to": 42, "text": "hi"})
    await drain(sender)
    assert (await client.get(f"/notify/{other.json()['message_ref']}", headers=auth())).json()[
        "state"
    ] == "queued"
    telegram.clock.advance(60)
    await drain(sender)
    assert len(telegram.sent) == 22
    assert any(event == "send_retry" for event, _, _ in store.audit_events())


async def test_forward_retries_transient_failure(client, env):
    telegram, sender = env[1:3]
    telegram.fail_with = TransientSendError("unavailable")
    response = await client.post(
        "/forward",
        headers=auth(),
        json={"from_chat": 1, "message_id": 2, "to": CHAT, "idempotency_key": "retry"},
    )
    await drain(sender)
    telegram.clock.advance(30)
    await drain(sender)
    assert len(telegram.sent) == 1
    assert (await client.get(f"/notify/{response.json()['message_ref']}", headers=auth())).json()[
        "state"
    ] == "sent"


@pytest.mark.parametrize("topic, expected", [(None, None), (1, None), (7, 7)])
async def test_aiogram_forward_arguments(topic, expected):
    class Bot:
        async def forward_message(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(message_id=99)

    bot = Bot()
    assert await AiogramTelegramClient(bot).forward_message(10, topic, -20, 30) == 99
    assert bot.kwargs == {
        "chat_id": 10,
        "message_thread_id": expected,
        "from_chat_id": -20,
        "message_id": 30,
    }

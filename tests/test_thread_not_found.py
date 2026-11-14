from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage

import tests.test_send as send_fixtures
from sixto_gateway.notifier import Notifier
from sixto_gateway.sender import Sender
from sixto_gateway.telegram.client import AiogramTelegramClient
from tests.test_send import CHAT, auth, drain

env = send_fixtures.env
client = send_fixtures.client


@pytest.mark.parametrize("kind", ["notify", "edit", "forward"])
async def test_missing_thread_fails_without_retry(client, env, kind):
    class Bot:
        calls = 0
        refuse = False

        async def request(self):
            self.calls += 1
            if self.refuse:
                raise TelegramBadRequest(
                    SendMessage(chat_id=CHAT, text="x"), "Bad Request: message thread not found"
                )
            return SimpleNamespace(message_id=42)

        async def send_message(self, **kwargs):
            return await self.request()

        async def edit_message_text(self, **kwargs):
            return await self.request()

        async def forward_message(self, **kwargs):
            return await self.request()

    bot = Bot()
    sender = Sender(env[0], AiogramTelegramClient(bot), Notifier(), clock=env[1].clock)
    if kind == "edit":
        original = await client.post("/notify", headers=auth(), json={"to": CHAT, "text": "hi"})
        await drain(sender)
        body = {"message_ref": original.json()["message_ref"], "text": "edited"}
    elif kind == "forward":
        body = {
            "from_chat": 1,
            "message_id": 2,
            "to": CHAT,
            "topic": 7,
            "idempotency_key": "forward",
        }
    else:
        body = {"to": CHAT, "topic": 7, "text": "hi"}
    bot.refuse = True
    response = await client.post(f"/{kind}", headers=auth(), json=body)
    assert response.status_code == 202
    ref = response.json()["message_ref"]
    await drain(sender)
    assert (await client.get(f"/notify/{ref}", headers=auth())).json() == {
        "state": "failed",
        "telegram_message_id": None,
        "error": "thread_not_found",
    }
    calls = bot.calls
    env[1].clock.advance(1000)
    await drain(sender)
    assert bot.calls == calls

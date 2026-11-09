"""The t6-core message contracts (ticket #52).

Each example request in the README section "Messages from t6-core" is read from the README,
posted to ``/notify`` as the ``t6core`` module, sent by the sender, and checked against what
reaches Telegram. Editing an example in a way the gateway would send differently fails here.
"""

import json
import re
from pathlib import Path

import httpx
import pytest

from sixto_gateway.api import create_app
from sixto_gateway.config import load_config
from sixto_gateway.notifier import Notifier
from sixto_gateway.sender import Sender
from sixto_gateway.store import Store
from tests.fakes import NOTION_ENV, NOTION_YAML, FakeClock, FakeTelegram

README = Path(__file__).parent.parent / "README.md"
T6CORE = "t" * 40
CYRIL = 274983989
COORDINATOR = 123456789
CONTACT = "0a1b2c3d-1111-2222-3333-444455556666"
PAGE = "0a1b2c3d111122223333444455556666"
CONTRACTS = ("review-done", "approval-card", "retry-card", "frameio-sign-in")


def readme_contracts() -> dict[str, dict]:
    """``name -> request body`` from each ``<!-- contract: name -->`` + json block."""
    pattern = re.compile(r"<!-- contract: ([a-z-]+) -->\n```json\n(.*?)\n```", re.DOTALL)
    return {name: json.loads(body) for name, body in pattern.findall(README.read_text())}


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "modules:\n"
        "  t6core: {secret_env: T6CORE_SECRET, delivery: push, push_url: 'http://vps:8790/tap'}\n"
        "routes: []\n"
        f"database_path: {tmp_path / 'gateway.db'}\n" + NOTION_YAML
    )
    config = load_config(
        path, {"TELEGRAM_BOT_TOKEN": "1:tok", "T6CORE_SECRET": T6CORE, **NOTION_ENV}
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


async def send(client, env, body: dict) -> dict:
    """Post ``body`` as t6core, run the sender, return the one message Telegram received."""
    r = await client.post("/notify", json=body, headers={"Authorization": f"Bearer {T6CORE}"})
    assert r.status_code == 202, r.text
    while await env[2].send_next():
        pass
    assert len(env[1].sent) == 1
    return env[1].sent[0]


def approve(store, contact=CONTACT, user=COORDINATOR):
    store._db.execute(
        "INSERT INTO members (telegram_user_id, handle, first_name, status, contact_page_id,"
        " created_at, updated_at) VALUES (?,?,?,'approved',?,0,0)",
        (user, "coordinator_handle", "Coordinator", contact),
    )
    store._db.commit()


def test_readme_documents_every_contract():
    assert sorted(readme_contracts()) == sorted(CONTRACTS)


async def test_review_done_dm_reaches_an_approved_coordinator(client, env):
    approve(env[0])
    sent = await send(client, env, readme_contracts()["review-done"])
    assert sent["chat_id"] == COORDINATOR and sent["topic_id"] is None
    assert sent["parse_mode"] == "HTML"
    assert sent["text"] == (
        "<b>Review done:</b> Example Show\n12 new comments on Episode 3 rough cut."
    )
    assert sent["keyboard"] == [
        [
            {
                "text": "Open in Frame.io",
                "url": "https://next.frame.io/share/00000000-0000-4000-8000-000000000000",
            }
        ]
    ]


async def test_review_done_dm_falls_back_to_the_show_topic_with_a_mention(client, env):
    body = readme_contracts()["review-done"]
    sent = await send(client, env, body)
    assert (sent["chat_id"], sent["topic_id"]) == (
        body["fallback"]["to"],
        body["fallback"]["topic"],
    )
    assert sent["text"].startswith("@coordinator_handle <b>Review done:</b> Example Show\n")
    assert sent["keyboard"][0][0]["text"] == "Open in Frame.io"


async def test_approval_card_reaches_cyril_with_an_approve_callback(client, env):
    sent = await send(client, env, readme_contracts()["approval-card"])
    assert sent["chat_id"] == CYRIL
    assert sent["text"].startswith("<b>Project Start:</b> Example Show\n")
    assert sent["keyboard"] == [
        [{"text": "Approve", "callback_data": f"mod:t6core:approve:{PAGE}"}]
    ]


async def test_retry_card_names_the_failed_step_and_carries_a_retry_callback(client, env):
    sent = await send(client, env, readme_contracts()["retry-card"])
    assert sent["chat_id"] == CYRIL
    assert "Step: Frame.io project" in sent["text"]
    assert sent["keyboard"] == [
        [{"text": "Retry Frame.io project", "callback_data": f"mod:t6core:retry:{PAGE}:frameio"}]
    ]


async def test_frameio_sign_in_alert_reaches_cyril_with_a_url_button(client, env):
    sent = await send(client, env, readme_contracts()["frameio-sign-in"])
    assert sent["chat_id"] == CYRIL
    assert sent["text"].startswith("<b>Frame.io sign-in needed.</b>\n")
    assert sent["keyboard"] == [
        [
            {
                "text": "Sign in to Frame.io",
                "url": "https://hooks.example.com/frameio/sign-in?state=example-state",
            }
        ]
    ]


@pytest.mark.parametrize(
    ("step", "accepted"), [("notion_pages", True), ("s" * 14, True), ("s" * 15, False)]
)
async def test_retry_payload_budget_is_a_14_byte_step_key(client, env, step, accepted):
    body = readme_contracts()["retry-card"]
    body["buttons"] = [[{"text": "Retry", "payload": f"retry:{PAGE}:{step}"}]]
    r = await client.post("/notify", json=body, headers={"Authorization": f"Bearer {T6CORE}"})
    assert r.status_code == (202 if accepted else 422)


async def test_a_repeated_contract_request_sends_once(client, env):
    body = readme_contracts()["approval-card"]
    headers = {"Authorization": f"Bearer {T6CORE}"}
    first = await client.post("/notify", json=body, headers=headers)
    again = await client.post("/notify", json=body, headers=headers)
    assert first.json() == again.json()
    while await env[2].send_next():
        pass
    assert len(env[1].sent) == 1

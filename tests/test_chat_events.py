import copy
import json
from pathlib import Path

from tests.fakes import INTAKE_CHAT
from tests.test_api import PSR, WATCHDOG, auth, feed

pytest_plugins = ["tests.test_api"]


async def test_events_are_delivered_to_chat_subscribers_once(env, client):
    fixtures = json.loads((Path(__file__).parent / "fixtures/chat_events.json").read_text())
    await client.put("/subscriptions", headers=auth(PSR), json=[{"chat": INTAKE_CHAT, "topic": 77}])
    left = copy.deepcopy(fixtures["chat_member"])
    left["update_id"] = 104
    member = left["chat_member"]
    member["old_chat_member"], member["new_chat_member"] = (
        member["new_chat_member"],
        member["old_chat_member"],
    )
    joined_service = copy.deepcopy(fixtures["topic_created"])
    joined_service["update_id"] = 105
    message = joined_service["message"]
    del message["forum_topic_created"]
    message["new_chat_members"] = [
        member["old_chat_member"]["user"],
        {"id": 103, "is_bot": False, "first_name": "Other"},
    ]
    left_service = copy.deepcopy(joined_service)
    left_service["update_id"] = 106
    message = left_service["message"]
    message["left_chat_member"] = message.pop("new_chat_members")[0]
    updates = list(fixtures.values()) + [left, joined_service, left_service]
    await feed(env, updates)
    await feed(env, updates)
    items = (await client.get("/updates", headers=auth(PSR))).json()["items"]
    assert [item["event"] for item in items] == [
        "topic_created",
        "topic_renamed",
        "member_joined",
        "member_left",
        "member_joined",
        "member_joined",
        "member_left",
    ]
    assert all(item["kind"] == "event" for item in items)
    assert [(item["topic"], item["name"]) for item in items[:2]] == [
        (77, "Example"),
        (77, "Renamed"),
    ]
    assert [item["user"]["id"] for item in items[2:]] == [102, 102, 102, 103, 102]
    assert (await client.get("/updates", headers=auth(WATCHDOG))).json()["items"] == []
    assert (
        await client.get("/updates", headers=auth(PSR), params={"after": items[-1]["seq"]})
    ).json()["items"] == []

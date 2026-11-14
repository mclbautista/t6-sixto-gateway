import httpx

from sixto_gateway.api import create_app
from sixto_gateway.poller import Poller
from sixto_gateway.store import Store
from tests.fakes import INTAKE_CHAT, INTAKE_TOPIC, FakeClock, FakeTelegram, fixture_updates, moved
from tests.test_api import PSR, WATCHDOG, auth, feed, intake

pytest_plugins = ["tests.test_api"]


async def test_subscriptions_route_replace_and_isolate(env):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env[-1]), base_url="http://gateway"
    ) as client:
        whole = {"chat": INTAKE_CHAT, "topic": None}
        specific = {"chat": INTAKE_CHAT, "topic": INTAKE_TOPIC}
        assert (
            await client.put("/subscriptions", headers=auth(PSR), json=[whole, specific])
        ).status_code == 200
        assert (
            await client.put("/subscriptions", headers=auth(WATCHDOG), json=[specific])
        ).status_code == 200
        assert (await client.get("/subscriptions", headers=auth(PSR))).json() == [whole, specific]
        assert (await client.get("/subscriptions")).status_code == 401
        await feed(env, [intake(1)])
        for secret in (WATCHDOG, PSR):
            items = (await client.get("/updates", headers=auth(secret))).json()["items"]
            assert [item["update_id"] for item in items] == [1]
        assert (await client.put("/subscriptions", headers=auth(PSR), json=[])).json() == []
        assert (await client.get("/subscriptions", headers=auth(WATCHDOG))).json() == [specific]
        await feed(env, [intake(2)])
        assert [
            item["update_id"]
            for item in (await client.get("/updates", headers=auth(PSR))).json()["items"]
        ] == [1]


async def test_edit_follows_original_and_subscriptions_survive_restart(env):
    config, store, notifier, _, _, app = env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        subscription = {"chat": INTAKE_CHAT, "topic": INTAKE_TOPIC}
        await client.put("/subscriptions", headers=auth(PSR), json=[subscription])
        await feed(env, [intake(1)])
        await client.put("/subscriptions", headers=auth(PSR), json=[])
        await client.put("/subscriptions", headers=auth(WATCHDOG), json=[subscription])
        edit = moved(fixture_updates()["text"], update_id=2)
        edit["edited_message"] = edit.pop("message")
        edit["edited_message"]["edit_date"] = 1789700100
        await feed(env, [edit])
        for secret in (PSR, WATCHDOG):
            assert [
                item["update_id"]
                for item in (await client.get("/updates", headers=auth(secret))).json()["items"]
            ] == [1, 2]
    store.close()
    store = Store(config.database_path, clock=FakeClock())
    app = create_app(config, store, notifier)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        assert (await client.get("/subscriptions", headers=auth(WATCHDOG))).json() == [subscription]
        assert (await client.get("/subscriptions", headers=auth(PSR))).json() == []
        telegram = FakeTelegram([intake(3)])
        poller = Poller(store, telegram, config, notifier, FakeClock())
        await poller.poll_once()
        assert [
            item["update_id"]
            for item in (await client.get("/updates", headers=auth(WATCHDOG))).json()["items"]
        ] == [1, 2, 3]
    store.close()

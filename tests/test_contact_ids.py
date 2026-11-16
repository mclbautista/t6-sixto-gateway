"""A member is found by a Notion page ID or a T6 Person ID, whichever the row has (#108)."""

import sqlite3

import httpx
import pytest

from sixto_gateway.api.app import create_app
from sixto_gateway.notifier import Notifier
from sixto_gateway.people import PeopleDirectory
from sixto_gateway.store import Store
from sixto_gateway.store.migrations import MIGRATIONS
from tests.fakes import FakeClock
from tests.test_onboarding import USER, Rig, buttons, start_from, tap
from tests.test_onboarding import config as config_fixture
from tests.test_people_reads import PAGE_ID, PERSON_ID, FakeAPI

config = config_fixture

NOTION = "0a1b2c3d-1111-2222-3333-444455556666"
NOTION_PLAIN = NOTION.replace("-", "")
T6 = "t6n_" + "a" * 32
FALLBACK = {"to": 1, "handle": "someone"}


def old_database(path, rows):
    """A database as the image before this change left it: migrations 1 to 10 only."""
    db = sqlite3.connect(path)
    for script in MIGRATIONS[:10]:
        db.executescript(script)
    db.execute("PRAGMA user_version = 10")
    for user, contact in rows:
        db.execute(
            "INSERT INTO members (telegram_user_id, status, contact_page_id, created_at,"
            " updated_at) VALUES (?, 'approved', ?, 0, 0)",
            (user, contact),
        )
    db.commit()
    db.close()


def columns(store, user):
    row = store._db.execute(
        "SELECT contact_page_id, contact_t6_id, contact_notion_id FROM members"
        " WHERE telegram_user_id = ?",
        (user,),
    ).fetchone()
    return tuple(row)


def test_migration_fills_the_one_id_each_old_row_shows(tmp_path):
    other = "0a1b2c3d-9999-2222-3333-444455556666"
    path = tmp_path / "old.db"
    old_database(path, [(1, NOTION), (2, T6), (3, other.replace("-", "").upper())])
    store = Store(path, clock=FakeClock())
    assert columns(store, 1) == (NOTION, None, NOTION_PLAIN)
    assert columns(store, 2) == (T6, T6, None)
    assert columns(store, 3)[1:] == (None, other.replace("-", ""))
    assert store.approved_user_for_contact(NOTION_PLAIN.upper()) == 1
    assert store.approved_user_for_contact(T6) == 2
    assert store.approved_user_for_contact(T6.replace("a", "b")) is None


def test_the_old_image_still_reads_and_writes_the_migrated_database(tmp_path):
    path = tmp_path / "old.db"
    old_database(path, [(1, NOTION)])
    Store(path, clock=FakeClock()).close()
    db = sqlite3.connect(path)  # what the old image's statements do, with no new columns
    assert db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    db.execute(
        "INSERT INTO members (telegram_user_id, status, contact_page_id, created_at, updated_at)"
        " VALUES (2, 'approved', ?, 0, 0)",
        (T6,),
    )
    row = db.execute(
        "SELECT telegram_user_id FROM members WHERE status = 'approved'"
        " AND REPLACE(contact_page_id, '-', '') = ?",
        (NOTION_PLAIN,),
    ).fetchone()
    db.commit()
    db.close()
    assert row == (1,)
    # the row the old image wrote has no new ID, and the new code still finds it
    store = Store(path, clock=FakeClock())
    assert store.approved_user_for_contact(T6) == 2


def test_a_missing_id_is_filled_when_a_listed_contact_shows_both(tmp_path):
    t6_only, other = "t6n_" + "b" * 32, "bbbbbbbb000000000000000000000002"
    store = Store(tmp_path / "g.db", clock=FakeClock())
    for user, t6, notion in ((1, None, NOTION_PLAIN), (2, t6_only, None), (3, T6, "c" * 32)):
        store._db.execute(
            "INSERT INTO members (telegram_user_id, status, contact_page_id, contact_t6_id,"
            " contact_notion_id, created_at, updated_at) VALUES (?, 'approved', ?, ?, ?, 0, 0)",
            (user, t6 or notion, t6, notion),
        )
    store.learn_contact_ids([(T6, NOTION), (t6_only, other), (T6, "d" * 32)])
    assert columns(store, 1)[1:] == (T6, NOTION_PLAIN)
    assert columns(store, 2)[1:] == (t6_only, other)
    assert columns(store, 3)[1:] == (T6, "c" * 32)  # a row with both is not changed


@pytest.fixture
async def api(config):
    rig = Rig(config, [start_from(1, username="example_handle")], [])
    rig.worker._contacts = PeopleDirectory(FakeAPI(), rig.notion)
    app = create_app(config, rig.store, Notifier(), Notifier())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        yield rig, client


async def notify(client, contact, config):
    body = {"contact": contact, "fallback": FALLBACK, "text": "hello"}
    secret = {"Authorization": "Bearer " + "w" * 40}
    return await client.post("/notify", json=body, headers=secret)


async def test_a_member_approved_through_the_people_directory_is_reached_by_both_ids(api, config):
    from tests.fakes import contact

    rig, client = api
    rig.notion.contacts = [contact(PAGE_ID, "Example Person")]
    await rig.settle()
    rig.clock.advance(10)
    tap(rig, 2, buttons(rig.card())[0]["callback_data"])
    await rig.settle()
    assert columns(rig.store, USER) == (PERSON_ID, PERSON_ID, PAGE_ID.replace("-", ""))
    for form in (PAGE_ID, PAGE_ID.replace("-", ""), PERSON_ID):
        assert (await notify(client, form, config)).status_code == 202
    targets = [r["target"] for r in rig.store.due_outbound(10**12) if r["kind"] == "message"]
    assert targets.count(str(USER)) >= 3


@pytest.mark.parametrize("stored", [NOTION, T6])
async def test_a_row_with_one_id_is_found_by_the_id_it_has(api, config, stored):
    rig, client = api
    rig.store._db.execute(
        "INSERT INTO members (telegram_user_id, status, contact_page_id, created_at, updated_at)"
        " VALUES (77, 'approved', ?, 0, 0)",
        (stored,),
    )
    assert (await notify(client, stored, config)).status_code == 202
    assert rig.store.due_outbound(10**12)[-1]["target"] == "77"


@pytest.mark.parametrize(
    "bad", ["t6n_" + "a" * 31, "t6n_" + "A" * 32, "t6n_" + "g" * 32, "t6x_" + "a" * 32, "nope", ""]
)
async def test_a_malformed_contact_id_is_still_422(api, config, bad):
    _, client = api
    assert (await notify(client, bad, config)).status_code == 422


def legacy_member(rig, user=9999):
    """An approved member stored before the T6 IDs: under the Notion page id only."""
    rig.store._db.execute(
        "INSERT INTO members (telegram_user_id, status, contact_page_id, created_at, updated_at)"
        " VALUES (?, 'approved', ?, 0, 0)",
        (user, PAGE_ID),
    )
    rig.store._db.execute(
        "UPDATE members SET contact_notion_id = REPLACE(contact_page_id, '-', '')"
    )
    rig.store._db.commit()


async def test_the_auto_approve_guard_sees_a_member_stored_under_the_notion_page_id(config):
    from tests.fakes import contact

    rig = Rig(config, [start_from(1, username="example_handle")], [contact(PAGE_ID, "X")])
    legacy_member(rig)
    api = FakeAPI()
    api.person["telegram_user_id"] = USER  # the Contact already holds this user's id
    rig.worker._contacts = PeopleDirectory(api, rig.notion)
    await rig.settle()
    assert rig.store.member(USER)["status"] == "pending"
    assert "Approve Example Person" in [b["text"] for b in buttons(rig.card())]


async def test_the_approve_guard_sees_a_member_stored_under_the_notion_page_id(config):
    from tests.fakes import contact

    rig = Rig(config, [start_from(1, username="example_handle")], [contact(PAGE_ID, "X")])
    legacy_member(rig)
    rig.worker._contacts = PeopleDirectory(FakeAPI(), rig.notion)
    await rig.settle()
    rig.clock.advance(10)
    tap(rig, 2, buttons(rig.card())[0]["callback_data"])
    await rig.settle()
    assert rig.store.member(USER)["status"] == "pending"
    assert rig.notion.writes == []
    assert rig.telegram.answers[-1]["alert"]


async def test_listing_contacts_during_onboarding_completes_a_legacy_row(config):
    from tests.fakes import contact

    rig = Rig(config, [start_from(1, username="example_handle")], [contact(PAGE_ID, "X")])
    legacy_member(rig)
    rig.worker._contacts = PeopleDirectory(FakeAPI(), rig.notion)
    await rig.settle()
    assert columns(rig.store, 9999) == (PAGE_ID, PERSON_ID, PAGE_ID.replace("-", ""))


CREATED = "cccccccc-0000-0000-0000-000000000001"  # the page FakeNotion makes first


@pytest.mark.parametrize("listed", [True, False])
async def test_a_created_contact_is_stored_like_a_linked_one(config, listed):
    rig = Rig(config, [start_from(1, username="nobody_here")], [])
    api = FakeAPI()
    api.person = {**api.person, "name": "Someone Else", "telegram_handle": "other"}
    rig.worker._contacts = PeopleDirectory(api, rig.notion)
    await rig.settle()
    if listed:  # the t6 API already lists the page the Create button is about to make
        api.person = {**api.person, "page_id": CREATED}
    rig.clock.advance(10)
    create = next(b for b in buttons(rig.card()) if b["text"].startswith("Create new"))
    tap(rig, 2, create["callback_data"])
    await rig.settle()
    assert rig.store.member(USER)["status"] == "approved"
    notion = CREATED.replace("-", "")
    expected = (PERSON_ID, PERSON_ID, notion) if listed else (CREATED, None, notion)
    assert columns(rig.store, USER) == expected

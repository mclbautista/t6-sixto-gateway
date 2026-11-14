"""The Contacts adapter and onboarding on t6-core's fake Notion (no Notion calls of our own)."""

import pytest
from t6.domain import Contact
from t6.notion.generated import ContactsRow
from t6.notion.testing import FakeNotion as T6FakeNotion
from t6.notion.testing import rich_text, title

from sixto_gateway.contacts import ContactsError, CoreContacts
from tests.test_onboarding import (
    USER,
    Rig,
    approve_data,
    config,  # noqa: F401  (pytest fixture)
    data_of,
    start_from,
    tap,
)

SOURCE = "ds-contacts"
JUAN_ID = "aaaaaaaa-0000-0000-0000-000000000001"


def notion_with_juan() -> T6FakeNotion:
    fake = T6FakeNotion()
    fake.add_snapshot_data_source(SOURCE, "contacts")
    fake.add_page(
        SOURCE,
        {
            ContactsRow.notion_name("name"): title("Juan Dela Cruz"),
            ContactsRow.notion_name("telegram_handle"): rich_text("@JuanDC"),
        },
        page_id=JUAN_ID,
    )
    return fake


def adapter(fake: T6FakeNotion) -> CoreContacts:
    return CoreContacts("ntn_secret", SOURCE, "2026-03-11", transport=fake.transport)


async def test_the_adapter_reads_contacts_in_domain_terms():
    contacts = adapter(notion_with_juan())
    (juan,) = await contacts.list_contacts()
    assert isinstance(juan, Contact)
    assert (juan.name, juan.telegram_handle, juan.telegram_user_id) == (
        "Juan Dela Cruz",
        "@JuanDC",
        None,
    )
    await contacts.close()


async def test_setting_the_id_returns_the_saved_contact():
    fake = notion_with_juan()
    contacts = adapter(fake)
    saved = await contacts.set_telegram_user_id(JUAN_ID, 42)
    assert saved.telegram_user_id == 42
    assert (await contacts.get_contact(JUAN_ID)).telegram_user_id == 42
    await contacts.close()


async def test_a_renamed_property_changes_nothing_for_the_gateway():
    fake = notion_with_juan()
    fake.rename_property(SOURCE, "TelegramUserID", "Telegram ID")
    fake.rename_property(SOURCE, "TelegramHandle", "Handle")
    contacts = adapter(fake)
    saved = await contacts.set_telegram_user_id(JUAN_ID, 42)
    assert (saved.telegram_user_id, saved.telegram_handle) == (42, "@JuanDC")
    await contacts.close()


async def test_create_saves_the_fields_and_one_note_line():
    fake = notion_with_juan()
    contacts = adapter(fake)
    made = await contacts.create_contact("Mia Cruz", "miac", 7, "Created by Sixto /start on X.")
    assert (made.name, made.telegram_handle, made.telegram_user_id) == ("Mia Cruz", "miac", 7)
    assert len(await contacts.list_contacts()) == 2
    await contacts.close()


async def test_a_refused_or_uncertain_write_is_a_contacts_error_without_the_key():
    fake = notion_with_juan()
    contacts = adapter(fake)
    fake.fail_next(400, code="validation_error")
    with pytest.raises(ContactsError) as refused:
        await contacts.set_telegram_user_id(JUAN_ID, 42)
    fake.time_out_next()
    with pytest.raises(ContactsError) as uncertain:
        await contacts.set_telegram_user_id(JUAN_ID, 42)
    fake.fail_next(503, code="service_unavailable", times=3)
    with pytest.raises(ContactsError) as read:
        await contacts.list_contacts()
    for caught in (refused, uncertain, read):
        assert "ntn_secret" not in str(caught.value)
    await contacts.close()


async def test_onboarding_approves_through_the_real_adapter(config):  # noqa: F811
    fake = notion_with_juan()
    rig = Rig(config, [start_from(1)], [])
    rig.notion = rig.worker._contacts = adapter(fake)
    await rig.settle()
    card = rig.card()
    assert "Juan Dela Cruz" in card["text"]
    tap(rig, 2, approve_data(rig))
    await rig.settle()
    assert rig.store.member(USER)["status"] == "approved"
    assert (await rig.notion.get_contact(JUAN_ID)).telegram_user_id == USER
    await rig.notion.close()


async def test_onboarding_creates_a_contact_through_the_real_adapter(config):  # noqa: F811
    fake = notion_with_juan()
    rig = Rig(config, [start_from(1, username="newperson", first="New", last="Person")], [])
    rig.notion = rig.worker._contacts = adapter(fake)
    await rig.settle()
    tap(rig, 2, data_of(rig.card(), "Create new Contact"))
    await rig.settle()
    member = rig.store.member(USER)
    assert member["status"] == "approved"
    made = await rig.notion.get_contact(member["contact_page_id"])
    assert (made.name, made.telegram_user_id) == ("New Person", USER)
    await rig.notion.close()

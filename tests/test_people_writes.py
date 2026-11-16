"""Onboarding writes go through the t6 API as the operator who tapped (#99).

The first half drives ``PeopleDirectory`` against a fake API client that follows the API's
rules (preview then confirm, one commit per idempotency key). The second half taps the card.
"""

import pytest

from sixto_gateway import strings
from sixto_gateway.people import PeopleError, Refusal, write_key
from tests.fakes import APPROVER, PERSON_ID, FakeAPI
from tests.test_onboarding import USER, Rig, buttons, start_from, tap
from tests.test_onboarding import config as config_fixture

config = config_fixture

OTHER = "t6n_" + "2" * 32
OTHER_PAGE = "aaaaaaaa-0000-0000-0000-000000000002"


async def person(api):
    return await api.directory().get_contact(PERSON_ID)


def other_person(telegram_user_id=None):
    return {
        "t6_id": OTHER,
        "page_id": OTHER_PAGE,
        "name": "Other Person",
        "telegram_handle": "other_handle",
        "aliases": [],
        "telegram_user_id": telegram_user_id,
        "kind": "staff",
    }


# --- the directory over a fake API client -----------------------------------------------------


async def test_link_is_a_preview_then_a_confirm_with_one_key_as_the_operator():
    api = FakeAPI()
    saved = await api.directory().link_telegram(await person(api), USER, APPROVER)

    assert saved.telegram_user_id == str(USER)
    (preview, confirm) = api.calls
    assert preview[:2] == ("set-telegram", APPROVER) and preview[3] is None
    assert confirm[:2] == ("set-telegram", APPROVER) and confirm[3] is not None
    assert preview[2] == confirm[2] == write_key("link", USER, PERSON_ID)


async def test_replace_id_overwrites_the_old_id_and_reads_the_new_one_back():
    api = FakeAPI()
    api.person["telegram_user_id"] = 987
    saved = await api.directory().link_telegram(await person(api), USER, APPROVER)
    assert saved.telegram_user_id == str(USER) and api.person["telegram_user_id"] == USER


async def test_create_makes_a_person_of_the_given_kind_and_reads_it_back():
    api = FakeAPI()
    made = await api.directory().create_contact(
        "New Person", "newperson", USER, "a note", APPROVER, "client"
    )

    assert made.page_id == api.persons[-1]["t6_id"] and made.telegram_user_id == str(USER)
    assert api.persons[-1]["kind"] == "client"
    assert api.persons[-1]["telegram_handle"] == "newperson"
    assert {c[1] for c in api.calls} == {APPROVER}
    assert {c[2] for c in api.calls} == {write_key("create", USER)}


async def test_a_create_retried_after_ambiguous_uses_the_same_key_and_makes_no_second_person():
    api = FakeAPI()
    api.pending_creates = 1
    directory = api.directory()
    for _ in range(2):  # the API stays unsure until a person checks the Contact
        with pytest.raises(PeopleError):
            await directory.create_contact(
                "New Person", "newperson", USER, "a note", APPROVER, "staff"
            )
    assert len(api.persons) == 1
    assert {c[2] for c in api.calls} == {write_key("create", USER)}


async def test_an_id_held_by_another_active_person_is_refused():
    api = FakeAPI()
    api.persons.append(other_person(USER))
    with pytest.raises(Refusal) as refused:
        await api.directory().link_telegram(await person(api), USER, APPROVER)
    assert refused.value.rule == "telegram_id"
    assert api.person["telegram_user_id"] is None


async def test_a_lost_confirm_answer_is_retried_safely_with_the_same_key():
    api = FakeAPI()
    api.lose_answers = 1
    directory = api.directory()
    with pytest.raises(PeopleError):
        await directory.link_telegram(await person(api), USER, APPROVER)
    assert api.person["telegram_user_id"] == USER  # it was written; only the answer was lost

    saved = await directory.link_telegram(await person(api), USER, APPROVER)
    assert saved.telegram_user_id == str(USER)
    assert {c[2] for c in api.calls} == {write_key("link", USER, PERSON_ID)}


async def test_an_approver_without_the_permission_is_refused_and_nothing_is_written():
    api = FakeAPI()
    api.operators = {APPROVER}
    directory = api.directory()
    with pytest.raises(Refusal) as refused:
        await directory.link_telegram(await person(api), USER, 4242)
    assert refused.value.rule == "permission"
    with pytest.raises(Refusal):
        await directory.create_contact("New Person", "", USER, "a note", 4242, "staff")
    assert api.person["telegram_user_id"] is None and len(api.persons) == 1


async def test_the_card_does_not_say_linked_when_the_read_back_lacks_the_id():
    api = FakeAPI()
    api._commit = lambda verb, inputs: {}  # the API says Done but the Person shows no ID
    with pytest.raises(PeopleError, match="does not show"):
        await api.directory().link_telegram(await person(api), USER, APPROVER)


def test_the_key_is_stable_and_differs_by_member_and_contact():
    assert write_key("link", 1, "a") == write_key("link", 1, "a")
    assert (
        len({write_key("link", 1, "a"), write_key("link", 2, "a"), write_key("link", 1, "b")}) == 3
    )
    assert write_key("link", 1, "a") != write_key("create", 1, "a")


# --- tapping the card -------------------------------------------------------------------------


async def card_rig(config, api, username="example_handle"):
    rig = Rig(config, [start_from(1, username=username, first="Example", last="Person")], [])
    rig.worker._contacts = api.directory()
    await rig.settle()
    rig.clock.advance(10)
    return rig


def button(rig, label):
    return next(b["callback_data"] for b in buttons(rig.card()) if b["text"].startswith(label))


async def test_an_id_held_by_another_person_answers_already_linked(config):
    api = FakeAPI()
    rig = await card_rig(config, api)
    api.persons.append(other_person(USER))  # taken after the card was sent
    tap(rig, 2, button(rig, "Approve"))
    await rig.settle()
    assert rig.telegram.answers[-1]["text"] == strings.TAP_ALREADY_LINKED
    assert rig.store.member(USER)["status"] == "pending" and rig.sleeps == []


async def test_an_approver_without_the_permission_sees_the_refusal(config):
    api = FakeAPI()
    api.operators = set()
    rig = await card_rig(config, api)
    tap(rig, 2, button(rig, "Approve"))
    await rig.settle()
    answer = rig.telegram.answers[-1]
    assert answer["alert"] and "needs operator" in answer["text"]
    assert rig.store.member(USER)["status"] == "pending" and rig.sleeps == []
    assert api.person["telegram_user_id"] is None


async def test_a_lost_confirm_answer_is_retried_by_the_worker_and_the_member_is_approved(config):
    api = FakeAPI()
    api.lose_answers = 1
    rig = await card_rig(config, api)
    tap(rig, 2, button(rig, "Approve"))
    await rig.settle()
    assert rig.sleeps == [1.0]
    assert rig.store.member(USER)["status"] == "approved"
    assert api.person["telegram_user_id"] == USER


async def test_a_create_after_ambiguous_offers_retry_and_makes_one_person(config):
    api = FakeAPI()
    api.pending_creates = 1
    rig = await card_rig(config, api, username="nobody_here")
    tap(rig, 2, button(rig, "Create new"))
    await rig.settle()
    tap(
        rig,
        3,
        next(b["callback_data"] for b in buttons(rig.telegram.edits[-1]) if b["text"] == "Staff"),
    )
    await rig.settle()
    assert rig.store.member(USER)["status"] == "pending"
    assert "Write failed" in rig.telegram.edits[-1]["text"]
    assert len(api.persons) == 1
    assert {c[2] for c in api.calls} == {write_key("create", USER)}


async def test_create_writes_a_contact_with_the_note_dated_from_the_start(config):
    api = FakeAPI()
    rig = await card_rig(config, api, username="nobody_here")
    rig.clock.advance(3 * 86400)  # the tap comes days after the Start; a retry must send one note
    tap(rig, 2, button(rig, "Create new"))
    await rig.settle()
    tap(
        rig,
        3,
        next(b["callback_data"] for b in buttons(rig.telegram.edits[-1]) if b["text"] == "Staff"),
    )
    await rig.settle()
    assert rig.store.member(USER)["status"] == "approved"
    assert api.persons[-1]["kind"] == "staff"
    assert api.persons[-1]["note"] == "Created by Sixto /start on 2026-09-18."
    assert {c[1] for c in api.calls} == {APPROVER}

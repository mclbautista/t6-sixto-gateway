"""Exercise onboarding through the synchronous Person client seam."""

from tests.fakes import APPROVER, PAGE_ID, PERSON_ID, FakeAPI
from tests.test_onboarding import USER, Rig, buttons, start_from, tap
from tests.test_onboarding import config as config_fixture

config = config_fixture


async def test_person_card_and_approval_reply(config):
    from tests.fakes import contact

    rig = Rig(
        config,
        [start_from(1, username="example_handle", first="Example", last="Person")],
        [contact(PAGE_ID, "Example Person")],
    )
    api = FakeAPI()
    rig.worker._contacts = api.directory()
    await rig.settle()
    card = rig.card()
    assert "Approve Example Person" in [b["text"] for b in buttons(card)]
    assert PERSON_ID in buttons(card)[0]["callback_data"]
    rig.clock.advance(10)
    tap(rig, 2, buttons(card)[0]["callback_data"])
    await rig.settle()
    assert "You are all set" in rig.texts_to(USER)[-1]
    assert PAGE_ID.replace("-", "") in rig.texts_to(USER)[-1]
    assert api.person["telegram_user_id"] == USER
    assert {operator for _, operator, _, _ in api.calls} == {APPROVER}  # written as the approver


async def test_old_card_is_reoffered(config):
    from tests.fakes import contact

    rig = Rig(
        config,
        [start_from(1, username="example_handle", first="Example", last="Person")],
        [contact(PAGE_ID, "Example Person", handle="example_handle")],
    )
    await rig.settle()
    old_button = buttons(rig.card())[0]["callback_data"]
    api = FakeAPI()
    rig.worker._contacts = api.directory()
    tap(rig, 2, old_button)
    await rig.settle()
    assert "Approve Example Person" in [b["text"] for b in buttons(rig.telegram.edits[-1])]
    assert PERSON_ID in buttons(rig.telegram.edits[-1])[0]["callback_data"]
    assert api.calls == []


async def test_unavailable_offers_retry(config):
    rig = Rig(config, [start_from(1, first="Example", last="Person")], [])
    api = FakeAPI()
    api.unavailable = True
    rig.worker._contacts = api.directory()
    await rig.settle()
    for seconds in (60, 240, 1500):
        rig.clock.advance(seconds)
        await rig.settle()
    card = rig.card()
    assert "Lookup failed" in card["text"]
    assert "Retry" in [b["text"] for b in buttons(card)]
    api.unavailable = False
    tap(rig, 2, buttons(card)[0]["callback_data"])
    await rig.settle()
    assert "Example Person" in rig.telegram.edits[-1]["text"]
    assert rig.telegram.answers[-1]["callback_id"]


def test_api_secret_is_redacted_from_logs_and_config_errors(config, tmp_path):
    from sixto_gateway.config import ConfigError, load_config
    from sixto_gateway.logsafe import Redactor

    secret = config.secrets.t6_api_secret
    assert (
        Redactor(config.secrets.redaction_values()).scrub(f"error {secret}") == "error [redacted]"
    )
    path = tmp_path / "bad.yaml"
    path.write_text(f"modules: {{{secret}: {{secret_env: MISSING}}}}\n")
    import pytest

    with pytest.raises(ConfigError) as error:
        load_config(path, {"GATEWAY_T6_API_SECRET": secret})
    assert secret not in str(error.value)


def found(match=None, method="none", score=0.0, candidates=()):
    return {
        "match": match,
        "method": method,
        "score": score,
        "needs_review": method == "review",
        "reason": None,
        "candidates": [{**c, "score": 1.0} for c in candidates],
    }


async def test_find_sends_the_mention_and_suggest_and_maps_a_decided_match():
    api = FakeAPI()
    api.finds["@example_handle"] = found(api.person, "exact_id", 1.0, [api.person])
    (contact,) = await api.directory().find("@example_handle")
    assert contact.page_id == PERSON_ID and contact.notion_page_id == PAGE_ID
    assert api.find_calls == [("@example_handle", False)]


async def test_find_without_suggest_offers_nothing_for_a_fuzzy_answer():
    api = FakeAPI()
    api.finds["@exampel"] = found(None, "fuzzy", 0.7, [api.person])
    assert await api.directory().find("@exampel") == []


async def test_find_without_suggest_offers_the_tied_matches():
    api = FakeAPI()
    twin = {
        **api.person,
        "t6_id": "t6n_" + "2" * 32,
        "page_id": "aaaaaaaa-0000-0000-0000-000000000002",
    }
    api.finds["@shared"] = found(None, "review", 1.0, [api.person, twin])
    assert [c.page_id for c in await api.directory().find("@shared")] == [PERSON_ID, twin["t6_id"]]


async def test_find_with_suggest_puts_the_decided_match_first_and_stops_at_3():
    api = FakeAPI()
    others = [
        {
            **api.person,
            "t6_id": "t6n_" + str(i) * 32,
            "page_id": f"aaaaaaaa-0000-0000-0000-00000000000{i}",
        }
        for i in (2, 3, 4)
    ]
    api.finds["Example Person"] = found(api.person, "normalized", 1.0, [*others, api.person])
    got = await api.directory().find("Example Person", suggest=True)
    assert [c.page_id for c in got] == [PERSON_ID, others[0]["t6_id"], others[1]["t6_id"]]
    assert api.find_calls == [("Example Person", True)]


async def test_find_that_gets_no_answer_raises_people_error():
    import pytest

    from sixto_gateway.people import PeopleError

    api = FakeAPI()
    api.unavailable = True
    with pytest.raises(PeopleError):
        await api.directory().find("Example Person")

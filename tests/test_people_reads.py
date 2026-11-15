"""Exercise onboarding through the synchronous Person client seam."""

from t6.client import Unavailable
from t6.verbs import Done

from sixto_gateway.people import PeopleDirectory
from tests.test_onboarding import USER, Rig, buttons, start_from, tap
from tests.test_onboarding import config as config_fixture

config = config_fixture

PERSON_ID = "t6n_" + "1" * 32
PAGE_ID = "aaaaaaaa-0000-0000-0000-000000000001"


class FakeAPI:
    def __init__(self):
        self.unavailable = False
        self.mentions = []
        self.person = {
            "t6_id": PERSON_ID,
            "page_id": PAGE_ID,
            "name": "Example Person",
            "telegram_handle": "example_handle",
            "aliases": ["Sample Alias"],
            "telegram_user_id": None,
        }

    def person_list(self, inputs):
        return (
            Unavailable("offline") if self.unavailable else Done(result={"people": [self.person]})
        )

    def person_find(self, inputs):
        self.mentions.append(inputs.mention)
        if self.unavailable:
            return Unavailable("offline")
        return Done(
            result={
                "match": self.person,
                "method": "normalized",
                "score": 1,
                "needs_review": False,
                "reason": None,
                "candidates": [],
            }
        )

    def person_get(self, inputs):
        return Unavailable("offline") if self.unavailable else Done(result={"person": self.person})


async def test_person_card_and_approval_reply(config):
    from tests.fakes import contact

    rig = Rig(
        config,
        [start_from(1, username="example_handle", first="Example", last="Person")],
        [contact(PAGE_ID, "Example Person")],
    )
    api = FakeAPI()
    rig.worker._contacts = PeopleDirectory(api, rig.notion)
    await rig.settle()
    card = rig.card()
    assert "Approve Example Person" in [b["text"] for b in buttons(card)]
    assert PERSON_ID in buttons(card)[0]["callback_data"]
    rig.clock.advance(10)
    tap(rig, 2, buttons(card)[0]["callback_data"])
    await rig.settle()
    assert "You are all set" in rig.texts_to(USER)[-1]
    assert PAGE_ID.replace("-", "") in rig.texts_to(USER)[-1]
    assert rig.notion.writes == [(PAGE_ID, USER)]


async def test_old_card_is_reoffered(config):
    from tests.fakes import contact

    rig = Rig(
        config,
        [start_from(1, username="example_handle", first="Example", last="Person")],
        [contact(PAGE_ID, "Example Person", handle="example_handle")],
    )
    await rig.settle()
    old_button = buttons(rig.card())[0]["callback_data"]
    rig.worker._contacts = PeopleDirectory(FakeAPI(), rig.notion)
    tap(rig, 2, old_button)
    await rig.settle()
    assert "Approve Example Person" in [b["text"] for b in buttons(rig.telegram.edits[-1])]
    assert PERSON_ID in buttons(rig.telegram.edits[-1])[0]["callback_data"]
    assert rig.notion.writes == []


async def test_unavailable_offers_retry(config):
    rig = Rig(config, [start_from(1, first="Example", last="Person")], [])
    api = FakeAPI()
    api.unavailable = True
    rig.worker._contacts = PeopleDirectory(api, rig.notion)
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


async def test_find_falls_back_to_name_and_shows_three_suggestions(config):
    rig = Rig(config, [start_from(1, username="unknown", first="Example", last="Person")], [])
    api = FakeAPI()
    people = [
        {**api.person, "t6_id": "t6n_" + str(i) * 32, "name": f"Placeholder {i}"}
        for i in range(1, 5)
    ]

    def find(inputs):
        api.mentions.append(inputs.mention)
        return Done(
            result={
                "match": None,
                "method": "review",
                "score": 0.7,
                "needs_review": True,
                "reason": None,
                "candidates": [] if inputs.mention == "unknown" else people,
            }
        )

    api.person_find = find
    rig.worker._contacts = PeopleDirectory(api, rig.notion)
    await rig.settle()
    assert api.mentions == ["unknown", "Example Person"]
    assert [b["text"] for b in buttons(rig.card())] == [
        "Pick Placeholder 1",
        "Pick Placeholder 2",
        "Pick Placeholder 3",
        "Create new Contact",
        "Reject",
    ]
    assert "Thanks for pressing Start" in rig.texts_to(USER)[0]
    assert rig.store.member(USER)["status"] == "pending"


async def test_duplicate_id_holders_are_not_auto_approved(config):
    rig = Rig(config, [start_from(1)], [])
    api = FakeAPI()
    api.person["telegram_user_id"] = USER
    other = {**api.person, "t6_id": "t6n_" + "2" * 32}
    api.person_list = lambda inputs: Done(result={"people": [api.person, other]})
    rig.worker._contacts = PeopleDirectory(api, rig.notion)
    await rig.settle()
    assert rig.store.member(USER)["status"] == "pending"
    assert "Approve Example Person" in [b["text"] for b in buttons(rig.card())]
    assert len(rig.texts_to(USER)) == 1


async def test_fuzzy_handle_result_is_presented_as_suggestion(config):
    rig = Rig(config, [start_from(1, username="approximate")], [])
    api = FakeAPI()
    api.person_find = lambda inputs: Done(
        result={
            "match": None,
            "method": "review",
            "score": 0.7,
            "needs_review": True,
            "reason": None,
            "candidates": [api.person],
        }
    )
    rig.worker._contacts = PeopleDirectory(api, rig.notion)
    await rig.settle()
    assert "Pick Example Person" in [b["text"] for b in buttons(rig.card())]
    assert rig.store.member(USER)["status"] == "pending"

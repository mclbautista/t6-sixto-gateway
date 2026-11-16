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

"""``/crm snooze <date>``, ``/crm unsnooze`` and ``/leads`` in a DM (t6-sixto-gateway#135)."""

import pytest

from sixto_gateway import strings
from tests.fakes import OWNER_ID, PROJECT_ID, dm_message, group_command
from tests.test_project_notes import COORDINATOR, OPERATOR, Rig

LEAD = {
    "project_id": PROJECT_ID,
    "page_id": "page",
    "name": "Example Show",
    "stage": "Proposal Made",
    "lead_owner_ids": [OWNER_ID],
    "studio_id": "studio-1",
    "delegate_producer_ids": [],
    "line_producer_ids": ["t6n_" + "c" * 32],
    "prod_coor_supe_ids": [],
    "days_in_stage": 3,
}


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


async def test_snooze_sends_the_date_and_shows_the_api_message(rig):
    await rig.send(dm_message(9, COORDINATOR, "/crm snooze 2026-12-01"))
    assert rig.replies(COORDINATOR) == [
        "Snoozed through 2026-12-01. Reminders return on 2026-12-02."
    ]
    # asked as the person who typed it
    assert rig.api.calls == [(COORDINATOR, "crm snooze", {"until": "2026-12-01"})]
    assert rig.pushes() == []


@pytest.mark.parametrize(
    ("text", "refusal"),
    [
        ("/crm snooze", "Say until when: /crm snooze <date>."),
        ("/crm snooze 2027-01-30", "A snooze can last 30 days at most."),
    ],
)
async def test_snooze_refusals_are_shown_as_is(rig, text, refusal):
    await rig.send(dm_message(9, COORDINATOR, text))
    assert rig.replies(COORDINATOR) == [refusal]


async def test_unsnooze_shows_the_api_message(rig):
    await rig.send(dm_message(9, COORDINATOR, "/crm unsnooze"))
    assert rig.replies(COORDINATOR) == ["Your CRM reminders are back on."]
    assert rig.api.calls == [(COORDINATOR, "crm unsnooze", {})]


async def test_leads_lists_open_leads_with_names(rig):
    rig.api.leads = [LEAD, {**LEAD, "name": "Other", "lead_owner_ids": [], "days_in_stage": None}]
    await rig.send(dm_message(9, COORDINATOR, "/leads"))
    sent = rig.replies(COORDINATOR)
    assert sent[0].startswith("Open Leads: 2\n")
    assert "Example Show · Proposal Made · 3 days in stage" in sent[0]
    assert "Lead Owner: Pat" in sent[0]
    assert "Line Producer: Pat" in sent[0]  # the fake names everyone Pat
    assert "Other · Proposal Made\n" in sent[0] + "\n"  # no day count when none is recorded
    assert (COORDINATOR, "lead list", {}) in rig.api.calls


async def test_no_open_leads_says_so(rig):
    await rig.send(dm_message(9, COORDINATOR, "/leads"))
    assert rig.replies(COORDINATOR) == [strings.LEADS_NONE]


async def test_api_down_is_answered(rig):
    rig.api.unavailable = True
    await rig.send(dm_message(9, COORDINATOR, "/crm snooze 2026-12-01"))
    assert rig.replies(COORDINATOR) == [strings.T6_NOT_ANSWERING]


@pytest.mark.parametrize("text", ["/crm snooze 2026-12-01", "/crm unsnooze", "/leads"])
async def test_in_a_group_a_pointer_to_the_dm_and_nothing_asked(rig, text):
    await rig.send(group_command(9, OPERATOR, text, -1001234567890, 77))
    command = text.split()[0][1:]
    assert [r["body"]["text"] for r in rig.store.due_outbound(rig.clock() + 1)] == [
        strings.CRM_DM_ONLY.format(command=command)
    ]
    assert rig.api.calls == []


async def test_an_edit_is_not_a_new_command_and_other_crm_words_are_not_asked(rig):
    await rig.send(dm_message(9, COORDINATOR, "/crm unsnooze", edited=True))
    await rig.send(dm_message(10, COORDINATOR, "/crm dance"))
    assert rig.api.calls == [] and rig.pushes() == []


async def test_the_date_is_never_audited(rig):
    await rig.send(dm_message(9, COORDINATOR, "/crm snooze 2026-12-01"))
    assert all("2026-12-01" not in (d or "") for _, _, d in rig.store.audit_events())

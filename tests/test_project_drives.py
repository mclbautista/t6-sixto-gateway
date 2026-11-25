"""Project Drive movements through fake Telegram and the t6 API (ticket #138)."""

import pytest
from t6.client import Unavailable
from t6.verbs import Refused

from sixto_gateway import strings
from sixto_gateway.project import MESSAGE_LIMIT
from tests.fakes import OWNER_ID, PROJECT_ID, dm_message, group_command
from tests.test_project_notes import COORDINATOR, OPERATOR, Rig

SECOND_COORDINATOR = "t6n_" + "c" * 32
MOVEMENT = {
    "id": "t6m_" + "d" * 32,
    "drive": "drive-example",
    "label": "Tape <A>",
    "on": "2026-11-25",
    "direction": "OUT",
    "person": "Riley",
    "recorder": None,
    "place": None,
    "project": {"page_id": PROJECT_ID, "text": None},
    "origin": None,
    "capture": "cap-example",
    "corrects": None,
    "description": None,
    "remarks": None,
    "recorder_telegram_id": None,
    "shipment_id": None,
    "posted_at": "2026-11-25T03:00:00+00:00",
    "to_review": True,
    "coordinators": [OWNER_ID, SECOND_COORDINATOR],
}


@pytest.fixture
def rig(tmp_path):
    made = Rig(tmp_path)
    yield made
    made.store.close()


async def test_movements_show_api_order_people_and_coordinators_at_the_move(rig):
    rig.api.names[SECOND_COORDINATOR] = "Kim"
    # Core orders effective movements newest first, including within the same day.
    rig.api.movements[PROJECT_ID] = [
        MOVEMENT,
        {
            **MOVEMENT,
            "id": "t6m_" + "e" * 32,
            "direction": "IN",
            "person": "Sam",
            "label": "Tape B",
            "posted_at": "2026-11-25T01:00:00+00:00",
            "to_review": False,
            "coordinators": [SECOND_COORDINATOR],
        },
    ]
    await rig.send(dm_message(9, COORDINATOR, "/project ABD drives"))
    assert rig.replies(COORDINATOR) == [
        "Drive movements for ABD\n"
        "2026-11-25 · OUT · Tape <A> · Recipient Riley · Coordinator then Pat, Kim · ⚠ to review\n"
        "2026-11-25 · IN · Tape B · Receiver Sam · Coordinator then Kim"
    ]
    assert rig.api.calls[0][0:2] == (COORDINATOR, "project resolve")
    assert rig.api.calls[1] == (
        COORDINATOR,
        "drive project-movements",
        {"project": PROJECT_ID},
    )
    assert [(u, b["person_id"]) for u, v, b in rig.api.calls if v == "person get"] == [
        (COORDINATOR, OWNER_ID),
        (COORDINATOR, SECOND_COORDINATOR),
    ]
    assert rig.store.due_outbound(rig.clock() + 1)[0]["body"]["format"] == "plain"
    assert rig.pushes() == []


async def test_only_the_ten_newest_movements_show_with_a_plain_text_remainder(rig):
    rig.api.movements[PROJECT_ID] = [
        {
            **MOVEMENT,
            "on": f"2026-11-{day:02d}",
            "label": f"Tape {day}",
            "coordinators": [OWNER_ID] if day >= 16 else [SECOND_COORDINATOR],
            "to_review": False,
        }
        for day in range(25, 12, -1)
    ]
    await rig.send(dm_message(9, COORDINATOR, "/project ABD drives"))
    (reply,) = rig.replies(COORDINATOR)
    lines = reply.splitlines()
    assert len(lines) == 12  # title, ten movements, remainder
    assert lines[1] == ("2026-11-25 · OUT · Tape 25 · Recipient Riley · Coordinator then Pat")
    assert lines[10] == ("2026-11-16 · OUT · Tape 16 · Recipient Riley · Coordinator then Pat")
    assert lines[-1] == "3 more in the Drives app"
    assert "Tape 15" not in reply and "Tape 13" not in reply
    # Hidden movements need no name reads; the repeated coordinator is read once.
    assert [b["person_id"] for _, v, b in rig.api.calls if v == "person get"] == [OWNER_ID]
    body = rig.store.due_outbound(rig.clock() + 1)[0]["body"]
    assert body["format"] == "plain" and "keyboard" not in body


async def test_an_empty_history_says_so(rig):
    await rig.send(dm_message(9, COORDINATOR, "/project ABD drives"))
    assert rig.replies(COORDINATOR) == ["No Drive movements for ABD yet."]


async def test_unknown_movement_details_are_shown_without_guessing(rig):
    rig.api.movements[PROJECT_ID] = [
        {**MOVEMENT, "on": None, "label": None, "person": None, "coordinators": []}
    ]
    await rig.send(dm_message(9, COORDINATOR, "/project ABD drives"))
    assert rig.replies(COORDINATOR) == [
        "Drive movements for ABD\n— · OUT · — · Recipient — · Coordinator then — · ⚠ to review"
    ]
    assert [v for _, v, _ in rig.api.calls] == ["project resolve", "drive project-movements"]


async def test_drives_are_read_only_on_demand_outside_the_poll_loop(rig):
    rig.api.movements[PROJECT_ID] = [MOVEMENT]
    assert await rig.worker.process_next() is False
    assert rig.api.calls == [] and rig.store.due_outbound(rig.clock() + 1) == []

    rig.telegram.push(dm_message(9, COORDINATOR, "/project ABD drives"))
    await rig.poller.poll_once()
    assert rig.api.calls == [] and rig.replies(COORDINATOR) == []
    assert rig.pushes() == []
    assert await rig.worker.process_next() is True
    assert "Tape <A>" in rig.replies(COORDINATOR)[0]
    assert await rig.worker.process_next() is False
    assert rig.pushes() == []


@pytest.mark.parametrize("match", [None, "fuzzy", "needs_review"])
async def test_unknown_or_unclear_codes_do_not_read_movements(rig, match):
    if match == "fuzzy":
        rig.api.codes["XYZ"] = {**rig.api.codes["ABD"], "method": "fuzzy"}
    elif match == "needs_review":
        rig.api.codes["XYZ"] = {**rig.api.codes["ABD"], "needs_review": True}
    await rig.send(dm_message(9, COORDINATOR, "/project XYZ drives"))
    assert rig.replies(COORDINATOR) == [strings.PROJECT_UNKNOWN_CODE.format(code="XYZ")]
    assert [v for _, v, _ in rig.api.calls] == ["project resolve"]


async def test_codes_with_spaces_and_mixed_case_drives_resolve_as_usual(rig):
    rig.api.codes["ABC 1"] = {**rig.api.codes["ABD"], "method": "normalized"}
    await rig.send(dm_message(9, COORDINATOR, "/project  ABC 1  DrIvEs"))
    assert rig.api.calls[0][2]["text"] == "ABC 1"
    assert rig.replies(COORDINATOR) == ["No Drive movements for ABD yet."]


@pytest.mark.parametrize(
    ("result", "reply"),
    [
        (
            Refused(rule="permission:reader", reason="Reader permission required"),
            "Reader permission required",
        ),
        (Unavailable("offline"), strings.T6_NOT_ANSWERING),
    ],
)
async def test_a_failed_movement_read_is_answered_once(rig, result, reply):
    rig.api.movement_result = result
    await rig.send(dm_message(9, COORDINATOR, "/project ABD drives"))
    assert rig.replies(COORDINATOR) == [reply]
    assert rig.store.next_project_request() is None
    assert rig.api.calls[-1] == (
        COORDINATOR,
        "drive project-movements",
        {"project": PROJECT_ID},
    )
    assert rig.pushes() == []


async def test_in_a_group_nothing_is_said_or_read(rig):
    await rig.send(group_command(9, OPERATOR, "/project ABD drives", -900009, 77))
    assert rig.store.due_outbound(rig.clock() + 1) == []
    assert rig.api.calls == [] and rig.pushes() == []
    assert ("command_refused", None, f"command=project drives from={OPERATOR} not_dm") in (
        rig.store.audit_events()
    )


async def test_an_edit_and_a_refetch_answer_once_without_auditing_the_code(rig):
    await rig.send(dm_message(9, COORDINATOR, "/project ABD drives"))
    await rig.send(dm_message(9, COORDINATOR, "/project ABD drives", edited=True))
    rig.store.set_offset(0)
    await rig.send(dm_message(9, COORDINATOR, "/project ABD drives"))
    assert len(rig.replies(COORDINATOR)) == 1
    assert [v for _, v, _ in rig.api.calls] == ["project resolve", "drive project-movements"]
    assert all("ABD" not in (detail or "") for _, _, detail in rig.store.audit_events())


async def test_long_labels_split_at_lines_within_telegram_limits_and_keep_the_cap(rig):
    rig.api.movements[PROJECT_ID] = [
        {**MOVEMENT, "label": f"Tape {n:02d} " + "😀" * 600} for n in range(12)
    ]
    await rig.send(dm_message(9, COORDINATOR, "/project ABD drives"))
    replies = rig.replies(COORDINATOR)
    assert len(replies) > 1
    assert all(len(text.encode("utf-16-le")) // 2 <= MESSAGE_LIMIT for text in replies)
    lines = [line for reply in replies for line in reply.splitlines()]
    assert len(lines) == 12
    assert lines[0] == "Drive movements for ABD"
    assert all(f"Tape {n:02d} " in lines[n + 1] for n in range(10))
    assert lines[-1] == "2 more in the Drives app"

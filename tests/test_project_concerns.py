"""``/project concerns``: open Concerns with Close, Confirm and Undo (ticket #124)."""

import pytest

from sixto_gateway import strings
from sixto_gateway.project import MESSAGE_LIMIT
from tests.fakes import button_tap, dm_message, group_command
from tests.test_project_notes import COORDINATOR, STRANGER, Rig

C1 = "t6c_" + "1" * 32
C2 = "t6c_" + "2" * 32
CLOSER = COORDINATOR


@pytest.fixture
def rig(tmp_path):
    rig = Rig(tmp_path)
    rig.api.add_concern(C1, "Waiting on lab for the DCP")
    rig.api.add_concern(C2, "Client to send KDM")
    return rig


def bodies(rig, chat):
    return [
        row["body"]
        for row in rig.store.due_outbound(rig.clock() + 1)
        if row["body"]["chat"] == chat
    ]


def buttons(body):
    return [(b["text"], b["callback_data"]) for row in body["keyboard"] for b in row]


async def tap(rig, update_id, data, user=COORDINATOR, message=100):
    await rig.send(button_tap(update_id, user, data, user, message))


async def listed(rig, user=COORDINATOR):
    await rig.send(dm_message(1, user, "/project concerns"))
    return bodies(rig, user)


def last_edit(rig):
    return rig.telegram.edits[-1]


async def test_each_open_concern_is_listed_with_a_close_button(rig):
    messages = await listed(rig)
    assert messages[0]["text"] == "Open Concerns: 2"
    assert messages[1]["text"] == "ABD · Waiting on lab for the DCP\nOwner Pat · open 4 days"
    assert buttons(messages[1]) == [("Close", f"pc:c:{C1}")]
    assert buttons(messages[2]) == [("Close", f"pc:c:{C2}")]
    assert [v for _, v, _ in rig.api.calls if v.startswith("concern")] == ["concern list"]


async def test_a_pending_close_is_listed_with_undo(rig):
    rig.api.concerns[C2].update(pending=True, close_by_name="Cy")
    messages = await listed(rig)
    assert messages[2]["text"].endswith("Closing after PSR (by Cy)")
    assert buttons(messages[2]) == [("Undo", f"pc:u:{C2}")]


async def test_no_open_concerns_says_so(rig):
    rig.api.concerns.clear()
    assert [m["text"] for m in await listed(rig)] == [strings.CONCERNS_NONE]


async def test_close_shows_confirm_and_confirm_leaves_a_pending_close_with_undo(rig):
    rig.api.names[COORDINATOR] = "Cy"
    await tap(rig, 10, f"pc:c:{C1}")
    edit = last_edit(rig)
    assert edit["text"].endswith(strings.CONCERN_CONFIRM_QUESTION)
    assert [b["text"] for row in edit["keyboard"] for b in row] == ["Confirm", "Cancel"]
    assert rig.api.concerns[C1]["pending"] is False  # a preview changes nothing
    assert rig.telegram.answers[-1]["callback_id"] == "cb10"

    await tap(rig, 11, f"pc:y:{C1}")
    assert rig.api.concerns[C1]["pending"] is True
    edit = last_edit(rig)
    assert edit["text"].endswith("Closing after PSR (by Cy)")
    assert edit["keyboard"] == [[{"text": "Undo", "callback_data": f"pc:u:{C1}"}]]
    assert edit["message_id"] == 100 and edit["parse_mode"] is None
    # the whole flow is asked as the person who tapped, with one idempotency key
    writes = [
        (u, v, b["idempotency_key"], b["confirm"])
        for u, v, b in rig.api.calls
        if v == "concern resolve"
    ]
    assert writes == [
        (COORDINATOR, "concern resolve", "close:10", None),
        (COORDINATOR, "concern resolve", "close:10", "token-0"),
    ]
    assert rig.store.concern_preview(COORDINATOR, 100) is None


async def test_undo_cancels_the_pending_close(rig):
    await tap(rig, 10, f"pc:c:{C1}")
    await tap(rig, 11, f"pc:y:{C1}")
    await tap(rig, 12, f"pc:u:{C1}")
    assert rig.api.concerns[C1]["pending"] is False
    edit = last_edit(rig)
    assert edit["text"] == "ABD · Waiting on lab for the DCP\nOwner Pat · open 4 days"
    assert edit["keyboard"] == [[{"text": "Close", "callback_data": f"pc:c:{C1}"}]]


async def test_cancel_goes_back_to_close_and_forgets_the_preview(rig):
    await tap(rig, 10, f"pc:c:{C1}")
    await tap(rig, 11, f"pc:x:{C1}")
    assert last_edit(rig)["keyboard"][0][0]["text"] == "Close"
    assert rig.store.concern_preview(COORDINATOR, 100) is None
    await tap(rig, 12, f"pc:y:{C1}")  # a Confirm of the forgotten preview
    assert rig.telegram.answers[-1] == {
        "callback_id": "cb12",
        "text": strings.TAP_EXPIRED,
        "alert": True,
    }
    assert rig.api.concerns[C1]["pending"] is False


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (lambda api: setattr(api, "closers", set()), "Only Closers may close a Concern"),
        (lambda api: api.rule_made.add(C1), "Date-sanity Concerns follow data only"),
        (lambda api: api.concerns[C1].update(pending=True), "Concern cannot make that transition"),
    ],
)
async def test_a_refusal_is_shown_as_is_and_nothing_is_edited(rig, setup, reason):
    setup(rig.api)
    await tap(rig, 10, f"pc:c:{C1}")
    assert rig.telegram.answers[-1] == {"callback_id": "cb10", "text": reason, "alert": True}
    assert rig.telegram.edits == []


async def test_undo_after_a_veto_shows_the_state_refusal_and_what_is_true_now(rig):
    await tap(rig, 10, f"pc:u:{C1}")  # the close was already cancelled: nothing to undo
    assert rig.telegram.answers[-1]["text"] == "Concern cannot make that transition"
    assert last_edit(rig)["keyboard"][0][0]["text"] == "Close"


async def test_a_concern_that_is_no_longer_open_is_said_so(rig):
    del rig.api.concerns[C1]
    await tap(rig, 10, f"pc:c:{C1}")
    assert rig.telegram.answers[-1]["text"] == "Concern not found"


async def test_api_down_on_a_tap_is_answered(rig):
    rig.api.unavailable = True
    await tap(rig, 10, f"pc:c:{C1}")
    assert rig.telegram.answers[-1] == {
        "callback_id": "cb10",
        "text": strings.T6_NOT_ANSWERING,
        "alert": True,
    }
    assert rig.store.next_project_request() is None  # a tap is never retried


async def test_a_stranger_is_refused_by_the_api_not_the_gateway(rig):
    rig.api.closers = {COORDINATOR}
    await tap(rig, 10, f"pc:c:{C1}", user=STRANGER)
    assert rig.telegram.answers[-1]["text"] == "Only Closers may close a Concern"
    assert rig.api.calls[-1][0] == STRANGER


async def test_in_a_group_it_points_to_the_dm_and_reads_nothing(rig):
    await rig.send(group_command(9, COORDINATOR, "/project concerns", -1001234567890, 77))
    texts = [r["body"]["text"] for r in rig.store.due_outbound(rig.clock() + 1)]
    assert texts == [strings.CONCERNS_DM_ONLY]
    assert rig.api.calls == [] and rig.pushes() == []


async def test_other_taps_still_go_to_onboarding(rig):
    await rig.send(button_tap(10, COORDINATOR, "ob:ok:1:abc", COORDINATOR, 100))
    assert rig.store.next_project_request() is None
    assert rig.store.next_tap() is not None


async def test_a_long_list_is_split_into_whole_concerns_within_the_limit(rig):
    for n in range(40):
        rig.api.add_concern("t6c_" + f"{n:032d}", f"item {n:02d} " + "x" * 3000)
    messages = await listed(rig)
    assert all(len(m["text"]) <= MESSAGE_LIMIT for m in messages)
    assert len(messages) == 43 and all("keyboard" in m for m in messages[1:])
    assert any("item 39" in m["text"] and m["text"].count("item") == 1 for m in messages)


async def test_no_text_is_stored_beyond_the_reply_queue(rig):
    await tap(rig, 10, f"pc:c:{C1}")
    saved = rig.store.concern_preview(COORDINATOR, 100)
    assert set(saved) == {"chat_id", "message_id", "concern", "token", "idempotency_key"}
    assert all("Concern" not in (d or "") for _, _, d in rig.store.audit_events())

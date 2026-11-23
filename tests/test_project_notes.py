"""``/project <code> notes``: a Project's PM Notes history in a DM (ticket #125)."""

import pytest

from sixto_gateway import strings
from sixto_gateway.config import load_config
from sixto_gateway.notifier import Notifier
from sixto_gateway.poller import Poller
from sixto_gateway.project import MESSAGE_LIMIT, ProjectCommands, ProjectWorker, split_message
from sixto_gateway.store import Store
from tests.fakes import (
    GATEWAY_ENV,
    ONBOARDING_YAML,
    PROJECT_ID,
    FakeClock,
    FakeProjectAPI,
    FakeTelegram,
    dm_message,
    group_command,
)

CORE_SECRET = "k" * 40
ENV = {"TELEGRAM_BOT_TOKEN": "123456:" + "t" * 35, "CORE_SECRET": CORE_SECRET, **GATEWAY_ENV}
OPERATOR, COORDINATOR, STRANGER = 555, 556, 999
PUSH_URL = "http://198.51.100.9:8790/tap"

LINES = [
    {
        "as_of": "2026-11-23",
        "text": "Grade booked Friday",
        "first_seen": "2026-11-24",
        "last_seen": "2026-11-25",
        "removed": None,
    },
    {
        "as_of": "2026-11-23",
        "text": "Grade booked",
        "first_seen": "2026-11-23",
        "last_seen": "2026-11-23",
        "removed": "2026-11-24",
    },
    {
        "as_of": "2026-11-16",
        "text": "Client sent notes",
        "first_seen": "2026-11-16",
        "last_seen": "2026-11-25",
        "removed": None,
    },
]


class Rig:
    def __init__(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "modules: {t6core: {secret_env: CORE_SECRET, delivery: push, push_url: "
            + PUSH_URL
            + "}}\n"
            f"commands: {{project: {{module: t6core, allow: [{OPERATOR}]}}}}\n"
            f"database_path: {tmp_path / 'gateway.db'}\n" + ONBOARDING_YAML
        )
        self.clock = FakeClock()
        self.config = load_config(path, ENV)
        self.store = Store(self.config.database_path, clock=self.clock)
        self.telegram = FakeTelegram([], self.clock)
        self.api = FakeProjectAPI()
        self.project_wake = Notifier()
        self.poller = Poller(
            self.store,
            self.telegram,
            self.config,
            Notifier(),
            clock=self.clock,
            project_wake=self.project_wake,
        )
        self.worker = ProjectWorker(
            self.store,
            ProjectCommands(self.api.as_user, self.store),
            self.project_wake,
            Notifier(),
            self.telegram,
        )

    async def send(self, raw):
        self.telegram.push(raw)
        await self.poller.poll_once()
        while await self.worker.process_next():
            pass

    def replies(self, chat):
        return [
            row["body"]["text"]
            for row in self.store.due_outbound(self.clock() + 1)
            if row["body"]["chat"] == chat
        ]

    def pushes(self):
        return self.store.due_pushes(self.clock() + 1)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


async def test_history_is_listed_newest_first_with_removed_lines_marked(rig):
    rig.api.history[PROJECT_ID] = LINES
    await rig.send(dm_message(9, COORDINATOR, "/project ABD notes"))
    assert rig.replies(COORDINATOR) == [
        "PM Notes history for ABD\n"
        "2026-11-23 · Grade booked Friday\n"
        "2026-11-23 · Grade booked (removed 2026-11-24)\n"
        "2026-11-16 · Client sent notes"
    ]
    # asked as the person who typed it, so the API decides who may read it
    assert [(user, verb) for user, verb, _ in rig.api.calls] == [
        (COORDINATOR, "project resolve"),
        (COORDINATOR, "project notes-history"),
    ]
    assert rig.api.calls[1][2] == {"project": PROJECT_ID}
    assert rig.pushes() == []  # answered by the gateway, never pushed to t6core


async def test_anyone_may_ask_in_a_dm_and_the_api_refusal_is_shown_as_is(rig):
    rig.api.allowed = {COORDINATOR}
    await rig.send(dm_message(9, STRANGER, "/project ABD notes"))
    assert rig.replies(STRANGER) == [
        "Only Closers and the Project's coordinators may read its PM Notes history"
    ]


async def test_an_unknown_code_is_answered_plainly(rig):
    await rig.send(dm_message(9, COORDINATOR, "/project XYZ notes"))
    assert rig.replies(COORDINATOR) == [strings.PROJECT_UNKNOWN_CODE.format(code="XYZ")]
    assert [verb for _, verb, _ in rig.api.calls] == ["project resolve"]


async def test_a_code_that_needs_review_is_not_guessed(rig):
    rig.api.codes["AB"] = {**rig.api.codes["ABD"], "method": "fuzzy"}
    await rig.send(dm_message(9, COORDINATOR, "/project AB notes"))
    assert rig.replies(COORDINATOR) == [strings.PROJECT_UNKNOWN_CODE.format(code="AB")]


async def test_a_code_with_a_space_and_any_case_of_notes(rig):
    rig.api.codes["ABC 1"] = rig.api.codes["ABD"]
    await rig.send(dm_message(9, COORDINATOR, "/project  ABC 1  Notes"))
    assert rig.api.calls[0][2]["text"] == "ABC 1"


async def test_an_empty_history_says_so(rig):
    await rig.send(dm_message(9, COORDINATOR, "/project ABD notes"))
    assert rig.replies(COORDINATOR) == [strings.PROJECT_NOTES_NONE.format(code="ABD")]


async def test_api_down_is_answered_and_nothing_is_retried(rig):
    rig.api.unavailable = True
    await rig.send(dm_message(9, COORDINATOR, "/project ABD notes"))
    assert rig.replies(COORDINATOR) == [strings.T6_NOT_ANSWERING]
    assert rig.store.next_project_request() is None


async def test_in_a_group_nothing_is_said_or_read(rig):
    rig.api.history[PROJECT_ID] = LINES
    await rig.send(group_command(9, OPERATOR, "/project ABD notes", -1001234567890, 77))
    assert rig.store.due_outbound(rig.clock() + 1) == []
    assert rig.api.calls == [] and rig.pushes() == []
    assert ("command_refused", None, f"command=project notes from={OPERATOR} not_dm") in (
        rig.store.audit_events()
    )


async def test_an_edit_and_a_refetch_answer_once(rig):
    rig.api.history[PROJECT_ID] = LINES
    await rig.send(dm_message(9, COORDINATOR, "/project ABD notes"))
    await rig.send(dm_message(9, COORDINATOR, "/project ABD notes", edited=True))
    rig.store.set_offset(0)  # Telegram serves the same update again
    await rig.send(dm_message(9, COORDINATOR, "/project ABD notes"))
    assert len(rig.replies(COORDINATOR)) == 1
    assert len([c for c in rig.api.calls if c[1] == "project resolve"]) == 1


async def test_other_project_commands_still_go_to_t6core(rig):
    await rig.send(dm_message(9, OPERATOR, "/project ABD"))
    assert len(rig.pushes()) == 1 and rig.api.calls == []


async def test_the_code_is_never_audited(rig):
    await rig.send(dm_message(9, COORDINATOR, "/project ABD notes"))
    assert all("ABD" not in (detail or "") for _, _, detail in rig.store.audit_events())


async def test_a_long_history_is_split_between_lines(rig):
    many = [{**LINES[2], "text": f"line {n:03d} " + "x" * 90} for n in range(120)]
    rig.api.history[PROJECT_ID] = many
    await rig.send(dm_message(9, COORDINATOR, "/project ABD notes"))
    messages = rig.replies(COORDINATOR)
    assert len(messages) > 1
    assert all(len(m) <= MESSAGE_LIMIT for m in messages)
    sent = [line for m in messages for line in m.split("\n")]
    assert sent[0] == "PM Notes history for ABD"
    assert sent[1:] == [f"2026-11-16 · line {n:03d} " + "x" * 90 for n in range(120)]


def test_split_counts_telegram_units_and_cuts_only_a_line_too_long_to_send():
    assert split_message(["a", "b"], limit=3) == ["a\nb"]
    assert split_message(["ab", "cd"], limit=4) == ["ab", "cd"]
    assert split_message(["😀😀", "x"], limit=5) == ["😀😀", "x"]  # an emoji is 2 units
    assert split_message(["x", "abcdefg", "y"], limit=4) == ["x", "ab", "cd", "ef", "g\ny"]

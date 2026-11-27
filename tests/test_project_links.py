"""``/project links`` in a chat: the links card, buttons first, the unlinked topic (t6-core#685)."""

import pytest

from sixto_gateway import strings
from sixto_gateway.config import load_config
from sixto_gateway.notifier import Notifier
from sixto_gateway.poller import Poller
from sixto_gateway.project import ProjectCommands, ProjectWorker
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

ENV = {"TELEGRAM_BOT_TOKEN": "123456:" + "t" * 35, "CORE_SECRET": "k" * 40, **GATEWAY_ENV}
OPERATOR, COORDINATOR, FREELANCER = 555, 556, 999
CHAT, TOPIC = -1001, 12
OTHER_ID = "t6p_" + "c" * 32
PUSH_URL = "http://198.51.100.9:8790/tap"


def card(project_id, code, name, chat_links=(), **more):
    return {
        "project_id": project_id,
        "name": name,
        "code": code,
        "notion_url": f"https://www.notion.so/{code.lower()}",
        "frameio_url": f"https://next.frame.io/project/{code.lower()}",
        "chat_links": list(chat_links),
        "links": {},
        "links_unavailable": False,
        **more,
    }


def link(state, topic=TOPIC, name="ABD - Edit", **more):
    return {
        "id": None,
        "chat": CHAT,
        "topic": topic,
        "topic_name": name,
        "state": state,
        "dead_since": None,
        "approval_id": None,
        "requested_by": None,
        "decided_by": None,
        "reason": None,
        **more,
    }


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
        self.api.cards[PROJECT_ID] = card(
            PROJECT_ID, "ABD", "Example Show", [link("active"), link("waiting", 13, "ABD - Grade")]
        )
        self.api.chat_links[(CHAT, TOPIC)] = [PROJECT_ID]
        self.api.link_operators = {OPERATOR}
        self.api.link_coordinators = {COORDINATOR}
        self.wake = Notifier()
        self.poller = Poller(
            self.store,
            self.telegram,
            self.config,
            Notifier(),
            clock=self.clock,
            project_wake=self.wake,
        )
        self.worker = ProjectWorker(
            self.store,
            ProjectCommands(self.api.as_user, self.api.service),
            self.wake,
            Notifier(),
        )

    async def send(self, raw):
        self.telegram.push(raw)
        await self.poller.poll_once()
        while await self.worker.process_next():
            pass

    def bodies(self, chat=CHAT):
        return [
            row["body"]
            for row in self.store.due_outbound(self.clock() + 1)
            if row["body"]["chat"] == chat
        ]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# --- #132: the card in a linked chat ------------------------------------------------------


async def test_anyone_in_a_linked_topic_gets_the_card_as_a_reply(rig):
    await rig.send(group_command(9, FREELANCER, "/project links", CHAT, TOPIC))
    [body] = rig.bodies()
    assert body["format"] == "html"
    assert body["topic"] == TOPIC and body["reply_to_message_id"] == 9
    assert body["text"] == (
        "<b>Example Show [ABD]</b>\n"
        "Telegram:\n"
        "• ABD - Edit: active\n"
        "• ABD - Grade: waiting for approval\n"
        '<a href="https://www.notion.so/abd">Notion page</a>\n'
        '<a href="https://next.frame.io/project/abd">Frame.io project</a>'
    )
    # asked with the gateway's own token (no Person record needed), never pushed to t6core
    assert rig.api.calls == [
        (
            None,
            "project links",
            {
                "project": None,
                "chat": CHAT,
                "topic": TOPIC,
                "topic_name": None,
                "asker_telegram_id": FREELANCER,
            },
        )
    ]
    assert rig.store.due_pushes(rig.clock() + 1) == []


async def test_the_card_shows_every_state_and_escapes_text(rig):
    rig.api.cards[PROJECT_ID] = card(
        PROJECT_ID,
        "ABD",
        "Rock <&> Roll",
        [
            link("active", topic=None, name=None),
            link("inactive", 2, "Two"),
            link("dead", 3, "Three", dead_since="2026-11-20"),
            link("denied", 4, "Four", reason="wrong show <x>"),
            link("denied", 5, "Five"),
        ],
        notion_url=None,
        frameio_url=None,
        links={"quote": ["https://example.invalid/q"], "Proposal": ["https://example.invalid/p"]},
        links_unavailable=False,
    )
    await rig.send(group_command(9, FREELANCER, "/project links", CHAT, TOPIC))
    [body] = rig.bodies()
    assert body["text"] == (
        "<b>Rock &lt;&amp;&gt; Roll [ABD]</b>\n"
        "Telegram:\n"
        "• whole chat: active\n"
        "• Two: inactive\n"
        "• Three: dead since 2026-11-20\n"
        "• Four: denied (wrong show &lt;x&gt;)\n"
        "• Five: denied\n"
        "quote: https://example.invalid/q\n"
        "Proposal: https://example.invalid/p"
    )


async def test_links_unavailable_is_said_and_a_card_with_no_links_is_still_a_card(rig):
    rig.api.cards[PROJECT_ID] = card(PROJECT_ID, "ABD", "Example Show", links_unavailable=True)
    await rig.send(group_command(9, FREELANCER, "/project links", CHAT, TOPIC))
    [body] = rig.bodies()
    assert body["text"].splitlines()[1:] == [
        '<a href="https://www.notion.so/abd">Notion page</a>',
        '<a href="https://next.frame.io/project/abd">Frame.io project</a>',
        "Links unavailable right now",
    ]


async def test_a_code_works_for_a_project_linked_to_the_chat(rig):
    await rig.send(group_command(9, FREELANCER, "/project abd links", CHAT, TOPIC))
    [body] = rig.bodies()
    assert body["text"].startswith("<b>Example Show [ABD]</b>")
    assert [user for user, *_ in rig.api.calls] == [None]


async def test_a_code_not_linked_to_the_chat_is_refused_for_a_non_operator(rig):
    rig.api.cards[OTHER_ID] = card(OTHER_ID, "XYZ", "Other Show")
    await rig.send(group_command(9, FREELANCER, "/project XYZ links", CHAT, TOPIC))
    [body] = rig.bodies()
    assert body["text"] == "Project is not linked to this chat"
    # the second try as the person typing was refused too: the first refusal is shown
    assert [user for user, *_ in rig.api.calls] == [None, FREELANCER]


async def test_an_operator_reads_any_project_in_a_group_and_in_a_dm(rig):
    rig.api.cards[OTHER_ID] = card(OTHER_ID, "XYZ", "Other Show")
    await rig.send(group_command(9, OPERATOR, "/project XYZ links", CHAT, TOPIC))
    await rig.send(dm_message(10, OPERATOR, "/project XYZ links"))
    [in_group] = rig.bodies()
    [in_dm] = rig.bodies(OPERATOR)
    assert in_group["text"].startswith("<b>Other Show [XYZ]</b>")
    assert in_dm["text"] == in_group["text"]
    assert "reply_to_message_id" not in in_dm
    assert [user for user, *_ in rig.api.calls] == [None, OPERATOR, OPERATOR]


async def test_a_non_operator_in_a_dm_is_refused_and_a_dm_needs_a_code(rig):
    await rig.send(dm_message(9, FREELANCER, "/project ABD links"))
    await rig.send(dm_message(10, OPERATOR, "/project links"))
    assert [b["text"] for b in rig.bodies(FREELANCER)] == ["Only an operator may read this"]
    assert [b["text"] for b in rig.bodies(OPERATOR)] == [strings.PROJECT_LINKS_DM_CODE]


async def test_the_project_allow_list_does_not_apply_and_the_api_being_down_is_said(rig):
    rig.api.unavailable = True
    await rig.send(group_command(9, FREELANCER, "/project links", CHAT, TOPIC))
    assert [b["text"] for b in rig.bodies()] == [strings.T6_NOT_ANSWERING]


async def test_other_project_commands_in_a_group_are_untouched(rig):
    await rig.send(group_command(9, FREELANCER, "/project ABD notes", CHAT, TOPIC))
    assert rig.bodies() == [] and rig.api.calls == []


async def test_an_edited_links_command_is_not_answered_again(rig):
    raw = group_command(9, FREELANCER, "/project links", CHAT, TOPIC)
    raw = {"update_id": 9, "edited_message": {**raw["message"], "edit_date": 1789700100}}
    await rig.send(raw)
    assert rig.bodies() == []

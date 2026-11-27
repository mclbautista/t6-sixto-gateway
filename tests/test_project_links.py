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
    button_tap,
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
        "frameio_url": f"https://frames.example.invalid/project/{code.lower()}",
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
            self.telegram,
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

    def add_second_project(self):
        self.api.cards[OTHER_ID] = card(OTHER_ID, "XYZ", "Other Show")
        self.api.chat_links[(CHAT, TOPIC)] = [PROJECT_ID, OTHER_ID]


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
        '<a href="https://frames.example.invalid/project/abd">Frame.io project</a>'
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
        '<a href="https://frames.example.invalid/project/abd">Frame.io project</a>',
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


# --- #133: buttons first in a chat with several Projects -----------------------------------


async def test_two_projects_give_one_buttons_message_with_the_topics_own_project_first(rig):
    rig.add_second_project()
    await rig.send(
        group_command(9, FREELANCER, "/project links", CHAT, TOPIC, topic_name="XYZ - Edit")
    )
    [body] = rig.bodies()
    assert body["text"] == strings.PROJECT_LINKS_PICK
    assert body["topic"] == TOPIC and body["reply_to_message_id"] == 9
    assert body["keyboard"] == [
        [{"text": "XYZ · Other Show", "callback_data": f"pl:{OTHER_ID}"}],
        [{"text": "ABD · Example Show", "callback_data": f"pl:{PROJECT_ID}"}],
    ]


async def test_buttons_keep_link_order_when_the_topic_name_names_neither(rig):
    rig.add_second_project()
    await rig.send(
        group_command(9, FREELANCER, "/project links", CHAT, TOPIC, topic_name="General")
    )
    [body] = rig.bodies()
    assert [row[0]["callback_data"] for row in body["keyboard"]] == [
        f"pl:{PROJECT_ID}",
        f"pl:{OTHER_ID}",
    ]


async def test_the_topic_name_comes_from_the_cards_when_the_command_carries_none(rig):
    rig.add_second_project()
    for project_id in (PROJECT_ID, OTHER_ID):  # the topic has one name, whichever card tells it
        rig.api.cards[project_id]["chat_links"] = [link("active", TOPIC, "XYZ - Edit")]
    await rig.send(group_command(9, FREELANCER, "/project links", CHAT, TOPIC))
    [body] = rig.bodies()
    assert body["keyboard"][0][0]["callback_data"] == f"pl:{OTHER_ID}"


async def test_anyone_may_tap_and_the_message_becomes_that_projects_card(rig):
    rig.add_second_project()
    await rig.send(group_command(9, FREELANCER, "/project links", CHAT, TOPIC))
    rig.api.calls.clear()
    await rig.send(button_tap(10, 777, f"pl:{OTHER_ID}", CHAT, 55, "supergroup", TOPIC))
    assert rig.telegram.edits == [
        {
            "chat_id": CHAT,
            "message_id": 55,
            "text": (
                "<b>Other Show [XYZ]</b>\n"
                '<a href="https://www.notion.so/xyz">Notion page</a>\n'
                '<a href="https://frames.example.invalid/project/xyz">Frame.io project</a>'
            ),
            "parse_mode": "HTML",
            "keyboard": None,
        }
    ]
    assert rig.telegram.answers[-1]["text"] is None and not rig.telegram.answers[-1]["alert"]
    assert rig.api.calls == [
        (
            None,
            "project links",
            {
                "project": OTHER_ID,
                "chat": CHAT,
                "topic": TOPIC,
                "topic_name": None,
                "asker_telegram_id": None,
            },
        )
    ]


async def test_a_tap_for_a_project_no_longer_linked_is_a_short_toast_and_no_edit(rig):
    rig.add_second_project()
    rig.api.chat_links[(CHAT, TOPIC)] = [PROJECT_ID]
    await rig.send(button_tap(10, 777, f"pl:{OTHER_ID}", CHAT, 55, "supergroup", TOPIC))
    assert rig.telegram.edits == []
    assert rig.telegram.answers[-1]["text"] == strings.PROJECT_LINKS_UNLINKED_TAP
    assert not rig.telegram.answers[-1]["alert"]


async def test_a_tap_while_t6_is_down_is_an_alert_and_leaves_the_buttons(rig):
    rig.api.unavailable = True
    await rig.send(button_tap(10, 777, f"pl:{PROJECT_ID}", CHAT, 55, "supergroup", TOPIC))
    assert rig.telegram.edits == []
    assert rig.telegram.answers[-1]["text"] == strings.T6_NOT_ANSWERING
    assert rig.telegram.answers[-1]["alert"]


# --- #134: an unlinked topic ----------------------------------------------------------------


async def test_an_unlinked_topic_says_so_to_anyone(rig):
    await rig.send(group_command(9, FREELANCER, "/project links", CHAT, 99, topic_name="General"))
    [body] = rig.bodies()
    assert body["text"] == "This topic is not linked to a Project."
    assert body["format"] == "plain" and body["topic"] == 99 and body["reply_to_message_id"] == 9


async def test_a_coordinator_in_a_topic_named_with_a_code_gets_the_link_hint(rig):
    await rig.send(
        group_command(9, COORDINATOR, "/project links", CHAT, 99, topic_name="ABC123 - Edit")
    )
    [body] = rig.bodies()
    assert body["text"] == (
        "This topic is not linked to a Project.\nSend /link to link it to ABC123."
    )
    # the topic's name and the asker go to the API, which says who may link
    assert rig.api.calls[0][2]["topic_name"] == "ABC123 - Edit"
    assert rig.api.calls[0][2]["asker_telegram_id"] == COORDINATOR


async def test_others_never_see_the_link_hint(rig):
    await rig.send(
        group_command(9, FREELANCER, "/project links", CHAT, 99, topic_name="ABC123 - Edit")
    )
    assert [b["text"] for b in rig.bodies()] == ["This topic is not linked to a Project."]


async def test_a_coordinator_in_a_topic_without_a_code_in_its_name_gets_no_hint(rig):
    await rig.send(group_command(9, COORDINATOR, "/project links", CHAT, 99, topic_name="Edit"))
    assert [b["text"] for b in rig.bodies()] == ["This topic is not linked to a Project."]

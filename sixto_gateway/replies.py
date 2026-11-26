"""DM replies to a module's own message (issue #149).

A plain DM message that replies to a message a module sent with ``/notify`` goes to that
module as a ``reply`` item, pushed to its ``push_url`` like taps and commands. The item adds
``reply_to_message_ref``: the ``/notify`` ref of the message being replied to, so the module
can tell which of its messages the answer is for. Only people the module's commands allow
are routed; anyone else's reply, and a reply to another module's message, is not.
"""

from __future__ import annotations

from sixto_gateway.commands import allowed_commands
from sixto_gateway.config import Config
from sixto_gateway.decision import Decision
from sixto_gateway.store import Store
from sixto_gateway.telegram.normalize import IncomingUpdate


def decide_reply(config: Config, store: Store, update: IncomingUpdate) -> Decision | None:
    """The decision for a DM reply to a module's ``/notify`` message, else None."""
    if (
        update.chat_type != "private"
        or update.command is not None
        or update.reply_to_message_id is None
        or update.from_id is None
        or not update.text.strip()
    ):
        return None
    sent = store.notify_ref(update.chat_id, update.reply_to_message_id)
    if sent is None:
        return None
    module, ref = sent
    module_config = config.modules.get(module)
    if module_config is None or module_config.delivery != "push":
        return None
    if update.edited:  # an edit is never a second answer
        return Decision(None)
    allowed = allowed_commands(config).get(update.from_id, [])
    if not any(config.commands[name].module == module for name in allowed):
        return Decision(None, audit=("reply_refused", module, f"from={update.from_id}"))
    return Decision(
        module,
        kind="reply",
        audit=("reply_routed", module, f"from={update.from_id} ref={ref}"),  # never the text
        push=True,
        reply_to_message_ref=ref,
    )

"""Commands and the allow-list (spec sections 6 step 3 and 8).

``Decision`` is what happens to one incoming update. ``register_menus`` sets the per-person
``/`` menus at boot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sixto_gateway.config import Config
from sixto_gateway.telegram.client import TelegramClient
from sixto_gateway.telegram.normalize import IncomingUpdate

if TYPE_CHECKING:  # the store imports this module for Decision
    from sixto_gateway.store import Store

log = logging.getLogger(__name__)
REFUSAL = "That command isn't available to you."
GATEWAY_MODULE = "gateway"  # owner of the gateway's own outbound rows (command refusals)


@dataclass(frozen=True)
class Decision:
    """Where an update goes, and what is saved or queued in the same transaction."""

    module: str | None  # owning module, or None: stored, not delivered
    kind: str = "update"  # "update" or "command"
    audit: tuple[str, str | None, str] | None = None  # (event, module, detail)
    reply: str | None = None  # text sent back to the sender's DM through the send queue


def allowed_commands(config: Config) -> dict[int, list[str]]:
    """Per Telegram user id, the commands they may use (allow entries are ids or group names)."""
    out: dict[int, list[str]] = {}
    for name, command in config.commands.items():
        users: set[int] = set()
        for entry in command.allow:
            users.update(config.groups[entry] if isinstance(entry, str) else [entry])
        for user in users:
            out.setdefault(user, []).append(name)
    return out


def decide_command(config: Config, update: IncomingUpdate) -> Decision:
    """The decision for a command typed in a private chat; the allow-list is checked here."""
    assert update.command is not None
    if update.edited:  # an edit is never a new command: no delivery, no second reply
        return Decision(None)
    sender = update.from_id
    command = config.commands.get(update.command)
    detail = f"command={update.command} from={sender}"  # never the args
    allowed = allowed_commands(config).get(sender, []) if sender is not None else []
    if command is not None and update.command in allowed:
        return Decision(command.module, "command", ("command_routed", command.module, detail))
    return Decision(None, audit=("command_refused", None, detail), reply=REFUSAL)


async def register_menus(store: Store, client: TelegramClient, config: Config) -> None:
    """Make Telegram's menus match the config: an empty default plus one menu per allowed person.

    People who had a menu at an earlier boot and are no longer on any list lose it. A Telegram
    refusal for one person (for example they never opened a chat with the bot) is audited and
    does not stop boot; the next boot tries again.
    """
    await client.set_my_commands(None, [])
    wanted = allowed_commands(config)
    for user, names in sorted(wanted.items()):
        menu = [(n, config.commands[n].description or n) for n in sorted(names)]
        try:
            await client.set_my_commands(user, menu)
        except Exception as exc:  # noqa: BLE001 - one person must not stop boot
            _menu_failed(store, "set", user, exc)
        else:
            store.add_menu_user(user)
    for user in sorted(store.menu_users() - wanted.keys()):
        try:
            await client.delete_my_commands(user)
        except Exception as exc:  # noqa: BLE001
            _menu_failed(store, "delete", user, exc)
        else:
            store.remove_menu_user(user)


def _menu_failed(store: Store, action: str, user: int, exc: Exception) -> None:
    error = f"{type(exc).__name__}: {exc}"[:300]
    log.warning("menu %s for user %s failed: %s", action, user, error)
    store.audit("menu_failed", detail=f"{action} user={user} {error}")

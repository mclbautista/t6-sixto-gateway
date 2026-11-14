"""Commands and the allow-list (spec sections 6 step 3 and 8).

``Decision`` is what happens to one incoming update. ``register_menus`` sets the per-person
``/`` menus at boot.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from functools import partial

from sixto_gateway import strings
from sixto_gateway.config import Config
from sixto_gateway.decision import Decision
from sixto_gateway.store import Store
from sixto_gateway.telegram.client import TelegramClient
from sixto_gateway.telegram.errors import PermanentSendError, RetryAfterError
from sixto_gateway.telegram.normalize import IncomingUpdate

log = logging.getLogger(__name__)
REFUSAL = "That command isn't available to you."


def allowed_commands(config: Config) -> dict[int, list[str]]:
    """Per Telegram user id, the commands they may use (allow entries are ids or group names)."""
    out: dict[int, list[str]] = {}
    for name, command in config.commands.items():
        users: set[int] = set()
        for entry in command.allow:
            users.update(config.group(entry) if isinstance(entry, str) else [entry])
        for user in users:
            out.setdefault(user, []).append(name)
    return out


def decide_command(config: Config, update: IncomingUpdate) -> Decision:
    """The decision for a command typed in a private chat; the allow-list is checked here."""
    assert update.command is not None
    if update.edited:  # an edit is never a new command: no delivery, no second reply
        return Decision(None)
    sender = update.from_id
    detail = f"command={update.command} from={sender}"  # never the args
    allowed = allowed_commands(config).get(sender, []) if sender is not None else []
    if update.command in allowed:  # only names from config.commands can be in the list
        module = config.commands[update.command].module
        assert module is not None  # built-ins are answered by decide_builtin, never here
        push = config.modules[module].delivery == "push"
        return Decision(module, "command", ("command_routed", module, detail), push=push)
    return Decision(None, audit=("command_refused", None, detail), reply=REFUSAL)


def is_builtin(config: Config, update: IncomingUpdate) -> bool:
    """True when the gateway answers this command itself (it is configured as a built-in)."""
    command = config.commands.get(update.command or "")
    return command is not None and command.builtin


def decide_builtin(config: Config, update: IncomingUpdate) -> Decision:
    """A built-in command (``/whereami``): answered here, never delivered to a module.

    An allowed caller gets the answer in the chat and topic it was sent from (a hint in a DM).
    Anyone else gets the standard refusal in a DM and silence in a group (the attempt is audited).
    """
    assert update.command is not None
    if update.edited:  # an edit never gets a second reply
        return Decision(None)
    sender = update.from_id
    detail = f"command={update.command} chat={update.chat_id} from={sender}"  # never the args
    private = update.chat_type == "private"
    if update.command not in (allowed_commands(config).get(sender, []) if sender else []):
        return Decision(
            None, audit=("command_refused", None, detail), reply=REFUSAL if private else None
        )
    audit = ("command_answered", None, detail)
    if private:
        return Decision(None, audit=audit, reply=strings.WHEREAMI_IN_DM)
    if update.topic_id is not None:
        topic = strings.WHEREAMI_TOPIC.format(topic_id=update.topic_id)
    elif update.chat_is_forum:  # Telegram sends no topic id for a forum's General topic
        topic = strings.WHEREAMI_GENERAL_TOPIC
    else:
        topic = strings.WHEREAMI_NO_TOPIC
    text = strings.WHEREAMI_CHAT.format(chat_id=update.chat_id) + "\n" + topic
    return Decision(None, audit=audit, reply=text, reply_format="html")


async def register_menus(
    store: Store,
    client: TelegramClient,
    config: Config,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> None:
    """Make Telegram's menus match the config: an empty default plus one menu per allowed person.

    People who had a menu at an earlier boot and are no longer on any list lose it. A Telegram
    refusal for one person (for example they never opened a chat with the bot) is audited and
    does not stop boot; the next boot tries again.

    A person is recorded before their menu is set and stays recorded until a delete succeeds,
    so a crash or failure at any point (Telegram may have set it anyway) never leaves a menu
    that a later boot does not know to remove. A delete Telegram refuses for good (the chat was
    never opened, or the bot is blocked) forgets the person instead of retrying every boot.

    Menu calls share the bot-wide 429 pause with the sender: each waits out a stored pause, and
    a 429 here extends it.
    """

    async def respecting_pause(request: Callable[[], Awaitable[None]]) -> None:
        wait = store.paused_until() - clock()
        if wait > 0:
            await sleep(wait)
        try:
            await request()
        except RetryAfterError as exc:
            store.extend_pause(clock() + exc.retry_after)
            raise

    await respecting_pause(partial(client.set_my_commands, None, []))
    wanted = allowed_commands(config)
    for user, names in sorted(wanted.items()):
        menu = [(n, config.commands[n].description or n) for n in sorted(names)]
        store.add_menu_user(user)
        try:
            await respecting_pause(partial(client.set_my_commands, user, menu))
        except Exception as exc:  # noqa: BLE001 - one person must not stop boot
            _menu_failed(store, "set", user, exc)
    for user in sorted(store.menu_users() - wanted.keys()):
        try:
            await respecting_pause(partial(client.delete_my_commands, user))
        except PermanentSendError as exc:  # chat not found or bot blocked: no retry can work
            _menu_failed(store, "delete", user, exc)
            store.remove_menu_user(user)
        except Exception as exc:  # noqa: BLE001
            _menu_failed(store, "delete", user, exc)
        else:
            store.remove_menu_user(user)


def _menu_failed(store: Store, action: str, user: int, exc: Exception) -> None:
    error = f"{type(exc).__name__}: {exc}"[:300]
    log.warning("menu %s for user %s failed: %s", action, user, error)
    store.audit("menu_failed", detail=f"{action} user={user} {error}")

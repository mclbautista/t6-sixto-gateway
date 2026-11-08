"""What happens to one incoming update (shared by the command policy, poller and store)."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, unquote

GATEWAY_MODULE = "gateway"  # owner of the gateway's own outbound rows (command refusals)
MODULE_CALLBACK_PREFIX = "mod"  # callback data of a module's own button: mod:<module>:<payload>


def module_callback_data(module: str, payload: str) -> str:
    """The callback data of a module's button. The module is percent-encoded (colons too)."""
    return f"{MODULE_CALLBACK_PREFIX}:{quote(module, safe='')}:{payload}"


def parse_module_callback(data: str) -> tuple[str, str] | None:
    """``(module, payload)`` from a module button's callback data, else None.

    The payload is opaque and may itself contain colons.
    """
    prefix, _, rest = data.partition(":")
    module, sep, payload = rest.partition(":")
    if prefix != MODULE_CALLBACK_PREFIX or not sep or not module or not payload:
        return None
    return unquote(module), payload


@dataclass(frozen=True)
class NewMember:
    """A person who pressed Start: saved as a ``pending`` member in the same transaction."""

    user_id: int
    handle: str | None
    first_name: str
    last_name: str


@dataclass(frozen=True)
class NewTap:
    """A tap on a button of the approver's card: saved for the onboarding worker, never routed."""

    callback_id: str
    from_id: int
    chat_id: int
    message_id: int
    data: str


@dataclass(frozen=True)
class Decision:
    """Where an update goes, and what is saved or queued in the same transaction."""

    module: str | None  # owning module, or None: stored, not delivered
    kind: str = "update"  # "update" or "command"
    audit: tuple[str, str | None, str] | None = None  # (event, module, detail)
    reply: str | None = None  # text sent back in the update's own chat and topic (send queue)
    reply_format: str = "plain"  # "plain" or "html" (html text must already be escaped)
    extra_audits: tuple[tuple[str, str | None, str], ...] = ()  # written after ``audit``
    member: NewMember | None = None  # onboarding: save a pending member (wakes the worker)
    unblock_member: int | None = (
        None  # onboarding: a blocked member pressed Start; back to approved
    )
    tap: NewTap | None = None  # onboarding: save a button tap (wakes the worker)
    push: bool = False  # the delivery is posted to the module's push URL, not read from /updates
    answer_tap: str | None = None  # a routed tap: the gateway answers it, with this text or ""

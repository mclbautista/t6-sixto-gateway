"""What happens to one incoming update (shared by the command policy, poller and store)."""

from __future__ import annotations

from dataclasses import dataclass

GATEWAY_MODULE = "gateway"  # owner of the gateway's own outbound rows (command refusals)


@dataclass(frozen=True)
class Decision:
    """Where an update goes, and what is saved or queued in the same transaction."""

    module: str | None  # owning module, or None: stored, not delivered
    kind: str = "update"  # "update" or "command"
    audit: tuple[str, str | None, str] | None = None  # (event, module, detail)
    reply: str | None = None  # text sent back to the sender's DM through the send queue

"""How a failed send looks to the sender, whatever Telegram client raised it.

The sender retries ``TransientSendError`` on the schedule and fails ``PermanentSendError`` at
once. Anything else a client raises is treated as transient: when in doubt, keep the message.
"""

from __future__ import annotations


class SendError(Exception):
    pass


class TransientSendError(SendError):
    """Network trouble or a Telegram 5xx: worth retrying."""


class RetryAfterError(TransientSendError):
    """Telegram 429: do not try again for ``retry_after`` seconds."""

    def __init__(self, retry_after: float, message: str = "Too Many Requests"):
        super().__init__(f"{message} (retry after {retry_after}s)")
        self.retry_after = float(retry_after)


class PermanentSendError(SendError):
    """A request that can never succeed (chat not found, bad HTML, bot blocked)."""


class BlockedByUserError(PermanentSendError):
    """Telegram 403: the person blocked the bot (or never allowed it to write to them)."""


class ThreadNotFoundError(PermanentSendError):
    """Telegram refused the request because the message thread was not found."""


class MessageNotModifiedError(PermanentSendError):
    """Telegram refused an edit because the text and buttons are already exactly that.

    For an edit this means the message already shows what was asked, so it counts as success.
    """

"""Keeps secrets out of logs and stored error text (spec section 10).

Gateway code never logs a token, a module secret or full message text. This is the backstop for
text it does not control: a network error from the Bot API client can carry the request URL,
which contains the bot token.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import TextIO

REDACTED = "[redacted]"
# Bot API URLs look like /bot<id>:<token>/method; the token alone looks like <id>:<35 chars>.
_BOT_TOKEN = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()):
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        text = _BOT_TOKEN.sub(REDACTED, text)
        return _BEARER.sub(f"Bearer {REDACTED}", text)


class RedactingFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        # formatted whole, so tracebacks and %-arguments are covered too
        return self._redactor.scrub(super().format(record))


def configure_logging(
    redactor: Redactor, stream: TextIO | None = None, level: int = logging.INFO
) -> logging.Handler:
    """Send every log record through the redactor. Replaces any earlier root handlers."""
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter(redactor))
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)
    return handler

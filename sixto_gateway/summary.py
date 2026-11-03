"""Daily onboarding summary for the approver (spec #17, ticket #25).

At ``onboarding.summary_time`` (Asia/Manila) the approver gets one DM: member counts, every
pending card (cards never expire, so this is the reminder), send failures from the last 24 hours
and the expected handles that still have no approved member.

The summary is queued with the key ``summary:<date>``. The outbound table's unique index on
(module, key) is what stops a second summary on the same day, including after a restart.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sixto_gateway import strings
from sixto_gateway.config import Config
from sixto_gateway.decision import GATEWAY_MODULE
from sixto_gateway.notifier import Notifier
from sixto_gateway.onboarding import MANILA, full_name
from sixto_gateway.store import Store
from sixto_gateway.store.db import SECONDS_PER_DAY

log = logging.getLogger(__name__)
CHECK_EVERY_SECONDS = 30.0
MAX_FAILURES_SHOWN = 5
MAX_PENDING_SHOWN = 20  # keeps the message under Telegram's 4096 characters
FAILURE_DETAIL_CHARS = 160


def _format_wait(seconds: float) -> str:
    days = int(seconds // SECONDS_PER_DAY)
    if days <= 0:
        return strings.SUMMARY_WAITING_TODAY
    return strings.SUMMARY_WAITING_DAYS.format(days=days, s="" if days == 1 else "s")


class SummaryWorker:
    def __init__(
        self,
        store: Store,
        config: Config,
        queue_wake: Notifier,
        clock: Callable[[], float] = time.time,
    ):
        self._store = store
        self._approver = config.onboarding.approver
        self._expected = config.onboarding.expected
        hour, minute = config.onboarding.summary_time.split(":")
        self._hour, self._minute = int(hour), int(minute)
        token_id = config.secrets.bot_token.split(":")[0]
        self._bot_id = token_id if token_id.isdigit() else None  # a bot's id starts its token
        self._queue_wake = queue_wake
        self._clock = clock
        self._done_date: str | None = None

    def send_due(self) -> bool:
        """Queue today's summary if its time has come and it is not queued yet."""
        now = self._clock()
        local = datetime.fromtimestamp(now, MANILA)
        if (local.hour, local.minute) < (self._hour, self._minute):
            return False
        date = local.date().isoformat()
        if self._done_date == date:
            return False
        key = f"summary:{date}"
        if self._store.has_outbound(GATEWAY_MODULE, key):  # queued before a restart
            self._done_date = date
            return False
        self._store.enqueue_outbound(
            GATEWAY_MODULE, "message", str(self._approver), self.render(now), key
        )
        self._done_date = date  # only once queued, so a failure here is retried at the next check
        self._store.audit("summary", None, f"date={date}")
        self._queue_wake.notify()
        return True

    def render(self, now: float) -> dict[str, Any]:
        date = datetime.fromtimestamp(now, MANILA).date().isoformat()
        counts = self._store.member_counts()
        lines = [
            f"<b>{strings.SUMMARY_TITLE.format(date=date)}</b>",
            strings.SUMMARY_COUNTS.format(**counts),
            "",
        ]
        lines += self._pending_lines(now)
        lines.append("")
        lines += self._failure_lines(now)
        if self._expected:
            lines.append("")
            lines += self._missing_lines()
        return {
            "chat": self._approver,
            "topic": None,
            "text": "\n".join(lines),
            "format": "html",
        }

    def _pending_lines(self, now: float) -> list[str]:
        pending = self._store.pending_members()
        if not pending:
            return [strings.SUMMARY_NO_PENDING]
        lines = [strings.SUMMARY_PENDING]
        for member in pending[:MAX_PENDING_SHOWN]:
            handle = f"@{member['handle']}" if member["handle"] else strings.CARD_NO_HANDLE
            person = html.escape(f"{full_name(member) or '(no name)'} {handle}")
            lines.append(
                strings.SUMMARY_PENDING_ITEM.format(
                    person=person,
                    waiting=_format_wait(now - member["created_at"]),
                    link=self._card_link(member),
                )
            )
        if len(pending) > MAX_PENDING_SHOWN:
            lines.append(strings.SUMMARY_PENDING_MORE.format(more=len(pending) - MAX_PENDING_SHOWN))
        return lines

    def _card_link(self, member: dict[str, Any]) -> str:
        message_id = member["card_message_id"]
        if message_id is None or self._bot_id is None:
            return strings.SUMMARY_NO_CARD
        # the card is a message in the approver's chat with the bot
        url = f"tg://openmessage?user_id={self._bot_id}&message_id={message_id}"
        return f'<a href="{url}">{strings.SUMMARY_CARD_LINK}</a>'

    def _failure_lines(self, now: float) -> list[str]:
        failures = self._store.send_failures_since(now - SECONDS_PER_DAY)
        if not failures:
            return [strings.SUMMARY_NO_FAILURES]
        lines = [strings.SUMMARY_FAILURES.format(count=len(failures))]
        lines += [
            f"• {html.escape(detail[:FAILURE_DETAIL_CHARS])}"
            for detail in failures[:MAX_FAILURES_SHOWN]
        ]
        if len(failures) > MAX_FAILURES_SHOWN:
            lines.append(
                strings.SUMMARY_FAILURE_MORE.format(more=len(failures) - MAX_FAILURES_SHOWN)
            )
        return lines

    def _missing_lines(self) -> list[str]:
        joined = self._store.approved_handles()
        missing = [h for h in self._expected if h not in joined]
        if not missing:
            return [strings.SUMMARY_NOT_MISSING]
        return [
            strings.SUMMARY_MISSING.format(handles=html.escape(", ".join(f"@{h}" for h in missing)))
        ]

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                self.send_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("onboarding summary failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=CHECK_EVERY_SECONDS)
            except TimeoutError:
                pass

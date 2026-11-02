"""Wakes long-polling API requests when a new delivery is committed.

A waiter takes ``current()`` *before* it reads the store, so a notify that lands between its
read and its wait is not lost: the event it holds is already set.
"""

from __future__ import annotations

import asyncio


class Notifier:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    def current(self) -> asyncio.Event:
        return self._event

    def notify(self) -> None:
        event, self._event = self._event, asyncio.Event()
        event.set()

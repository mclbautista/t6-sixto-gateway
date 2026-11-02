"""Send pacing under Telegram's documented limits (spec section 9).

At most 1 message per second per private chat, 20 per minute per group and 25 per second
overall. Sliding windows over the times of recent sends; the state lives in memory, so a restart
starts with a clean slate (the limits are far looser than one restart's worth of sends).
"""

from __future__ import annotations

from collections import deque

PRIVATE_LIMIT = (1, 1.0)
GROUP_LIMIT = (20, 60.0)
OVERALL_LIMIT = (25, 1.0)


class _Window:
    def __init__(self, count: int, seconds: float):
        self.count = count
        self.seconds = seconds
        self.times: deque[float] = deque()

    def wait(self, now: float) -> float:
        while self.times and now - self.times[0] >= self.seconds:
            self.times.popleft()
        if len(self.times) < self.count:
            return 0.0
        return self.times[-self.count] + self.seconds - now

    def record(self, now: float) -> None:
        self.times.append(now)


class Pacer:
    def __init__(self) -> None:
        self._overall = _Window(*OVERALL_LIMIT)
        self._chats: dict[int, _Window] = {}

    def wait(self, chat_id: int, now: float) -> float:
        """Seconds until a send to ``chat_id`` is allowed; 0 means now."""
        window = self._chats.get(chat_id)
        chat_wait = window.wait(now) if window else 0.0
        return max(chat_wait, self._overall.wait(now))

    def record(self, chat_id: int, now: float) -> None:
        """Note that a request to ``chat_id`` is being made at ``now``."""
        if chat_id not in self._chats:
            self._chats[chat_id] = _Window(*(PRIVATE_LIMIT if chat_id > 0 else GROUP_LIMIT))
        self._chats[chat_id].record(now)
        self._overall.record(now)
        self._forget_idle_chats(now)

    def _forget_idle_chats(self, now: float) -> None:
        if len(self._chats) < 256:
            return
        self._chats = {
            c: w for c, w in self._chats.items() if now - w.times[-1] < w.seconds
        }  # keep only chats whose last send is still inside their window

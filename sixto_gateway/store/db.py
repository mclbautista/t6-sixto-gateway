"""SQLite store (WAL). Single writer connection, used from the event-loop thread only."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sixto_gateway.store.migrations import MIGRATIONS
from sixto_gateway.telegram.normalize import IncomingUpdate


class Store:
    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time):
        self._clock = clock
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._db = sqlite3.connect(path, isolation_level=None)  # explicit transactions
        self._db.row_factory = sqlite3.Row
        path.chmod(0o600)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self._db.close()

    def _migrate(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        for number, script in enumerate(MIGRATIONS, start=1):
            if number <= version:
                continue
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for statement in script.split(";"):
                    if statement.strip():
                        self._db.execute(statement)
                self._db.execute(f"PRAGMA user_version = {number}")
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def _commit(self) -> None:
        self._db.execute("COMMIT")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._commit()
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    # --- polling cursor -------------------------------------------------------------------

    def last_update_id(self) -> int | None:
        row = self._db.execute("SELECT last_update_id FROM poll_state WHERE id = 1").fetchone()
        return row[0] if row else None

    def next_offset(self) -> int:
        """The offset for ``getUpdates``: last committed update id + 1 (0 before any)."""
        last = self.last_update_id()
        return 0 if last is None else last + 1

    # --- updates and deliveries -----------------------------------------------------------

    def save_update(
        self, update: IncomingUpdate, module: str | None, received_at: float | None = None
    ) -> int | None:
        """Save an update, its delivery (if routed) and move the cursor, in one transaction.

        Returns the new delivery's ``seq``, or None if nothing new was delivered (unrouted, or
        the update was already stored, which makes a re-fetch after a crash idempotent).
        """
        received_at = self._clock() if received_at is None else received_at
        seq: int | None = None
        with self._transaction():
            inserted = self._db.execute(
                "INSERT OR IGNORE INTO updates (update_id, chat_id, topic_id, message_id,"
                " from_id, edited, module, raw, received_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    update.update_id,
                    update.chat_id,
                    update.topic_id,
                    update.message_id,
                    update.from_id,
                    int(update.edited),
                    module,
                    json.dumps(update.raw, ensure_ascii=False),
                    received_at,
                ),
            ).rowcount
            if inserted and module is not None:
                payload = json.dumps(_item_payload(update), ensure_ascii=False)
                cursor = self._db.execute(
                    "INSERT INTO deliveries (module, kind, update_id, payload, created_at)"
                    " VALUES (?, 'update', ?, ?, ?)",
                    (module, update.update_id, payload, received_at),
                )
                seq = cursor.lastrowid
            self._db.execute(
                "INSERT INTO poll_state (id, last_update_id) VALUES (1, ?)"
                " ON CONFLICT(id) DO UPDATE SET last_update_id = MAX(last_update_id, ?)",
                (update.update_id, update.update_id),
            )
        return seq

    def advance_cursor(self, update_id: int) -> None:
        """Move the cursor past an update we do not store (a kind we do not handle)."""
        with self._transaction():
            self._db.execute(
                "INSERT INTO poll_state (id, last_update_id) VALUES (1, ?)"
                " ON CONFLICT(id) DO UPDATE SET last_update_id = MAX(last_update_id, ?)",
                (update_id, update_id),
            )

    def module_of_original(self, chat_id: int, message_id: int) -> str | None:
        """The module the original (non-edited) message of an edit was routed to."""
        row = self._db.execute(
            "SELECT module FROM updates WHERE chat_id = ? AND message_id = ? AND edited = 0"
            " ORDER BY update_id LIMIT 1",
            (chat_id, message_id),
        ).fetchone()
        return row[0] if row else None

    def deliveries_after(self, module: str, after: int, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT seq, payload FROM deliveries WHERE module = ? AND seq > ? ORDER BY seq LIMIT ?",
            (module, after, limit),
        ).fetchall()
        return [{"seq": r["seq"], **json.loads(r["payload"])} for r in rows]

    # --- audit ----------------------------------------------------------------------------

    def audit(self, event: str, module: str | None = None, detail: str | None = None) -> None:
        with self._transaction():
            self._db.execute(
                "INSERT INTO audit (at, event, module, detail) VALUES (?,?,?,?)",
                (self._clock(), event, module, detail),
            )

    # --- inspection (tests, later healthz) -------------------------------------------------

    def count_updates(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM updates").fetchone()[0]

    def unrouted_update_ids(self) -> list[int]:
        rows = self._db.execute(
            "SELECT update_id FROM updates WHERE module IS NULL ORDER BY update_id"
        ).fetchall()
        return [r[0] for r in rows]

    def received_at(self, update_id: int) -> float:
        return self._db.execute(
            "SELECT received_at FROM updates WHERE update_id = ?", (update_id,)
        ).fetchone()[0]

    def audit_events(self) -> list[tuple[str, str | None, str | None]]:
        rows = self._db.execute("SELECT event, module, detail FROM audit ORDER BY id").fetchall()
        return [(r[0], r[1], r[2]) for r in rows]


def _item_payload(update: IncomingUpdate) -> dict[str, Any]:
    """The delivery item as served by ``GET /updates`` (everything except ``seq``)."""
    return {
        "kind": "update",
        "update_id": update.update_id,
        "chat": update.chat_id,
        "topic": update.topic_id,
        "message_id": update.message_id,
        "from": {"id": update.from_id, "username": update.from_username, "contact_id": None},
        "text": update.text,
        "edited": update.edited,
        "edit_date": update.edit_date,
        "raw": update.raw,
    }

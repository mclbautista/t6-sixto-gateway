"""SQLite store (WAL). Single writer connection, used from the event-loop thread only."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sixto_gateway.decision import GATEWAY_MODULE, Decision
from sixto_gateway.store.migrations import MIGRATIONS
from sixto_gateway.telegram.normalize import IncomingUpdate

SECONDS_PER_DAY = 86400.0
AUDIT_RETENTION_DAYS = 180


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

    def set_offset(self, last_update_id: int) -> None:
        """Set the cursor to exactly ``last_update_id`` (cutover); the next poll uses ``+ 1``.

        Unlike the poller's own advance this may also move the cursor backwards.
        """
        with self._transaction():
            self._db.execute(
                "INSERT INTO poll_state (id, last_update_id) VALUES (1, ?)"
                " ON CONFLICT(id) DO UPDATE SET last_update_id = excluded.last_update_id",
                (last_update_id,),
            )
            self._audit_in_transaction("set_offset", None, f"last_update_id={last_update_id}")

    # --- updates and deliveries -----------------------------------------------------------

    def save_update(
        self,
        update: IncomingUpdate,
        decision: Decision | str | None,
        received_at: float | None = None,
    ) -> int | None:
        """Save an update, its delivery (if routed) and move the cursor, in one transaction.

        ``decision`` is a ``Decision``, or just the owning module (or None) for a plain update.
        A decision's audit row and DM reply are saved in the same transaction, and only the
        first time the update is seen, so a re-fetch never answers twice.

        Returns the new delivery's ``seq``, or None if nothing new was delivered (unrouted, or
        the update was already stored, which makes a re-fetch after a crash idempotent).
        """
        received_at = self._clock() if received_at is None else received_at
        if not isinstance(decision, Decision):
            decision = Decision(decision)
        module = decision.module
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
                payload = json.dumps(_item_payload(update, decision.kind), ensure_ascii=False)
                cursor = self._db.execute(
                    "INSERT INTO deliveries (module, kind, update_id, payload, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (module, decision.kind, update.update_id, payload, received_at),
                )
                seq = cursor.lastrowid
            if inserted and decision.audit is not None:
                self._insert_audit(*decision.audit)
            if inserted and decision.reply is not None:
                self._insert_outbound(
                    GATEWAY_MODULE,
                    "message",
                    str(update.chat_id),
                    {
                        "chat": update.chat_id,
                        "topic": None,
                        "text": decision.reply,
                        "format": "plain",
                    },
                    None,
                )
            self._move_cursor(update.update_id)
        return seq

    def _move_cursor(self, update_id: int) -> None:
        self._db.execute(
            "INSERT INTO poll_state (id, last_update_id) VALUES (1, ?)"
            " ON CONFLICT(id) DO UPDATE SET last_update_id = MAX(last_update_id, ?)",
            (update_id, update_id),
        )

    def original_route(self, chat_id: int, message_id: int) -> tuple[bool, str | None]:
        """(known, module) for the original (non-edited) message of an edit.

        ``known`` separates "the original was stored unrouted" from "never seen".
        """
        row = self._db.execute(
            "SELECT module FROM updates WHERE chat_id = ? AND message_id = ? AND edited = 0"
            " ORDER BY update_id LIMIT 1",
            (chat_id, message_id),
        ).fetchone()
        return (True, row[0]) if row else (False, None)

    def deliveries_after(self, module: str, after: int, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT seq, payload FROM deliveries WHERE module = ? AND seq > ? ORDER BY seq LIMIT ?",
            (module, after, limit),
        ).fetchall()
        return [{"seq": r["seq"], **json.loads(r["payload"])} for r in rows]

    # --- outbound queue -------------------------------------------------------------------

    def enqueue_outbound(
        self,
        module: str,
        kind: str,
        target: str,
        body: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> int:
        """Save an outbound row and return its ``message_ref`` (the row id).

        A repeated ``idempotency_key`` from the same module returns the existing ref and queues
        nothing new. The row is committed (``synchronous=FULL``) before this returns.
        """
        with self._transaction():
            return self._insert_outbound(module, kind, target, body, idempotency_key)

    def _insert_outbound(
        self,
        module: str,
        kind: str,
        target: str,
        body: dict[str, Any],
        idempotency_key: str | None,
    ) -> int:
        """``enqueue_outbound`` without its own transaction (the caller holds one)."""
        now = self._clock()
        if idempotency_key is not None:
            row = self._db.execute(
                "SELECT id FROM outbound WHERE module = ? AND idempotency_key = ?",
                (module, idempotency_key),
            ).fetchone()
            if row:
                return int(row[0])
        cursor = self._db.execute(
            "INSERT INTO outbound (module, kind, target, body, idempotency_key,"
            " next_attempt_at, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                module,
                kind,
                target,
                json.dumps(body, ensure_ascii=False),
                idempotency_key,
                now,
                now,
            ),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def next_outbound(self) -> dict[str, Any] | None:
        """The queued row to send next: earliest ``next_attempt_at``, then id."""
        row = self._db.execute(
            "SELECT id, module, kind, target, body, attempts FROM outbound"
            " WHERE state = 'queued' ORDER BY next_attempt_at, id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return {**dict(row), "body": json.loads(row["body"])}

    def mark_outbound_sent(self, ref: int, telegram_message_id: int | None) -> None:
        with self._transaction():
            self._db.execute(
                "UPDATE outbound SET state = 'sent', attempts = attempts + 1,"
                " telegram_message_id = ?, last_error = NULL WHERE id = ?",
                (telegram_message_id, ref),
            )

    def mark_outbound_failed(self, ref: int, error: str) -> None:
        with self._transaction():
            self._db.execute(
                "UPDATE outbound SET state = 'failed', attempts = attempts + 1, last_error = ?"
                " WHERE id = ?",
                (error, ref),
            )

    def outbound_status(self, ref: int, module: str) -> dict[str, Any] | None:
        """State of a module's own outbound row, or None (unknown, or another module's)."""
        row = self._db.execute(
            "SELECT state, telegram_message_id, last_error FROM outbound"
            " WHERE id = ? AND module = ?",
            (ref, module),
        ).fetchone()
        if row is None:
            return None
        return {
            "state": row["state"],
            "telegram_message_id": row["telegram_message_id"],
            "error": row["last_error"],
        }

    # --- audit ----------------------------------------------------------------------------

    def audit(self, event: str, module: str | None = None, detail: str | None = None) -> None:
        with self._transaction():
            self._insert_audit(event, module, detail)

    def _insert_audit(self, event: str, module: str | None, detail: str | None) -> None:
        self._db.execute(
            "INSERT INTO audit (at, event, module, detail) VALUES (?,?,?,?)",
            (self._clock(), event, module, detail),
        )

    # --- per-person "/" menus -------------------------------------------------------------

    def menu_users(self) -> set[int]:
        return {r[0] for r in self._db.execute("SELECT user_id FROM menu_users")}

    def add_menu_user(self, user_id: int) -> None:
        with self._transaction():
            self._db.execute("INSERT OR IGNORE INTO menu_users (user_id) VALUES (?)", (user_id,))

    def remove_menu_user(self, user_id: int) -> None:
        with self._transaction():
            self._db.execute("DELETE FROM menu_users WHERE user_id = ?", (user_id,))

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


def _item_payload(update: IncomingUpdate, kind: str = "update") -> dict[str, Any]:
    """The delivery item as served by ``GET /updates`` (everything except ``seq``).

    A ``command`` item adds ``command`` and ``args``.
    """
    extra = {"command": update.command, "args": update.command_args} if kind == "command" else {}
    return {
        **extra,
        "kind": kind,
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

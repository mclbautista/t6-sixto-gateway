"""SQLite store (WAL). Single writer connection, used from the event-loop thread only."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sixto_gateway.config import normalize_handle
from sixto_gateway.decision import GATEWAY_MODULE, Decision, parse_module_callback
from sixto_gateway.store.migrations import MIGRATIONS
from sixto_gateway.telegram.normalize import IncomingUpdate

log = logging.getLogger(__name__)
SECONDS_PER_DAY = 86400.0
AUDIT_RETENTION_DAYS = 180


class StoreCorrupt(RuntimeError):
    """The database file failed SQLite's integrity check at startup (t6-jo-watchdog#131)."""


def backup(source: str | Path, dest: str | Path) -> str:
    """Copy a database, live or not, to a new file ``dest`` and return its ``integrity_check``.

    Uses SQLite's online backup API, so the copy is consistent while the gateway keeps writing.
    Run it where the gateway runs (inside its container): SQLite's locks do not hold across a
    macOS bind mount (t6-jo-watchdog#131). ``dest`` must not exist; it ends up as a single file,
    with no ``-wal`` or ``-shm`` next to it, that is safe to copy anywhere.
    """
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(f"no database at {source}")
    os.close(os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    src = sqlite3.connect(source)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        return dst.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dst.close()
        src.close()


class Store:
    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time):
        self._clock = clock
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._db = sqlite3.connect(path, isolation_level=None)  # explicit transactions
        self._db.row_factory = sqlite3.Row
        path.chmod(0o600)
        # Before anything is written or migrated: a damaged file must not be opened.
        try:
            problem = self._db.execute("PRAGMA quick_check(1)").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            problem = str(exc)
        if problem != "ok":
            self._db.close()
            raise StoreCorrupt(
                f"the database at {path} failed its startup integrity check ({problem}); "
                "nothing was changed. Keep the gateway stopped, keep the file, and restore the "
                'last good backup: see "Database storage and backups" in the README'
            )
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
            self._insert_audit("set_offset", None, f"last_update_id={last_update_id}")

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

        A re-fetch of a stored update is idempotent: it delivers nothing new unless the update
        was stored for a module without its delivery, which is then repaired (issue #46). The
        cursor never moves past a routed update that has no delivery.

        Returns the new delivery's ``seq``, or None if nothing new was delivered (unrouted, or
        already delivered).
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
            if inserted:
                if module is not None:
                    seq = self._insert_delivery(update, decision.kind, module, received_at)
            else:
                # The module the update was first stored for owns it, even if routes changed.
                module = self._stored_module(update.update_id)
                if module is not None and not self._has_delivery(update.update_id, module):
                    seq = self._repair_delivery(update, decision.kind, module, received_at)
            if seq is not None and decision.push:
                self._db.execute(
                    "INSERT OR IGNORE INTO tap_pushes (seq, module, next_attempt_at, created_at)"
                    " VALUES (?,?,?,?)",
                    (seq, module, received_at, received_at),
                )
            if inserted and decision.audit is not None:
                self._insert_audit(*decision.audit)
            if inserted:
                for extra in decision.extra_audits:
                    self._insert_audit(*extra)
            if inserted and decision.member is not None:
                m = decision.member
                self._db.execute(
                    "INSERT INTO members (telegram_user_id, handle, first_name, last_name,"
                    " status, created_at, updated_at) VALUES (?,?,?,?,'pending',?,?)"
                    " ON CONFLICT(telegram_user_id) DO NOTHING",
                    (m.user_id, m.handle, m.first_name, m.last_name, received_at, received_at),
                )
            if inserted and decision.unblock_member is not None:
                self._db.execute(
                    "UPDATE members SET status = 'approved', updated_at = ?"
                    " WHERE telegram_user_id = ? AND status = 'blocked'",
                    (received_at, decision.unblock_member),
                )
            if inserted and decision.tap is not None:
                t = decision.tap
                self._db.execute(
                    "INSERT OR IGNORE INTO taps (update_id, callback_id, from_id, chat_id,"
                    " message_id, data, created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        update.update_id,
                        t.callback_id,
                        t.from_id,
                        t.chat_id,
                        t.message_id,
                        t.data,
                        received_at,
                    ),
                )
            if inserted and decision.reply is not None:
                self._insert_outbound(
                    GATEWAY_MODULE,
                    "message",
                    str(update.chat_id),
                    {
                        "chat": update.chat_id,
                        "topic": update.topic_id if update.chat_type != "private" else None,
                        "text": decision.reply,
                        "format": decision.reply_format,
                    },
                    None,
                )
            if module is not None and not self._has_delivery(update.update_id, module):
                # Never acknowledge a routed update to Telegram without its delivery: rolling
                # back keeps the cursor where it was, so the next poll fetches it again.
                log.error(
                    "update %s routed to %s has no delivery: not moving the cursor",
                    update.update_id,
                    module,
                )
                raise RuntimeError(f"update {update.update_id} routed to {module} has no delivery")
            self._move_cursor(update.update_id)
        return seq

    def _stored_module(self, update_id: int) -> str | None:
        row = self._db.execute(
            "SELECT module FROM updates WHERE update_id = ?", (update_id,)
        ).fetchone()
        return row[0] if row else None

    def _has_delivery(self, update_id: int, module: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM deliveries WHERE update_id = ? AND module = ? LIMIT 1",
            (update_id, module),
        ).fetchone()
        return row is not None

    def _insert_delivery(
        self, update: IncomingUpdate, kind: str, module: str, received_at: float
    ) -> int | None:
        payload = json.dumps(_item_payload(update, kind), ensure_ascii=False)
        return self._db.execute(
            "INSERT INTO deliveries (module, kind, update_id, payload, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (module, kind, update.update_id, payload, received_at),
        ).lastrowid

    def _repair_delivery(
        self, update: IncomingUpdate, kind: str, module: str, received_at: float
    ) -> int | None:
        """A re-fetched update was stored for ``module`` without its delivery: add it now."""
        seq = self._insert_delivery(update, kind, module, received_at)
        log.warning(
            "update %s was stored without a delivery for %s: repaired as seq %s",
            update.update_id,
            module,
            seq,
        )
        self._insert_audit("delivery_repaired", module, f"update_id={update.update_id} seq={seq}")
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
            "SELECT seq, payload FROM deliveries WHERE module = ? AND seq > ?"
            " AND seq NOT IN (SELECT seq FROM tap_pushes)"  # pushed taps are not pulled
            " ORDER BY seq LIMIT ?",
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

    def due_outbound(self, now: float, limit: int = 500) -> list[dict[str, Any]]:
        """Queued rows whose time has come, in send order: ``next_attempt_at``, then id."""
        rows = self._db.execute(
            "SELECT id, module, kind, target, body, attempts FROM outbound"
            " WHERE state = 'queued' AND next_attempt_at <= ? ORDER BY next_attempt_at, id"
            " LIMIT ?",
            (now, limit),
        ).fetchall()
        return [{**dict(r), "body": json.loads(r["body"])} for r in rows]

    def next_outbound_at(self, after: float) -> float | None:
        """When the earliest queued row that is not yet due (later than ``after``) comes due."""
        row = self._db.execute(
            "SELECT MIN(next_attempt_at) FROM outbound"
            " WHERE state = 'queued' AND next_attempt_at > ?",
            (after,),
        ).fetchone()
        return row[0]

    def schedule_outbound_retry(self, ref: int, next_attempt_at: float, error: str) -> None:
        """A failed attempt that will be retried: count it, keep the row queued."""
        with self._transaction():
            self._db.execute(
                "UPDATE outbound SET attempts = attempts + 1, next_attempt_at = ?,"
                " last_error = ? WHERE id = ?",
                (next_attempt_at, error, ref),
            )

    def mark_outbound_sent(self, ref: int, telegram_message_id: int | None) -> None:
        with self._transaction():
            self._db.execute(
                "UPDATE outbound SET state = 'sent', attempts = attempts + 1,"
                " telegram_message_id = ?, last_error = NULL WHERE id = ?",
                (telegram_message_id, ref),
            )
            row = self._db.execute("SELECT body FROM outbound WHERE id = ?", (ref,)).fetchone()
            card_for = json.loads(row["body"]).get("card_for") if row else None
            if card_for is not None:  # an approval card: remember it so it can be edited later
                self._db.execute(
                    "UPDATE members SET card_message_id = ?, updated_at = ?"
                    " WHERE telegram_user_id = ?",
                    (telegram_message_id, self._clock(), card_for),
                )
                self._insert_audit("card_sent", None, f"user={card_for}")

    # --- onboarding members ---------------------------------------------------------------

    def member(self, user_id: int) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT * FROM members WHERE telegram_user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        member = dict(row)
        member["candidates"] = (
            None if member["candidates"] is None else json.loads(member["candidates"])
        )
        return member

    def members_awaiting_lookup(self) -> list[dict[str, Any]]:
        """Pending members whose Notion lookup has not finished, oldest first."""
        rows = self._db.execute(
            "SELECT telegram_user_id FROM members WHERE status = 'pending' AND candidates IS NULL"
            " ORDER BY created_at, telegram_user_id"
        ).fetchall()
        return [m for r in rows if (m := self.member(r[0])) is not None]

    def record_lookup_failure(self, user_id: int, error: str) -> int:
        """Count a failed Notion lookup for a waiting member. Returns the failures so far."""
        with self._transaction():
            now = self._clock()
            self._db.execute(
                "UPDATE members SET lookup_attempts = lookup_attempts + 1,"
                " lookup_failed_at = COALESCE(lookup_failed_at, ?), updated_at = ?"
                " WHERE telegram_user_id = ? AND status = 'pending' AND candidates IS NULL",
                (now, now, user_id),
            )
            row = self._db.execute(
                "SELECT lookup_attempts FROM members WHERE telegram_user_id = ?", (user_id,)
            ).fetchone()
            attempts = 0 if row is None else int(row[0])
            self._insert_audit("lookup", None, f"user={user_id} failed #{attempts}: {error}"[:300])
            return attempts

    def set_candidates(self, user_id: int, candidates: list[str], detail: str) -> bool:
        """Store a lookup that finally worked after the card said "Lookup failed".

        False (nothing changed) when the member is not pending.
        """
        with self._transaction():
            updated = self._db.execute(
                "UPDATE members SET candidates = ?, lookup_attempts = 0, lookup_failed_at = NULL,"
                " updated_at = ? WHERE telegram_user_id = ? AND status = 'pending'",
                (json.dumps(candidates), self._clock(), user_id),
            ).rowcount
            if updated:
                self._insert_audit("lookup", None, f"user={user_id} {detail}")
            return bool(updated)

    def queue_approval_card(
        self, user_id: int, candidates: list[str], card_chat: int, card: dict[str, Any], detail: str
    ) -> None:
        """Record the finished lookup and queue the approver's card, in one transaction.

        A second call for the same member is ignored, so a card is never queued twice.
        """
        with self._transaction():
            updated = self._db.execute(
                "UPDATE members SET candidates = ?, updated_at = ?"
                " WHERE telegram_user_id = ? AND candidates IS NULL",
                (json.dumps(candidates), self._clock(), user_id),
            ).rowcount
            if not updated:
                return
            self._insert_audit("lookup", None, f"user={user_id} {detail}")
            self._insert_outbound(
                GATEWAY_MODULE,
                "message",
                str(card_chat),
                {**card, "chat": card_chat, "topic": None, "card_for": user_id},
                f"card:{user_id}",
            )

    def member_counts(self) -> dict[str, int]:
        """How many members have each status (all four statuses are always present)."""
        counts = dict.fromkeys(("approved", "pending", "rejected", "blocked"), 0)
        for row in self._db.execute("SELECT status, COUNT(*) FROM members GROUP BY status"):
            counts[row[0]] = row[1]
        return counts

    def pending_members(self) -> list[dict[str, Any]]:
        """Members still waiting for the approver, oldest first."""
        rows = self._db.execute(
            "SELECT telegram_user_id FROM members WHERE status = 'pending'"
            " ORDER BY created_at, telegram_user_id"
        ).fetchall()
        return [m for r in rows if (m := self.member(r[0])) is not None]

    def approved_handles(self) -> set[str]:
        """Telegram handles (lowercase, no ``@``) of approved members."""
        rows = self._db.execute(
            "SELECT handle FROM members WHERE status = 'approved' AND handle IS NOT NULL"
        )
        return {normalize_handle(r[0]) for r in rows if r[0]}

    def approved_user_for_contact(self, contact_page_id: str) -> int | None:
        """The Telegram user id of the approved member linked to a Contact, else None.

        Dashes in the page id do not matter. ``/notify`` addresses people through this.
        """
        row = self._db.execute(
            "SELECT telegram_user_id FROM members WHERE status = 'approved'"
            " AND REPLACE(contact_page_id, '-', '') = ?",
            (contact_page_id.replace("-", ""),),
        ).fetchone()
        return None if row is None else int(row[0])

    def approve_member(
        self, user_id: int, contact_page_id: str, decided_by: int, reply: dict[str, Any]
    ) -> bool:
        """Mark a pending member approved and queue their "approved" reply, in one transaction.

        Call this only after the Contact's ``TelegramUserID`` is written. False (nothing
        changed) when the member is no longer pending or the Contact belongs to someone else.
        """
        try:
            with self._transaction():
                now = self._clock()
                updated = self._db.execute(
                    "UPDATE members SET status = 'approved', contact_page_id = ?, decided_by = ?,"
                    " decided_at = ?, updated_at = ? WHERE telegram_user_id = ?"
                    " AND status = 'pending'",
                    (contact_page_id, decided_by, now, now, user_id),
                ).rowcount
                if not updated:
                    return False
                self._insert_audit(
                    "decision",
                    None,
                    f"user={user_id} approved contact={contact_page_id} by={decided_by}",
                )
                self._insert_audit("reply", None, f"user={user_id} kind=approved")
                self._insert_outbound(
                    GATEWAY_MODULE, "message", str(user_id), reply, f"approved:{user_id}"
                )
                return True
        except sqlite3.IntegrityError:  # the unique index: the Contact already has a member
            return False

    def auto_approve_member(
        self,
        user_id: int,
        contact_page_id: str,
        reply: dict[str, Any],
        fyi_chat: int,
        fyi: dict[str, Any],
    ) -> bool:
        """Approve a member whose Contact already holds their id: no card and no Notion write.

        Marks the pending member approved (nobody decided, so ``decided_by`` stays empty) and
        queues the "approved" reply and a one-line FYI for the approver, in one transaction.
        False (nothing changed) when the lookup is already done, the member is no longer pending
        or the Contact belongs to someone else.
        """
        try:
            with self._transaction():
                now = self._clock()
                updated = self._db.execute(
                    "UPDATE members SET status = 'approved', contact_page_id = ?, candidates = ?,"
                    " decided_at = ?, updated_at = ? WHERE telegram_user_id = ?"
                    " AND status = 'pending' AND candidates IS NULL",
                    (contact_page_id, json.dumps([contact_page_id]), now, now, user_id),
                ).rowcount
                if not updated:
                    return False
                self._insert_audit("lookup", None, f"user={user_id} contact already has this id")
                self._insert_audit(
                    "decision", None, f"user={user_id} approved contact={contact_page_id} by=id"
                )
                self._insert_audit("reply", None, f"user={user_id} kind=approved")
                self._insert_outbound(
                    GATEWAY_MODULE, "message", str(user_id), reply, f"approved:{user_id}"
                )
                self._insert_outbound(
                    GATEWAY_MODULE, "message", str(fyi_chat), fyi, f"fyi:{user_id}"
                )
                return True
        except sqlite3.IntegrityError:  # the unique index: the Contact already has a member
            return False

    def reject_member(self, user_id: int, decided_by: int, reply: dict[str, Any]) -> bool:
        """Mark a pending member rejected and queue the neutral reply, in one transaction.

        False (nothing changed) when the member is no longer pending. The reply is queued once
        per member: rejecting again after an Undo does not repeat it.
        """
        with self._transaction():
            now = self._clock()
            updated = self._db.execute(
                "UPDATE members SET status = 'rejected', decided_by = ?, decided_at = ?,"
                " updated_at = ? WHERE telegram_user_id = ? AND status = 'pending'",
                (decided_by, now, now, user_id),
            ).rowcount
            if not updated:
                return False
            self._insert_audit("decision", None, f"user={user_id} rejected by={decided_by}")
            key = f"rejected:{user_id}"
            seen = self._db.execute(
                "SELECT 1 FROM outbound WHERE module = ? AND idempotency_key = ?",
                (GATEWAY_MODULE, key),
            ).fetchone()
            if seen is None:
                self._insert_audit("reply", None, f"user={user_id} kind=rejected")
                self._insert_outbound(GATEWAY_MODULE, "message", str(user_id), reply, key)
            return True

    def undo_rejection(self, user_id: int, undone_by: int) -> bool:
        """Put a rejected member back to pending, keeping the candidates the card showed.

        False (nothing changed) when the member is not rejected.
        """
        with self._transaction():
            updated = self._db.execute(
                "UPDATE members SET status = 'pending', decided_by = NULL, decided_at = NULL,"
                " updated_at = ? WHERE telegram_user_id = ? AND status = 'rejected'",
                (self._clock(), user_id),
            ).rowcount
            if not updated:
                return False
            self._insert_audit("undo", None, f"user={user_id} by={undone_by}")
            return True

    def block_member(self, chat_id: int) -> bool:
        """The bot cannot write to this chat: an approved member becomes ``blocked``.

        Only approved members change. A pending or rejected one must not skip the approver's
        decision when they unblock the bot and press Start again. False when nothing changed
        (a group chat, an unknown chat, or a member who is not approved).
        """
        with self._transaction():
            updated = self._db.execute(
                "UPDATE members SET status = 'blocked', updated_at = ?"
                " WHERE telegram_user_id = ? AND status = 'approved'",
                (self._clock(), chat_id),
            ).rowcount
            if updated:
                self._insert_audit("blocked", None, f"user={chat_id} blocked the bot")
            return bool(updated)

    def next_tap(self) -> dict[str, Any] | None:
        """The oldest button tap not yet handled."""
        row = self._db.execute(
            "SELECT * FROM taps WHERE done = 0 ORDER BY update_id LIMIT 1"
        ).fetchone()
        return None if row is None else dict(row)

    def finish_tap(self, update_id: int) -> None:
        with self._transaction():
            self._db.execute("UPDATE taps SET done = 1 WHERE update_id = ?", (update_id,))

    # --- taps pushed to a module (ticket #50) --------------------------------------------------

    def due_pushes(self, now: float, limit: int = 50) -> list[dict[str, Any]]:
        """Pending tap pushes that are due, oldest first, each with its delivery item."""
        rows = self._db.execute(
            "SELECT p.seq, p.module, p.attempts, d.payload FROM tap_pushes p"
            " JOIN deliveries d ON d.seq = p.seq"
            " WHERE p.state = 'pending' AND p.next_attempt_at <= ? ORDER BY p.seq LIMIT ?",
            (now, limit),
        ).fetchall()
        return [
            {
                "seq": r["seq"],
                "module": r["module"],
                "attempts": r["attempts"],
                "item": {"seq": r["seq"], **json.loads(r["payload"])},
            }
            for r in rows
        ]

    def next_push_at(self) -> float | None:
        row = self._db.execute(
            "SELECT MIN(next_attempt_at) FROM tap_pushes WHERE state = 'pending'"
        ).fetchone()
        return row[0]

    def mark_push_done(self, seq: int) -> None:
        with self._transaction():
            self._db.execute(
                "UPDATE tap_pushes SET state = 'done', attempts = attempts + 1, last_error = NULL"
                " WHERE seq = ?",
                (seq,),
            )

    def schedule_push_retry(self, seq: int, next_attempt_at: float, error: str) -> None:
        with self._transaction():
            self._db.execute(
                "UPDATE tap_pushes SET attempts = attempts + 1, next_attempt_at = ?,"
                " last_error = ? WHERE seq = ?",
                (next_attempt_at, error, seq),
            )

    def push_status(self, seq: int) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM tap_pushes WHERE seq = ?", (seq,)).fetchone()
        return None if row is None else dict(row)

    def mark_outbound_failed(self, ref: int, error: str) -> None:
        with self._transaction():
            self._db.execute(
                "UPDATE outbound SET state = 'failed', attempts = attempts + 1, last_error = ?"
                " WHERE id = ?",
                (error, ref),
            )

    def outbound_row(self, ref: int) -> dict[str, Any] | None:
        """One outbound row (any module), with its body decoded, or None."""
        row = self._db.execute(
            "SELECT id, module, kind, state, body, telegram_message_id FROM outbound WHERE id = ?",
            (ref,),
        ).fetchone()
        return None if row is None else {**dict(row), "body": json.loads(row["body"])}

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

    def send_failures_since(self, since: float) -> list[str]:
        """Detail of each ``send_failed`` audit row since ``since``, oldest first."""
        rows = self._db.execute(
            "SELECT module, detail FROM audit WHERE event = 'send_failed' AND at >= ? ORDER BY id",
            (since,),
        )
        return [f"{r[0] or 'gateway'}: {r[1] or ''}".strip() for r in rows]

    def has_outbound(self, module: str, idempotency_key: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM outbound WHERE module = ? AND idempotency_key = ?",
            (module, idempotency_key),
        ).fetchone()
        return row is not None

    # --- retention ------------------------------------------------------------------------

    def purge(
        self, retention_days: int, audit_retention_days: int = AUDIT_RETENTION_DAYS
    ) -> dict[str, int]:
        """Delete rows past their limit and return how many went from each table.

        ``updates``, ``deliveries`` and *finished* ``outbound`` rows (``sent`` or ``failed``) are
        kept ``retention_days``; ``audit`` rows ``audit_retention_days``. Queued sends, the
        polling cursor and the menu list are never deleted. Age is measured from ``received_at``
        / ``created_at``.
        """
        now = self._clock()
        cutoff = now - retention_days * SECONDS_PER_DAY
        audit_cutoff = now - audit_retention_days * SECONDS_PER_DAY
        with self._transaction():
            # finished pushes first (they reference deliveries); a pending push keeps its delivery
            self._db.execute(
                "DELETE FROM tap_pushes WHERE state = 'done' AND created_at < ?", (cutoff,)
            )
            # deliveries next: they reference updates
            deliveries = self._db.execute(
                "DELETE FROM deliveries WHERE created_at < ?"
                " AND seq NOT IN (SELECT seq FROM tap_pushes)",
                (cutoff,),
            ).rowcount
            updates = self._db.execute(
                "DELETE FROM updates WHERE received_at < ?"
                " AND update_id NOT IN"
                " (SELECT update_id FROM deliveries WHERE update_id IS NOT NULL)",
                (cutoff,),
            ).rowcount
            outbound = self._db.execute(
                "DELETE FROM outbound WHERE state IN ('sent', 'failed') AND created_at < ?",
                (cutoff,),
            ).rowcount
            audit = self._db.execute("DELETE FROM audit WHERE at < ?", (audit_cutoff,)).rowcount
        return {"updates": updates, "deliveries": deliveries, "outbound": outbound, "audit": audit}

    # --- health ---------------------------------------------------------------------------

    def is_writable(self) -> bool:
        """True if a write transaction can be taken and committed right now."""
        try:
            with self._transaction():
                # PRAGMA cannot take bound parameters; the value is an int read from the file
                version = self._db.execute("PRAGMA user_version").fetchone()[0]
                # rewriting the header with its own value touches the file without changing it
                self._db.execute(f"PRAGMA user_version = {int(version)}")
        except sqlite3.Error:
            return False
        return True

    def queued_count(self) -> int:
        row = self._db.execute("SELECT COUNT(*) FROM outbound WHERE state = 'queued'").fetchone()
        return int(row[0])

    def failed_sends_since(self, since: float) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) FROM audit WHERE event = 'send_failed' AND at >= ?", (since,)
        ).fetchone()
        return int(row[0])

    # --- pacing history --------------------------------------------------------------------

    def record_send_attempt(self, chat_id: int, at: float, keep_seconds: float) -> None:
        """Note a request to Telegram before it is made; forget ones older than the windows."""
        with self._transaction():
            self._db.execute("INSERT INTO send_attempts (at, chat_id) VALUES (?, ?)", (at, chat_id))
            self._db.execute("DELETE FROM send_attempts WHERE at < ?", (at - keep_seconds,))

    def send_attempts_since(self, since: float) -> list[tuple[float, int]]:
        rows = self._db.execute(
            "SELECT at, chat_id FROM send_attempts WHERE at >= ? ORDER BY at", (since,)
        )
        return [(r[0], r[1]) for r in rows]

    def paused_until(self) -> float:
        """When the bot-wide 429 pause ends (0: no pause was ever recorded)."""
        row = self._db.execute("SELECT until FROM send_pause WHERE id = 1").fetchone()
        return float(row[0]) if row else 0.0

    def extend_pause(self, until: float) -> None:
        """Pause all sending until at least ``until``; an earlier value never shortens it."""
        with self._transaction():
            self._db.execute(
                "INSERT INTO send_pause (id, until) VALUES (1, ?) ON CONFLICT (id) DO UPDATE"
                " SET until = MAX(until, excluded.until)",
                (until,),
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

    def missing_deliveries(self) -> list[int]:
        """Update ids stored for a module that have no delivery for it (gaps)."""
        rows = self._db.execute(
            "SELECT u.update_id FROM updates u WHERE u.module IS NOT NULL AND NOT EXISTS"
            " (SELECT 1 FROM deliveries d WHERE d.update_id = u.update_id"
            " AND d.module = u.module) ORDER BY u.update_id"
        ).fetchall()
        return [r[0] for r in rows]

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

    A ``command`` item adds ``command`` and ``args``. A ``tap`` item adds ``data``: the button's
    payload without the module prefix (``text`` is empty).
    """
    extra: dict[str, Any] = {}
    if kind == "command":
        extra = {"command": update.command, "args": update.command_args}
    elif kind == "tap":
        parsed = parse_module_callback(update.callback_data)
        extra = {"data": parsed[1] if parsed else ""}
    if kind != "tap":
        extra.update(
            reply_to_message_id=update.reply_to_message_id,
            has_file=update.has_file,
            has_link=update.has_link,
        )
        if update.topic_name is not None:
            extra["topic_name"] = update.topic_name
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

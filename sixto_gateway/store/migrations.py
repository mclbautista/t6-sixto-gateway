"""Numbered migrations, run at boot. ``PRAGMA user_version`` records the last one applied.

Append new migrations; never edit an applied one. Tables beyond ``updates``/``deliveries`` are
created now so later slices (send queue, commands) need no schema change to start.
"""

MIGRATIONS: list[str] = [
    # 1: initial schema (spec section 5)
    """
    CREATE TABLE poll_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        last_update_id INTEGER NOT NULL
    );
    CREATE TABLE updates (
        update_id INTEGER PRIMARY KEY,
        chat_id INTEGER NOT NULL,
        topic_id INTEGER,
        message_id INTEGER NOT NULL,
        from_id INTEGER,
        edited INTEGER NOT NULL,
        module TEXT,
        raw TEXT NOT NULL,
        received_at REAL NOT NULL
    );
    CREATE INDEX updates_message ON updates (chat_id, message_id);
    CREATE TABLE deliveries (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        module TEXT NOT NULL,
        kind TEXT NOT NULL,
        update_id INTEGER REFERENCES updates (update_id),
        payload TEXT NOT NULL,
        created_at REAL NOT NULL
    );
    CREATE INDEX deliveries_module_seq ON deliveries (module, seq);
    CREATE TABLE outbound (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        module TEXT NOT NULL,
        kind TEXT NOT NULL,
        target TEXT NOT NULL,
        body TEXT NOT NULL,
        idempotency_key TEXT,
        state TEXT NOT NULL DEFAULT 'queued',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL NOT NULL,
        telegram_message_id INTEGER,
        last_error TEXT,
        created_at REAL NOT NULL
    );
    CREATE UNIQUE INDEX outbound_idempotency
        ON outbound (module, idempotency_key) WHERE idempotency_key IS NOT NULL;
    CREATE TABLE audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at REAL NOT NULL,
        event TEXT NOT NULL,
        module TEXT,
        detail TEXT
    );
    """,
]

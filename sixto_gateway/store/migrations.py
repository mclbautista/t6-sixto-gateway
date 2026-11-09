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
    # 2: private chats that currently have their own "/" menu (spec section 8), so a person
    # who leaves every allow list has their menu removed at the next boot
    """
    CREATE TABLE menu_users (user_id INTEGER PRIMARY KEY);
    """,
    # 3: recent send attempts, so pacing windows survive a restart (spec section 9)
    """
    CREATE TABLE send_attempts (at REAL NOT NULL, chat_id INTEGER NOT NULL);
    CREATE INDEX send_attempts_at ON send_attempts (at);
    """,
    # 4: the bot-wide pause after a 429, so a restart does not send before Telegram allows it
    """
    CREATE TABLE send_pause (id INTEGER PRIMARY KEY CHECK (id = 1), until REAL NOT NULL);
    """,
    # 5: team members who pressed /start (onboarding spec #17). The user id is also the DM chat id.
    # ``candidates`` stays NULL until the Notion lookup is done; then it is a JSON list of
    # Contact page ids. One Contact can belong to only one approved member.
    """
    CREATE TABLE members (
        telegram_user_id INTEGER PRIMARY KEY,
        contact_page_id TEXT,
        handle TEXT,
        first_name TEXT NOT NULL DEFAULT '',
        last_name TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'blocked')),
        candidates TEXT,
        card_message_id INTEGER,
        decided_by INTEGER,
        decided_at REAL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE UNIQUE INDEX members_approved_contact
        ON members (contact_page_id) WHERE status = 'approved';
    """,
    # 6: taps on the approver's card buttons, saved with the update and handled by the onboarding
    # worker (Notion is written before a member is approved, so a tap cannot be handled inline)
    """
    CREATE TABLE taps (
        update_id INTEGER PRIMARY KEY,
        callback_id TEXT NOT NULL,
        from_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        data TEXT NOT NULL,
        done INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL
    );
    """,
    # 7: the Notion lookup for a waiting member is retried after 1, 5 and 30 minutes (ticket #27).
    # ``lookup_attempts`` counts failed lookups and ``lookup_failed_at`` is the first failure, so
    # the schedule survives a restart. 4 failed lookups means the card said "Lookup failed".
    """
    ALTER TABLE members ADD COLUMN lookup_attempts INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE members ADD COLUMN lookup_failed_at REAL;
    """,
    # 8: taps for a module in push mode (ticket #50). One row per tap delivery, kept until the
    # module's URL has accepted it; the retry schedule is stored so it survives a restart.
    # Rows here are not served by ``GET /updates``.
    """
    CREATE TABLE tap_pushes (
        seq INTEGER PRIMARY KEY REFERENCES deliveries (seq),
        module TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'done')),
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL NOT NULL,
        last_error TEXT,
        created_at REAL NOT NULL
    );
    CREATE INDEX tap_pushes_due ON tap_pushes (state, next_attempt_at);
    """,
]

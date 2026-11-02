# Sixto gateway

The only process that holds the `@sixto_t6bot` token and the only caller of `getUpdates`.
It saves each Telegram update, routes it to a module by `(chat, topic)`, and modules read their
items over HTTP with their own secret. Design: the "Sixto gateway plan" doc; phase 1 spec:
issue #1.

Status: this README covers the intake path (config, polling, routing, `GET /updates`) and basic
sending (`/notify`, `/react`, `GET /notify/{ref}`). Pacing and retries, commands, health and the cutover runbook are added by later slices.

## Run the tests

```sh
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest -q          # no network: fake Telegram, real SQLite in a temp dir, fake clock
ruff check . && ruff format --check . && mypy
```

## Config and secrets

| Where | What | Notes |
|---|---|---|
| `config.yaml` | everything non-secret: listen address, poll settings, modules, groups, routes | copy `config.example.yaml`; mounted read-only |
| `.env` | `TELEGRAM_BOT_TOKEN` and one secret per module (the env var named by `modules.<name>.secret_env`, e.g. `WATCHDOG_SECRET`) | never committed; each module secret is at least 32 characters and unique |

The config is validated at boot and a bad value stops the process (exit code 2) with a message
that names the key or env var, never its value. Routes carry the real intake chat and topic ids,
copied from the watchdog's `config.yaml`; the example file has placeholders to fill in.

Make a module secret with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

## How an item travels

1. The poller calls `getUpdates(offset = last committed update_id + 1)`.
2. Each update is stored, routed and its cursor moved **in one SQLite transaction** (save before
   advance). A crash before the commit just re-fetches the update; the unique `update_id` keeps
   that idempotent.
3. Routing: the first `routes` entry matching the chat and topic wins. Edits follow the original
   message's route. Unrouted updates are kept in `updates` and never delivered.
4. A module calls `GET /updates?after=<seq>&timeout=<0..50>` with `Authorization: Bearer
   <its secret>`. It stores `seq` as its cursor only after it has saved the items itself.
   Items stay readable for the retention period, so re-reading is safe. A wrong or missing
   secret gets `401` and is logged and audited.

## Sending

- `POST /notify` `{"to": <chat>, "topic": <optional>, "text": "...", "format": "html"|"plain",
  "idempotency_key": "..."}` and `POST /react` `{"chat", "message_id", "emoji"}` save a row in
  `outbound` and answer `202 {"message_ref": N}`. A repeated `idempotency_key` from the same
  module returns the same ref and queues nothing new. Invalid bodies get `422`.
- One sender task sends queued rows in order (link previews off; `html` uses Telegram's HTML
  parse mode). `GET /notify/{ref}` returns `{state: queued|sent|failed, telegram_message_id,
  error}` for the calling module's own refs (`404` otherwise).
- Basic form only: a failed send is recorded as `failed` at once. Pacing, retries and 429
  handling come in the next slice. Each attempt and outcome goes to the audit log.

## Run

```sh
mkdir -p data && chmod 700 data && sudo chown 1000:1000 data
docker compose up -d --build         # Intel (linux/amd64), non-root, ./data volume
```

Set `telegram.polling: false` to serve the API without touching Telegram (no polling and no
sending; `/notify` rows are saved and stay `queued`).

Rollback: `GATEWAY_TAG=<previous> docker compose up -d`.

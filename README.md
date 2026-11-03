# Sixto gateway

The only process that holds the `@sixto_t6bot` token and the only caller of `getUpdates`.
It saves each Telegram update, routes it to a module by `(chat, topic)`, and modules read their
items over HTTP with their own secret. Design: the "Sixto gateway plan" doc; phase 1 spec:
issue #1.

Status: intake (config, polling, routing, `GET /updates`), sending with pacing and retries
(`/notify`, `/react`, `GET /notify/{ref}`), commands with the allow-list, and operations
(`/healthz`, deadman, retention, `set-offset`, deploy, rollback and the cutover runbook).

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
| `.env` | `TELEGRAM_BOT_TOKEN`, `NOTION_API_KEY` (same value as t6-jo-tg-ntn) and one secret per module (the env var named by `modules.<name>.secret_env`, e.g. `WATCHDOG_SECRET`) | never committed; each module secret is at least 32 characters and unique |

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
- **To a person, by Notion Contact.** Instead of `to`, send `{"contact": "<Contact page id>",
  "fallback": {"to": <group chat>, "topic": <optional>, "handle": "@juandc"}, "text": "..."}`.
  If the Contact has an `approved` member (see /start onboarding), the message is queued to that
  person's DM. Otherwise it goes to the fallback chat and topic with `@handle` added at the start
  of the text. The lookup reads only the local `members` table, never Notion, and dashes in the
  page id do not matter. Send either `to` or `contact` with `fallback`, never both and never
  neither (`422`); the topic belongs inside `fallback`, and a text that the `@handle` would push past 4096 characters is a `422` too. `format`, `text` and `idempotency_key` work the same in both forms.
- One sender task sends queued rows in order (link previews off; `html` uses Telegram's HTML
  parse mode). `GET /notify/{ref}` returns `{state: queued|sent|failed, telegram_message_id,
  error}` for the calling module's own refs (`404` otherwise).
- Sends and reactions are paced under Telegram's limits (1/s per private chat, 20/min per group,
  25/s overall); attempt times are stored, so a restart does not reset the windows. Network
  errors and 5xx are retried after 30 s, 2 min, 5 min and 10 min, then the row is `failed`; a 429
  waits at least `retry_after` and pauses all sending that long, a pause that is stored and survives
  a restart; a request that can never succeed (chat not found, bad HTML, bot
  blocked) fails at once. Reactions follow the same rules. Each attempt and outcome goes to the
  audit log.

## Commands and the allow-list

```yaml
commands:
  jo: { module: jo, allow: [psr, 274983989], description: "Job orders" }   # group names or user ids
```

- **Menus, at boot** (only when `telegram.polling` is true): the default `/` menu is set empty,
  and each user id named in any `allow` list (directly or through a group) gets a per-chat menu
  with only their commands. People who had a menu at an earlier boot and are no longer on any
  list lose it, so `commands: {}` leaves no per-person menu. If Telegram refuses one person
  (for example they never opened a chat with the bot) the boot continues, `menu_failed` goes to
  the audit log and the next boot retries; a removed person whose chat Telegram refuses for good
  is forgotten rather than retried every boot. Menu setup gets 2 minutes in total; if Telegram hangs
  it is abandoned (`menu_failed`, "setup timed out") and polling starts anyway. A person is
  recorded before their menu is set, so a crash part way through never leaves a menu that a
  later boot does not know to remove.
- **In a DM**, a message that starts with `/name` is checked against the allow-list before any
  module sees it. Allowed: a `command` item (with `command` and `args`) is delivered to the
  owning module through `GET /updates`. Refused or unknown: the update is stored unrouted and
  the gateway queues one DM reply, "That command isn't available to you." The reply is saved in
  the same transaction as the update, so a re-fetched update is never answered twice.
- **In a group**, commands get no reply and are routed like any other message. An edited
  command is never delivered or answered.
- Every routed (`command_routed`) and refused (`command_refused`) command is in the audit log
  (command name and sender id, never the arguments).

## /start onboarding (first slice)

`/start` in a private chat is onboarding, never checked against the command allow-list. The poller
saves a `pending` row in `members` and queues the "waiting" reply in the same transaction as the
update. A separate worker then reads the Notion Contacts (`notion.contacts_data_source_id`),
matches the person's handle (case-insensitive, `@` ignored) against `TelegramHandle` and
`Aliases`, and queues an approval card for `onboarding.approver` with inline buttons. A slow or
down Notion never holds up polling; a failed lookup is retried every minute. Buttons are shown
but taps are not handled yet (#21). `/start` in a group gets a "message me directly" reply with a
deep link. All user-facing text is in `sixto_gateway/strings.py`. Events go to the `audit` table.

## Health, deadman, retention, logs

- `GET /healthz` (no auth): `200` when a `getUpdates` call completed in the last 5 minutes and the
  database accepts a write, `503` otherwise. Body: `status`, `polling`, `database_writable`,
  `last_poll_at` (UTC), `queued` (sends waiting) and `failed_last_24h`. The Docker health check
  calls it; `docker compose ps` shows `healthy` or `unhealthy`. With `telegram.polling: false`
  there is no poll to wait for, so only the database counts and the body says `"polling": false`.
- **Deadman:** if no `getUpdates` call completes for 10 minutes (counted from boot if none ever
  does), the process exits with code 1 and `restart: unless-stopped` starts it again. A stuck
  Telegram call cannot hold it up: tasks get 5 seconds to stop, are then cancelled, and the
  process exits hard if one still has not finished. It is off when polling is off. Exit code 2 is a bad config, which a restart will not fix.
- **Retention:** once a day (and at every boot) the gateway deletes `updates`, `deliveries` and
  finished (`sent` or `failed`) `outbound` rows older than `retention_days` (default 30), and
  `audit` rows older than 180 days. Queued sends and the polling cursor are never deleted. Age is
  counted from when the row was created. Each run writes a `retention` audit row with the counts.
- **Audit log:** the `audit` table records config loads, auth failures, each send attempt and
  outcome, `set_offset`, `retention` and `deadman`. Read it with
  `docker compose exec gateway python -c "import sqlite3; [print(r) for r in sqlite3.connect('/app/data/gateway.db').execute('select datetime(at,\'unixepoch\'),event,module,detail from audit order by id desc limit 30')]"`.
- **Logs** (`docker compose logs -f gateway`): one line per update received (id, chat, topic,
  routed module), per send outcome and per auth failure. Message text is never logged. Every log
  line, including tracebacks, goes through a redactor that replaces the bot token, every module
  secret, anything shaped like a bot token and any `Bearer ...` value with `[redacted]`. The same
  scrub is applied to send errors before they are stored in `outbound` or `audit`.

## Run

```sh
mkdir -p data && chmod 700 data && sudo chown 1000:1000 data
cp config.example.yaml config.yaml && cp .env.example .env   # then fill both in (see above)
GATEWAY_TAG=$(git rev-parse --short HEAD) docker compose up -d --build   # Intel linux/amd64
```

Set `telegram.polling: false` to serve the API without touching Telegram (no polling, no
sending, no deadman; `/notify` rows are saved and stay `queued`). Changing config means
`docker compose restart gateway`.

## Deploy

Run on the Mac mini (Hermes does this over SSH):

```sh
cd <gateway checkout>
git pull && GATEWAY_TAG=$(git rev-parse --short HEAD) docker compose up -d --build
docker compose ps                                    # wait for "healthy" (up to about 1 minute)
curl -fsS http://127.0.0.1:8780/healthz              # expect HTTP 200 and "status": "ok"
```

Plain `git pull && docker compose up -d --build` also works, but it rebuilds `sixto-gateway:latest`
in place and leaves no image to roll back to. The `GATEWAY_TAG` form keeps one image per commit.
A restart keeps the data: the SQLite file lives in `./data`, and migrations run at boot.

## Rollback (a bad deploy)

```sh
docker images sixto-gateway                          # tags are commit ids; pick the previous one
GATEWAY_TAG=<previous tag> docker compose up -d      # no --build: reuses that image
curl -fsS http://127.0.0.1:8780/healthz
```

Keep `GATEWAY_TAG=<previous tag>` on every later `docker compose` command until you deploy again,
or compose will go back to `latest`. Migrations only add, so the previous image can open a
database that a newer one has already migrated.

## Cutover runbook (watchdog moves from direct polling to the gateway)

For Hermes, step by step. Stop at the first failed check and use the rollback below. `N` is a
number you read in step 2. Set the shell variables first:

```sh
MINI=192.168.88.151                      # the Mac mini
WATCHDOG_SECRET='<value from .env>'      # the same value in the gateway and watchdog .env
```
Prerequisites: the gateway `.env` has `TELEGRAM_BOT_TOKEN` and `WATCHDOG_SECRET`; the gateway
`config.yaml` has the real intake `chat` and `topic` copied from the watchdog's `config.yaml`.
The watchdog's gateway mode (`telegram_mode: gateway`, `gateway_url`, `reset-offset`) is the
watchdog repo's ticket and must be merged first.

1. **Deploy the gateway with polling off.** In `config.yaml` set `telegram.polling: false`, then
   follow Deploy above. Check:
   - `curl -fsS http://$MINI:8780/healthz` answers `200` with `"polling": false`;
   - `curl -s -o /dev/null -w '%{http_code}\n' -H 'Authorization: Bearer wrong' http://$MINI:8780/updates`
     prints `401`;
   - `curl -fsS -H "Authorization: Bearer $WATCHDOG_SECRET" "http://$MINI:8780/updates?timeout=0"`
     prints `{"items":[]}`.
2. **Stop the old watchdog** (on Cyril's machine: `docker compose stop`). Read its last committed
   offset `N`:
   `sqlite3 <watchdog ledger file> 'SELECT COALESCE((SELECT last_committed_offset FROM offsets WHERE id = 1), 0);'`
   (the ledger file is `ledger_path` in the watchdog's `config.yaml`; a ledger that never
   committed has no offsets row, which prints `0`). Write `N` down.
3. **Hand the cursor to the gateway.** On the mini:
   ```sh
   docker compose exec gateway python -m sixto_gateway set-offset N
   ```
   It prints `the next getUpdates will use offset N+1`. Then set `telegram.polling: true` in
   `config.yaml` and `docker compose restart gateway`. Telegram keeps unread updates for 24 hours,
   so nothing sent while nobody polled is lost. Check `curl -fsS http://$MINI:8780/healthz` shows
   `"polling": true`, `"status": "ok"` and a recent `last_poll_at` within a minute.
4. **Start the watchdog against the gateway.** Copy the watchdog's `data/` to the mini while it
   is stopped, run `python -m jo_watchdog.app reset-offset --to 0` (the cursor now counts gateway
   `seq`, not Telegram ids), set `telegram_mode: gateway` and `gateway_url: http://$MINI:8780`, put
   `WATCHDOG_SECRET` in its `.env` in place of `TELEGRAM_BOT_TOKEN`, and start it. Redelivered
   items are skipped by the ledger.
5. **Live checks** (spec section 13): post one real intake JO and see it captured with its
   reaction; the 07:00 heartbeat arrives in the ops topic; stop the watchdog for a few minutes,
   post a JO, start it again and confirm nothing was lost; `grep TELEGRAM_BOT_TOKEN` finds it
   only in the gateway's `.env`.

### Rollback after cutover

The watchdog goes back to polling Telegram itself without losing an update. Two facts drive
every step:

- Gateway `seq` numbers and Telegram update ids are separate counters, with no order between
  them. The watchdog's gateway-mode cursor is a `seq`, so it must always be reset before direct
  mode; keeping it can skip updates.
- Both cursors store the last id already handled, and the next poll adds one. So the watchdog is
  reset to `T`, the last Telegram update id the gateway fetched, never to `T + 1`.

The gateway's `poll_state` only says what the gateway fetched and stored, not what the watchdog
has consumed, and Telegram does not send again what the gateway already acknowledged. So the
watchdog must drain the gateway first. Never delete either database.

Run this on the mini with `MINI` and `WATCHDOG_SECRET` set as for the cutover, plus the two
checkouts. `LEDGER` is the watchdog's `ledger_path` as seen inside its container; the files in
`data/` belong to the container user, so they are read through `docker compose exec`, not from the
host:

```sh
GW=<gateway checkout>; WD=<watchdog checkout>; LEDGER=/app/data/ledger.db
```

1. **Stop gateway polling, keep its API.** Edit `$GW/config.yaml` in place (it is a single-file
   mount, so do not replace the file) to set `telegram.polling: false`, then
   `cd "$GW" && docker compose restart gateway`. Check that
   `curl -fsS "http://$MINI:8780/healthz"` shows `"polling": false`. From now on nobody polls, and
   Telegram holds new updates for up to 24 hours.
2. **Drain the watchdog.** Leave it running in gateway mode until the gateway has nothing left
   for it. Read its last committed `seq` and ask the gateway for anything after it:
   ```sh
   W=$(cd "$WD" && docker compose exec -T watchdog python -c "import sqlite3, sys; print(sqlite3.connect(sys.argv[1]).execute('SELECT COALESCE((SELECT last_committed_offset FROM offsets WHERE id = 1), 0)').fetchone()[0])" "$LEDGER")
   curl -fsS -H "Authorization: Bearer $WATCHDOG_SECRET" "http://$MINI:8780/updates?after=$W&timeout=0"
   ```
   Repeat until it prints `{"items":[]}`.
   - **If it cannot drain** (the gateway-mode watchdog is what is broken), export what it never
     got, in order, with everything needed to re-enter it:
     ```sh
     cd "$GW" && docker compose exec -T gateway python -c "import json, sqlite3, sys; db = sqlite3.connect('/app/data/gateway.db'); db.row_factory = sqlite3.Row; [print(json.dumps(dict(r))) for r in db.execute('SELECT d.seq, u.update_id, u.chat_id, u.topic_id, u.message_id, u.edited, datetime(u.received_at, \'unixepoch\') AS received, u.raw FROM deliveries d JOIN updates u USING (update_id) WHERE d.module = \'watchdog\' AND d.seq > ? ORDER BY d.seq', (int(sys.argv[1]),))]" "$W" > "$WD/stranded-$(date +%F).jsonl"
     ```
     Telegram will not send these again. Reconcile them after step 6, in `seq` order. A row with
     `"edited": 0` is a new post: if
     `cd "$WD" && docker compose exec -T watchdog python -c "import sqlite3, sys; print(sqlite3.connect(sys.argv[1]).execute('SELECT COUNT(*) FROM messages WHERE telegram_key = ?', (sys.argv[2],)).fetchone()[0])" "$LEDGER" '<chat_id>:<message_id>'`
     prints `0`, enter that JO by hand from the text in `raw`. A row with `"edited": 1` is an
     edit: apply it by hand to that JO in Notion.
3. **Read `T` while the gateway is still up:**
   ```sh
   T=$(cd "$GW" && docker compose exec -T gateway python -c "import sqlite3; print(sqlite3.connect('/app/data/gateway.db').execute('SELECT COALESCE((SELECT last_update_id FROM poll_state WHERE id = 1), 0)').fetchone()[0])")
   echo "T=$T"
   ```
4. **Stop the watchdog, keep a copy, reset its cursor to `T`.** Do this with the image it runs now,
   which has gateway mode; images from before gateway mode have no `reset-offset`.
   ```sh
   cd "$WD" && CURRENT=$(docker inspect --format '{{.Config.Image}}' "$(docker compose ps -q watchdog)" | cut -d: -f2) && echo "CURRENT=$CURRENT"
   cd "$WD" && docker compose stop && cp -Rp data "data-backup-$(date +%F)" && \
     JO_WATCHDOG_TAG="$CURRENT" docker compose run --rm watchdog python -m jo_watchdog.app reset-offset --to "$T"
   ```
   It prints the old offset. The next poll will be `T + 1`: the first update the gateway never
   fetched.
5. **Note the unsent replies, stop the gateway and keep a copy.** With polling off the gateway also
   sends nothing, so reactions and ops posts the watchdog queued during the drain are still
   waiting. List them first:
   ```sh
   cd "$GW" && docker compose exec -T gateway python -c "import sqlite3; [print(r) for r in sqlite3.connect('/app/data/gateway.db').execute('SELECT id, module, kind, target, body, datetime(created_at, \'unixepoch\') FROM outbound WHERE state = \'queued\' ORDER BY id')]"
   ```
   They are not sent after the rollback: set those reactions by hand and read the ops posts.
   Then `docker compose stop gateway && cp -Rp data "data-backup-$(date +%F)"`.
6. **Start the watchdog in direct mode.** Put `TELEGRAM_BOT_TOKEN` back in `$WD/.env`, choose the
   tag `TAG` to run, and edit `$WD/config.yaml` in place (it is a single-file mount too) to match
   that image:
   - **Current image, or any image with gateway mode** (usually the right choice when the gateway
     was the problem): set `telegram_mode: direct` and remove `gateway_url`.
   - **An image from before gateway mode** (only if the watchdog itself must go back; anything
     older than `$CURRENT` that `docker compose run --rm watchdog python -m jo_watchdog.app --help`
     shows without `reset-offset`): it refuses unknown config keys, so delete the
     `telegram_mode` and `gateway_url` lines altogether.

   Check the config against that image before starting it, then start it:
   ```sh
   cd "$WD" && JO_WATCHDOG_TAG="$TAG" docker compose run --rm watchdog python -m jo_watchdog.app healthcheck
   ```
   Any output other than `bad config: ...` is fine here (a stale poll reads as unhealthy).
   Then `cd "$WD" && JO_WATCHDOG_TAG="$TAG" docker compose up -d --no-build` and check
   `docker compose logs --tail 20` shows it polling.
7. Run the watchdog live check (one intake JO captured with its reaction), and reconcile the
   stranded rows from step 2 if there were any. Leave the gateway stopped.

To go forward again later, repeat the runbook from step 2, so `set-offset` is given a fresh `N`.

## Cutover offset command

`python -m sixto_gateway set-offset N` sets the cursor to exactly `N` (it may also move it
backwards) and writes a `set_offset` audit row; the next `getUpdates` uses `N + 1`. `N` must be 0
or more. Run it with `docker compose exec` so it uses the running container's mounted `./data`.

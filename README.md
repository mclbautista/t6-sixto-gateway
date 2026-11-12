# Sixto gateway

The only process that holds the `@sixto_t6bot` token and the only caller of `getUpdates`.
It saves each Telegram update, routes it to a module by `(chat, topic)`, and modules read their
items over HTTP with their own secret. Design: the "Sixto gateway plan" doc; phase 1 spec:
issue #1.

Status: intake (config, polling, routing, `GET /updates`), sending with pacing and retries
(`/notify` with inline buttons, `/edit`, `/react`, `GET /notify/{ref}`), button taps (pull or
push), commands with the allow-list, `/start` onboarding with its daily summary, and operations
(`/healthz`, deadman, retention, `set-offset`, `show-offset`, deploy, rollback and the cutover runbook).

## This repo is public

Anyone can read this repository and its full history. Never commit:

- `.env` or any real secret: the Telegram bot token, the Notion API key, module secrets
- `config.yaml` (it holds the real chat and topic ids; only `config.example.yaml` is tracked)
- `data/`, `*.db` or any SQLite file (saved Telegram updates and messages)
- real client, show or person names, in code, tests, fixtures or docs

`.gitignore` covers the files above, and the **Public repo safety** workflow (from t6-core)
fails any push or pull request that adds a secret, one of those paths or a denied name. If a
secret ever lands in a commit, rotate it; deleting the commit is not enough.

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
| `.env` | `TELEGRAM_BOT_TOKEN`, `NOTION_API_KEY` (same value as the watchdog, t6-jo-watchdog) and one secret per module (the env var named by `modules.<name>.secret_env`, e.g. `WATCHDOG_SECRET`) | never committed; each module secret is at least 32 characters and unique |

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

## Taps on a module's buttons

A tap on a button that `/notify` put on a message (callback data `mod:<module>:<payload>`) goes
to that module once, as a `tap` item: `kind: "tap"`, `data` (the module's own payload, prefix
removed), `from.id` (the tapper's Telegram user id), `chat`, `message_id`, `update_id`. The
gateway answers the callback query itself so the tapper's spinner stops; the module need not.
The tap is saved with its delivery before the poll cursor moves, like any other update.

Each module chooses a delivery mode in `config.yaml`:

- **Pull (default).** The module reads the tap from `GET /updates` with its other items.
- **Push.** `delivery: push` and `push_url: http://host:port/path`. The gateway POSTs the tap
  to that URL with `Authorization: Bearer <the module's secret>`. The body is the tap item
  (including `seq`) plus `idempotency_key` (`tap-<update_id>`), also sent as an
  `Idempotency-Key` header; it is the same on every retry, so the receiver must treat a repeat
  as the same tap. Any 2xx accepts it. Otherwise the gateway retries after 5 s, 30 s, 2 min and
  then every 10 min, and never gives up. The schedule is stored, so it survives a restart. A
  pushed tap is not also served by `/updates`. The gateway calls out; nothing calls into it.

A tap naming a module that is not in `modules` is written to the audit table
(`tap_unknown_module`), answered with "This button is no longer active." and not delivered.
Taps with an `ob:` prefix (or any other data without a module prefix) still go to the
onboarding worker.

## Sending

- `POST /notify` `{"to": <chat>, "topic": <optional>, "text": "...", "format": "html"|"plain",
  "idempotency_key": "..."}` and `POST /react` `{"chat", "message_id", "emoji"}` save a row in
  `outbound` and answer `202 {"message_ref": N}`. A repeated `idempotency_key` from the same
  module returns the same ref and queues nothing new. Invalid bodies get `422`.
- **Inline buttons.** `/notify` optionally accepts `buttons`, an array of rows, for example:
  `"buttons": [[{"text": "Approve", "payload": "approve:42"},
  {"text": "Sign in", "url": "https://example.com/sign-in"}]]`.
  Each button has nonempty `text` and exactly one of nonempty `payload` or an HTTPS `url`.
  Empty rows and extra button fields are rejected (`422`); omitted, null or empty `buttons`
  send a message without buttons. Callback data is stored and sent as
  `mod:<module>:<payload>`, where the gateway derives `<module>` from the authenticated caller
  and percent-encodes it (including any colons). The payload is opaque, so even a payload
  starting with `ob:` or another module's prefix stays under the caller's namespace.
  This format cannot collide with onboarding's `ob:` callbacks. The complete callback must
  fit within **64 UTF-8 bytes**, including the prefix; otherwise `/notify` returns `422`
  naming the limit and queues nothing. Buttons survive retries and restarts; reusing an
  idempotency key keeps the original buttons, like the original text.
- **Editing a sent message.** `POST /edit` `{"message_ref": N, "text": "...", "format":
  "html"|"plain", "buttons": [...], "idempotency_key": "..."}` replaces the text and buttons
  of a message the calling module sent with `/notify` (`N` is the ref `/notify` returned). For
  example, after Approve is tapped: `{"message_ref": 17, "text": "Approved by Alex"}`.
  `buttons` follow the `/notify` rules; omitted, null or empty `buttons` remove the buttons, so
  nobody can tap twice. It answers `202 {"message_ref": M}`, and `GET /notify/M` reports the
  edit (`telegram_message_id` is the edited message). A ref from another module, or from the
  gateway itself, gets `404`, like an unknown ref; a `/react` or `/edit` ref gets `422`; a message
  whose send already `failed` gets `409`. Edits go through the same queue, pacing, retries and
  idempotency keys as `/notify`. **An edit of a message that is still queued waits for it to be
  sent**, then edits it; if that send fails, the edit fails too with an error naming the ref.
  Telegram's "message is not modified" (the message already shows exactly this) counts as sent.
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

## Messages from t6-netops

t6-netops sends its notifications through `/notify` as the module **`netops`**
(`modules.netops` in `config.yaml`, secret in `NETOPS_SECRET`). It has its own secret and never
holds the Telegram bot token. Pacing, audit and `idempotency_key` retries work as for every
other module; netops should send a stable key per event so a retried request queues nothing new.

## Messages from t6-core (webhooks contracts)

The t6-core webhooks server and its Frame.io worker send three messages through `/notify`.
They authenticate as the module **`t6core`** (`modules.t6core` in `config.yaml`, secret in
`T6CORE_SECRET`), so their callback buttons arrive as `mod:t6core:<payload>` and their taps are
pushed to t6-core's tap endpoint (see "Taps on a module's buttons"). The gateway does not know
these message types: each one is an ordinary `/notify` body, and the requests below are the
contract t6-core builds against. Every example here is posted as written by
`tests/test_t6core_contracts.py`, which checks the text and buttons that reach Telegram, so a
change to this section that the gateway would send differently fails the tests.

Names, ids and links in the examples are placeholders (`Example Show`, page ids, links); the
shape, the payload formats and the idempotency keys are the contract. Text uses `format:
"html"`, so t6-core must HTML-escape anything it puts into `text` (`&`, `<`, `>`).

### 1. Review-done DM to the show's coordinator

Sent when a Frame.io review has been quiet for 15 minutes. Addressed by the coordinator's Notion
Contact, so it reaches their DM once they are approved and the show's group topic with an
`@handle` mention until then. The Frame.io link is a URL button.

<!-- contract: review-done -->
```json
{
  "contact": "0a1b2c3d-1111-2222-3333-444455556666",
  "fallback": {"to": -1001234567890, "topic": 77, "handle": "coordinator_handle"},
  "text": "<b>Review done:</b> Example Show\n12 new comments on Episode 3 rough cut.",
  "format": "html",
  "idempotency_key": "review-done:3f9a1c2e-0000-4000-8000-00000000abcd:12",
  "buttons": [[{"text": "Open in Frame.io", "url": "https://next.frame.io/share/00000000-0000-4000-8000-000000000000"}]]
}
```

- `contact`: the coordinator's Contact page id; `fallback`: the show's group chat and topic and
  the coordinator's handle.
- `idempotency_key`: `review-done:<Frame.io asset or review id>:<comment count>`, so a retried
  request never sends twice but a later review of the same asset does.

### 2. Project Start approval card to Cyril

Sent when a Project reaches the Project Start status. `to` is Cyril's Telegram user id (the
`cyril` group). The Approve button's payload is `approve:<Project page id>`, with the page id
written as 32 hex characters without dashes.

<!-- contract: approval-card -->
```json
{
  "to": 274983989,
  "text": "<b>Project Start:</b> Example Show\nClient: Example Client\nCoordinator: coordinator_handle\nApprove to create the folders, Frame.io project, Instagantt and Notion pages.",
  "format": "html",
  "idempotency_key": "project-start:0a1b2c3d111122223333444455556666",
  "buttons": [[{"text": "Approve", "payload": "approve:0a1b2c3d111122223333444455556666"}]]
}
```

When a setup step fails, t6-core sends a second card that names the step and carries a Retry
button. Its payload is `retry:<Project page id>:<step>`, where `<step>` is one of t6-core's step
keys (`folders`, `frameio`, `instagantt`, `notion_pages`).

<!-- contract: retry-card -->
```json
{
  "to": 274983989,
  "text": "<b>Project Start failed:</b> Example Show\nStep: Frame.io project\nThe earlier steps are done and are not repeated. Retry runs this step and the ones after it.",
  "format": "html",
  "idempotency_key": "project-start-retry:0a1b2c3d111122223333444455556666:frameio:1",
  "buttons": [[{"text": "Retry Frame.io project", "payload": "retry:0a1b2c3d111122223333444455556666:frameio"}]]
}
```

Payload budget: Telegram allows 64 bytes of callback data and the gateway adds `mod:t6core:`
(11 bytes), leaving **53 bytes** for the payload. `approve:<32 hex>` is 40 bytes. `retry:<32
hex>:` is 39, so a step key may be at most **14 bytes**; a longer one gets `422` and nothing is
queued. When Cyril taps, t6-core receives a `tap` item whose `data` is the payload exactly as
sent (`approve:…` or `retry:…:<step>`), with `from.id`, `chat` and `message_id` of the card.
The retry key ends with an attempt number so each new failure of the same step sends a new card.

### 3. Frame.io re-sign-in alert to Cyril

Sent by the t6-core Frame.io worker when the token refresh fails. The sign-in link is a URL
button (HTTPS only); t6-core builds the link.

<!-- contract: frameio-sign-in -->
```json
{
  "to": 274983989,
  "text": "<b>Frame.io sign-in needed.</b>\nThe token refresh failed. Sign in again so t6-core can keep reading Frame.io.",
  "format": "html",
  "idempotency_key": "frameio-sign-in:2026-11-09T08",
  "buttons": [[{"text": "Sign in to Frame.io", "url": "https://hooks.example.com/frameio/sign-in?state=example-state"}]]
}
```

- `idempotency_key`: `frameio-sign-in:<UTC date and hour>`, so the worker can retry freely and
  sends at most one alert an hour.

## Commands and the allow-list

```yaml
commands:
  jo: { module: jo, allow: [psr, 274983989], description: "Job orders" }   # group names or user ids
```

- **Built-in commands** (`builtin: true`, no `module`) are answered by the gateway itself and
  never appear in `/updates`. `/whereami` replies, in the same chat and topic, with the chat id
  and topic id in tap-to-copy code format. Allowed callers are those in its `allow` list
  (example config: group `chat_ids`). In a group, anyone else gets no reply (audited as
  `command_refused`); in a DM an allowed caller is told to send it in the group, anyone else gets
  the standard refusal. Each answer is audited as `command_answered` (chat id and caller id,
  never args). An edited message gets no second reply.
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
down Notion never holds up polling. `/start` in a group gets a "message me directly" reply with a
deep link. All user-facing text is in `sixto_gateway/strings.py`. Events go to the `audit` table.

### When Notion or Telegram fail

- **Lookup:** a failed lookup is retried 1, 5 and 30 minutes after the first failure (the count
  is in `members`, so it survives a restart). After the fourth failure the card says "Lookup
  failed" with Retry, Create new Contact and Reject. Retry looks the person up once more.
- **Writes after a tap** (approve, Replace ID, create): retried after 1, 5 and 15 seconds. If all
  fail, the card says "Write failed" with a Retry button, and the member stays `pending`.
- **Create new Contact** first looks for a Contact that already has the person's Telegram id (left
  by an earlier create that did not finish) and uses it instead of making a second page.
- **Sends:** the send queue retries cards and replies; permanent failures go to the `audit` table
  (`send_failed`) and the daily summary, and a 403 on an approved member sets them to `blocked`.

### Daily onboarding summary

Every day at `onboarding.summary_time` (default `09:00`, Asia/Manila) `onboarding.approver` gets one
DM: counts of approved, pending, rejected and blocked members; each pending card with an "open
card" link (cards never expire, so this is the reminder); `send_failed` audit rows from the last
24 hours (first 5 shown); and the `onboarding.expected` handles with no approved member. The
summary is queued under the key `summary:<date>`, so a restart never sends a second one for the
same day; a gateway that was down at 09:00 sends it as soon as it is back up. It runs only while
`telegram.polling` is on. Both keys are checked at boot. "Still missing" compares the list with
the Telegram handles of approved members, so a person who uses a different handle than the one
listed shows as missing until the list is updated. The "open card" link is a `tg://openmessage`
link built from the bot's id and the card's message id.

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
  outcome, `set_offset`, `delivery_repaired`, `retention` and `deadman`. Read it with
  `docker compose exec gateway python -c "import sqlite3; [print(r) for r in sqlite3.connect('/app/data/gateway.db').execute('select datetime(at,\'unixepoch\'),event,module,detail from audit order by id desc limit 30')]"`.
- **Logs** (`docker compose logs -f gateway`): one line per update received (id, chat, topic,
  routed module), per send outcome, per auth failure and per repaired or missing delivery. Message text is never logged. Every log
  line, including tracebacks, goes through a redactor that replaces the bot token, every module
  secret, anything shaped like a bot token and any `Bearer ...` value with `[redacted]`. The same
  scrub is applied to send errors before they are stored in `outbound` or `audit`.

## Run

```sh
docker volume create sixto-gateway-data   # once per host; compose refuses to start without it
cp config.example.yaml config.yaml && cp .env.example .env   # then fill both in (see above)
GATEWAY_TAG=$(git rev-parse --short HEAD) docker compose up -d --build   # Intel linux/amd64
```

Set `telegram.polling: false` to serve the API without touching Telegram (no polling, no
sending, no deadman; `/notify` rows are saved and stay `queued`). Changing config means
`docker compose restart gateway`.

## Deploy

These default commands do **not** enable or preserve the optional Tailscale overlay
unless the file and profile selection described below is present in this shell.
Always use that selection for every deploy, restart, and rollback of an enabled overlay.

Run on the Mac mini (Hermes does this over SSH):

```sh
cd <gateway checkout>
git pull && GATEWAY_TAG=$(git rev-parse --short HEAD) docker compose up -d --build
docker compose ps                                    # wait for "healthy" (up to about 1 minute)
curl -fsS http://127.0.0.1:8780/healthz              # expect HTTP 200 and "status": "ok"
```

Plain `git pull && docker compose up -d --build` also works, but it rebuilds `sixto-gateway:latest`
in place and leaves no image to roll back to. The `GATEWAY_TAG` form keeps one image per commit.
A restart keeps the data: the SQLite file lives in the `sixto-gateway-data` volume, and
migrations run at boot.

## Optional Tailscale gateway node

The default deployment above is unchanged and does not start Tailscale. To enable it,
use the opt-in overlay with Docker Compose 2.24.4 or newer (`!reset` support), a
working `/dev/net/tun` in the Colima VM, and the existing external Docker network
`sixto`. The gateway shares the sidecar's network namespace; the sidecar owns the
port and `gateway` / `sixto-gateway` DNS aliases. It uses kernel networking and a
pinned image, advertises `tag:t6-gateway`, and keeps node identity in the named
`tailscale_state` volume. Do not delete that volume on restart, update, or rollback.

**Operational blocker — do not enable for unattended production use yet.** The gateway
shares the sidecar's network namespace. An unexpected sidecar crash/restart or
recreation can leave the gateway attached to a stale or unavailable namespace;
Docker restart policies and Compose startup health dependencies do not establish
automatic namespace recovery. Daemon/host reboot ordering and recovery have not been
validated. An unhealthy sidecar is not automatically restarted by its health check.
Arrange monitoring and an operator, and validate crash, recreation and reboot recovery
in an isolated environment before cutover. Otherwise keep the default deployment.

Before switching, back up the existing untracked `compose.override.yaml` and check
its actual network aliases and port binding. Add any additional aliases to the
sidecar in a local overlay; they do not transfer automatically. Create a separate
`.env.tailscale` (mode 0600), containing `TS_AUTHKEY` set to a non-ephemeral key
authorized for `tag:t6-gateway`. This file is passed only to the sidecar, excluded
from Git and the build context, and must remain present even after enrollment (it may
then be empty). Remove `TS_AUTHKEY` from the gateway's `.env`; if it was previously
there, treat it as exposed to the gateway and rotate/revoke it as appropriate. In `.env`, set
`GATEWAY_BIND_IP` to the existing host-side bind address (the current Colima setup
uses `127.0.0.1`; if publishing directly to a LAN interface, use its specific IP).
Never bind to all interfaces or expose the port to the internet. The auth key is
needed for initial enrollment, not subsequent starts with intact state. Treat both env
files and `docker compose config` output as secret. Set the t6core `push_url` in
`config.yaml` to the VPS tailnet IPv4 and tap port 8791; MagicDNS is not assumed
to work in the gateway container. Confirm the deployed server's tap path and port.

For a checkout with an existing `compose.override.yaml`, select the files explicitly,
in this order, for **every** Compose command while the overlay is enabled:

```sh
export COMPOSE_FILE=compose.yaml:compose.override.yaml:compose.tailscale.yaml
export COMPOSE_PROFILES=tailscale
GATEWAY_TAG=$(git rev-parse --short HEAD) docker compose up -d --build
```

If there is no override, omit `:compose.override.yaml` from `COMPOSE_FILE`. Inspect
the resolved config privately before starting: gateway must have neither ports nor
networks, and the sidecar must own the intended host binding and all shared-network
aliases. Check the LAN health URL from another app, and check both shared-network
clients can still resolve and reach the gateway. With the VPS listening on both
ports, check TCP from *inside the gateway* (substitute its tailnet IPv4):

```sh
docker compose exec -T gateway python -c 'import socket; s=socket.create_connection(("<VPS_TAILNET_IPV4>", 8791), 5); s.close(); print("tap reachable")'
docker compose exec -T gateway python -c 'import socket; s=socket.create_connection(("<VPS_TAILNET_IPV4>", 8790), 5); s.close(); print("ERROR: queue reachable")'
```

The first must succeed and the second must fail; a failure alone does not prove an
ACL denial if the queue is down. Confirm queue availability from an authorized
worker and confirm the applied netops ACLs. Check the sidecar's tailnet identity
before and after restart/reboot without printing the auth key. Compose's dependency
restart propagation covers explicit Compose restarts, not automatic runtime restarts.
After a crash, sidecar recreation, or host reboot, check sidecar health, gateway health,
LAN and shared-network access, and tailnet access; if connectivity is broken, restore
sidecar health first and then run `docker compose up -d --force-recreate gateway` with
the same overlay/profile selection and image tag. Repeat all checks; roll back if they
fail. This is a manual recovery procedure, not a tested automatic recovery guarantee.

**Rollback the networking change:** stop the gateway and sidecar using the same
file/profile selection, then unset both variables and start the original Compose
configuration (including the backed-up override) with the previous gateway image.
Expect an interruption while the port and network namespace move. Remove only the
old sidecar container if it remains; never remove its state volume. Verify host/LAN
health and both clients' DNS/connectivity again. The image rollback below also works
without changing networking if you keep `COMPOSE_FILE` and `COMPOSE_PROFILES` set.

## Database storage and backups

The database must stay inside the Docker VM. On 2026-11-12 the watchdog's ledger was found
corrupt after living on a bind mount from macOS into the Colima VM, where SQLite's file locks and
WAL index do not hold (mclbautista/t6-jo-watchdog#131). `gateway.db` sat on the same kind of mount
and showed the same warning sign (a second connection failed with `disk I/O error`).

- `/app/data` is the named volume `sixto-gateway-data`. It is declared `external`, so
  `docker compose up` fails until it exists (a deploy cannot start on an empty database by
  accident) and `docker compose down -v` cannot delete it. Do not change it back to a host path.
- Nothing on macOS opens the live database: no `sqlite3`, no DB browser, no `cp` of a running
  database, no sync or backup tool pointed at it. Read it with `docker compose exec gateway ...`,
  or read a backup.
- **Back up** while the gateway runs. `backup` uses SQLite's online backup API in the gateway's
  container, refuses to overwrite a file, checks the copy with `integrity_check` (exit 1 if it is
  damaged) and leaves one file with no `-wal` or `-shm`:

  ```sh
  docker compose exec gateway python -m sixto_gateway backup /tmp/gateway-backup.db
  docker compose cp gateway:/tmp/gateway-backup.db "./gateway-backup-$(date +%F).db"
  docker compose exec gateway rm /tmp/gateway-backup.db
  chmod 600 gateway-backup-*.db    # saved Telegram messages: keep it private, never commit it
  ```

- **Startup check.** Every start runs `PRAGMA quick_check` before any migration. A damaged file
  stops the boot with one line, `database: the database at /app/data/gateway.db failed its startup
  integrity check (...)`, exit code 1, and nothing is written to the file. The container restarts
  and repeats the line; run `docker compose stop gateway`, keep the file and restore a backup.
- **Restore a backup.** A helper container mounts the backup read-only and installs it; the
  damaged files are moved aside in the volume, never deleted:

  ```sh
  docker compose stop gateway
  docker compose run --rm --no-deps --user root --entrypoint sh \
    -v "$PWD/gateway-backup-YYYY-MM-DD.db:/restore.db:ro" gateway -c '
    set -e
    cd /app/data
    aside="damaged-$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir "$aside"
    for f in gateway.db gateway.db-wal gateway.db-shm; do
      if [ -e "$f" ]; then mv "$f" "$aside/"; fi
    done
    cp /restore.db gateway.db
    chown -R 1000:1000 gateway.db "$aside"
    chmod 600 gateway.db
  '
  docker compose up -d
  ```

  A restored database is older than what the modules have seen. The Telegram cursor goes back,
  so the gateway fetches again what Telegram still holds (24 hours), and delivery `seq` numbers
  continue from the backup's last one. A module whose own cursor is ahead of that would skip new
  items, so before starting the modules compare `show-offset` and each module's cursor, as in
  "Rollback after cutover".

### Move an existing `./data` into the volume (once per host)

For a host that still has `gateway.db` in the bind-mounted `./data`. This changes production
data, so it needs the owner's approval and a quiet moment. Until it is done, the new
`compose.yaml` refuses to start (`external volume "sixto-gateway-data" not found`), which is the
intended guard. With the Tailscale overlay, use the same file and profile selection as for a
deploy on every command.

```sh
docker compose stop gateway               # nothing may write during the copy
git pull                                  # brings the compose.yaml with the named volume
docker volume create sixto-gateway-data
docker compose run --rm --no-deps --user root --entrypoint sh \
  -v "$PWD/data:/from:ro" gateway -c '
  set -e
  test ! -e /app/data/gateway.db          # never overwrite a database already in the volume
  cp -a /from/. /app/data/
  chown -R 1000:1000 /app/data
  chmod 700 /app/data
  find /app/data -type f -exec chmod 600 {} +
'
docker compose run --rm --no-deps --entrypoint python gateway -c "
import sqlite3
print(sqlite3.connect('/app/data/gateway.db').execute('PRAGMA integrity_check').fetchone()[0])
"                                         # must print: ok
GATEWAY_TAG=$(git rev-parse --short HEAD) docker compose up -d --build
curl -fsS http://127.0.0.1:8780/healthz
```

The copy reads the old folder with the gateway stopped, so no lock is involved. If the check
does not print `ok`, do not start the gateway: the file was already damaged on the mount. Keep
the old `./data` untouched until the gateway has run healthy for a day, then move it somewhere
private.

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
The watchdog's gateway mode (`telegram_mode: gateway`, `gateway_url`, `reset-offset`) is merged
in t6-jo-watchdog; see "Gateway mode" in its README.

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
4. **Start the watchdog against the gateway.** Move the watchdog's ledger to the mini while it
   is stopped (a `backup` file, installed into its volume; see the watchdog's
   `docs/runbooks/ledger-storage.md`), run `python -m jo_watchdog.app reset-offset --to 0` (the cursor now counts gateway
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
checkouts. `LEDGER` is the watchdog's `ledger_path` as seen inside its container; both databases
live in named volumes, so they are read through `docker compose exec`, never from the host:

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
   cd "$WD" && docker compose stop && \
     JO_WATCHDOG_TAG="$CURRENT" docker compose run --rm --no-deps watchdog backup "/app/data/ledger-backup-$(date +%F).db" && \
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
   Then stop the gateway and keep a copy (it stays in the volume, next to the database):
   ```sh
   cd "$GW" && docker compose stop gateway && \
     docker compose run --rm --no-deps gateway backup "/app/data/gateway-backup-$(date +%F).db"
   ```
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
or more. Run it with `docker compose exec` so it uses the running container's database.

## Inspecting the cursor and repairing dropped updates

`python -m sixto_gateway show-offset` prints the cursor (`last_update_id` and the next
`getUpdates` offset) and the ids of any updates stored for a module that have no delivery.

A routed update never moves the cursor without its delivery: if the delivery cannot be written,
the whole save is rolled back, an error is logged and the next poll fetches the update again. When
a re-fetched update is already stored for a module but its delivery is missing, the gateway adds
the delivery (a new `seq`), logs a warning and writes a `delivery_repaired` audit row. A re-fetch
never delivers an update twice, and it always goes to the module it was first stored for.

To recover updates listed by `show-offset` (or lost before this check existed), rewind the cursor
to just before the first one with `set-offset <first id - 1>`. Telegram only resends updates it
still holds, which is about 24 hours. Older gaps cannot be recovered this way.

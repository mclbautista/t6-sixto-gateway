# Tailscale sidecar: enable, check, roll back (#57)

For the person at the Mac mini (Sixto). The code and config are in the repo; the steps below need
your hands because they create a credential and change a live container's network. Nothing here
has been run against the real tailnet yet: treat every "expect" line as the acceptance check.

## Before you start

- [ ] The gateway PR for #80 is merged and you have deployed or are deploying that image
      (`T6_OPERATOR_IDS` in `.env`, README "Config and secrets").
- [ ] The tailnet policy from t6-netops (`tailscale/policy.hujson`) is applied: it defines
      `tag:t6-gateway` and allows it to reach only the VPS tap port 8791.
- [ ] Docker Compose 2.24.4 or newer: `docker compose version`.
- [ ] `/dev/net/tun` exists in the Colima VM: `colima ssh -- ls -l /dev/net/tun`.
- [ ] Back up the untracked `compose.override.yaml` (it holds the real port binding and aliases):
      `cp compose.override.yaml compose.override.yaml.bak`.

## 1. Create the key (Tailscale admin console, you only)

Settings > Keys > Generate auth key: **not** ephemeral, **pre-approved**, tag `tag:t6-gateway`.
Put it in a new file next to `compose.yaml`, mode 0600:

```sh
cp .env.tailscale.example .env.tailscale && chmod 600 .env.tailscale
# edit .env.tailscale: TS_AUTHKEY=tskey-auth-...
```

If an old `TS_AUTHKEY` is in the gateway's `.env`, delete it there and revoke that key.

## 2. Set the overlay and check the config before starting

```sh
export COMPOSE_FILE=compose.yaml:compose.override.yaml:compose.tailscale.yaml   # omit the override if there is none
export COMPOSE_PROFILES=tailscale
docker compose config | grep -n "network_mode\|8780\|aliases" # treat this output as secret: it can show env
```

Expect: `gateway` has `network_mode: service:tailscale` and no ports or networks; the `tailscale`
service owns `127.0.0.1:8780:8780` and the aliases `gateway` and `sixto-gateway`.

## 3. Start it

```sh
GATEWAY_TAG=$(git rev-parse --short HEAD) docker compose up -d --build
```

Expect a short interruption while the port and network move. The default image stays for rollback.

## 4. Check (each line is an acceptance criterion of #57)

1. Enrolled once, with the tag: `docker compose exec tailscale tailscale status` lists
   `t6-gateway` and the VPS. In the admin console the node shows `tag:t6-gateway`.
2. Gateway and sidecar healthy: `docker compose ps` shows both `healthy`.
3. LAN port unchanged: `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8780/healthz` prints 200
   on the Mac mini, and from the reports and watchdog containers
   `http://gateway:8780/healthz` still prints 200.
4. Tap port reachable, queue port not (substitute the VPS tailnet IPv4):

   ```sh
   docker compose exec -T gateway python -c 'import socket; s=socket.create_connection(("<VPS_TAILNET_IPV4>", 8791), 5); s.close(); print("tap reachable")'
   docker compose exec -T gateway python -c 'import socket; s=socket.create_connection(("<VPS_TAILNET_IPV4>", 8790), 5); s.close(); print("ERROR: queue reachable")'
   ```

   The first prints `tap reachable`, the second fails. A failure alone does not prove the ACL
   (the queue could be down): check 8790 from the t6 worker node, where it must work.
5. Same node after a restart: note the node ID, run `docker compose restart tailscale gateway`,
   and check that `tailscale status` shows the same name and IP and that the admin console shows
   no second `t6-gateway-1` device.
6. Taps reach the VPS: set the t6core `push_url` in `config.yaml` to the VPS tailnet IPv4 and
   port 8791 (MagicDNS is not assumed in the gateway container), restart the gateway, press a
   button on a test card and check that the message is delivered (README "Taps on a module's
   buttons").

## Recover after a sidecar restart

If the sidecar crashed, was recreated, or the Mac rebooted, the gateway can be left in the old
network namespace: it runs but is unreachable. Restore the sidecar first, then recreate the
gateway with the same file and profile selection:

```sh
docker compose ps tailscale                  # healthy?
GATEWAY_TAG=<current tag> docker compose up -d --force-recreate gateway
```

Then repeat checks 2 to 4. This is a manual procedure, not an automatic guarantee.

## Roll back

```sh
docker compose down            # same COMPOSE_FILE and COMPOSE_PROFILES as for the start
unset COMPOSE_FILE COMPOSE_PROFILES
GATEWAY_TAG=<previous tag> docker compose up -d
```

Never delete the `tailscale_state` volume (it holds the node identity) and never
`docker compose down -v`. Remove only a leftover sidecar container. Then check
`/healthz` on the host and from the reports and watchdog containers.

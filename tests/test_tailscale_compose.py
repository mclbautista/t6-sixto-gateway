"""Offline checks of both Compose deployment modes; never start containers."""

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def compose(*files: str) -> dict:
    env = {**os.environ, "TS_AUTHKEY": "test-key", "GATEWAY_BIND_IP": "127.0.0.1"}
    args = ["docker", "compose", "--env-file", ".env.example"]
    if "compose.tailscale.yaml" in files:
        args.extend(["--profile", "tailscale"])
    for file in files:
        args.extend(["-f", file])
    result = subprocess.run(
        [*args, "config", "--no-env-resolution", "--format", "json"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


def test_tailscale_overlay_preserves_shared_network_and_published_port(tmp_path: Path) -> None:
    # Simulate the existing untracked production override, including its DNS alias.
    override = tmp_path / "existing.yaml"
    override.write_text(
        "services:\n  gateway:\n    ports: !override\n"
        '      - "127.0.0.1:8780:8780"\n    networks:\n'
        "      sixto:\n        aliases: [gateway, sixto-gateway]\n"
        "networks:\n  sixto:\n    external: true\n"
    )
    config = compose("compose.yaml", str(override), "compose.tailscale.yaml")
    gateway = config["services"]["gateway"]
    sidecar = config["services"]["tailscale"]
    assert gateway["network_mode"] == "service:tailscale"
    assert not gateway.get("networks") and not gateway.get("ports")
    assert {"gateway", "sixto-gateway"} <= set(sidecar["networks"]["sixto"]["aliases"])
    assert config["networks"]["sixto"]["external"] is True
    assert sidecar["ports"][0]["host_ip"] == "127.0.0.1"
    assert sidecar["ports"][0]["published"] == "8780"
    assert sidecar["environment"]["TS_EXTRA_ARGS"] == "--advertise-tags=tag:t6-gateway"
    assert sidecar["environment"]["TS_USERSPACE"] == "false"
    assert sidecar["volumes"][0]["source"] == "tailscale_state"
    assert "@sha256:" in sidecar["image"]

    # Dropping the overlay restores the previous namespace, port and aliases.
    original = compose("compose.yaml", str(override))["services"]["gateway"]
    assert not original.get("network_mode")
    assert {"gateway", "sixto-gateway"} <= set(original["networks"]["sixto"]["aliases"])
    assert any(port.get("host_ip") == "127.0.0.1" for port in original["ports"])


def test_sidecar_key_is_not_in_gateway_environment(tmp_path: Path) -> None:
    # Resolve env files, not just Compose interpolation, in an isolated project.
    for name in ("compose.yaml", "compose.tailscale.yaml"):
        (tmp_path / name).write_text((ROOT / name).read_text())
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=placeholder\n")
    (tmp_path / ".env.tailscale").write_text("TS_AUTHKEY=sidecar-only-test-key\n")
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "tailscale",
            "-f",
            "compose.yaml",
            "-f",
            "compose.tailscale.yaml",
            "config",
            "--format",
            "json",
        ],
        cwd=tmp_path,
        env={k: v for k, v in os.environ.items() if k not in {"TS_AUTHKEY", "COMPOSE_FILE"}},
        check=True,
        capture_output=True,
    )
    services = json.loads(result.stdout)["services"]
    assert services["tailscale"]["environment"]["TS_AUTHKEY"] == "sidecar-only-test-key"
    assert "TS_AUTHKEY" not in services["gateway"]["environment"]


def test_default_compose_keeps_gateway_networking() -> None:
    config = compose("compose.yaml")
    gateway = config["services"]["gateway"]
    assert not gateway.get("network_mode")
    assert gateway["ports"][0]["published"] == "8780"
    assert "tailscale" not in config["services"]

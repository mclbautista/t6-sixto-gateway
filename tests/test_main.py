import subprocess
import sys

from tests.fakes import NOTION_YAML


def test_bad_config_stops_the_boot_with_exit_code_2(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("modules: {watchdog: {secret_env: WATCHDOG_SECRET}}\n" + NOTION_YAML)
    result = subprocess.run(
        [sys.executable, "-m", "sixto_gateway", "--config", str(config)],
        env={"PATH": "/usr/bin"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "WATCHDOG_SECRET is not set" in result.stderr


HUNG_GATEWAY = """
import asyncio, sys, time
import sixto_gateway.__main__ as entry
from sixto_gateway.config import load_config
from sixto_gateway.liveness import DEADMAN_SECONDS, Liveness
from tests.fakes import NOTION_YAML, FakeClock, FakeTelegram

entry.SHUTDOWN_GRACE_SECONDS = 0.2


class StuckInAThread(FakeTelegram):
    # like a DNS lookup that never returns: cancelling the task cannot stop the thread
    async def get_updates(self, offset, timeout, allowed_updates):
        await asyncio.get_running_loop().run_in_executor(None, time.sleep, 3600)


clock = FakeClock()
liveness = Liveness(clock)
clock.advance(DEADMAN_SECONDS)
config = load_config(
    sys.argv[1],
    {"TELEGRAM_BOT_TOKEN": "1:t", "WATCHDOG_SECRET": "w" * 40, "NOTION_API_KEY": "n" * 40},
)
sys.exit(entry.run_serving(entry.serve(config, StuckInAThread([]), liveness)))
"""


def test_a_deadman_exit_is_not_held_up_by_a_stuck_thread(tmp_path):
    import socket
    from pathlib import Path

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = tmp_path / "config.yaml"
    config.write_text(
        "modules: {watchdog: {secret_env: WATCHDOG_SECRET}}\n"
        f"listen: {{host: 127.0.0.1, port: {port}}}\n"
        f"database_path: {tmp_path / 'gateway.db'}\n" + NOTION_YAML
    )
    script = tmp_path / "hung.py"
    script.write_text(HUNG_GATEWAY)
    result = subprocess.run(
        [sys.executable, str(script), str(config)],
        cwd=Path(__file__).resolve().parents[1],
        env={"PATH": "/usr/bin", "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1, result.stderr
    assert "no getUpdates call completed for 10 minutes" in result.stderr
    assert "Traceback" not in result.stderr

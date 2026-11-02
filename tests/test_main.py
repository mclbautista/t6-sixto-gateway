import subprocess
import sys


def test_bad_config_stops_the_boot_with_exit_code_2(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("modules: {watchdog: {secret_env: WATCHDOG_SECRET}}\n")
    result = subprocess.run(
        [sys.executable, "-m", "sixto_gateway", "--config", str(config)],
        env={"PATH": "/usr/bin"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "WATCHDOG_SECRET is not set" in result.stderr

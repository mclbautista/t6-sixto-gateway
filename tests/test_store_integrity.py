"""Startup integrity check and the online ``backup`` command (t6-jo-watchdog#131)."""

import sqlite3
import stat

import pytest

from sixto_gateway.__main__ import main
from sixto_gateway.store import Store
from sixto_gateway.store.db import StoreCorrupt
from tests.fakes import NOTION_YAML

ENV = {"TELEGRAM_BOT_TOKEN": "1:t", "WATCHDOG_SECRET": "w" * 40, "NOTION_API_KEY": "n" * 40}


@pytest.fixture
def db(tmp_path):
    return tmp_path / "data" / "gateway.db"


@pytest.fixture
def config(tmp_path, db, monkeypatch):
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    path = tmp_path / "config.yaml"
    path.write_text(
        "modules: {watchdog: {secret_env: WATCHDOG_SECRET}}\n"
        f"database_path: {db}\n" + NOTION_YAML
    )
    return str(path)


def audit_count(path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    finally:
        conn.close()


def corrupt(db) -> None:
    """Overwrite every page after the first, as a torn write from outside would."""
    store = Store(db)
    for n in range(200):
        store.audit("filler", detail="x" * 200 + str(n))
    store.close()
    raw = bytearray(db.read_bytes())
    page = int.from_bytes(raw[16:18], "big")
    raw[page:] = b"\xa5" * (len(raw) - page)
    db.write_bytes(raw)


def test_a_healthy_database_opens_again(db):
    Store(db).close()
    Store(db).close()


def test_a_corrupt_database_is_refused_and_left_untouched(db):
    corrupt(db)
    before = db.read_bytes()
    with pytest.raises(StoreCorrupt) as caught:
        Store(db)
    assert str(db) in str(caught.value)
    assert "integrity check" in str(caught.value)
    assert db.read_bytes() == before


def test_a_file_that_is_not_a_database_is_refused(db):
    db.parent.mkdir()
    db.write_bytes(b"not a database, " * 400)
    with pytest.raises(StoreCorrupt):
        Store(db)


@pytest.mark.parametrize("command", [[], ["show-offset"], ["set-offset", "5"]])
def test_every_command_stops_on_a_corrupt_database_with_the_reason(db, config, capsys, command):
    corrupt(db)
    assert main(["--config", config, *command]) == 1
    assert "integrity check" in capsys.readouterr().err


def test_backup_of_a_live_database_is_a_complete_standalone_copy(db, config, tmp_path, capsys):
    store = Store(db)
    for n in range(5):
        store.audit("kept", detail=str(n))
    assert db.with_name("gateway.db-wal").stat().st_size > 0  # rows are still only in the WAL
    dest = tmp_path / "out" / "gateway-backup.db"
    dest.parent.mkdir()

    assert main(["--config", config, "backup", str(dest)]) == 0

    assert "integrity_check ok" in capsys.readouterr().out
    assert audit_count(dest) == 5
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert [p.name for p in dest.parent.iterdir()] == [dest.name]
    conn = sqlite3.connect(dest)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    conn.close()
    store.audit("later")  # the live database keeps working
    assert audit_count(dest) == 5
    store.close()


def test_backup_never_overwrites_an_existing_file(db, config, tmp_path, capsys):
    Store(db).close()
    dest = tmp_path / "taken.db"
    dest.write_text("keep me")
    assert main(["--config", config, "backup", str(dest)]) == 2
    assert "already exists" in capsys.readouterr().err
    assert dest.read_text() == "keep me"


def test_backup_never_creates_a_missing_database(db, config, tmp_path, capsys):
    dest = tmp_path / "gateway-backup.db"
    assert main(["--config", config, "backup", str(dest)]) == 2
    assert "no database" in capsys.readouterr().err
    assert not db.exists() and not dest.exists()

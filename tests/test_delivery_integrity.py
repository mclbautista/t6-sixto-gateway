"""A routed update always has a delivery before the cursor moves (issue #46)."""

import logging

import pytest

from sixto_gateway.__main__ import main
from sixto_gateway.store import Store
from sixto_gateway.telegram.normalize import IncomingUpdate
from tests.fakes import FakeClock
from tests.test_ops import ENV, write_config

DROPPED = 601013703  # one of the updates lost in production on 2026-11-04


def update(update_id=DROPPED):
    return IncomingUpdate(
        update_id=update_id,
        chat_id=-1003261335368,
        topic_id=2,
        message_id=10,
        from_id=7,
        from_username="u",
        text="hello",
        edited=False,
        edit_date=None,
        raw={"update_id": update_id},
        chat_type="supergroup",
    )


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "gateway.db", clock=FakeClock())


def deliveries(store, update_id):
    return store._db.execute(
        "SELECT seq, module FROM deliveries WHERE update_id = ?", (update_id,)
    ).fetchall()


def lose_delivery(store, update_id):
    """The state the bug left behind: the update stored, its delivery missing."""
    store._db.execute("DELETE FROM deliveries WHERE update_id = ?", (update_id,))


def test_a_routed_update_has_its_delivery_when_the_cursor_moves(store):
    seq = store.save_update(update(), "watchdog")
    assert seq is not None
    assert [r["module"] for r in deliveries(store, DROPPED)] == ["watchdog"]
    assert store.last_update_id() == DROPPED


def test_a_refetch_does_not_duplicate_the_delivery(store):
    store.save_update(update(), "watchdog")
    assert store.save_update(update(), "watchdog") is None
    assert len(deliveries(store, DROPPED)) == 1


def test_a_stored_update_missing_its_delivery_gets_one_on_refetch(store):
    store.save_update(update(), "watchdog")
    lose_delivery(store, DROPPED)

    seq = store.save_update(update(), "watchdog")

    assert seq is not None
    assert [r["seq"] for r in deliveries(store, DROPPED)] == [seq]
    assert store.deliveries_after("watchdog", 0)[0]["seq"] == seq
    events = [e for e, _, _ in store.audit_events()]
    assert "delivery_repaired" in events


def test_repair_logs_a_warning(store, caplog):
    store.save_update(update(), "watchdog")
    lose_delivery(store, DROPPED)
    with caplog.at_level(logging.WARNING):
        store.save_update(update(), "watchdog")
    assert any(str(DROPPED) in r.getMessage() for r in caplog.records)


def test_repair_is_idempotent(store):
    store.save_update(update(), "watchdog")
    lose_delivery(store, DROPPED)
    store.save_update(update(), "watchdog")
    assert store.save_update(update(), "watchdog") is None
    assert len(deliveries(store, DROPPED)) == 1


def test_an_update_stored_unrouted_stays_undelivered_on_refetch(store):
    store.save_update(update(), None)
    assert store.save_update(update(), "watchdog") is None
    assert deliveries(store, DROPPED) == []
    assert store.last_update_id() == DROPPED


def test_a_refetch_after_a_routes_change_does_not_deliver_twice(store):
    store.save_update(update(), "watchdog")
    assert store.save_update(update(), "shift") is None
    assert [r["module"] for r in deliveries(store, DROPPED)] == ["watchdog"]
    assert "delivery_repaired" not in [e for e, _, _ in store.audit_events()]


def test_a_repair_after_a_routes_change_goes_to_the_original_module(store):
    store.save_update(update(), "watchdog")
    lose_delivery(store, DROPPED)
    store.save_update(update(), "shift")
    assert [r["module"] for r in deliveries(store, DROPPED)] == ["watchdog"]


def test_a_failed_delivery_insert_does_not_move_the_cursor(store, monkeypatch):
    store.set_offset(100)
    real = store._db

    class Failing:
        def __getattr__(self, name):
            return getattr(real, name)

        def execute(self, sql, *args):
            if sql.lstrip().startswith("INSERT INTO deliveries"):
                raise RuntimeError("disk full")
            return real.execute(sql, *args)

    monkeypatch.setattr(store, "_db", Failing())
    with pytest.raises(RuntimeError):
        store.save_update(update(), "watchdog")
    monkeypatch.setattr(store, "_db", real)

    assert store.last_update_id() == 100
    assert store._db.execute("SELECT COUNT(*) FROM updates").fetchone()[0] == 0


def test_the_cursor_never_moves_past_a_routed_update_without_a_delivery(store, monkeypatch):
    store.set_offset(100)
    monkeypatch.setattr(store, "_insert_delivery", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="delivery"):
        store.save_update(update(), "watchdog")
    assert store.last_update_id() == 100


def test_a_crash_after_storing_then_a_retry_ends_with_one_delivery(store, monkeypatch):
    real_move = store._move_cursor

    def crash(_):
        raise RuntimeError("crash")

    monkeypatch.setattr(store, "_move_cursor", crash)
    with pytest.raises(RuntimeError):
        store.save_update(update(), "watchdog")
    monkeypatch.setattr(store, "_move_cursor", real_move)

    assert store.last_update_id() is None
    store.save_update(update(), "watchdog")
    assert len(deliveries(store, DROPPED)) == 1
    assert store.last_update_id() == DROPPED


def test_a_batch_that_fails_midway_resumes_at_the_failed_update(store, monkeypatch):
    store.save_update(update(1), "watchdog")
    real_insert = store._insert_delivery

    def fail(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "_insert_delivery", fail)
    with pytest.raises(RuntimeError):
        store.save_update(update(2), "watchdog")
    monkeypatch.setattr(store, "_insert_delivery", real_insert)

    assert store.next_offset() == 2  # the next poll fetches update 2 again
    store.save_update(update(2), "watchdog")
    assert [i["seq"] for i in store.deliveries_after("watchdog", 0)] == [1, 2]
    assert store.missing_deliveries() == []


def test_missing_deliveries_lists_routed_updates_without_one(store):
    store.save_update(update(1), "watchdog")
    store.save_update(update(2), "watchdog")
    lose_delivery(store, 2)
    assert store.missing_deliveries() == [2]


# --- CLI ------------------------------------------------------------------------------------


def test_show_offset_prints_the_cursor_and_gaps(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    assert main(["--config", str(path), "set-offset", "500"]) == 0
    capsys.readouterr()

    assert main(["--config", str(path), "show-offset"]) == 0
    out = capsys.readouterr().out
    assert "500" in out and "501" in out

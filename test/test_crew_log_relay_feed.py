"""The board relay decides off the writer and catches up from a durable cursor.

Each test runs the real crew-log writer and the real relay consumer on a temp data home,
with a real binding file, so the binding read, the header read and the board's newest
unit are the ones a gateway uses. A "restart" is :func:`work_relay.forget_for_tests`:
it drops every in-memory relay state and keeps what is on disk, like a new process.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew import session_ledger
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import work_relay, work_relay_feed
from kiro_crew.crew_log.emit_runtime import _write as emit_write
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, default_timeout, wait_until
from kiro_crew.work_ledger_writes import _write_binding

LEAD = "chat-relay-feed-lead"
WORKER = "chat-relay-feed-worker"
LEAD_UNIT = "u-feed-lead"
WORKER_UNIT = "u-feed-worker"


@pytest.fixture(autouse=True)
def _live_crew_log(tmp_path):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        patch.setenv("KIROCREW_CREW_LOG", "1")
        crew_log_emit.reset_caches()
        crew_log_eager.stop_for_tests()
        crew_log.forget_slot_folds()
        work_relay.forget_for_tests()
        yield patch
        crew_log_emit.flush()
        crew_log_eager.stop_for_tests()
        crew_log.forget_slot_folds()
        work_relay.forget_for_tests()
        crew_log_emit.reset_caches()


def _unit(unit: str, slot: str) -> None:
    CrewLog.create(lg.KIND_SESSION, unit, owner="owner", agent="lead", slot=slot)


def _settle() -> None:
    assert crew_log_emit.flush()
    assert crew_log_eager.drain(timeout=default_timeout())


def _board() -> None:
    _unit(LEAD_UNIT, LEAD)
    data = {"slot": LEAD, "actor": "conductor", "by": LEAD, "action": "goal", "goal": "g"}
    assert crew_log_emit.on_work_recorded(
        LEAD_UNIT, {**data, "round": 1}, timeout=default_timeout()
    )
    _settle()


def _bind() -> None:
    _write_binding(WORKER, LEAD, "it_0000000a")


def _turn(turn: int, credits: float) -> None:
    emit_write(
        WORKER_UNIT,
        "turn/completed",
        {
            "turn": turn,
            "stop_reason": "end_turn",
            "depth": 0,
            "duration_ms": 10,
            "model": "opus",
            "provider": "kiro",
            "credits": credits,
            "tokens": {"input": 5, "output": 1, "cache_read": 0, "cache_write": 0},
        },
    )
    _settle()


def _charges() -> "list[float]":
    log = CrewLog.open(lg.KIND_SESSION, LEAD_UNIT)
    return [e.data["credits"] for e in log.iter_from(1) if e.type == "work/charged"]


def _restart() -> None:
    """Forget every in-memory relay state, as a new process would; disk stays."""
    _settle()
    work_relay.forget_for_tests()


def test_the_writer_lane_never_resolves_a_relay(_live_crew_log):
    """The header, binding and newest-unit reads all run on the relay's own thread."""
    _board()
    _unit(WORKER_UNIT, WORKER)
    _bind()
    threads: "list[str]" = []
    for name in ("_unit_slot", "_bound_board"):
        real = getattr(work_relay, name)

        def spy(*args: Any, _real: Any = real) -> Any:
            threads.append(threading.current_thread().name)
            return _real(*args)

        _live_crew_log.setattr(work_relay, name, spy)
    real_newest = session_ledger.newest_work_unit

    def newest(board: str) -> str:
        threads.append(threading.current_thread().name)
        return real_newest(board)

    _live_crew_log.setattr(session_ledger, "newest_work_unit", newest)
    _turn(1, 2.5)
    assert _charges() == [2.5]
    assert threads and set(threads) == {"crew-log-relay"}, threads


def test_a_closer_the_crash_cut_off_is_relayed_after_the_restart(_live_crew_log):
    _board()
    _unit(WORKER_UNIT, WORKER)
    _bind()
    _turn(1, 1.0)
    with pytest.MonkeyPatch.context() as crash:
        # The closer commits, and the process dies before the relay decides it.
        crash.setattr(work_relay_feed, "note", lambda *a, **k: None)
        _turn(2, 2.0)
    assert _charges() == [1.0]
    _restart()
    _turn(3, 3.0)
    assert _charges() == [1.0, 2.0, 3.0]


def test_a_copy_that_landed_before_its_cursor_was_written_is_not_copied_again(
    _live_crew_log,
):
    _board()
    _unit(WORKER_UNIT, WORKER)
    _bind()
    _turn(1, 1.0)
    work_relay_feed.persist()
    cursor = work_relay_feed._cursor_path(work_relay_feed._state_home)
    assert json.loads(cursor.read_text())["units"][WORKER_UNIT] > 0
    # A crash after the copy landed and before the cursor moved: no cursor on disk.
    cursor.unlink()
    _restart()
    _turn(2, 2.0)
    assert _charges() == [1.0, 2.0]


def _bind_after(unit: str, seq_entry_type: str = "turn/completed") -> None:
    """Bind, with the binding file stamped 1 ms after *unit*'s newest closer."""
    from kiro_crew import work_ledger

    log = CrewLog.open(lg.KIND_SESSION, unit)
    closer = [e for e in log.iter_from(1) if e.type == seq_entry_type][-1]
    _bind()
    stamp = (int(closer.time) + 1) * 1_000_000
    os.utime(work_ledger.binding_path(WORKER), ns=(stamp, stamp))


def test_a_catch_up_does_not_relay_what_was_written_before_the_bind(_live_crew_log):
    _board()
    _unit(WORKER_UNIT, WORKER)
    _turn(1, 1.0)
    _bind_after(WORKER_UNIT)
    _restart()
    _turn(2, 2.0)
    assert _charges() == [2.0]


def test_a_full_queue_is_caught_up_from_the_cursor_in_order(_live_crew_log):
    _board()
    _unit(WORKER_UNIT, WORKER)
    _bind()
    _turn(1, 1.0)
    _live_crew_log.setattr(work_relay_feed, "QUEUE_LIMIT", 0)
    for turn in (2, 3, 4):
        _turn(turn, float(turn))
    assert work_relay_feed.overflowed() >= 3
    assert _charges() == [1.0, 2.0, 3.0, 4.0]


def test_the_cursor_only_moves_forward_and_survives_a_restart(_live_crew_log):
    _board()
    _unit(WORKER_UNIT, WORKER)
    _bind()
    _turn(1, 1.0)
    _turn(2, 2.0)
    work_relay_feed.persist()
    path = work_relay_feed._cursor_path(work_relay_feed._state_home)
    first = json.loads(path.read_text())["units"][WORKER_UNIT]
    _restart()
    _turn(3, 3.0)
    work_relay_feed.persist()
    later = json.loads(path.read_text())["units"][WORKER_UNIT]
    assert later > first
    assert _charges() == [1.0, 2.0, 3.0]


def _refuse_charges(patch: pytest.MonkeyPatch, credits: float) -> "dict[str, int]":
    """Make the store refuse every ``work/charged`` copy carrying *credits*."""
    from kiro_crew.crew_log.errors import CODE_BAD_DATA, CrewLogError

    calls = {"n": 0}
    real_many, real_one = CrewLog.append_many, CrewLog.append

    def many(log: CrewLog, items: Any, **kwargs: Any) -> Any:
        if any(i["type"] == "work/charged" and i["data"].get("credits") == credits for i in items):
            calls["n"] += 1
            raise CrewLogError("refused for the test", code=CODE_BAD_DATA)
        return real_many(log, items, **kwargs)

    def one(log: CrewLog, entry_type: str, data: Any, **kwargs: Any) -> Any:
        if entry_type == "work/charged" and data.get("credits") == credits:
            calls["n"] += 1
            raise CrewLogError("refused for the test", code=CODE_BAD_DATA)
        return real_one(log, entry_type, data, **kwargs)

    patch.setattr(CrewLog, "append_many", many)
    patch.setattr(CrewLog, "append", one)
    return calls


def test_a_refused_copy_is_decided_once_and_never_retried(_live_crew_log):
    """A refusal settles the entry: the cursor passes it and no retry marks it again."""
    _live_crew_log.setattr(work_relay_feed, "_RETRY_SECONDS", 0)
    _board()
    _unit(WORKER_UNIT, WORKER)
    _bind()
    _turn(1, 1.0)
    calls = _refuse_charges(_live_crew_log, 2.0)
    _turn(2, 2.0)
    _turn(3, 3.0)
    assert _charges() == [1.0, 3.0]
    refused_attempts = calls["n"]
    _turn(4, 4.0)
    assert _charges() == [1.0, 3.0, 4.0]
    with work_relay_feed._cond:
        assert WORKER_UNIT not in work_relay_feed._held
        assert not work_relay_feed._retry_at
    assert calls["n"] == refused_attempts, "a refused copy was tried again"


def test_a_header_that_cannot_be_read_is_retried_not_passed(_live_crew_log):
    """An unprovable header holds the cursor; the charge lands once the read recovers."""
    from kiro_crew.crew_log import store_units

    _live_crew_log.setattr(work_relay_feed, "_RETRY_SECONDS", 0)
    _board()
    _unit(WORKER_UNIT, WORKER)
    _bind()
    _turn(1, 1.0)
    work_relay._UNIT_SLOTS.clear()
    real = store_units._unit_header_object
    calls = {"n": 0}

    def once(kind: str, unit: str) -> Any:
        # Only the relay's own header read fails, once; every other reader is served.
        if unit == WORKER_UNIT and sys._getframe(1).f_code.co_name == "_unit_slot":
            calls["n"] += 1
            if calls["n"] == 1:
                return None
        return real(kind, unit)

    _live_crew_log.setattr(store_units, "_unit_header_object", once)
    _turn(2, 2.0)
    _turn(3, 3.0)
    _wait_for([1.0, 2.0, 3.0])
    assert calls["n"] >= 2, "the relay never read the header again"


def test_a_queued_closer_older_than_its_binding_is_not_charged(_live_crew_log):
    """The binding-time floor holds on the live path too, not only on a catch-up."""
    from kiro_crew import work_ledger

    _board()
    _unit(WORKER_UNIT, WORKER)
    _bind()
    stamp = os.stat(work_ledger.binding_path(WORKER)).st_mtime_ns + 3_600 * 10**9
    os.utime(work_ledger.binding_path(WORKER), ns=(stamp, stamp))
    _turn(1, 1.0)  # committed an hour "before" the binding was written
    assert _charges() == []


def _wait_for(charges: "list[float]") -> None:
    def landed() -> bool:
        crew_log_emit.flush()
        return _charges() == charges

    wait_until(landed, describe=lambda: _charges())
    assert _charges() == charges


def test_a_failed_binding_read_is_retried_and_its_copies_keep_their_order(_live_crew_log):
    """A read that fails holds the cursor; the unit's later closers wait for the retry."""
    _live_crew_log.setattr(work_relay_feed, "_RETRY_SECONDS", 0)
    _board()
    _unit(WORKER_UNIT, WORKER)
    _bind()
    _turn(1, 1.0)
    real = work_relay._bound_board
    calls = {"n": 0}

    def once(worker: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient")
        return real(worker)

    _live_crew_log.setattr(work_relay, "_bound_board", once)
    _turn(2, 2.0)
    _turn(3, 3.0)
    _wait_for([1.0, 2.0, 3.0])
    work_relay_feed.persist()
    path = work_relay_feed._cursor_path(work_relay_feed._state_home)
    assert json.loads(path.read_text())["units"][WORKER_UNIT] > 0


def test_a_copy_dropped_and_requeued_by_the_writer_settles_once_it_lands(_live_crew_log):
    """The requeued follow-on carries the ``after``, so the cursor moves past it."""
    from kiro_crew.crew_log.writer import WriterLimits

    crew_log_emit.reset_caches(limits=WriterLimits(max_write_attempts=1, retry_backoff_seconds=0.0))
    # Long, so only the requeued copy's own ``after`` can release the hold here.
    _live_crew_log.setattr(work_relay_feed, "_RETRY_SECONDS", FALLBACK_TIMEOUT_SECS)
    _board()
    _unit(WORKER_UNIT, WORKER)
    _bind()
    _turn(1, 1.0)
    real = work_relay._append_run
    calls = {"n": 0}

    def fail_once(*args: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk hiccup")
        return real(*args)

    _live_crew_log.setattr(work_relay, "_append_run", fail_once)
    _turn(2, 2.0)
    _wait_for([1.0, 2.0])
    _turn(3, 3.0)
    _wait_for([1.0, 2.0, 3.0])
    with work_relay_feed._cond:
        assert WORKER_UNIT not in work_relay_feed._held


# --------------------------------------------------------------------------- #
# The cursor file is advisory, atomic, idempotent to replay, and has one writer
# --------------------------------------------------------------------------- #


def _cursor_file() -> Any:
    return work_relay_feed._cursor_path(work_relay_feed._state_home)


def _crash_cut_closer(turn: int, credits: float) -> None:
    """Commit a closer the relay never hears about, as a crash before its wake would."""
    with pytest.MonkeyPatch.context() as crash:
        crash.setattr(work_relay_feed, "note", lambda *a, **k: None)
        _turn(turn, credits)


@pytest.mark.parametrize("damage", ["garbage", "stale"])
def test_an_unreadable_or_stale_cursor_falls_back_to_a_rescan(_live_crew_log, damage):
    _board()
    _unit(WORKER_UNIT, WORKER)
    _bind()
    _turn(1, 1.0)
    work_relay_feed.persist()
    path = _cursor_file()
    _crash_cut_closer(2, 2.0)
    _settle()
    if damage == "garbage":
        path.write_text("{not json", encoding="utf-8")
    else:
        path.write_text(json.dumps({"schema": 1, "units": {WORKER_UNIT: 10_000}}))
    _restart()
    _turn(3, 3.0)
    assert _charges() == [1.0, 2.0, 3.0]


def test_the_cursor_is_replaced_whole_and_a_failed_replace_keeps_the_old_one(
    _live_crew_log,
):
    from kiro_crew import atomic_write as aw

    _board()
    _unit(WORKER_UNIT, WORKER)
    _bind()
    _turn(1, 1.0)
    work_relay_feed.persist()
    path = _cursor_file()
    before = path.read_text(encoding="utf-8")
    seen: "dict[str, Any]" = {}
    real = aw.atomic_write

    def spy(target: Any, content: Any, **kwargs: Any) -> None:
        seen.update(kwargs)
        real(target, content, **kwargs)

    def no_rename(*_a: Any, **_k: Any) -> None:
        raise OSError("crash before the rename")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(aw, "atomic_write", spy)
        patch.setattr(aw, "publish_replacement", no_rename)
        _turn(2, 2.0)
        work_relay_feed.persist()
    assert seen.get("fsync") is True, seen
    assert path.read_text(encoding="utf-8") == before, "a torn or partial cursor was left"
    assert not [p for p in path.parent.iterdir() if p.name not in ("cursor.json", "relay.lock")]


def test_replaying_from_an_old_cursor_bills_each_closer_once(_live_crew_log):
    """A cursor behind what landed replays those entries; nothing is copied or billed twice."""
    _board()
    _unit(WORKER_UNIT, WORKER)
    _bind()
    for turn in (1, 2, 3):
        _turn(turn, float(turn))
    _settle()
    _cursor_file().write_text(json.dumps({"schema": 1, "units": {WORKER_UNIT: 1}}))
    _restart()
    _turn(4, 4.0)
    assert _charges() == [1.0, 2.0, 3.0, 4.0]
    from kiro_crew.crew_log.projection_readers import _slot_units_for_fold

    units = _slot_units_for_fold(LEAD, "workstreams")
    state = crew_log.fold_slot_warm("workstreams", units, slot=LEAD).state
    assert state["spend"][WORKER]["credits"] == 10.0
    assert state["spend"][WORKER]["reported"] == 4


def test_a_second_relayer_is_detected_and_never_writes_the_cursor(_live_crew_log, caplog):
    import logging
    import os

    from kiro_crew.platform_lock_compat import release_lock, try_acquire_lock_or_raise

    _board()
    _unit(WORKER_UNIT, WORKER)
    _bind()
    _turn(1, 1.0)
    work_relay_feed.persist()
    path = _cursor_file()
    before = path.read_text(encoding="utf-8")
    _restart()
    # Another process holds the relay lock for this data home.
    fd = os.open(path.with_name("relay.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        assert try_acquire_lock_or_raise(fd, exclusive=True)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.crew_log.work_relay_feed"):
            _turn(2, 2.0)
            work_relay_feed.persist()
        assert "another process holds" in caplog.text
        assert path.read_text(encoding="utf-8") == before
    finally:
        release_lock(fd)
        os.close(fd)

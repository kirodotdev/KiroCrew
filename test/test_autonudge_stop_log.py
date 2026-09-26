"""Every auto-nudge loop stop is logged at WARNING and kept in autonudge-stops.jsonl."""

from __future__ import annotations

import asyncio
import json
import logging
import os

import pytest

from kiro_crew import autonudge as _an
from kiro_crew import autonudge_stop_log as stoplog
from kiro_crew.autonudge import AutoNudgeService


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.fixture(autouse=True)
def _unpublish():
    yield
    _an._INSTANCE = None


def _records(base):
    path = stoplog.stop_log_path(base)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _run(coro):
    return asyncio.run(coro)


# ── pure diff ──


def _row(loop_id, *, active, **extra):
    return {
        "id": loop_id,
        "slot_key": "chat-1",
        "active": active,
        "cycle_count": 3,
        "max_cycles": 24,
        "created_ts": 1000.0,
        "max_runtime_secs": 14400,
        **extra,
    }


def test_inactive_row_reports_its_stored_reason():
    before = stoplog.active_summaries([_row("a", active=True)])
    out = stoplog.stop_records(
        before, [_row("a", active=False, stopped_reason="runtime_budget")], {}, now=1500.0
    )
    assert [(r["loop_id"], r["reason"], r["ran_secs"], r["cycle_count"]) for r in out] == [
        ("a", "runtime_budget", 500, 3)
    ]


def test_removed_row_takes_the_note_and_falls_back_to_removed():
    before = stoplog.active_summaries([_row("a", active=True), _row("b", active=True)])
    out = stoplog.stop_records(before, [], {"a": ("autonudge_stop", "goal met")}, now=2000.0)
    by_id = {r["loop_id"]: r for r in out}
    assert (by_id["a"]["reason"], by_id["a"]["detail"]) == ("autonudge_stop", "goal met")
    assert by_id["b"]["reason"] == stoplog.REMOVED_REASON


def test_a_note_never_labels_a_row_that_is_still_there():
    """A note belongs to a removal; a surviving row's own reason is the only truth."""
    before = stoplog.active_summaries([_row("a", active=True)])
    out = stoplog.stop_records(
        before,
        [_row("a", active=False, stopped_reason="cycle_cap")],
        {"a": ("autonudge_stop", "stale")},
    )
    assert (out[0]["reason"], out[0]["detail"]) == ("cycle_cap", "")


def test_store_text_is_clipped_and_scrubbed_before_it_is_kept():
    long_key = "chat-" + "x" * 1000
    [summary] = stoplog.active_summaries([_row("a", active=True, slot_key=long_key)]).values()
    assert len(summary["slot_key"]) <= stoplog.FIELD_MAX_CHARS


def test_log_line_escapes_a_newline_in_store_text(caplog):
    record = {"kind": "loop", "loop_id": "a\nFORGED", "slot_key": "s", "reason": "r"}
    with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"):
        stoplog.log_record(record)
    assert "\n" not in caplog.records[-1].getMessage()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_append_refuses_a_planted_symlink(tmp_path):
    victim = tmp_path / "policy.json"
    victim.write_text("{}")
    link = tmp_path / stoplog.STOP_LOG_FILE
    try:
        link.symlink_to(victim)
    except OSError:
        pytest.skip("symlinks not permitted here")
    with pytest.raises(OSError):
        stoplog.append_records(link, [{"n": 1}])
    assert victim.read_text() == "{}"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_append_refuses_a_planted_fifo_without_blocking(tmp_path):
    """A FIFO with no reader must be refused, not waited on forever."""
    import threading

    fifo = tmp_path / stoplog.STOP_LOG_FILE
    os.mkfifo(fifo)
    outcome: list[BaseException | None] = []

    def attempt():
        try:
            stoplog.append_records(fifo, [{"n": 1}])
            outcome.append(None)
        except BaseException as exc:  # noqa: BLE001 - recorded for the assert
            outcome.append(exc)

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive(), "append blocked on a FIFO"
    assert isinstance(outcome[0], OSError)


def test_poisoned_store_numbers_are_dropped_not_kept():
    """An agent-written store may carry huge or non-numeric values."""
    row = _row("a", active=True, created_ts=10**400, cycle_count="1\nFORGED")
    before = stoplog.active_summaries([row])
    [rec] = stoplog.stop_records(before, [], {}, now=2000.0)
    assert (rec["cycle_count"], rec["ran_secs"]) == (None, None)


def test_a_failed_record_still_moves_the_baseline(tmp_path, monkeypatch):
    """A baseline entry that breaks recording must not break every later stop."""
    real = stoplog.stop_records
    poison: list[str] = []

    def flaky(previous_active, rows, notes, **kwargs):
        if poison and poison[0] in previous_active:
            raise OverflowError("poisoned baseline entry")
        return real(previous_active, rows, notes, **kwargs)

    monkeypatch.setattr(stoplog, "stop_records", flaky)

    async def body():
        svc = AutoNudgeService(base_dir=tmp_path)
        first = await svc.add("chat-12", "tick", idle_secs=60)
        poison.append(first.id)
        second = await svc.add("chat-13", "tick", idle_secs=60)
        await svc.update(first.id, active=False, stopped_reason="cycle_cap")
        await svc.update(second.id, active=False, stopped_reason="runtime_budget")
        svc.stop()
        return second.id

    second_id = _run(body())
    assert [(r["loop_id"], r["reason"]) for r in _records(tmp_path)] == [
        (second_id, "runtime_budget")
    ]


def test_still_active_and_never_active_rows_yield_nothing():
    before = stoplog.active_summaries([_row("a", active=True), _row("b", active=False)])
    assert before.keys() == {"a"}
    assert stoplog.stop_records(before, [_row("a", active=True)], {}) == []


def test_structured_monitor_reason_comes_from_the_monitor_state():
    monitor = {
        "target": "https://x/pull/1",
        "stopped_reason": "user_stop",
        "user_stop_reason": "why",
    }
    before = stoplog.active_summaries(
        [_row("m", active=True, monitor={"target": "https://x/pull/1"})]
    )
    out = stoplog.stop_records(before, [_row("m", active=False, monitor=monitor)], {})
    assert (out[0]["kind"], out[0]["reason"], out[0]["detail"]) == ("monitor", "user_stop", "why")


def test_append_rotates_past_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(stoplog, "STOP_LOG_MAX_BYTES", 30)
    path = tmp_path / stoplog.STOP_LOG_FILE
    stoplog.append_records(path, [{"n": 1, "pad": "x" * 10}])
    stoplog.append_records(path, [{"n": 2}])
    assert [json.loads(x)["n"] for x in path.read_text().splitlines()] == [2]
    assert json.loads((tmp_path / (stoplog.STOP_LOG_FILE + ".1")).read_text())["n"] == 1


# ── the service's commit point ──


def test_deactivation_is_logged_at_warning_and_recorded(tmp_path, caplog):
    async def body():
        svc = AutoNudgeService(base_dir=tmp_path)
        loop = await svc.add("chat-7", "tick", idle_secs=60)
        await svc.update(loop.id, active=False, stopped_reason="runtime_budget")
        svc.stop()
        return loop.id

    with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"):
        loop_id = _run(body())
    [rec] = _records(tmp_path)
    assert (rec["loop_id"], rec["slot_key"], rec["reason"]) == (loop_id, "chat-7", "runtime_budget")
    assert any(
        r.levelno == logging.WARNING and "reason='runtime_budget'" in r.getMessage()
        for r in caplog.records
    )


def test_removed_legacy_loop_keeps_the_agents_reason(tmp_path):
    async def body():
        svc = AutoNudgeService(base_dir=tmp_path)
        loop = await svc.add("chat-8", "tick", idle_secs=60)
        await svc.remove(loop.id, stop_reason=_an.AUTONUDGE_STOP_REASON, stop_detail="goal met")
        svc.stop()
        return loop.id

    loop_id = _run(body())
    [rec] = _records(tmp_path)
    assert (rec["loop_id"], rec["reason"], rec["detail"]) == (loop_id, "autonudge_stop", "goal met")


def test_a_failed_removal_leaves_no_reason_for_a_later_stop(tmp_path, monkeypatch):
    async def body():
        svc = AutoNudgeService(base_dir=tmp_path)
        loop = await svc.add("chat-10", "tick", idle_secs=60)
        real = svc._write_state

        def boom(payload):
            raise OSError("disk full")

        monkeypatch.setattr(svc, "_write_state", boom)
        with pytest.raises(OSError):
            await svc.remove(loop.id, stop_reason=_an.AUTONUDGE_STOP_REASON, stop_detail="x")
        assert svc._stop_notes == {}
        monkeypatch.setattr(svc, "_write_state", real)
        await svc.remove(loop.id)
        svc.stop()
        return loop.id

    loop_id = _run(body())
    assert [(r["loop_id"], r["reason"], r["detail"]) for r in _records(tmp_path)] == [
        (loop_id, stoplog.REMOVED_REASON, "")
    ]


def test_baseline_comes_from_the_loaded_store(tmp_path):
    """A loop armed before a restart is still recorded when it stops after one."""

    async def arm():
        svc = AutoNudgeService(base_dir=tmp_path)
        loop = await svc.add("chat-9", "tick", idle_secs=60)
        svc.stop()
        return loop.id

    async def stop_after_restart(loop_id):
        svc = AutoNudgeService(base_dir=tmp_path)
        svc._load()
        await svc.update(loop_id, active=False, stopped_reason="cycle_cap")
        svc.stop()

    loop_id = _run(arm())
    assert _records(tmp_path) == []
    _run(stop_after_restart(loop_id))
    assert [(r["loop_id"], r["reason"]) for r in _records(tmp_path)] == [(loop_id, "cycle_cap")]


def test_a_row_held_aside_at_load_is_not_reported_as_removed(tmp_path):
    """A quarantined row stays on disk for repair; it did not stop."""

    async def arm():
        svc = AutoNudgeService(base_dir=tmp_path)
        good = await svc.add("chat-11", "tick", idle_secs=60)
        svc.stop()
        return good.id

    good_id = _run(arm())
    store = tmp_path / _an._NUDGES_FILE
    data = json.loads(store.read_text())
    bad = dict(data["loops"][0], id="bad1", slot_key="chat-\x1b[31m-12")
    data["loops"].append(bad)
    store.write_text(json.dumps(data))

    async def reload_and_stop():
        svc = AutoNudgeService(base_dir=tmp_path)
        svc._load()
        assert "bad1" not in svc._loops
        await svc.update(good_id, active=False, stopped_reason="cycle_cap")
        svc.stop()

    _run(reload_and_stop())
    assert [(r["loop_id"], r["reason"]) for r in _records(tmp_path)] == [(good_id, "cycle_cap")]

"""Structured session health: waits are waits, queued is queued, a true stall is bounded.

Drives ``SessionHealthMonitor`` with plain snapshots and an injected clock; the
legacy log scan is exercised only as the secondary source it now is.
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew.dashboard import session_health as sh
from kiro_crew.metrics import events as ev

# ── helpers ──────────────────────────────────────────────────────────────────


def _snap(key: str = "chat-1-1", **kw) -> sh.SlotSnapshot:
    base = dict(key=key, running=True, progress_marker=(1, "t0", 5, 2, None))
    base.update(kw)
    return sh.SlotSnapshot(**base)


def _health(slots, *, mono=1000.0, wall=1_700_000_000.0) -> sh.HealthSnapshot:
    return sh.HealthSnapshot(slots=list(slots), wall_now=wall, mono_now=mono)


def _monitor(**kw) -> sh.SessionHealthMonitor:
    kw.setdefault("include_log_scan", False)
    return sh.SessionHealthMonitor(**kw)


class _Rec:
    def __init__(self) -> None:
        self.counters: list = []
        self.hists: list = []

    def counter(self, name, value=1, *, attrs=None, **kw):
        self.counters.append({"name": name, "attrs": dict(attrs or {})})

    def histogram(self, name, value, *, attrs=None, **kw):
        self.hists.append({"name": name, "value": value, "attrs": dict(attrs or {})})


@pytest.fixture
def rec():
    r = _Rec()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=r):
        yield r


# ── waits are not stalls ─────────────────────────────────────────────────────


class TestWaitsAreNotStalls:
    def test_waiting_permission_is_not_stalled_however_old(self):
        mon = _monitor()
        snap = _snap(pending_approval=True)
        mon.compute(_health([snap], mono=0.0))
        out = mon.compute(_health([snap], mono=5 * 3600.0))
        assert out["stalled"] == {}
        assert out["slots"]["chat-1-1"]["classification"] == sh.HEALTH_WAITING_PERMISSION
        assert out["waiting"][0]["reason"] == sh.HEALTH_WAITING_PERMISSION
        assert out["waiting"][0]["age_secs"] == 5 * 3600.0

    def test_handle_awaiting_permission_alone_is_enough(self):
        mon = _monitor()
        out = mon.compute(_health([_snap(awaiting_permission=True)]))
        assert out["slots"]["chat-1-1"]["classification"] == sh.HEALTH_WAITING_PERMISSION
        assert "handle awaiting_permission" in out["slots"]["chat-1-1"]["evidence"]

    def test_question_pending_is_waiting_input(self):
        out = _monitor().compute(_health([_snap(question_pending=True)]))
        assert out["slots"]["chat-1-1"]["classification"] == sh.HEALTH_WAITING_INPUT

    def test_wait_tool_is_waiting_dependency(self):
        out = _monitor().compute(_health([_snap(wait_state=True)]))
        assert out["slots"]["chat-1-1"]["classification"] == sh.HEALTH_WAITING_DEPENDENCY

    def test_children_running_under_a_spawn_call_is_waiting_children(self):
        out = _monitor().compute(
            _health([_snap(children_running=3, inflight_tool_name="spawn_sub_agents")])
        )
        s = out["slots"]["chat-1-1"]
        assert s["classification"] == sh.HEALTH_WAITING_CHILDREN
        assert "3 child run(s) active" in s["evidence"]

    def test_children_running_without_a_wait_call_is_still_running(self):
        """A parent that keeps working while children run is not waiting."""
        out = _monitor().compute(_health([_snap(children_running=2, inflight_tool_name="fs_read")]))
        assert out["slots"]["chat-1-1"]["classification"] == sh.HEALTH_RUNNING

    def test_retry_in_flight_is_recovering(self):
        out = _monitor().compute(_health([_snap(recovery_kinds=["tool_stallx1"])]))
        s = out["slots"]["chat-1-1"]
        assert s["classification"] == sh.HEALTH_RECOVERING
        assert out["recovering"][0]["key"] == "chat-1-1"
        assert out["stalled"] == {}


# ── queued is queued ─────────────────────────────────────────────────────────


class _FakeStore:
    def __init__(self, rows, by_state, oldest):
        self._rows = rows
        self._by_state = by_state
        self._oldest = oldest

    def count_by_state(self):
        return dict(self._by_state)

    def oldest_wait_secs(self):
        return self._oldest

    def active_rows(self):
        return list(self._rows)


def _row(id_, state, **kw):
    base = dict(
        id=id_,
        kind="subagent",
        session_key="chat-9-9",
        state=state,
        attempts=0,
        next_run_at=None,
        updated_at=1_700_000_000.0 - 30.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class TestQueuedIsQueued:
    def test_queue_depth_and_oldest_wait_come_from_the_store(self):
        store = _FakeStore(
            rows=[],
            by_state={"queued": 1990, "running": 10, "retry_wait": 3, "done": 40},
            oldest=1234.5,
        )
        out = _monitor().compute(_health([]), taskq=store)
        assert out["queued"]["available"] is True
        assert out["queued"]["count"] == 1993  # queued + admitted + retry_wait
        assert out["queued"]["oldest_wait_secs"] == 1234.5
        assert out["queued"]["by_state"]["running"] == 10
        assert out["counts"]["queued"] == 1993
        assert out["stalled"] == {}

    def test_task_waits_and_recoveries_are_listed_not_stalled(self):
        store = _FakeStore(
            rows=[
                _row("t1", "waiting_children"),
                _row("t2", "waiting_permission"),
                _row("t3", "recovering", attempts=2, next_run_at=1_700_000_030.0),
                _row("t4", "running"),
            ],
            by_state={
                "waiting_children": 1,
                "waiting_permission": 1,
                "recovering": 1,
                "running": 1,
            },
            oldest=0.0,
        )
        out = _monitor().compute(_health([]), taskq=store)
        reasons = {w["id"]: w["reason"] for w in out["waiting"]}
        assert reasons == {"t1": sh.HEALTH_WAITING_CHILDREN, "t2": sh.HEALTH_WAITING_PERMISSION}
        assert [r["id"] for r in out["recovering"]] == ["t3"]
        assert out["recovering"][0]["attempts"] == 2
        assert out["recovering"][0]["next_run_at"] == 1_700_000_030.0
        assert out["waiting"][0]["age_secs"] == 30.0
        assert out["stalled"] == {}

    def test_a_broken_store_reads_as_unavailable_not_an_error(self):
        class _Boom:
            def count_by_state(self):
                raise RuntimeError("locked")

        out = _monitor().compute(_health([]), taskq=_Boom())
        assert out["queued"]["available"] is False
        assert out["sources"]["taskq"] is False

    def test_no_store_at_all(self):
        out = _monitor().compute(_health([]))
        assert out["queued"] == {
            "available": False,
            "count": 0,
            "oldest_wait_secs": 0.0,
            "by_state": {},
        }


# ── a true stall is flagged in bounded time ──────────────────────────────────


class TestStallDetection:
    def test_true_stall_is_flagged_once_the_bound_passes(self):
        mon = _monitor(stall_after_secs=600.0)
        snap = _snap(
            tool_dispatched=True, inflight_tool_name="execute_bash", inflight_dispatch_age_secs=0.0
        )
        out0 = mon.compute(_health([snap], mono=0.0))
        assert out0["slots"]["chat-1-1"]["classification"] == sh.HEALTH_RUNNING
        out1 = mon.compute(_health([snap], mono=599.0))
        assert out1["stalled"] == {}
        snap2 = _snap(
            tool_dispatched=True,
            inflight_tool_name="execute_bash",
            inflight_dispatch_age_secs=600.0,
        )
        out2 = mon.compute(_health([snap2], mono=600.0))
        assert "chat-1-1" in out2["stalled"]
        st = out2["stalled"]["chat-1-1"]
        assert st["reason"] == "no_progress"
        assert st["age_secs"] == 600.0
        assert any("no progress marker moved for 600s" in e for e in st["evidence"])
        assert any("execute_bash dispatched 600s ago" in e for e in st["evidence"])
        assert out2["counts"]["stalled"] == 1

    def test_progress_resets_the_clock(self):
        mon = _monitor(stall_after_secs=600.0)
        mon.compute(_health([_snap(progress_marker=(1, "a", 5, 2, None))], mono=0.0))
        mon.compute(_health([_snap(progress_marker=(2, "b", 9, 4, None))], mono=590.0))
        out = mon.compute(_health([_snap(progress_marker=(2, "b", 9, 4, None))], mono=1100.0))
        assert out["stalled"] == {}
        assert out["slots"]["chat-1-1"]["age_secs"] == 510.0

    def test_long_running_with_progress_is_never_flagged(self):
        """A 40-minute build that keeps producing events is running, not stalled."""
        mon = _monitor(stall_after_secs=600.0)
        for i in range(0, 2400, 60):
            out = mon.compute(_health([_snap(progress_marker=(i, "t", i, 1, None))], mono=float(i)))
            assert out["stalled"] == {}, i
            assert out["slots"]["chat-1-1"]["classification"] == sh.HEALTH_RUNNING

    def test_liveness_oracle_vouching_overrides_the_age_rule(self):
        """A silent shell child the oracle sees WORKING is not a stall."""
        mon = _monitor(stall_after_secs=600.0)
        snap = _snap(
            tool_dispatched=True, inflight_tool_name="execute_bash", liveness_verdict="WORKING"
        )
        mon.compute(_health([snap], mono=0.0))
        out = mon.compute(_health([snap], mono=3600.0))
        assert out["stalled"] == {}
        assert "liveness oracle WORKING" in out["slots"]["chat-1-1"]["evidence"]

    def test_idle_slots_are_not_reported_and_forget_their_memo(self):
        mon = _monitor()
        mon.compute(_health([_snap()], mono=0.0))
        out = mon.compute(_health([_snap(running=False)], mono=10.0))
        assert out["slots"] == {}
        assert "chat-1-1" not in mon._progress

    def test_vanished_slots_drop_their_memo(self):
        mon = _monitor()
        mon.compute(_health([_snap("a"), _snap("b")], mono=0.0))
        mon.compute(_health([_snap("a")], mono=1.0))
        assert set(mon._progress) == {"a"}


# ── the legacy log scan is secondary ─────────────────────────────────────────


def _ts_from_file(path: Path) -> str:
    return datetime.datetime.fromtimestamp(path.stat().st_mtime).strftime("%H:%M:%S")


def _write_log(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n")
    os.utime(path, None)


class TestLogScanIsSecondary:
    def test_scan_still_recognises_the_two_shapes(self, tmp_path: Path):
        log = tmp_path / "gateway.log"
        _write_log(log, [""])
        ts = _ts_from_file(log)
        _write_log(
            log,
            [
                f"{ts} WARNING kiro_crew.dashboard.chat: ACP error in slot chat-7-1: [AcpPromptBusy] busy",
                f"{ts} WARNING kiro_crew.slack.gateway: Injected timeout error for subagent abc into slot chat-3-1",
                f"{ts} WARNING kiro_crew.slack.gateway: Injected timeout error for subagent x into slot cron_1",
                f"{ts} WARNING kiro_crew.session: Session chat-2-2 has dead provider — removing stale entry",
                f"{ts} WARNING kiro_crew.dashboard.chat: ACP error in slot chat-8-1: [AcpError] throttled",
            ],
        )
        hits = sh.scan_log_for_stalls(log_path=log, now=log.stat().st_mtime)
        assert hits["chat-7-1"]["reason"] == "prompt_stuck"
        assert hits["chat-3-1"]["reason"] == "subagent_timeout"
        assert "cron_1" not in hits and "chat-2-2" not in hits and "chat-8-1" not in hits

    def test_scan_skips_lines_outside_the_window_and_missing_files(self, tmp_path: Path):
        log = tmp_path / "gateway.log"
        _write_log(log, [""])
        old = (
            datetime.datetime.fromtimestamp(log.stat().st_mtime) - datetime.timedelta(minutes=20)
        ).strftime("%H:%M:%S")
        _write_log(
            log,
            [
                f"{old} WARNING kiro_crew.dashboard.chat: ACP error in slot chat-4-2: [AcpPromptBusy] x"
            ],
        )
        assert sh.scan_log_for_stalls(log_path=log) == {}
        assert sh.scan_log_for_stalls(log_path=tmp_path / "nope.log") == {}

    def test_a_log_hit_never_overrides_a_structured_wait(self, tmp_path: Path):
        log = tmp_path / "gateway.log"
        _write_log(log, [""])
        ts = _ts_from_file(log)
        _write_log(
            log,
            [
                f"{ts} WARNING kiro_crew.dashboard.chat: ACP error in slot chat-1-1: [AcpPromptBusy] x"
            ],
        )
        mon = sh.SessionHealthMonitor(include_log_scan=True)
        out = mon.compute(
            _health([_snap(pending_approval=True)], wall=log.stat().st_mtime), log_path=log
        )
        assert out["slots"]["chat-1-1"]["classification"] == sh.HEALTH_WAITING_PERMISSION
        assert out["stalled"] == {}

    def test_a_log_hit_on_a_running_slot_is_evidence_backed_stall(self, tmp_path: Path):
        log = tmp_path / "gateway.log"
        _write_log(log, [""])
        ts = _ts_from_file(log)
        _write_log(
            log,
            [
                f"{ts} WARNING kiro_crew.dashboard.chat: ACP error in slot chat-1-1: [AcpPromptBusy] x"
            ],
        )
        mon = sh.SessionHealthMonitor(include_log_scan=True)
        out = mon.compute(_health([_snap()], wall=log.stat().st_mtime), log_path=log)
        assert out["stalled"]["chat-1-1"]["reason"] == "prompt_stuck"
        assert out["slots"]["chat-1-1"]["source"] == "log"

    def test_without_state_objects_the_log_is_the_only_source(self, tmp_path: Path):
        log = tmp_path / "gateway.log"
        _write_log(log, [""])
        ts = _ts_from_file(log)
        _write_log(
            log,
            [
                f"{ts} WARNING kiro_crew.dashboard.chat: ACP error in slot chat-5-5: [AcpPromptBusy] x"
            ],
        )
        out = sh.compute_session_health(None, log_path=log, now=log.stat().st_mtime)
        assert out["stalled"]["chat-5-5"]["reason"] == "prompt_stuck"
        assert out["sources"] == {"slots": False, "taskq": False, "log": True}


# ── snapshots from live objects ──────────────────────────────────────────────


class _Future:
    def __init__(self, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


class TestSnapshotFromObjects:
    def test_snapshot_reads_slot_and_handle_fail_soft(self):
        inflight = SimpleNamespace(tool_name="execute_bash", dispatch_ts=900.0)
        handle = SimpleNamespace(
            is_turn_active=lambda: True,
            awaiting_permission=False,
            _tool_dispatched=True,
            _inflight_tool=inflight,
            parked_for_secs=lambda: 2.5,
            _ingress_seq=17,
            last_prompt_stats=SimpleNamespace(text_chunks=4),
        )
        slot = SimpleNamespace(
            key="chat-2-2",
            running=True,
            _approval_futures={"r1": _Future(True), "r2": _Future(False)},
            _question_pending=None,
            _wait_state=None,
            _subagent_deliveries_inflight=False,
            _tool_stall_retries=1,
            messages=[{"ts": "2026-09-12T10:00:00"}],
            _acp_client=handle,
        )
        snap = sh.snapshot_slot(slot, children_running=1, mono_now=1000.0)
        assert snap.pending_approval is True
        assert snap.recovery_kinds == ["tool_stallx1"]
        assert snap.turn_active is True
        assert snap.inflight_tool_name == "execute_bash"
        assert snap.inflight_dispatch_age_secs == 100.0
        assert snap.parked_for_secs == 2.5
        assert snap.progress_marker == (1, "2026-09-12T10:00:00", 17, 4, 900.0)

    def test_snapshot_of_a_bare_object_does_not_raise(self):
        snap = sh.snapshot_slot(object())
        assert snap.key == "" and snap.running is False

    def test_snapshot_state_walks_slots_children_and_caps(self):
        child = SimpleNamespace(parent_session_key="chat-1-1")
        subagents = SimpleNamespace(
            running=[child, child], max_concurrent=6, running_count=2, _queue=[1, 2, 3]
        )
        state = SimpleNamespace(
            subagents=subagents,
            _slots={
                "chat-1-1": SimpleNamespace(key="chat-1-1", running=True, messages=[]),
                "_bg": SimpleNamespace(key="_bg", running=True, messages=[]),
                "cron_abc": SimpleNamespace(key="cron_abc", running=True, messages=[]),
            },
        )
        snap = sh.snapshot_state(state, now=1.0, mono_now=2.0)
        assert [s.key for s in snap.slots] == ["chat-1-1"]
        assert snap.slots[0].children_running == 2
        assert (snap.subagents_cap, snap.subagents_running, snap.subagents_window_queued) == (
            6,
            2,
            3,
        )
        out = _monitor().compute(snap)
        assert out["effective_caps"]["subagents"] == {
            "effective": 6,
            "running": 2,
            "window_queued": 3,
        }

    def test_compute_session_health_uses_the_managers_store(self):
        store = _FakeStore(rows=[], by_state={"queued": 5}, oldest=12.0)
        state = SimpleNamespace(subagents=SimpleNamespace(running=[], _taskq=store), _slots={})
        out = sh.compute_session_health(state, monitor=_monitor())
        assert out["queued"]["count"] == 5


# ── caps, degrade reason, metrics ────────────────────────────────────────────


class TestCapsAndMetrics:
    def test_registered_sources_feed_caps_and_degrade_reason(self):
        mon = _monitor()
        mon.register_cap_source("spawn_gate", lambda: {"effective": 4, "user_max": 8})
        mon.register_pressure_source(lambda: None)
        mon.register_pressure_source(lambda: "memory")
        out = mon.compute(_health([]))
        assert out["effective_caps"]["spawn_gate"] == {"effective": 4, "user_max": 8}
        assert out["degrade_reason"] == "memory"

    def test_a_raising_source_is_skipped(self):
        mon = _monitor()

        def boom():
            raise RuntimeError("x")

        mon.register_cap_source("spawn_gate", boom)
        mon.register_pressure_source(boom)
        out = mon.compute(_health([]))
        assert out["effective_caps"] == {}
        assert out["degrade_reason"] is None

    def test_metrics_are_sampled_with_closed_attributes(self, rec):
        mon = _monitor()
        mon.register_cap_source("spawn_gate", lambda: {"effective": 4})
        mon.register_pressure_source(lambda: "loop_lag")
        store = _FakeStore(rows=[], by_state={"queued": 7, "running": 2}, oldest=99.0)
        mon.compute(_health([]), taskq=store)
        depth = {h["attrs"]["state"]: h["value"] for h in rec.hists if h["name"] == ev.TASKQ_DEPTH}
        assert depth == {"queued": 7.0, "running": 2.0}
        oldest = [h for h in rec.hists if h["name"] == ev.TASKQ_OLDEST_WAIT_SECS]
        assert oldest[-1]["value"] == 99.0
        caps = {
            h["attrs"]["lane_kind"]: h["value"]
            for h in rec.hists
            if h["name"] == ev.TASKQ_EFFECTIVE_CAP
        }
        assert caps == {"spawn_gate": 4.0}
        pressure = [c for c in rec.counters if c["name"] == ev.TASKQ_PRESSURE_REASON]
        assert pressure[-1]["attrs"] == {"reason": "loop_lag"}

    def test_payload_shape_for_the_ui(self):
        out = _monitor().compute(_health([_snap()]))
        assert set(out) >= {
            "stalled",
            "slots",
            "waiting",
            "recovering",
            "queued",
            "effective_caps",
            "degrade_reason",
            "counts",
            "sources",
            "generated_at",
        }
        assert set(out["counts"]) == {"running", "queued", "waiting", "recovering", "stalled"}
        assert out["counts"]["running"] == 1

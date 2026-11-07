"""Stage 4 regressions: truthful process termination and immutable terminal runs."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kiro_byo.durable_runs import DurableRunStore
from kiro_byo.process_manager import ByteRingBuffer, ProcessError, ProcessHandle, ProcessManager


def test_unkillable_process_is_never_reported_as_terminal(monkeypatch, tmp_path: Path) -> None:
    class Unkillable:
        pid = 12345
        stdin = stdout = stderr = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("unkillable", timeout)

    manager = ProcessManager(max_processes=1)
    proc = Unkillable()
    handle = ProcessHandle("proc_stubborn", proc, "stubborn", tmp_path, 1.0, ByteRingBuffer(4096), ByteRingBuffer(4096))
    manager._handles[handle.process_id] = handle
    monkeypatch.setattr(ProcessManager, "_kill_tree", lambda *_a, **_kw: None)
    with pytest.raises(ProcessError, match="still running"):
        manager.terminate(handle.process_id, grace_seconds=0.05)
    assert handle.state == "running"
    assert manager._live_count() == 1
    with pytest.raises(ProcessError, match="maximum concurrent"):
        manager.start(["pretend"], command="pretend", cwd=tmp_path, initial_wait_ms=0)


def test_terminal_run_cannot_be_reopened_by_new_step_or_terminal_rewrite(tmp_path: Path) -> None:
    store = DurableRunStore(tmp_path / "runs")
    run = store.create_run("s", metadata={"user_event_seq": 0})
    run_id = run["run_id"]
    store.finish_run(run_id, status="completed", session_event_seq=3)
    original = store.get(run_id)
    with pytest.raises(ValueError, match="terminal"):
        store.start_step(run_id, kind="turn")
    with pytest.raises(ValueError, match="terminal"):
        store.finish_run(run_id, status="failed", recovery_state="retryable")
    with pytest.raises(ValueError, match="terminal"):
        store.mark_recovery(run_id, "pending_restart")
    assert store.get(run_id) == original


def test_new_attempt_requires_running_step_and_terminal_run_is_immutable(tmp_path: Path) -> None:
    store = DurableRunStore(tmp_path / "runs")
    run = store.create_run("s")
    run_id = run["run_id"]
    with pytest.raises(ValueError, match="unknown|running"):
        store.start_attempt(run_id, "unknown", provider_id="p")
    step = store.start_step(run_id, kind="turn")
    assert step is not None
    store.finish_step(run_id, step["step_id"], status="completed")
    with pytest.raises(ValueError, match="running"):
        store.start_attempt(run_id, step["step_id"], provider_id="p")
    store.finish_run(run_id, status="cancelled")
    with pytest.raises(ValueError, match="terminal"):
        store.start_attempt(run_id, step["step_id"], provider_id="p")


def test_terminal_checkpoint_and_late_step_updates_are_rejected(tmp_path: Path) -> None:
    store = DurableRunStore(tmp_path / "runs")
    row = store.create_run("owner")
    rid = row["run_id"]
    step = store.start_step(rid, kind="turn")
    assert step is not None
    attempt = store.start_attempt(rid, step["step_id"], provider_id="p", model_id="m")
    assert attempt is not None
    store.finish_attempt(rid, step["step_id"], attempt["attempt_id"], status="completed")
    store.finish_step(rid, step["step_id"], status="completed")
    store.finish_run(rid, status="completed")
    original = store.get(rid)
    for operation in (
        lambda: store.checkpoint(rid, name="late", data={"side_effect_risk": False}),
        lambda: store.finish_attempt(rid, step["step_id"], attempt["attempt_id"], status="failed"),
        lambda: store.finish_step(rid, step["step_id"], status="failed"),
    ):
        with pytest.raises(ValueError, match="terminal"):
            operation()
    assert store.get(rid) == original


def test_running_step_cannot_have_two_active_attempts(tmp_path: Path) -> None:
    store = DurableRunStore(tmp_path / "runs")
    row = store.create_run("owner")
    step = store.start_step(row["run_id"], kind="turn")
    assert step is not None
    assert store.start_attempt(row["run_id"], step["step_id"])
    original = store.get(row["run_id"])
    with pytest.raises(ValueError, match="active attempt"):
        store.start_attempt(row["run_id"], step["step_id"])
    assert store.get(row["run_id"]) == original


def test_interrupted_attempt_and_step_cannot_be_rewritten_by_late_worker(tmp_path: Path) -> None:
    store = DurableRunStore(tmp_path / "runs")
    row = store.create_run("owner", metadata={"user_event_seq": 0})
    run_id = row["run_id"]
    step = store.start_step(run_id, kind="turn")
    assert step
    attempt = store.start_attempt(run_id, step["step_id"])
    assert attempt
    store.mark_recovery(run_id, "pending_restart")
    store.prepare_resume(run_id, session_id="owner")
    snapshot = store.get(run_id)
    assert snapshot["steps"][0]["status"] == "interrupted"
    assert snapshot["steps"][0]["attempts"][0]["status"] == "interrupted"
    with pytest.raises(ValueError, match="not running"):
        store.finish_attempt(run_id, step["step_id"], attempt["attempt_id"], status="completed")
    with pytest.raises(ValueError, match="not running"):
        store.finish_step(run_id, step["step_id"], status="completed")
    assert store.get(run_id) == snapshot

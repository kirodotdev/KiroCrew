"""S3-03 crash/fault injection for durable recovery admission.

These tests exercise the *persisted* ledger.  They do not claim that a
remote MCP call can be cancelled or executed exactly once across processes.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_byo.durable_runs import DurableRunStore
from kiro_byo.recovery_policy import RecoveryPolicy
from kiro_byo.turn_runner import TurnRunnerMixin


def _recoverable(store: DurableRunStore, session_id: str = "s") -> dict:
    run = store.create_run(session_id, metadata={"user_event_seq": 0})
    store.finish_run(run["run_id"], status="failed", recovery_state="retryable")
    return run


def test_interrupted_atomic_replace_keeps_old_record_and_cleans_temp(tmp_path: Path) -> None:
    store = DurableRunStore(tmp_path / "runs")
    run = store.create_run("owner")
    old = store.get(run["run_id"])
    assert old is not None
    with patch("kiro_byo.durable_runs.os.replace", side_effect=OSError("injected power loss")):
        with pytest.raises(OSError, match="injected power loss"):
            store.checkpoint(run["run_id"], name="side_effect_boundary", data={"side_effect_risk": True})
    assert store.get(run["run_id"]) == old
    assert not list(store.root.glob("*.tmp-*"))
    assert json.loads(store._path(run["run_id"]).read_text()) == old


def test_competing_resume_requests_admit_exactly_one_attempt(tmp_path: Path) -> None:
    store = DurableRunStore(tmp_path / "runs")
    run = _recoverable(store)
    run_id = run["run_id"]
    successes, failures = [], []
    start = threading.Barrier(3)

    def work() -> None:
        start.wait()
        try:
            successes.append(store.prepare_resume(run_id, session_id="s", strategy="resume"))
        except ValueError as exc:
            failures.append(str(exc))

    threads = [threading.Thread(target=work) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert len(successes) == 1
    assert len(failures) == 1
    row = store.get(run_id)
    assert row and row["resume_count"] == 1 and row["recovery_state"] == "resuming"
    with pytest.raises(ValueError, match="already active"):
        store.prepare_resume(run_id, session_id="s")
    # A subsequent process restart must reconcile the interrupted attempt.
    store.mark_recovery(run_id, store.restart_recovery_state(row))
    assert store.prepare_resume(run_id, session_id="s") is not None
    assert store.get(run_id)["resume_count"] == 2


def test_recovery_admission_cannot_override_completed_or_side_effect(tmp_path: Path) -> None:
    store = DurableRunStore(tmp_path / "runs")
    complete = store.create_run("s", metadata={"user_event_seq": 0})
    store.finish_run(complete["run_id"], status="completed")
    with pytest.raises(ValueError, match="completed"):
        store.prepare_resume(complete["run_id"], session_id="s")

    unsafe = _recoverable(store)
    store.checkpoint(unsafe["run_id"], name="tool_admitted", data={"side_effect_risk": True})
    # Even an optimistic recovery state cannot erase monotonic side-effect evidence.
    store.mark_recovery(unsafe["run_id"], "retryable")
    assert not RecoveryPolicy.durable_run(store.get(unsafe["run_id"])).safe
    with pytest.raises(ValueError, match="side effects"):
        store.prepare_resume(unsafe["run_id"], session_id="s")


def test_failed_resume_admission_does_not_create_new_steps(tmp_path: Path) -> None:
    store = DurableRunStore(tmp_path / "runs")
    run = _recoverable(store)
    first = store.prepare_resume(run["run_id"], session_id="s")
    assert first and first["status"] == "running"
    with pytest.raises(ValueError, match="already active"):
        store.prepare_resume(run["run_id"], session_id="s")
    saved = store.get(run["run_id"])
    assert saved and saved["steps"] == [] and saved["resume_count"] == 1


def test_run_start_does_not_claim_success_when_step_write_fails(tmp_path: Path) -> None:
    class FaultyStore(DurableRunStore):
        def start_step(self, *args, **kwargs):
            raise OSError("injected disk error")

    class Harness(TurnRunnerMixin):
        def __init__(self):
            self.store = FaultyStore(tmp_path / "runs")

        def _durable_run_store(self):
            return self.store

        def trace(self, _message):
            pass

    harness = Harness()
    session = SimpleNamespace(
        session_id="s", events=[], active_agent_plan_id="",
        active_run_id="", active_run_step_id="", active_run_attempt_id="",
    )
    result = harness._begin_durable_run(
        session, None, selection="m", provider_id="p", model_id="m",
        user_event_seq=0,
    )
    assert result == "" and not session.active_run_id
    # Persisted orphan is left visible for safe reconciliation, never erased.
    assert len(harness.store.pending()) == 1

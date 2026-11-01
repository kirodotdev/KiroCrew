"""S3-01: crash recovery must not lose evidence or allow unsafe tools."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_byo.durable_runs import DurableRunStore
from kiro_byo.turn_runner import DurableCheckpointError, TurnRunnerMixin


def test_risk_survives_checkpoint_rotation(tmp_path: Path) -> None:
    store = DurableRunStore(tmp_path / "runs")
    run = store.create_run("session", metadata={"user_event_seq": 1})
    run_id = run["run_id"]
    store.checkpoint(run_id, name="tool_batch_admitted", data={"side_effect_risk": True})
    for i in range(70):
        store.checkpoint(run_id, name=f"after_tool_{i}", data={"side_effect_risk": False})
    saved = store.get(run_id)
    assert saved is not None
    assert len(saved["checkpoints"]) == 64
    assert all(not row["data"].get("side_effect_risk") for row in saved["checkpoints"])
    assert saved["side_effect_seen"] is True
    assert store.restart_recovery_state(saved) == "unknown_side_effect"


def test_legacy_full_window_cannot_prove_absence_of_old_side_effects() -> None:
    old_record = {
        "schema_version": 1,
        "checkpoints": [{"data": {"side_effect_risk": False}} for _ in range(64)],
    }
    assert DurableRunStore.restart_recovery_state(old_record) == "unknown_side_effect"
    old_record["checkpoints"].pop()
    assert DurableRunStore.restart_recovery_state(old_record) == "pending_restart"
    # New records have a monotonic risk bit; a full all-read-only window is safe.
    old_record["side_effect_seen"] = False
    old_record["checkpoints"].append({"data": {"side_effect_risk": False}})
    assert DurableRunStore.restart_recovery_state(old_record) == "pending_restart"


def test_history_pruning_keeps_pending_failed_and_cancelled_runs(tmp_path: Path) -> None:
    store = DurableRunStore(tmp_path / "runs", max_runs=16)
    protected_ids: list[str] = []
    for status in ("running", "failed", "cancelled"):
        record = store.create_run(f"{status}-session")
        if status != "running":
            store.finish_run(record["run_id"], status=status, recovery_state="retryable")
        protected_ids.append(record["run_id"])
    for i in range(22):
        record = store.create_run(f"completed-{i}")
        store.finish_run(record["run_id"], status="completed")
    for run_id in protected_ids:
        assert store.get(run_id) is not None
    assert len(store.list(limit=100)) <= 17  # pruning occurs during create
    # With only protected runs, exceeding the nominal cap is safer than deletion.
    store.clear()
    assert all(store.get(run_id) is not None for run_id in protected_ids)
    for i in range(19):
        record = store.create_run(f"active-{i}")
        protected_ids.append(record["run_id"])
    assert len(store.pending()) == 20  # original running run remains protected


def test_critical_checkpoint_requires_run_and_existing_ledger(tmp_path: Path) -> None:
    class CheckpointHarness(TurnRunnerMixin):
        def __init__(self) -> None:
            self.store = DurableRunStore(tmp_path / "runs")

        def _durable_run_store(self) -> DurableRunStore:
            return self.store

    harness = CheckpointHarness()
    session = SimpleNamespace(active_run_id="", active_run_step_id="", active_run_attempt_id="", active_turn_id="session:1", events=[])
    with pytest.raises(DurableCheckpointError):
        harness._checkpoint_durable_run(session, "tool_batch_admitted", critical=True, data={"side_effect_risk": True})
    # A non-critical trace checkpoint does not block a non-tool response.
    assert harness._checkpoint_durable_run(session, "turn_ready", critical=False) is None
    session.active_run_id = "nonexistent"
    with pytest.raises(DurableCheckpointError):
        harness._checkpoint_durable_run(session, "tool_batch_admitted", critical=True)


def test_recovery_policy_blocks_side_effect_even_with_optimistic_state(tmp_path: Path) -> None:
    from kiro_byo.recovery_policy import RecoveryPolicy

    store = DurableRunStore(tmp_path / "runs")
    record = store.create_run("example", selection="kiro-byo-type:tiga:multi", metadata={"user_event_seq": 1})
    run_id = record["run_id"]
    store.checkpoint(run_id, name="tool_batch_admitted", data={"side_effect_risk": True})
    store.mark_recovery(run_id, "retryable")
    current = store.get(run_id)
    for strategy in ("retry", "resume", "replan"):
        decision = RecoveryPolicy.durable_run(current, strategy=strategy)
        assert not decision.safe
        assert "side-effect" in decision.reason

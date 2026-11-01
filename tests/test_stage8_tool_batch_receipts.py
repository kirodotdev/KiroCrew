"""Stage 8: durable tool admission is fenced before external side effects."""
from __future__ import annotations

import concurrent.futures
from pathlib import Path

import pytest

from kiro_byo.durable_runs import DurableRunStore


def prepared(tmp_path: Path):
    store = DurableRunStore(tmp_path / "runs")
    run = store.create_run("s", lease_owner="owner", metadata={"user_event_seq": 1})
    step = store.start_step(run["run_id"], kind="turn", owner_token="owner")
    attempt = store.start_attempt(run["run_id"], step["step_id"], owner_token="owner")
    return store, run["run_id"], step["step_id"], attempt["attempt_id"]


def admit(store, run, step, attempt, key="batch-a"):
    return store.checkpoint(
        run, step_id=step, attempt_id=attempt, name="tool_batch_admitted",
        data={"batch_id": key, "tool_count": 2, "side_effect_risk": True},
        owner_token="owner",
    )


def complete(store, run, step, attempt, key="batch-a"):
    return store.checkpoint(
        run, step_id=step, attempt_id=attempt, name="tool_batch_completed",
        data={"batch_id": key}, owner_token="owner",
    )


def test_duplicate_admission_rejected_without_revision_change(tmp_path):
    store, run, step, attempt = prepared(tmp_path)
    admit(store, run, step, attempt)
    before = store.get(run)
    with pytest.raises(ValueError, match="duplicate"):
        admit(store, run, step, attempt)
    assert store.get(run) == before
    assert store.get(run)["tool_batches"]["batch-a"]["status"] == "admitted"
    assert store.restart_recovery_state(store.get(run)) == "unknown_side_effect"


def test_completion_requires_matching_admission_and_is_single_use(tmp_path):
    store, run, step, attempt = prepared(tmp_path)
    with pytest.raises(ValueError, match="not admitted"):
        complete(store, run, step, attempt)
    admit(store, run, step, attempt)
    complete(store, run, step, attempt)
    assert store.get(run)["tool_batches"]["batch-a"]["status"] == "completed"
    before = store.get(run)
    with pytest.raises(ValueError, match="already completed"):
        complete(store, run, step, attempt)
    assert store.get(run) == before


def test_batched_admission_requires_active_step_attempt_and_lease(tmp_path):
    store, run, step, attempt = prepared(tmp_path)
    with pytest.raises(ValueError, match="attempt"):
        admit(store, run, step, "missing")
    with pytest.raises(ValueError, match="lease"):
        store.checkpoint(run, step_id=step, attempt_id=attempt, name="tool_batch_admitted",
                         data={"batch_id": "bad-lease"}, owner_token="wrong")
    assert store.get(run)["tool_batches"] == {}


def test_concurrent_duplicate_tool_admission_has_one_winner(tmp_path):
    store, run, step, attempt = prepared(tmp_path)
    def attempt_admit(_):
        try:
            admit(store, run, step, attempt)
            return "ok"
        except ValueError:
            return "rejected"
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt_admit, range(12)))
    assert results.count("ok") == 1
    assert results.count("rejected") == 11
    assert len(store.get(run)["tool_batches"]) == 1


def test_receipt_contains_no_tool_arguments_or_credentials(tmp_path):
    store, run, step, attempt = prepared(tmp_path)
    admit(store, run, step, attempt)
    receipt = store.get(run)["tool_batches"]["batch-a"]
    assert set(receipt) == {"step_id", "attempt_id", "status", "tool_count", "side_effect_risk", "admitted_at", "completed_at"}


def test_separate_processes_competing_for_one_tool_batch(tmp_path: Path) -> None:
    """The sidecar lock must fence actual OS processes, not just threads."""
    import os
    import subprocess
    import sys
    import time

    store, run, step, attempt = prepared(tmp_path)
    go = tmp_path / "go.flag"
    script = r'''
import sys, time
from pathlib import Path
from kiro_byo.durable_runs import DurableRunStore
root, run, step, attempt, marker = sys.argv[1:]
Path(marker + '.ready').write_text('ready', encoding='ascii')
for _ in range(200):
    if Path(root).parent.joinpath('go.flag').exists():
        break
    time.sleep(.01)
else:
    raise SystemExit(2)
try:
    DurableRunStore(Path(root)).checkpoint(
        run, step_id=step, attempt_id=attempt, name='tool_batch_admitted',
        data={'batch_id': 'shared-batch', 'tool_count': 1, 'side_effect_risk': True},
        owner_token='owner')
except ValueError:
    print('rejected')
else:
    print('accepted')
'''
    processes = []
    try:
        for i in range(3):
            marker = str(tmp_path / f"worker-{i}")
            proc = subprocess.Popen(
                [sys.executable, "-c", script, str(store.root), run, step, attempt, marker],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            )
            processes.append(proc)
        deadline = time.monotonic() + 12
        while sum((tmp_path / f"worker-{i}.ready").exists() for i in range(3)) < 3:
            if time.monotonic() > deadline:
                pytest.fail("tool admission worker failed to start")
            time.sleep(.02)
        go.write_text("start", encoding="ascii")
        outputs = []
        for proc in processes:
            stdout, stderr = proc.communicate(timeout=12)
            assert proc.returncode == 0, stderr
            outputs.append(stdout.strip())
        assert outputs.count("accepted") == 1
        assert outputs.count("rejected") == 2
        assert len(store.get(run)["tool_batches"]) == 1
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5)


def test_run_cannot_be_claimed_completed_with_unresolved_tool_receipt(tmp_path):
    store, run, step, attempt = prepared(tmp_path)
    admit(store, run, step, attempt)
    store.finish_attempt(run, step, attempt, status="completed", owner_token="owner")
    store.finish_step(run, step, status="completed", owner_token="owner")
    before = store.get(run)
    with pytest.raises(ValueError, match="unresolved tool batch"):
        store.finish_run(run, status="completed", owner_token="owner")
    assert store.get(run) == before
    # This is *not* a tool retry. Reconciliation of the completed batch's
    # receipt only is allowed while still owning the run.
    # Complete earlier, before sealing steps, in ordinary executions.


def test_completed_tool_receipt_allows_completed_run(tmp_path):
    store, run, step, attempt = prepared(tmp_path)
    admit(store, run, step, attempt)
    complete(store, run, step, attempt)
    store.finish_attempt(run, step, attempt, status="completed", owner_token="owner")
    store.finish_step(run, step, status="completed", owner_token="owner")
    store.finish_run(run, status="completed", owner_token="owner")
    assert store.get(run)["status"] == "completed"

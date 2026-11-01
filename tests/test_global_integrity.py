"""Independent cross-process fault regressions for the JSON state stores."""
from __future__ import annotations

import json
import multiprocessing as mp
import time
from pathlib import Path

import pytest

from kiro_byo.durable_runs import AgentStateStore, DurableRunStore
from kiro_byo.recovery_policy import RecoveryPolicy


def _mutate_worker(root: str, run_id: str, entered, slow: bool, result) -> None:
    store = DurableRunStore(Path(root))
    try:
        def append(record):
            if slow:
                entered.set()
                time.sleep(0.3)
            record.setdefault("checkpoints", []).append({
                "checkpoint_id": "slow" if slow else "fast",
                "data": {"side_effect_risk": slow},
            })
            if slow:
                record["side_effect_seen"] = True
        store._mutate(run_id, append)
        result.put("ok")
    except BaseException as exc:
        result.put(f"{type(exc).__name__}: {exc}")


def _agent_worker(path: str, task_id: str, result) -> None:
    try:
        store = AgentStateStore(Path(path))
        store.upsert({"task_id": task_id, "status": "running", "session_id": "s"})
        result.put("ok")
    except BaseException as exc:
        result.put(f"{type(exc).__name__}: {exc}")


def test_two_processes_preserve_all_checkpoint_updates_and_side_effect(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    store = DurableRunStore(root)
    run_id = store.create_run("session", metadata={"user_event_seq": 0})["run_id"]
    ctx = mp.get_context("spawn")
    entered = ctx.Event()
    output = ctx.Queue()
    first = ctx.Process(target=_mutate_worker, args=(str(root), run_id, entered, True, output))
    second = ctx.Process(target=_mutate_worker, args=(str(root), run_id, entered, False, output))
    try:
        first.start()
        assert entered.wait(5), "first process did not start its read-modify-write"
        second.start()
        for proc in (first, second):
            proc.join(8)
            assert proc.exitcode == 0
        assert [output.get(timeout=1) for _ in range(2)] == ["ok", "ok"]
        saved = store.get(run_id)
        assert saved is not None
        assert {cp["checkpoint_id"] for cp in saved["checkpoints"]} == {"fast", "slow"}
        assert saved["side_effect_seen"] is True
        assert saved["revision"] == 2
        assert not RecoveryPolicy.durable_run(saved).safe
    finally:
        for proc in (first, second):
            if proc.is_alive():
                proc.terminate()
                proc.join(2)


def test_agent_projection_processes_do_not_drop_tasks(tmp_path: Path) -> None:
    path = tmp_path / "agents.json"
    ctx = mp.get_context("spawn")
    output = ctx.Queue()
    children = [ctx.Process(target=_agent_worker, args=(str(path), f"task-{i}", output)) for i in range(8)]
    try:
        for child in children:
            child.start()
        for child in children:
            child.join(10)
            assert child.exitcode == 0
        assert all(output.get(timeout=1) == "ok" for _ in children)
        assert {task["task_id"] for task in AgentStateStore(path).list(limit=100)} == {
            f"task-{i}" for i in range(len(children))
        }
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(2)


def test_recovery_policy_and_transaction_agree_on_terminal_and_active_states(tmp_path: Path) -> None:
    store = DurableRunStore(tmp_path / "runs")
    for status, recovery_state in (
        ("completed", "clean"), ("cancelled", "clean"),
        ("running", "clean"), ("running", "resuming"),
    ):
        row = store.create_run("s", selection="kiro-byo-type:tiga:multi", metadata={"user_event_seq": 0})
        if status != "running":
            store.finish_run(row["run_id"], status=status, recovery_state=recovery_state)
        else:
            store.mark_recovery(row["run_id"], recovery_state)
        saved = store.get(row["run_id"])
        for strategy in ("resume", "retry", "replan"):
            assert RecoveryPolicy.durable_run(saved, strategy=strategy).safe is False
            with pytest.raises(ValueError):
                store.prepare_resume(row["run_id"], session_id="s", strategy=strategy)
        assert row["run_id"] not in {r["run_id"] for r in store.resumable(session_id="s")}


def test_agent_state_corruption_is_not_silently_erased(tmp_path: Path) -> None:
    path = tmp_path / "tasks.json"
    path.write_text('{"tasks":', encoding="utf-8")
    with pytest.raises(OSError, match="ledger"):
        AgentStateStore(path).upsert({"task_id": "new", "status": "running"})
    assert path.read_text(encoding="utf-8") == '{"tasks":'


def test_durable_checkpoint_write_is_fsynced_before_replace(tmp_path: Path, monkeypatch) -> None:
    from kiro_byo import state_io

    store = DurableRunStore(tmp_path / "runs")
    row = store.create_run("s")
    original = store.get(row["run_id"])
    def fail_fsync(_fd):
        raise OSError("injected fsync fault")
    monkeypatch.setattr(state_io.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="fsync"):
        store.checkpoint(row["run_id"], name="write", data={"side_effect_risk": True})
    assert store.get(row["run_id"]) == original


def test_workspace_manifest_corruption_is_preserved(tmp_path: Path) -> None:
    from kiro_byo.workspace_manager import WorkspaceError, WorkspaceManager
    manager = WorkspaceManager(tmp_path / "workspaces")
    manager.manifest_path.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="manifest"):
        manager._load()
    assert manager.manifest_path.read_text(encoding="utf-8") == "{corrupt"


def _trace_worker(path: str, task_id: str, result) -> None:
    try:
        from kiro_byo.runtime_trace import RuntimeTraceStore
        from kiro_byo.telemetry import TurnTelemetry, TurnTelemetryStore
        trace = RuntimeTraceStore(Path(path) / "trace.jsonl", max_records=1000)
        metric = TurnTelemetryStore(Path(path) / "turns.jsonl", max_records=1000)
        for index in range(10):
            trace.record("test.event", session_id=task_id, data={"index": index})
            metric.record(TurnTelemetry(
                timestamp=float(index), session_id=task_id, outcome="success",
                requested_model="m", provider_id="p", served_model="m", effort="",
                total_ms=1.0, first_visible_ms=0, upstream_ms=0, tool_ms=0,
                mcp_wait_ms=0, tool_calls=0, native_calls=0, tool_search_calls=0,
                active_tools=0, tool_schema_bytes=0, stream_chunks=0, stream_chars=0,
            ))
        result.put("ok")
    except BaseException as exc:
        result.put(f"{type(exc).__name__}: {exc}")


def test_cross_process_telemetry_and_runtime_events_keep_all_rows(tmp_path: Path) -> None:
    ctx = mp.get_context("spawn")
    result = ctx.Queue()
    children = [ctx.Process(target=_trace_worker, args=(str(tmp_path), f"p{i}", result)) for i in range(4)]
    try:
        for child in children:
            child.start()
        for child in children:
            child.join(10)
            assert child.exitcode == 0
        assert all(result.get(timeout=1) == "ok" for _ in children)
        assert len((tmp_path / "trace.jsonl").read_text().splitlines()) == 40
        assert len((tmp_path / "turns.jsonl").read_text().splitlines()) == 40
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(2)

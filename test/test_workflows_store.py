"""FIX-21 — durable workflow-run persistence across gateway restarts.

The RunRegistry mirrors runs to a JSON-per-run store; on restart a fresh registry
rehydrates them so list/result/source + the resume cache survive. Asserts the
round-trip, the running→failed demotion for interrupted runs, atomic/clean writes,
eviction deletes files, and corrupt files are skipped.
"""

from __future__ import annotations

import json
from pathlib import Path

from kiro_crew.workflows import WorkflowEvent
from kiro_crew.workflows.registry import (
    STATUS_FINISHED,
    STATUS_RUNNING,
    RunHandle,
    RunRegistry,
)
from kiro_crew.workflows.store import WorkflowRunStore


def _store(tmp_path) -> WorkflowRunStore:
    return WorkflowRunStore(base_dir=tmp_path)


def _seed(reg: RunRegistry, run_id="wf_000001", **kw) -> RunHandle:
    h = RunHandle(
        run_id=run_id, name=kw.get("name", "demo"),
        source=kw.get("source", "META={}\nasync def workflow(ctx):\n    return {}\n"),
        args=kw.get("args", {}),
    )
    reg.register(h)
    return h


def test_run_persists_and_reloads_across_restart(tmp_path) -> None:
    store = _store(tmp_path)
    reg_a = RunRegistry(store=store)
    h = _seed(reg_a, source="META={'name':'x'}\nasync def workflow(ctx):\n    return {}\n",
              args={"k": "v"})
    for i in range(6):
        reg_a.record_event(
            "wf_000001",
            WorkflowEvent(run_id="wf_000001", seq=i, ts="t", type="log", data={"message": f"s{i}"}),
        )
    h.agent_results = {0: "a", 1: "b"}
    reg_a.mark_terminal("wf_000001", STATUS_FINISHED, result={"ok": True})

    # New registry on the same store = a restart.
    reg_b = RunRegistry(store=store)
    assert reg_b.load_persisted() == 1
    snap = reg_b.status("wf_000001", include_events=True)
    assert snap["status"] == "finished"
    assert snap["result"] == {"ok": True}
    assert "async def workflow" in snap["source"]
    assert snap["event_count"] == 6
    h2 = reg_b.get("wf_000001")
    assert h2.args == {"k": "v"}
    # resume cache survives AND keys are coerced back to int
    assert h2.agent_results == {0: "a", 1: "b"}
    assert all(isinstance(k, int) for k in h2.agent_results)


def test_interrupted_running_run_demoted_to_failed(tmp_path) -> None:
    store = _store(tmp_path)
    reg_a = RunRegistry(store=store)
    _seed(reg_a, run_id="wf_000002")  # left RUNNING (never marked terminal)
    assert reg_a.get("wf_000002").status == STATUS_RUNNING

    reg_b = RunRegistry(store=store)
    reg_b.load_persisted()
    # A run mid-flight when the process died can't resume → demoted to failed.
    assert reg_b.get("wf_000002").status == "failed"


def test_eviction_deletes_the_file(tmp_path) -> None:
    store = _store(tmp_path)
    reg = RunRegistry(max_runs=2, store=store)
    for i in range(1, 4):  # 3 terminal runs, cap 2 → oldest evicted
        _seed(reg, run_id=f"wf_{i:06d}")
        reg.mark_terminal(f"wf_{i:06d}", STATUS_FINISHED, result={})
    files = {p.name for p in (tmp_path / "runs").glob("*.json")}
    assert "wf_000001.json" not in files  # evicted run's file removed
    assert "wf_000003.json" in files


def test_load_skips_corrupt_files(tmp_path) -> None:
    store = _store(tmp_path)
    reg = RunRegistry(store=store)
    _seed(reg, run_id="wf_000009")
    reg.mark_terminal("wf_000009", STATUS_FINISHED, result={})
    # drop a garbage file next to the good one
    (tmp_path / "runs" / "garbage.json").write_text("{ not json", encoding="utf-8")

    reg2 = RunRegistry(store=store)
    loaded = reg2.load_persisted()
    assert loaded == 1  # good run loaded, corrupt skipped
    assert reg2.get("wf_000009") is not None


def test_store_writes_no_temp_leftover_and_is_valid_json(tmp_path) -> None:
    store = _store(tmp_path)
    reg = RunRegistry(store=store)
    _seed(reg, run_id="wf_000010")
    reg.mark_terminal("wf_000010", STATUS_FINISHED, result={"x": 1})
    runs = tmp_path / "runs"
    assert not list(runs.glob("*.tmp"))  # atomic write left no temp file
    obj = json.loads((runs / "wf_000010.json").read_text(encoding="utf-8"))
    assert obj["run_id"] == "wf_000010" and obj["status"] == "finished"


def test_no_store_is_pure_in_memory(tmp_path) -> None:
    reg = RunRegistry()  # store=None
    _seed(reg, run_id="wf_000011")
    assert reg.load_persisted() == 0  # nothing to load, no crash
    # and no files were created anywhere we control
    assert not list(Path(tmp_path).glob("**/*.json"))

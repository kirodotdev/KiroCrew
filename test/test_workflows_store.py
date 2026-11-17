"""FIX-21 — durable workflow-run persistence across gateway restarts.

The RunRegistry mirrors runs to a JSON-per-run store; on restart a fresh registry
rehydrates them so list/result/source + the resume cache survive. Asserts the
round-trip, the running→failed demotion for interrupted runs, atomic/clean writes,
eviction deletes files, and corrupt files are skipped.
"""

from __future__ import annotations

import json
from pathlib import Path

from redaction_structured_cases import github_classic

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
    assert h2.source_is_original is True
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


def test_store_marks_redacted_source_as_non_original(tmp_path) -> None:
    store = _store(tmp_path)
    reg = RunRegistry(store=store)
    source = "META={}\n" "async def workflow(ctx):\n" f"    return '{github_classic(seed=7)}'\n"
    _seed(reg, run_id="wf_000012", source=source)
    reg.mark_terminal("wf_000012", STATUS_FINISHED, result={})

    obj = json.loads((tmp_path / "runs" / "wf_000012.json").read_text(encoding="utf-8"))
    assert obj["source_is_original"] is False


def test_legacy_store_record_has_unknown_source_provenance() -> None:
    restored = RunHandle.from_store_json(
        {
            "run_id": "wf_legacy",
            "name": "legacy",
            "status": STATUS_FINISHED,
            "source": "META={}\nasync def workflow(ctx):\n    return {}\n",
        }
    )

    assert restored.source_is_original is False


def test_no_store_is_pure_in_memory(tmp_path) -> None:
    reg = RunRegistry()  # store=None
    _seed(reg, run_id="wf_000011")
    assert reg.load_persisted() == 0  # nothing to load, no crash
    # and no files were created anywhere we control
    assert not list(Path(tmp_path).glob("**/*.json"))


def _events(rid: str, ts: str) -> WorkflowEvent:
    return WorkflowEvent(run_id=rid, seq=0, ts=ts, type="run_started", data={})


def _admit(reg: RunRegistry, rid: str, admitted_at) -> RunHandle:
    """Register a run that was admitted at ``admitted_at`` (None: an older record)."""
    handle = RunHandle(run_id=rid, name="demo", source="META={}\n")
    handle.admitted_at = admitted_at  # type: ignore[attr-defined]
    reg.register(handle)
    return handle


def _set_mtimes(tmp_path, **mtimes) -> None:
    import os

    for rid, mtime in mtimes.items():
        os.utime(tmp_path / "runs" / f"{rid}.json", (mtime, mtime))


def test_the_run_list_keeps_its_order_across_a_restart(tmp_path) -> None:
    """Runs reload in the order they were admitted, not the order their files were written."""
    reg_a = RunRegistry(store=_store(tmp_path))
    for rid, admitted in (("wf_000001", 1_000.0), ("wf_000002", 2_000.0)):
        _admit(reg_a, rid, admitted)
        reg_a.record_event(rid, _events(rid, "2026-01-01T00:00:00Z"))
        reg_a.mark_terminal(rid, STATUS_FINISHED, result=1)
    assert [s["run_id"] for s in reg_a.list()] == ["wf_000002", "wf_000001"]
    # The older run finished last, so its file is the newest on disk.
    _set_mtimes(tmp_path, wf_000002=1_000_000_000, wf_000001=1_000_000_100)

    reg_b = RunRegistry(store=_store(tmp_path))
    reg_b.load_persisted()
    assert [s["run_id"] for s in reg_b.list()] == ["wf_000002", "wf_000001"]


def test_a_run_with_no_event_keeps_its_place_across_two_restarts(tmp_path) -> None:
    """The restart that demotes an interrupted run rewrites its file, not its place."""
    reg_a = RunRegistry(store=_store(tmp_path))
    _admit(reg_a, "wf_000001", 1_000.0)  # still running, no event written
    _admit(reg_a, "wf_000002", 2_000.0)
    reg_a.mark_terminal("wf_000002", STATUS_FINISHED, result=1)
    _set_mtimes(tmp_path, wf_000001=1_000_000_000, wf_000002=1_000_000_100)

    first = RunRegistry(store=_store(tmp_path))
    first.load_persisted()  # demotes wf_000001 to failed and rewrites its file
    assert [s["run_id"] for s in first.list()] == ["wf_000002", "wf_000001"]
    second = RunRegistry(store=_store(tmp_path))
    second.load_persisted()
    assert [s["run_id"] for s in second.list()] == ["wf_000002", "wf_000001"]


def test_a_record_saved_before_admission_times_ranks_by_its_first_event(tmp_path) -> None:
    reg_a = RunRegistry(store=_store(tmp_path))
    for rid, started in (
        ("wf_000001", "2026-01-01T00:00:00Z"),
        ("wf_000002", "2026-01-01T01:00:00Z"),
    ):
        _admit(reg_a, rid, None)  # a record from an older version
        reg_a.record_event(rid, _events(rid, started))
        reg_a.mark_terminal(rid, STATUS_FINISHED, result=1)
    _set_mtimes(tmp_path, wf_000002=1_000_000_000, wf_000001=1_000_000_100)

    reg_b = RunRegistry(store=_store(tmp_path))
    reg_b.load_persisted()
    assert [s["run_id"] for s in reg_b.list()] == ["wf_000002", "wf_000001"]


def test_start_order_prefers_the_admission_time_and_ignores_unusable_values() -> None:
    from kiro_crew.workflows.store import _start_order

    events = [{"ts": "2026-01-01T00:00:00Z"}]
    jan_first = _start_order((500.0, {"events": events}))[0]
    assert _start_order((500.0, {"admitted_at": 42.0, "events": events}))[0] == 42.0
    for bad in (None, True, float("nan"), float("inf"), -1.0, 0, "42", 10**309):
        assert _start_order((500.0, {"admitted_at": bad, "events": events}))[0] == jan_first, bad
    assert _start_order((500.0, {"admitted_at": "x"}))[0] == 500.0


def test_start_order_reads_every_timestamp_form_on_one_scale(local_tz) -> None:
    from kiro_crew.workflows.store import _start_order

    local_tz("Pacific/Kiritimati")  # UTC+14: a zone-less stamp read as local time would differ

    def start(ts, mtime=500.0):
        return _start_order((mtime, {"events": [{"ts": ts}]}))[0]

    assert start("2026-01-01T00:00:00Z") == start("2026-01-01T00:00:00+00:00")
    assert start("2026-01-01T00:00:00") == start("2026-01-01T00:00:00Z")  # no zone is UTC
    assert start("2026-01-01T01:00:00+01:00") == start("2026-01-01T00:00:00Z")
    assert start("not a time") == 500.0  # unreadable: the write time stands in
    assert _start_order((500.0, {"events": []}))[0] == 500.0


def test_a_record_with_an_oversized_admission_time_does_not_stop_the_load(tmp_path) -> None:
    import json

    reg_a = RunRegistry(store=_store(tmp_path))
    _admit(reg_a, "wf_000001", 1_000.0)
    reg_a.mark_terminal("wf_000001", STATUS_FINISHED, result=1)
    _admit(reg_a, "wf_000002", 2_000.0)
    reg_a.mark_terminal("wf_000002", STATUS_FINISHED, result=1)
    path = tmp_path / "runs" / "wf_000001.json"
    path.write_text(path.read_text(encoding="utf-8").replace("1000.0", "1" + "0" * 309))

    reg_b = RunRegistry(store=_store(tmp_path))
    assert reg_b.load_persisted() == 2  # the bad value costs one record its rank, not the load
    assert json.loads(path.read_text(encoding="utf-8"))["admitted_at"] == 10**309
    assert sorted(s["run_id"] for s in reg_b.list()) == ["wf_000001", "wf_000002"]

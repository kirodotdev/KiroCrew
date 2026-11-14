"""Phase 2: DSH-inspired Crew lineage is an evidence view, never a task owner."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_byo.durable_runs import DurableRunStore
from kiro_byo.paths import use_app_home
from kiro_byo.work_trace import execution_signal, work_trace_snapshot


def linked_run(store, task, assignment, session, *, role="worker", lease=False):
    return store.create_run(
        session, selection="selected:fixed-model",
        metadata={
            "model_id": "model-1",
            "host_work_link": {"taskId": task, "assignmentId": assignment, "role": role},
            "secret_key": "NEVER_EXPOSE_THIS",
        },
        lease_owner="worker-token" if lease else "",
    )


def test_only_explicit_host_links_and_no_credentials_in_response(tmp_path):
    store = DurableRunStore(tmp_path / "runs")
    a = linked_run(store, "task-1", "assignment-A", "session-A", lease=True)
    b = linked_run(store, "task-1", "assignment-B", "session-B")
    store.create_run("no-link", metadata={"secret": "NEVER_EXPOSE_THIS"})
    store.create_run("bad-link", metadata={"host_work_link": {"taskId": "task-1", "grant": "root"}})
    value = work_trace_snapshot(store=store)
    assert value["summary"]["linkedTasksInWindow"] == 1
    assert value["summary"]["linkedRunsInWindow"] == 2
    assert len(value["tasks"]) == 1
    item = value["tasks"][0]
    assert (item["taskId"], item["runCount"], item["assignmentCount"], item["sessionCount"]) == ("task-1", 2, 2, 2)
    assert item["executionSignal"] == "needs_review"  # second run has no lease
    assert {r["runId"] for r in item["runs"]} == {a["run_id"], b["run_id"]}
    assert "NEVER_EXPOSE_THIS" not in json.dumps(value)
    assert "worker-token" not in json.dumps(value)
    assert "grant" not in json.dumps(value)
    assert value["summary"]["taskCompletionAuthoritative"] is False


def test_hard_evidence_signals_and_completed_side_effects():
    live = {"status": "running", "lease": {"expires_at": 9999999999999}, "side_effect_seen": False}
    stale = {"status": "running", "lease": {"expires_at": 1}, "side_effect_seen": False}
    risky = {"status": "failed", "side_effect_seen": True}
    safe_done = {"status": "completed", "side_effect_seen": True, "recovery_state": "clean"}
    failed = {"status": "failed", "side_effect_seen": False}
    assert execution_signal(live) == "working"
    assert execution_signal(stale) == "needs_review"
    assert execution_signal(risky) == "unknown_side_effect"
    assert execution_signal(safe_done) == "completed_run"
    assert execution_signal(failed) == "failed_run"
    assert execution_signal({"status": "completed", "recovery_state": "manual_verification_required"}) == "unknown_side_effect"
    assert execution_signal({"status": "cancelled", "side_effect_seen": False}) == "cancelled_run"


def test_filter_limit_and_no_mutations(tmp_path):
    store = DurableRunStore(tmp_path / "runs")
    one = linked_run(store, "task-1", "A", "s1")
    linked_run(store, "task-2", "B", "s2")
    original = store.get(one["run_id"])
    result = work_trace_snapshot(limit=1, store=store)
    assert len(result["tasks"]) == 1
    assert result["summary"]["hasMore"] is True
    result = work_trace_snapshot(task_id="task-1", store=store)
    assert [x["taskId"] for x in result["tasks"]] == ["task-1"]
    assert store.get(one["run_id"]) == original
    with pytest.raises(ValueError):
        work_trace_snapshot(task_id="../secrets", store=store)
    with pytest.raises(ValueError):
        work_trace_snapshot(task_id="a" * 129, store=store)


def test_app_route_reads_same_canonical_ledger_and_respects_app_home(tmp_path, monkeypatch):
    from test_records_health_regression import _app_routes, _Request
    for routes in _app_routes():
        monkeypatch.setattr(routes, "_ensure_app_state", lambda ctx: tmp_path)
        assert any(route[0:2] == ("GET", "/work-traces") for route, _ in routes.register_routes(object()))
        with routes.use_app_home(tmp_path):
            run = linked_run(routes.DurableRunStore(), "crew-1", "a1", "s1")
            before = routes.DurableRunStore().get(run["run_id"])
        result = asyncio.run(routes.get_work_traces(_Request(limit="10", taskId="crew-1"), object()))
        assert result.status == 200
        payload = json.loads(result.text)
        assert payload["tasks"][0]["taskId"] == "crew-1"
        with routes.use_app_home(tmp_path):
            assert routes.DurableRunStore().get(run["run_id"]) == before


def test_acp_handler_success_and_validation_without_crew_or_models(tmp_path):
    from kiro_byo.acp_server import AcpServer
    from io import StringIO
    output = StringIO()
    server = AcpServer(stdin=StringIO(), stdout=output)
    server._kiro_durable_run_store = DurableRunStore(tmp_path / "runs")
    linked_run(server._kiro_durable_run_store, "crew-A", "worker-a", "session-1")
    try:
        server.handle({"jsonrpc": "2.0", "id": 99, "method": "_kiro/work/list", "params": {"limit": 20}})
        row = next(json.loads(x) for x in output.getvalue().splitlines() if json.loads(x).get("id") == 99)
        assert row["result"]["tasks"][0]["taskId"] == "crew-A"
        server.handle({"jsonrpc": "2.0", "id": 100, "method": "_kiro/work/list", "params": {"taskId": "../../bad"}})
        row = next(json.loads(x) for x in output.getvalue().splitlines() if json.loads(x).get("id") == 100)
        assert row["error"]["code"] == -32602
    finally:
        server._executor.shutdown(wait=True)


def test_ui_and_all_five_locales_show_non_authoritative_contract():
    ui = (Path(__file__).resolve().parents[1] / "ui" / "index.mjs").read_text(encoding="utf-8")
    assert "'/work-traces?limit=50'" in ui
    assert "{id:'work',label:tr('work.tab')}" in ui
    assert "tab==='work'?workview:all" in ui
    assert "work?.summary?.windowSaturated" in ui
    root = Path(__file__).resolve().parents[1] / "src" / "kiro_byo" / "locales"
    keys = None
    for lang in ("zh-CN", "zh-TW", "en", "ja", "ko"):
        messages = json.loads((root / (lang + ".json")).read_text(encoding="utf-8"))
        own_keys = {k for k in messages if k.startswith("work.")}
        assert all(messages[k] for k in own_keys)
        if keys is None:
            keys = own_keys
        else:
            assert own_keys == keys
    assert keys and len(keys) >= 10


def test_completed_byo_run_never_certifies_host_ticket_completion(tmp_path):
    """DSH-style executor/Ticket separation remains explicit and read-only."""
    store = DurableRunStore(tmp_path / "runs")
    run = linked_run(store, "ticket-one", "assignment-one", "session-one")
    store.finish_run(run["run_id"], status="completed")
    record_before = store.get(run["run_id"])

    snapshot = work_trace_snapshot(store=store)
    assert snapshot["tasks"][0]["executionSignal"] == "completed_run"
    assert snapshot["tasks"][0]["requiresReview"] is False
    assert snapshot["attention"]["items"] == []
    assert snapshot["attention"]["taskCompletionAuthoritative"] is False
    assert snapshot["summary"]["taskCompletionAuthoritative"] is False
    assert "taskStatus" not in snapshot["tasks"][0]
    assert store.get(run["run_id"]) == record_before

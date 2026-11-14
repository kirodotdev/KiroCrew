"""S3-02: correlate durable Run lifecycle without making trace into truth."""
from __future__ import annotations

import json
from pathlib import Path
from io import StringIO

from kiro_byo.acp_server import AcpServer
from kiro_byo.durable_runs import DurableRunStore
from kiro_byo.run_diagnostics import diagnose_run
from kiro_byo.runtime_trace import RuntimeTraceStore


def test_filtered_trace_searches_entire_retained_window(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KIRO_BYO_RUNTIME_TRACE", "metadata")
    store = RuntimeTraceStore(tmp_path / "events.jsonl", max_records=500)
    store.record("run.started", session_id="owner", run_id="target")
    for index in range(200):
        store.record("provider.update", session_id="other", run_id="unrelated", data={"index": index})
    store.record("run.completed", session_id="owner", run_id="target")
    assert [row["event"] for row in store.recent(2, run_id="target")] == ["run.started", "run.completed"]


def test_diagnostics_correlates_run_and_attempt_but_never_includes_content(tmp_path: Path) -> None:
    store = DurableRunStore(tmp_path / "runs")
    row = store.create_run("s1", selection="kiro-byo-type:tiga:sky", metadata={"user_event_seq": 3, "prompt": "private message"})
    run_id = row["run_id"]
    step = store.start_step(run_id, kind="turn")
    assert step is not None
    attempt = store.start_attempt(run_id, step["step_id"], provider_id="private-provider", model_id="model-1")
    assert attempt is not None
    store.checkpoint(run_id, step_id=step["step_id"], attempt_id=attempt["attempt_id"], name="tool_admitted", session_event_seq=7, data={"side_effect_risk": True, "tool_args": "sensitive"})
    store.finish_attempt(run_id, step["step_id"], attempt["attempt_id"], status="failed", error_kind="TransportError", error_category="transport")
    store.finish_step(run_id, step["step_id"], status="failed")
    store.finish_run(run_id, status="failed", recovery_state="retryable", session_event_seq=9)
    row = store.get(run_id)
    assert row is not None
    diagnostic = diagnose_run(row, trace_events=[
        {"run_id": "other", "event": "wrong", "data": {}},
        {"run_id": run_id, "event": "run.started", "turn_id": "t1", "step_id": step["step_id"], "attempt_id": attempt["attempt_id"], "timestamp": 10.0,
         "data": {"session_event_seq": 7, "prompt": "sensitive tracing content"}, "content": {"prompt": "private"}},
    ], trace_mode="full")
    assert diagnostic["identity"]["step_id"] == step["step_id"]
    assert diagnostic["identity"]["attempt_id"] == attempt["attempt_id"]
    assert diagnostic["anchors"] == {"user_message_seq": 3, "latest_session_event_seq": 9}
    assert diagnostic["side_effect_may_have_occurred"] is True
    assert diagnostic["recovery"]["resume"]["allowed"] is False
    assert diagnostic["recovery"]["retry"]["allowed"] is False
    assert diagnostic["trace"]["events"][0]["session_event_seq"] == 7
    assert "private" not in json.dumps(diagnostic)
    assert "sensitive" not in json.dumps(diagnostic)
    assert "wrong" not in json.dumps(diagnostic)


def test_absent_runtime_trace_not_used_as_recovery_evidence(tmp_path: Path) -> None:
    store = DurableRunStore(tmp_path / "runs")
    row = store.create_run("s", metadata={"user_event_seq": 1})
    result = diagnose_run(row, trace_mode="off")
    assert result["trace"]["events"] == []
    assert result["trace"]["absence_proves_no_execution"] is False
    assert result["recovery"]["automatic_retry_scheduled"] is False
    assert result["recovery"]["execution_owner"] == "client_explicit_request"


def test_run_get_supplies_content_free_timeline_and_correlated_recovery_event(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KIRO_BYO_HOME", str(tmp_path))
    monkeypatch.setenv("KIRO_BYO_RUNTIME_TRACE", "metadata")
    out = StringIO()
    server = AcpServer(stdin=StringIO(), stdout=out)
    run_store = server._durable_run_store()
    row = run_store.create_run("host-session", metadata={"user_event_seq": 2}, selection="kiro-byo-type:tiga:multi")
    run_id = row["run_id"]
    server.runtime_trace.record("run.recovery.requested", session_id="host-session", run_id=run_id, data={"strategy": "retry"})
    server._handle_run_get_request("get-1", {"runId": run_id})
    messages = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    diag = messages[-1]["result"]["run"]["diagnostics"]
    assert diag["trace"]["events"][-1]["event"] == "run.recovery.requested"
    assert diag["trace"]["events"][-1]["session_event_seq"] == -1
    server._executor.shutdown(wait=True)


def test_recovery_rejection_is_correlated_and_never_dispatches_prompt(tmp_path: Path, monkeypatch) -> None:
    from kiro_byo.acp_server import Session
    monkeypatch.setenv("KIRO_BYO_HOME", str(tmp_path))
    out = StringIO()
    server = AcpServer(stdin=StringIO(), stdout=out)
    session = Session("active", str(tmp_path), "m1")
    server._put_session(session)
    record = server._durable_run_store().create_run("active", metadata={"user_event_seq": 2})
    server._durable_run_store().checkpoint(record["run_id"], name="side_effect", data={"side_effect_risk": True})
    server._handle_run_resume_request(42, {"sessionId": "active", "runId": record["run_id"]})
    payload = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()][-1]
    assert payload["id"] == 42 and "error" in payload
    events = server.runtime_trace.recent(10, run_id=record["run_id"])
    assert any(e["event"] == "run.recovery.rejected" and e["data"]["reason_code"] == "recovery_policy_denied" for e in events)
    assert not any(e["event"] == "run.resumed" for e in events)
    server._executor.shutdown(wait=True)

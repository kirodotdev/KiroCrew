"""R30: Crew owns human decisions; BYO exposes two bounded evidence categories."""
from pathlib import Path

from kiro_byo.durable_runs import DurableRunStore
from kiro_byo.work_trace import work_trace_snapshot


def test_two_groups_maintain_backward_compatible_bounded_attention(tmp_path):
    store = DurableRunStore(tmp_path / "runs")
    for n in range(9):
        task = f"task-{n}"
        record = store.create_run(task, metadata={"host_work_link": {"taskId": task, "role": "worker"}})
        if n < 3:
            store.checkpoint(record["run_id"], name="tool_started", data={"side_effect_risk": True})
        store.finish_run(record["run_id"], status="failed")
    snapshot = work_trace_snapshot(store=store)
    attention = snapshot["attention"]
    safety = attention["safetyVerificationItems"]
    diagnostics = attention["executionDiagnosticItems"]
    assert len(attention["items"]) <= 5
    assert safety + diagnostics == attention["items"]  # safety priority, then diagnostics
    assert all(x["executionSignal"] == "unknown_side_effect" for x in safety)
    assert all(x["executionSignal"] != "unknown_side_effect" for x in diagnostics)
    assert attention["triage"]["manualVerificationInWindow"] == 3
    assert attention["triage"]["executionDiagnosticsInWindow"] == 6
    assert attention["totalInWindow"] == 9
    assert attention["taskCompletionAuthoritative"] is False
    assert attention["triage"]["hostHumanAttentionKnown"] is False


def test_frontend_renders_separate_evidence_categories():
    ui = (Path(__file__).resolve().parents[1] / "ui" / "index.mjs").read_text()
    assert "attention?.safetyVerificationItems" in ui
    assert "attention?.executionDiagnosticItems" in ui
    assert "safetyItems.map" in ui
    assert "diagnosticItems.map" in ui

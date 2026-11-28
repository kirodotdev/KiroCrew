"""R27: DSH-style attention semantics without becoming Crew's task owner."""
from __future__ import annotations

import json
from pathlib import Path

from kiro_byo.durable_runs import DurableRunStore
from kiro_byo.work_trace import work_trace_snapshot


def _linked(store, task_id):
    return store.create_run("crew-session-" + task_id, metadata={
        "host_work_link": {"taskId": task_id, "role": "worker"}
    })


def test_safety_verification_and_diagnostics_have_separate_counts(tmp_path):
    store = DurableRunStore(tmp_path / "runs")
    unsafe = _linked(store, "side-effect")
    store.checkpoint(unsafe["run_id"], name="tool_started", data={"side_effect_risk": True})
    store.finish_run(unsafe["run_id"], status="failed")
    ordinary = _linked(store, "ordinary-failure")
    store.finish_run(ordinary["run_id"], status="failed")
    old = _linked(store, "recovered")
    store.finish_run(old["run_id"], status="failed")
    new = _linked(store, "recovered")
    store.finish_run(new["run_id"], status="completed")

    before = store.get(unsafe["run_id"])
    result = work_trace_snapshot(store=store)
    triage = result["attention"]["triage"]
    assert (triage["manualVerificationInWindow"], triage["executionDiagnosticsInWindow"]) == (1, 1)
    assert triage["hostHumanAttentionKnown"] is False
    assert triage["owner"] == "kirocrew"
    assert triage["windowComplete"] is True
    assert result["attention"]["totalInWindow"] == 2
    assert result["attention"]["items"][0]["executionSignal"] == "unknown_side_effect"
    assert store.get(unsafe["run_id"]) == before  # read-only


def test_full_scan_window_never_advertises_all_clear():
    class FullPage:
        def list(self, *, limit):
            assert limit == 500
            return [{"run_id": str(i), "status": "completed", "metadata": {}} for i in range(500)]

    result = work_trace_snapshot(store=FullPage())
    assert result["attention"]["totalInWindow"] == 0
    assert result["attention"]["triage"]["windowComplete"] is False
    assert result["summary"]["windowSaturated"] is True
    assert result["summary"]["taskCompletionAuthoritative"] is False


def test_frontend_has_bounded_attentions_and_five_locale_parity():
    root = Path(__file__).resolve().parents[1]
    ui = (root / "ui/index.mjs").read_text(encoding="utf-8")
    assert "attentionTriage.manualVerificationInWindow" in ui
    assert "attentionTriage.executionDiagnosticsInWindow" in ui
    assert "work.attention.empty_limited" in ui
    keys = None
    for language in ("zh-CN", "zh-TW", "en", "ja", "ko"):
        messages = json.loads((root / f"src/kiro_byo/locales/{language}.json").read_text(encoding="utf-8"))
        own = {key for key in messages if key.startswith("work.")}
        assert all(messages[key] for key in own)
        assert {"work.attention.manual", "work.attention.diagnostic", "work.attention.crew_owner", "work.attention.empty_limited"} <= own
        if keys is not None:
            assert own == keys
        keys = own

"""Tiga learning must not confuse request outcomes with model reliability."""
from __future__ import annotations

import time

import pytest

from kiro_byo.adaptive_routing import (
    current_snapshot,
    reliability_adjustment,
    runtime_failure_rows,
    snapshot_from_rows,
)
from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.mode_router import route_model
from kiro_byo.runtime_trace import RuntimeTraceStore
from kiro_byo.telemetry import TurnTelemetry, TurnTelemetryStore


def _row(outcome: str, **changes):
    result = {
        "timestamp": time.time(), "provider_id": "p", "served_model": "fast",
        "outcome": outcome, "task_profile": "casual", "first_visible_ms": 250,
        "total_ms": 1200, "error_category": "", "error_kind": "",
    }
    result.update(changes)
    return result


def test_cancel_and_interrupt_preserve_reliability_and_confidence() -> None:
    baseline = [_row("success") for _ in range(8)]
    with_controls = baseline + [
        _row("cancelled", error_category="control") for _ in range(9)
    ] + [_row("interrupted", error_category="control") for _ in range(9)]
    before = snapshot_from_rows(baseline, task_profile="casual").for_model("p", "fast")
    after_snap = snapshot_from_rows(with_controls, task_profile="casual")
    after = after_snap.for_model("p", "fast")
    assert after_snap.turns == 26  # diagnostic source count still available
    assert after.samples == before.samples == 8
    assert after.task_samples == before.task_samples == 8
    assert after.success_rate == before.success_rate == 1.0
    assert after.reliability == pytest.approx(before.reliability, abs=1e-9)
    assert after.confidence == before.confidence
    assert after.median_total_ms == before.median_total_ms
    assert reliability_adjustment(after, strength=0.5) == pytest.approx(
        reliability_adjustment(before, strength=0.5), abs=1e-9
    )


@pytest.mark.parametrize("category", ["internal", "durable_checkpoint", "control", "tool", "permission"])
def test_non_model_failures_do_not_poison_evidence(category: str) -> None:
    rows = [_row("success")] + [_row("error", error_category=category) for _ in range(20)]
    model = snapshot_from_rows(rows, task_profile="casual").for_model("p", "fast")
    assert model.samples == 1
    assert model.success_rate == 1.0
    assert model.task_success_rate == 1.0


@pytest.mark.parametrize("category", ["provider", "transport", ""])
def test_attributable_provider_errors_remain_real_failures(category: str) -> None:
    model = snapshot_from_rows(
        [_row("success"), _row("error", error_category=category)], task_profile="casual"
    ).for_model("p", "fast")
    assert model.samples == 2
    assert model.success_rate == pytest.approx(0.5)
    assert model.reliability < 0.94


def test_unknown_outcomes_and_control_only_history_never_create_fake_confidence() -> None:
    rows = [_row("cancelled"), _row("interrupted"), _row("pending"), _row("unknown")]
    snap = snapshot_from_rows(rows, task_profile="casual")
    assert snap.observed_models == 0
    model = snap.for_model("p", "fast")
    assert model.samples == 0
    assert model.confidence == "none"
    assert model.reliability == 0.94


def test_invalid_old_row_latency_and_timestamp_are_ignored_safely() -> None:
    rows = [_row("success", timestamp="not-a-time", total_ms="infinity", first_visible_ms="n/a")]
    model = snapshot_from_rows(rows, task_profile="casual").for_model("p", "fast")
    assert model.samples == 1
    assert model.median_total_ms is None
    assert model.median_ttft_ms is None
    assert model.latency_index is None


def test_trace_control_failures_do_not_count_as_model_failures(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KIRO_BYO_HOME", str(tmp_path))
    trace = RuntimeTraceStore()
    # Successful request after a failed Worker does not hide an attributable
    # provider error; control-driven Worker/Reviewer interruptions are excluded.
    trace.record("model.relay.worker_failed", data={
        "provider_id": "p", "model": "fast", "error_category": "control", "error": "ProviderInterrupted",
    })
    trace.record("model.execution.tiga.glitter.review.failed", data={
        "review_provider": "p", "review_model": "fast", "error": "ProviderInterrupted",
    })  # old reviewer trace had no category
    trace.record("model.execution.tiga.glitter.review.failed", data={
        "review_provider": "p", "review_model": "fast", "error_category": "provider", "error": "ProviderError",
    })
    trace.record("model.route.fallback", data={
        "from_provider": "p", "from_model": "fast", "error_category": "transport", "error": "ProviderTransportError",
    })
    rows = runtime_failure_rows(trace)
    assert len(rows) == 2
    assert {r["adaptive_source"] for r in rows} == {
        "model.execution.tiga.glitter.review.failed", "model.route.fallback",
    }
    assert all(r["outcome"] == "error" for r in rows)
    snapshot = current_snapshot(task_profile="casual")
    assert snapshot.for_model("p", "fast").samples == 2


def test_control_outcomes_do_not_change_tiga_four_type_selection(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KIRO_BYO_HOME", str(tmp_path))
    models = (
        ModelSpec("p", "P", "fast", "Fast", tier="fast", tool_use=True,
                  routing_quality=.62, routing_speed=.85, routing_economy=.85),
        ModelSpec("p", "P", "strong", "Strong", tier="strong", tool_use=True,
                  routing_quality=.91, routing_speed=.58, routing_economy=.38),
    )
    cfg = KiroByoConfig((ProviderSpec("p", "P", "openai-compatible", "x", "", models),))

    def choices() -> dict[str, str]:
        result = {}
        for policy in ("sky", "multi", "power", "glitter"):
            decision = route_model(
                cfg, pack="tiga", policy=policy,
                prompt="你好", history=[], has_image=False,
                tools_available=True, tools_required=False,
            )
            assert decision is not None
            result[policy] = decision.model.model_id
        return result

    before = choices()
    store = TurnTelemetryStore()
    for i in range(20):
        store.record(TurnTelemetry(
            timestamp=time.time(), session_id=f"control-{i}", outcome="cancelled",
            requested_model="kiro-byo-type:tiga:multi", provider_id="p", served_model="fast",
            effort="", total_ms=60_000, first_visible_ms=-1, upstream_ms=0, tool_ms=0,
            mcp_wait_ms=0, tool_calls=0, native_calls=0, tool_search_calls=0,
            active_tools=0, tool_schema_bytes=0, stream_chunks=0, stream_chars=0,
            mode_pack="tiga", mode_type="multi", task_profile="casual", error_category="control",
        ))
    assert choices() == before
    assert before["sky"] == "fast"
    assert before["power"] == before["glitter"] == "strong"


def test_adaptive_snapshot_expires_without_new_ledger_writes(tmp_path, monkeypatch) -> None:
    """Old observations must age even when the runtime is otherwise idle."""
    import kiro_byo.adaptive_routing as adaptive

    monkeypatch.setenv("KIRO_BYO_HOME", str(tmp_path))
    store = TurnTelemetryStore()
    store.record(TurnTelemetry(
        timestamp=time.time() - 60, session_id="s", outcome="success",
        requested_model="kiro-byo-type:tiga:multi", provider_id="p", served_model="fast",
        effort="", total_ms=1100, first_visible_ms=200, upstream_ms=1100, tool_ms=0,
        mcp_wait_ms=0, tool_calls=0, native_calls=0, tool_search_calls=0,
        active_tools=0, tool_schema_bytes=0, stream_chunks=3, stream_chars=20,
        mode_pack="tiga", mode_type="multi", task_profile="casual",
    ))
    original_now = adaptive.time.time()
    monkeypatch.setattr(adaptive.time, "time", lambda: original_now)
    first = adaptive.current_snapshot(task_profile="casual")
    again = adaptive.current_snapshot(task_profile="casual")
    assert again is first
    monkeypatch.setattr(adaptive.time, "time", lambda: original_now + 65)
    refreshed = adaptive.current_snapshot(task_profile="casual")
    assert refreshed is not first
    assert refreshed.generated_at > first.generated_at
    assert refreshed.for_model("p", "fast").reliability < first.for_model("p", "fast").reliability


def test_offline_tiga_policy_preflight_is_credential_free() -> None:
    import json
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    output = subprocess.check_output([sys.executable, str(root / "scripts/tiga_policy_preflight.py")], cwd=root, text=True)
    data = json.loads(output)
    assert data["status"] == "pass"
    assert data["real_provider_invocations"] == 0
    assert len(data["checks"]) == 16
    assert {(row["task"], row["type"]) for row in data["checks"]} == {
        (task, typ)
        for task in ("greeting", "code", "cross_module", "vision")
        for typ in ("sky", "multi", "power", "glitter")
    }

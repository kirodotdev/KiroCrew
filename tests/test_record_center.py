"""Unified read-only Records center: scope, linkage, safety, i18n."""
import json
import time
from pathlib import Path

import pytest

from kiro_byo.durable_runs import DurableRunStore
from kiro_byo.paths import use_app_home
from kiro_byo.record_center import records_snapshot
from kiro_byo.runtime_trace import RuntimeTraceStore
from kiro_byo.telemetry import TurnTelemetry, TurnTelemetryStore


def _turn(**extra):
    fields = dict(timestamp=time.time(), session_id="s1", outcome="success",
                  requested_model="kiro-byo-type:dyna:flash", provider_id="p",
                  served_model="model", effort="", total_ms=3200,
                  first_visible_ms=800, upstream_ms=1100, tool_ms=0, mcp_wait_ms=0,
                  tool_calls=0, native_calls=0, tool_search_calls=0,
                  active_tools=0, tool_schema_bytes=0, stream_chunks=1, stream_chars=8,
                  mode_pack="dyna", mode_type="flash", turn_id="turn1", run_id="run1")
    fields.update(extra)
    return TurnTelemetry(**fields)


def test_unified_counts_linkage_and_cleanup_isolation(tmp_path):
    with use_app_home(tmp_path):
        TurnTelemetryStore().record(_turn())
        RuntimeTraceStore().record(
            "model.route.selected", session_id="s1", turn_id="turn1", run_id="run1",
            data={"pack":"dyna", "policy":"flash", "resolved_model":"model",
                  "secret":"must-not-be-returned"},
            content={"prompt":"must-not-be-returned"})
        RuntimeTraceStore().record("tool.call.started", session_id="s1", turn_id="turn1",
                                   data={"api_key":"topsecret"})
        runs = DurableRunStore()
        run = runs.create_run("s1", mode="dyna", selection="flash")
        result = records_snapshot(limit=100)
        counts = result["summary"]
        assert counts["scope"] == "retained"
        assert counts["turns"] == 1 and counts["routing"] == 1 and counts["runtime"] == 1
        assert counts["protected_runs"] == 1
        assert {e["kind"] for e in result["events"]} == {"turn", "routing", "runtime"}
        text = json.dumps(result)
        assert "must-not-be-returned" not in text
        assert "topsecret" not in text
        assert all("content" not in e and "data" not in e for e in result["events"])
        assert records_snapshot(kind="routing")["events"][0]["turn_id"] == "turn1"
        assert len(records_snapshot(mode="dyna")["events"]) == 2  # tool event has no Mode
        assert TurnTelemetryStore().clear() == 1
        after = records_snapshot()
        assert after["summary"]["turns"] == 0
        assert after["summary"]["routing"] == 1
        assert after["summary"]["protected_runs"] == 1
        assert runs.get(run["run_id"])["status"] == "running"


def test_snapshot_uses_entire_bounded_trace_not_just_last_500(tmp_path):
    with use_app_home(tmp_path):
        store = RuntimeTraceStore(max_records=900)
        for i in range(550):
            store.record("model.route.selected" if i == 0 else "turn.phase", data={"pack":"dyna"})
        snap = records_snapshot(limit=10, kind="routing")
        assert snap["summary"]["routing"] == 1
        assert len(snap["events"]) == 1
        assert snap["events"][0]["kind"] == "routing"


def test_invalid_filters_and_limit_are_rejected(tmp_path):
    with use_app_home(tmp_path):
        with pytest.raises(ValueError, match="invalid record kind"):
            records_snapshot(kind="unknown")
        with pytest.raises(ValueError, match="mode filter is too long"):
            records_snapshot(mode="d" * 65)
        assert len(records_snapshot(limit=0)["events"]) == 0


def test_all_five_locales_have_compatible_record_center_keys():
    root = Path(__file__).resolve().parents[1] / "src" / "kiro_byo" / "locales"
    langs = ["zh-CN", "zh-TW", "en", "ja", "ko"]
    resources = [json.loads((root / (name + ".json")).read_text(encoding="utf-8")) for name in langs]
    keys = {k for k in resources[0] if k.startswith("records.center.")}
    assert len(keys) >= 17
    for locale in resources:
        assert {k for k in locale if k.startswith("records.center.")} == keys
        assert all(locale[k].strip() for k in keys)


def test_app_records_endpoint_is_read_only_and_isolated(tmp_path, monkeypatch):
    import asyncio
    from test_records_health_regression import _app_routes, _Request

    for routes in _app_routes():
        monkeypatch.setattr(routes, "_ensure_app_state", lambda ctx: tmp_path)
        assert any(route[0:2] == ("GET", "/records") for route, _ in routes.register_routes(object()))
        with routes.use_app_home(tmp_path):
            routes.TurnTelemetryStore().record(_turn())
            before = routes.TurnTelemetryStore().path.read_bytes()
        response = asyncio.run(routes.get_record_center(_Request(limit="10"), object()))
        payload = json.loads(response.text)
        assert response.status == 200
        assert payload["summary"]["turns"] == 1
        assert payload["events"][0]["kind"] == "turn"
        assert TurnTelemetryStore(path=tmp_path / "data" / "telemetry" / "turns.jsonl").path.read_bytes() == before

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kiro_byo.providers.openai_compatible import _usage_info
from kiro_byo.providers.anthropic_messages import _usage as _anthropic_usage
from kiro_byo.telemetry import TurnTelemetry, TurnTelemetryStore


class TelemetryTests(unittest.TestCase):
    def test_usage_parser_accepts_openai_and_responses_style_counters(self) -> None:
        usage = _usage_info({
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "prompt_tokens_details": {"cached_tokens": 80},
            "completion_tokens_details": {"reasoning_tokens": 9},
        })
        self.assertEqual((usage.input_tokens, usage.output_tokens), (120, 30))
        self.assertEqual((usage.cached_tokens, usage.reasoning_tokens), (80, 9))
        self.assertTrue(usage.cached_tokens_reported)
        self.assertTrue(usage.reasoning_tokens_reported)

        alt = _usage_info({
            "input_tokens": 10,
            "output_tokens": 4,
            "input_tokens_details": {"cached_tokens": 6},
            "output_tokens_details": {"reasoning_tokens": 2},
        })
        self.assertEqual((alt.input_tokens, alt.output_tokens), (10, 4))
        self.assertEqual((alt.cached_tokens, alt.reasoning_tokens), (6, 2))
        unknown = _usage_info({"prompt_tokens": 10, "completion_tokens": 2})
        self.assertFalse(unknown.cached_tokens_reported)
        self.assertFalse(unknown.reasoning_tokens_reported)


    def test_anthropic_usage_keeps_cache_read_and_write_separate(self) -> None:
        usage = _anthropic_usage({
            "input_tokens": 20,
            "output_tokens": 5,
            "cache_read_input_tokens": 70,
            "cache_creation_input_tokens": 10,
        })
        self.assertEqual(usage.input_tokens, 100)
        self.assertEqual(usage.cached_tokens, 70)
        self.assertEqual(usage.cache_write_tokens, 10)
        self.assertTrue(usage.cached_tokens_reported)
        self.assertTrue(usage.cache_write_tokens_reported)

    def test_turn_ledger_is_kirocrew_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "turns.jsonl"
            store = TurnTelemetryStore(path)
            store.record(TurnTelemetry(
                timestamp=1.0,
                session_id="session-1",
                outcome="success",
                requested_model="auto",
                provider_id="demo",
                served_model="model-a",
                effort="high",
                total_ms=1500,
                first_visible_ms=300,
                upstream_ms=1200,
                tool_ms=100,
                mcp_wait_ms=0,
                tool_calls=2,
                native_calls=1,
                tool_search_calls=1,
                active_tools=4,
                tool_schema_bytes=1234,
                stream_chunks=20,
                stream_chars=1000,
                input_tokens=200,
                output_tokens=50,
                cached_tokens=80,
                cache_write_tokens=25,
                cache_usage_reported=True,
                cache_write_usage_reported=True,
            ))
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("prompt", raw)
            self.assertNotIn("assistant_text", raw)
            item = json.loads(raw)
            self.assertEqual(item["client"], "kiro-crew")
            self.assertEqual(item["requested_model"], "auto")
            self.assertEqual(item["served_model"], "model-a")
            summary = store.summary()
            self.assertEqual(summary["turns"], 1)
            self.assertEqual(summary["success"], 1)
            self.assertEqual(summary["input_tokens"], 200)
            self.assertEqual(summary["cache_usage_reported_turns"], 1)
            self.assertEqual(summary["cache_write_usage_reported_turns"], 1)
            self.assertEqual(summary["cached_tokens"], 80)
            self.assertEqual(summary["cache_write_tokens"], 25)
            self.assertAlmostEqual(summary["cache_hit_rate"], 0.4)

    def test_summary_reports_error_categories_and_provider_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "turns.jsonl"
            store = TurnTelemetryStore(path)
            base = dict(
                requested_model="auto", provider_id="demo", served_model="model-a", effort="",
                total_ms=100, first_visible_ms=20, upstream_ms=80, tool_ms=0, mcp_wait_ms=0,
                tool_calls=0, native_calls=0, tool_search_calls=0, active_tools=0,
                tool_schema_bytes=0, stream_chunks=1, stream_chars=1,
            )
            store.record(TurnTelemetry(timestamp=1.0, session_id="ok", outcome="success",
                                       transport_retries=1, fallbacks=1, **base))
            store.record(TurnTelemetry(timestamp=2.0, session_id="bad", outcome="error",
                                       error_kind="ProviderTransportError", error_category="transport", **base))
            store.record(TurnTelemetry(timestamp=3.0, session_id="cancel", outcome="cancelled",
                                       cancel_source="client_session_cancel", error_category="control", **base))
            summary = store.summary()
            self.assertEqual(summary["success"], 1)
            self.assertEqual(summary["failed"], 1)
            self.assertEqual(summary["cancelled"], 1)
            self.assertEqual(summary["error_categories"], {"transport": 1})
            health = summary["provider_health"][0]
            self.assertEqual(health["turns"], 3)
            self.assertEqual(health["success"], 1)
            self.assertEqual(health["failed"], 1)
            self.assertEqual(health["cancelled"], 1)
            self.assertEqual(health["transport_errors"], 1)
            self.assertEqual(health["transport_retries"], 1)
            self.assertEqual(health["fallbacks"], 1)
            self.assertAlmostEqual(health["success_rate"], 1 / 3)

    def test_turn_ledger_is_bounded_and_clearable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "turns.jsonl"
            store = TurnTelemetryStore(path, max_records=20)
            for idx in range(25):
                store.record(TurnTelemetry(
                    timestamp=float(idx),
                    session_id=f"session-{idx}",
                    outcome="success",
                    requested_model="auto",
                    provider_id="demo",
                    served_model="model-a",
                    effort="",
                    total_ms=10,
                    first_visible_ms=5,
                    upstream_ms=5,
                    tool_ms=0,
                    mcp_wait_ms=0,
                    tool_calls=0,
                    native_calls=0,
                    tool_search_calls=0,
                    active_tools=0,
                    tool_schema_bytes=0,
                    stream_chunks=1,
                    stream_chars=1,
                ))
            rows = store.recent(100)
            self.assertEqual(len(rows), 20)
            self.assertEqual(rows[0]["session_id"], "session-5")
            self.assertEqual(rows[-1]["session_id"], "session-24")
            self.assertEqual(store.clear(), 20)
            self.assertEqual(store.recent(100), [])
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()

"""Tests for kiro_crew.dashboard.handlers.usage."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.dashboard.handlers.usage as usage_mod
from kiro_crew.dashboard.handlers.usage import (
    _cached_parse_sessions,
    _parse_sessions,
    _parse_token_history,
    api_kiro_usage,
    get_usage_cache,
    persist_token_record,
    persist_token_record_async,
    read_context_tokens,
    read_effective_agent,
    read_effective_model,
)

# ── _parse_sessions ─────────────────────────────────────────────────────


def _write_session(path, lines, mtime=None):
    """Write a JSONL session file and optionally set mtime."""
    path.write_text("\n".join(json.dumps(item) for item in lines) + "\n")
    if mtime:
        os.utime(path, (mtime, mtime))


class TestParseSessions:
    def test_no_directory(self, tmp_path):
        with patch.object(usage_mod, "_SESSIONS_DIR", tmp_path / "nope"):
            assert _parse_sessions() == {"error": "No sessions directory"}

    def test_iterdir_oserror(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        with patch.object(usage_mod, "_SESSIONS_DIR", d), patch(
            "pathlib.Path.iterdir", side_effect=OSError("boom")
        ):
            result = _parse_sessions()
            assert "error" in result
            assert "boom" in result["error"]

    def test_skips_non_jsonl(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        (d / "readme.txt").write_text("hi")
        with patch.object(usage_mod, "_SESSIONS_DIR", d):
            r = _parse_sessions()
            assert r["total_sessions"] == 0
            assert r["all_time_sessions"] == 0

    def test_skips_invalid_path(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        _write_session(d / "s1.jsonl", [{"kind": "Prompt"}])
        with patch.object(usage_mod, "_SESSIONS_DIR", d), patch.object(
            usage_mod, "validate_file_path", return_value=None
        ):
            r = _parse_sessions()
            assert r["total_sessions"] == 0

    def test_stat_oserror(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "s1.jsonl"
        _write_session(f, [{"kind": "Prompt"}])
        orig_stat = Path.stat

        def stat_side_effect(self_, *a, **kw):
            if self_.name == f.name:
                raise OSError("stat fail")
            return orig_stat(self_, *a, **kw)

        with patch.object(usage_mod, "_SESSIONS_DIR", d), patch.object(
            usage_mod, "validate_file_path", return_value=str(f)
        ), patch.object(Path, "stat", stat_side_effect):
            r = _parse_sessions()
            assert r["all_time_sessions"] == 0

    def test_old_session_counted_alltime_only(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "old.jsonl"
        old_mtime = time.time() - (60 * 86400)  # 60 days ago
        _write_session(f, [{"kind": "Prompt"}], mtime=old_mtime)
        with patch.object(usage_mod, "_SESSIONS_DIR", d), patch.object(
            usage_mod, "validate_file_path", return_value=str(f)
        ):
            r = _parse_sessions()
            assert r["all_time_sessions"] == 1
            assert r["total_sessions"] == 0

    def test_counts_messages_and_tools(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "s1.jsonl"
        lines = [
            {"kind": "Prompt"},
            {"kind": "AssistantMessage"},
            {"kind": "ToolResults"},
            {"kind": "ToolResults"},
            {"kind": "Other"},
        ]
        _write_session(f, lines)
        with patch.object(usage_mod, "_SESSIONS_DIR", d), patch.object(
            usage_mod, "validate_file_path", return_value=str(f)
        ):
            r = _parse_sessions()
            assert r["total_sessions"] == 1
            assert r["total_messages"] == 2
            assert r["total_tool_calls"] == 2
            assert r["all_time_sessions"] == 1
            assert len(r["daily_history"]) == 1
            assert r["avg_msgs_per_session"] == 2.0
            assert r["avg_tools_per_session"] == 2.0

    def test_json_decode_error_skipped(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "s1.jsonl"
        f.write_text('{"kind":"Prompt"}\nNOT_JSON\n{"kind":"ToolResults"}\n')
        with patch.object(usage_mod, "_SESSIONS_DIR", d), patch.object(
            usage_mod, "validate_file_path", return_value=str(f)
        ):
            r = _parse_sessions()
            assert r["total_messages"] == 1
            assert r["total_tool_calls"] == 1

    def test_non_dict_json_skipped(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "s1.jsonl"
        f.write_text('"just a string"\n42\nnull\n[1,2]\n{"kind":"Prompt"}\n')
        with patch.object(usage_mod, "_SESSIONS_DIR", d), patch.object(
            usage_mod, "validate_file_path", return_value=str(f)
        ):
            r = _parse_sessions()
            assert r["total_messages"] == 1

    def test_file_read_oserror(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "s1.jsonl"
        _write_session(f, [{"kind": "Prompt"}])
        orig_open = Path.open

        def open_raises(self_, *a, **kw):
            if self_.suffix == ".jsonl":
                raise OSError("read fail")
            return orig_open(self_, *a, **kw)

        with patch.object(usage_mod, "_SESSIONS_DIR", d), patch.object(
            usage_mod, "validate_file_path", return_value=str(f)
        ), patch.object(Path, "open", open_raises):
            r = _parse_sessions()
            assert r["all_time_sessions"] == 1
            assert r["total_sessions"] == 0  # not counted when file read fails
            assert r["total_messages"] == 0

    def test_period_summaries(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        now = time.time()
        f = d / "today.jsonl"
        _write_session(f, [{"kind": "Prompt"}, {"kind": "ToolResults"}], mtime=now)
        with patch.object(usage_mod, "_SESSIONS_DIR", d), patch.object(
            usage_mod, "validate_file_path", return_value=str(f)
        ):
            r = _parse_sessions()
            assert r["today"]["sessions"] == 1
            assert r["today"]["messages"] == 1
            assert r["today"]["tool_calls"] == 1
            assert r["this_week"]["sessions"] >= 1
            assert r["this_month"]["sessions"] >= 1

    def test_timestamp_derives_day(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "s1.jsonl"
        lines = [
            {"kind": "Prompt", "timestamp": "2026-04-20T10:00:00"},
            {"kind": "ToolResults"},
        ]
        # mtime is today, but timestamp says April 20
        _write_session(f, lines)
        with patch.object(usage_mod, "_SESSIONS_DIR", d), patch.object(
            usage_mod, "validate_file_path", return_value=str(f)
        ):
            r = _parse_sessions()
            assert r["total_sessions"] == 1
            assert r["daily_history"][0]["date"] == "2026-04-20"
            assert r["total_messages"] == 1
            assert r["total_tool_calls"] == 1

    def test_malformed_timestamp_fallback_to_mtime(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "s1.jsonl"
        lines = [
            {"kind": "Prompt", "timestamp": "not-a-date"},
            {"kind": "ToolResults"},
        ]
        _write_session(f, lines)
        with patch.object(usage_mod, "_SESSIONS_DIR", d), patch.object(
            usage_mod, "validate_file_path", return_value=str(f)
        ):
            r = _parse_sessions()
            assert r["total_sessions"] == 1
            # Messages still counted despite bad timestamp
            assert r["total_messages"] == 1
            assert r["total_tool_calls"] == 1

    def test_z_suffix_timestamp(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "s1.jsonl"
        lines = [
            {"kind": "Prompt", "timestamp": "2026-04-20T10:00:00Z"},
            {"kind": "AssistantMessage"},
        ]
        _write_session(f, lines)
        with patch.object(usage_mod, "_SESSIONS_DIR", d), patch.object(
            usage_mod, "validate_file_path", return_value=str(f)
        ):
            r = _parse_sessions()
            assert r["total_sessions"] == 1
            # Z suffix parsed and converted to local TZ via .astimezone()
            expected = (
                datetime(2026, 4, 20, 10, 0, 0, tzinfo=timezone.utc)
                .astimezone()
                .strftime("%Y-%m-%d")
            )
            assert r["daily_history"][0]["date"] == expected
            assert r["total_messages"] == 2


# ── get_usage_cache ──────────────────────────────────────────────────────


class TestGetUsageCache:
    def test_returns_cache(self):
        mock_cache = {"credits_used": 42}
        with patch.dict(
            "sys.modules",
            {"kiro_crew.dashboard.handlers.sessions": MagicMock(_usage_cache=mock_cache)},
        ):
            assert get_usage_cache() == {"credits_used": 42}

    def test_empty_cache(self):
        with patch.dict(
            "sys.modules", {"kiro_crew.dashboard.handlers.sessions": MagicMock(_usage_cache={})}
        ):
            assert get_usage_cache() == {}

    def test_import_error(self):
        with patch.dict("sys.modules", {"kiro_crew.dashboard.handlers.sessions": None}):
            assert get_usage_cache() == {}


# ── api_kiro_usage ───────────────────────────────────────────────────────


class TestApiKiroUsage:
    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        usage_mod._CACHE = {}
        usage_mod._CACHE_TS = 0.0
        yield
        usage_mod._CACHE = {}
        usage_mod._CACHE_TS = 0.0

    @pytest.mark.asyncio
    async def test_returns_cached(self):
        usage_mod._CACHE = {"cached": True}
        usage_mod._CACHE_TS = time.time()
        app = web.Application()
        app.router.add_get("/api/usage/kiro", api_kiro_usage)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/usage/kiro")
            assert resp.status == 200
            data = await resp.json()
            assert data == {"cached": True}

    @pytest.mark.asyncio
    async def test_fresh_fetch(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "s.jsonl"
        _write_session(f, [{"kind": "Prompt"}])
        with patch.object(usage_mod, "_SESSIONS_DIR", d), patch.object(
            usage_mod, "validate_file_path", return_value=str(f)
        ), patch.object(
            usage_mod,
            "get_usage_cache",
            return_value={
                "credits_used": 10,
                "credits_plan": 100,
                "cost_usd": 0,
                "resets": "May 1",
                "plan": "Pro",
                "overage_rate": 0.01,
            },
        ):
            app = web.Application()
            app.router.add_get("/api/usage/kiro", api_kiro_usage)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/usage/kiro")
                assert resp.status == 200
                data = await resp.json()
                assert "sessions" in data
                assert "billing" in data
                assert data["billing"]["credits_used"] == 10
                assert data["sessions"]["total_sessions"] == 1

    @pytest.mark.asyncio
    async def test_error_not_cached(self, tmp_path):
        with patch.object(usage_mod, "_SESSIONS_DIR", tmp_path / "nope"), patch.object(
            usage_mod, "get_usage_cache", return_value={}
        ):
            app = web.Application()
            app.router.add_get("/api/usage/kiro", api_kiro_usage)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/usage/kiro")
                data = await resp.json()
                assert "error" in data
                # Cache should NOT be set
                assert usage_mod._CACHE == {}

    @pytest.mark.asyncio
    async def test_unavailable_sentinel_yields_empty_billing(self, tmp_path):
        # The {"available": False} sentinel is truthy but carries no plan — billing
        # must stay {} rather than a dict of all-None fields.
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "s.jsonl"
        _write_session(f, [{"kind": "Prompt"}])
        with patch.object(usage_mod, "_SESSIONS_DIR", d), \
             patch.object(usage_mod, "validate_file_path", return_value=str(f)), \
             patch.object(usage_mod, "get_usage_cache", return_value={"available": False}):
            app = web.Application()
            app.router.add_get("/api/usage/kiro", api_kiro_usage)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/usage/kiro")
                data = await resp.json()
                assert data["billing"] == {}


# ── _parse_token_history ─────────────────────────────────────────────────


def _reset_token_cache():
    """Tests share a process-global cache; clear it before each test that
    swaps out the shard directory."""
    usage_mod._TOKEN_CACHE = {}
    usage_mod._TOKEN_CACHE_KEY = None
    usage_mod._TOKEN_CACHE_TS = 0.0


def _patch_shard_layout(monkeypatch, tmp_path):
    """Point the module at an isolated shard directory so each test runs
    against an empty slate.
    """
    shard_dir = tmp_path / "tokens"
    shard_dir.mkdir()
    monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", shard_dir)
    _reset_token_cache()
    return shard_dir


def _write_shard(shard_dir: Path, day: str, records):
    (shard_dir / f"{day}.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")


class TestParseTokenHistory:
    def test_no_file(self, tmp_path, monkeypatch):
        _patch_shard_layout(monkeypatch, tmp_path)
        # Empty shard dir → empty result.
        assert _parse_token_history() == {}

    def test_skips_old_records(self, tmp_path, monkeypatch):
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        record = {
            "_type": "tokens",
            "ts": "2020-01-01T00:00:00+00:00",
            "input": 100,
            "output": 50,
        }
        _write_shard(shard_dir, "2020-01-01", [record])
        # Old shard is outside the 30-day window → directory listing skips
        # it, so the parse returns the empty-history result.
        assert _parse_token_history() == {}

    def test_aggregates_tokens(self, tmp_path, monkeypatch):
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
        ts1 = (now - timedelta(minutes=30)).isoformat()
        ts2 = (now - timedelta(minutes=15)).isoformat()
        records = [
            {
                "_type": "tokens",
                "ts": ts1,
                "input": 100,
                "output": 50,
                "cache_create": 10,
                "cache_read": 5,
                "cost": 0.01,
            },
            {
                "_type": "tokens",
                "ts": ts2,
                "input": 200,
                "output": 100,
                "cache_create": 20,
                "cache_read": 10,
                "cost": 0.02,
            },
        ]
        _write_shard(shard_dir, now.strftime("%Y-%m-%d"), records)
        result = _parse_token_history()
        assert result["total_input"] == 300
        assert result["total_output"] == 150
        assert result["cache_creation"] == 30
        assert result["cache_read"] == 15
        assert result["total"] == 495
        assert result["cost_usd"] == 0.03
        assert len(result["daily_history"]) == 1
        assert result["daily_history"][0]["input"] == 300

    def test_multi_day_aggregation(self, tmp_path, monkeypatch):
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        now = datetime.now().astimezone().replace(hour=12, minute=0, second=0)
        yesterday = now - timedelta(days=1)
        _write_shard(
            shard_dir,
            yesterday.strftime("%Y-%m-%d"),
            [{"_type": "tokens", "ts": yesterday.isoformat(), "input": 50, "output": 25}],
        )
        _write_shard(
            shard_dir,
            now.strftime("%Y-%m-%d"),
            [{"_type": "tokens", "ts": now.isoformat(), "input": 100, "output": 75}],
        )
        result = _parse_token_history()
        assert result["total_input"] == 150
        assert result["total_output"] == 100
        assert len(result["daily_history"]) == 2

    def test_ignores_non_token_records(self, tmp_path, monkeypatch):
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        now = datetime.now().astimezone()
        recent_ts = (now - timedelta(hours=1)).isoformat()
        records = [
            {"_type": "metadata", "created_at": recent_ts},
            {"role": "user", "content": "hello"},
            {"_type": "tokens", "ts": recent_ts, "input": 42, "output": 10},
        ]
        _write_shard(shard_dir, now.strftime("%Y-%m-%d"), records)
        result = _parse_token_history()
        assert result["total_input"] == 42
        assert result["total_output"] == 10

    def test_skips_files_with_invalid_names(self, tmp_path, monkeypatch):
        """A stray file with a non-date stem (e.g. README, .DS_Store) in
        the shard dir must not crash the parser."""
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        (shard_dir / "README.txt").write_text("not a shard")
        (shard_dir / "garbage.jsonl").write_text("{}\n")
        # Real shard alongside the noise.
        now = datetime.now().astimezone()
        _write_shard(
            shard_dir,
            now.strftime("%Y-%m-%d"),
            [{"_type": "tokens", "ts": now.isoformat(), "input": 9, "output": 1}],
        )
        result = _parse_token_history()
        assert result["total_input"] == 9


# ── _persist_token_record ────────────────────────────────────────────────


class TestPersistTokenRecord:
    def test_writes_token_record(self, tmp_path, monkeypatch):
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)

        slot_key = "test-slot"
        model = "claude-sonnet-4"

        event = MagicMock()
        event.input_tokens = 500
        event.output_tokens = 200
        event.cache_creation_tokens = 50
        event.cache_read_tokens = 25
        event.cost_usd = 0.05
        event.num_turns = 3
        event.duration_ms = 1500

        persist_token_record(slot_key, model, event)

        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        shard_path = shard_dir / f"{today}.jsonl"
        assert shard_path.exists()
        lines = shard_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["_type"] == "tokens"
        assert record["slot"] == "test-slot"
        assert record["model"] == "claude-sonnet-4"
        assert record["input"] == 500
        assert record["output"] == 200
        assert record["cache_create"] == 50
        assert record["cache_read"] == 25
        assert record["cost"] == 0.05
        assert record["turns"] == 3
        assert record["duration_ms"] == 1500
        assert "ts" in record

    def test_no_crash_on_error(self, tmp_path, monkeypatch):
        slot_key = "test"
        model = "test-model"
        event = MagicMock()
        event.input_tokens = 100
        event.output_tokens = 50

        # Point the shard dir at an unwritable location; persist must swallow.
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", Path("/proc/nonexistent/usage/tokens"))
        # Should not raise
        persist_token_record(slot_key, model, event)

    def test_appends_multiple_records(self, tmp_path, monkeypatch):
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)

        slot_key = "s1"
        model = "opus"

        event1 = MagicMock()
        event1.input_tokens = 100
        event1.output_tokens = 50
        event1.cache_creation_tokens = 0
        event1.cache_read_tokens = 0
        event1.cost_usd = 0.01
        event1.num_turns = 1
        event1.duration_ms = 500

        event2 = MagicMock()
        event2.input_tokens = 200
        event2.output_tokens = 80
        event2.cache_creation_tokens = 10
        event2.cache_read_tokens = 5
        event2.cost_usd = 0.02
        event2.num_turns = 1
        event2.duration_ms = 800

        persist_token_record(slot_key, model, event1)
        persist_token_record(slot_key, model, event2)

        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        shard_path = shard_dir / f"{today}.jsonl"
        lines = shard_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2

    def test_writes_provider_field(self, tmp_path, monkeypatch):
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        event = MagicMock()
        event.input_tokens = 10
        event.output_tokens = 5
        event.cache_creation_tokens = 0
        event.cache_read_tokens = 0
        event.cost_usd = 0.0
        event.num_turns = 0
        event.duration_ms = 0

        persist_token_record("slot", "opus", event, provider="claude_code")

        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        record = json.loads((shard_dir / f"{today}.jsonl").read_text(encoding="utf-8").strip())
        assert record["provider"] == "claude_code"
        assert record["model"] == "opus"

    def test_provider_defaults_to_empty(self, tmp_path, monkeypatch):
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        event = MagicMock()
        event.input_tokens = 10
        event.output_tokens = 5
        event.cache_creation_tokens = 0
        event.cache_read_tokens = 0
        event.cost_usd = 0.0
        event.num_turns = 0
        event.duration_ms = 0

        persist_token_record("slot", "opus", event)

        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        record = json.loads((shard_dir / f"{today}.jsonl").read_text(encoding="utf-8").strip())
        assert record["provider"] == ""

    def test_parse_token_history_aggregates_provider(self, tmp_path, monkeypatch):
        _patch_shard_layout(monkeypatch, tmp_path)
        for ev in (
            ("opencode", "claude-sonnet-4", 100, 50),
            ("opencode", "claude-haiku-3", 30, 10),
            ("claude_code", "opus", 200, 80),
        ):
            event = MagicMock()
            event.input_tokens = ev[2]
            event.output_tokens = ev[3]
            event.cache_creation_tokens = 0
            event.cache_read_tokens = 0
            event.cost_usd = 0.01
            event.num_turns = 1
            event.duration_ms = 100
            persist_token_record("slot", ev[1], event, provider=ev[0])

        # Cache may have been populated by the empty-shard parse triggered
        # before the writes; reset so the next read picks them up.
        _reset_token_cache()
        history = usage_mod._parse_token_history()

        assert sorted(history["providers"]) == ["claude_code", "opencode"]
        # Non-claude_code provider models keep their raw namespace verbatim.
        assert "claude-sonnet-4" in history["models"]
        # claude_code models are canonicalized so pre/post-migration records
        # aggregate into one bucket: bare 'opus' -> canonical 'opus-4.8-1m'.
        assert "opus-4.8-1m" in history["models"]
        assert "opus" not in history["models"]
        # The day entry should carry both providers + models sub-maps.
        day = history["daily_history"][0]
        assert "providers" in day
        assert "models" in day
        assert "opencode" in day["providers"]
        assert day["providers"]["opencode"]["input"] == 130

        # provider_models maps each provider to ONLY the models that have
        # actually appeared paired with it, so the frontend can cascade
        # the model dropdown safely.
        pm = history["provider_models"]
        assert sorted(pm["opencode"]) == ["claude-haiku-3", "claude-sonnet-4"]
        assert pm["claude_code"] == ["opus-4.8-1m"]  # canonicalized for claude_code
        # opus must NOT appear under opencode — that would be the bug.
        assert "opus" not in pm["opencode"]

        # Per-day cross-tab carries the true intersection bucket, so the
        # chart can render correct numbers when both filters are active.
        day_pm = day["provider_models"]
        assert day_pm["opencode"]["claude-sonnet-4"]["input"] == 100
        assert day_pm["opencode"]["claude-haiku-3"]["input"] == 30
        assert day_pm["claude_code"]["opus-4.8-1m"]["input"] == 200
        # Invalid pair (opencode + opus) is absent from the cross-tab.
        assert "opus" not in day_pm["opencode"]


# ── persist_token_record_async / _cached_parse_sessions (event-loop hygiene) ──


def _reset_sessions_cache():
    usage_mod._SESSIONS_CACHE = None
    usage_mod._SESSIONS_CACHE_TS = 0.0


class TestPersistTokenRecordAsync:
    @pytest.mark.asyncio
    async def test_async_writes_same_record_as_sync(self, tmp_path, monkeypatch):
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        event = MagicMock()
        event.input_tokens = 7
        event.output_tokens = 3
        event.cache_creation_tokens = 0
        event.cache_read_tokens = 0
        event.cost_usd = 0.01
        event.num_turns = 1
        event.duration_ms = 42

        await persist_token_record_async("slot-a", "opus", event, provider="claude_code")

        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        record = json.loads((shard_dir / f"{today}.jsonl").read_text(encoding="utf-8").strip())
        assert record["slot"] == "slot-a"
        assert record["provider"] == "claude_code"
        assert record["input"] == 7

    @pytest.mark.asyncio
    async def test_async_no_crash_on_error(self, monkeypatch):
        event = MagicMock()
        event.input_tokens = 1
        event.output_tokens = 1
        monkeypatch.setattr(
            usage_mod, "_TOKEN_USAGE_DIR", Path("/proc/nonexistent/usage/tokens")
        )
        await persist_token_record_async("s", "m", event)  # must not raise


class TestCachedParseSessions:
    @pytest.mark.asyncio
    async def test_returns_empty_without_dir(self, tmp_path, monkeypatch):
        _reset_sessions_cache()
        monkeypatch.setattr(usage_mod, "_SESSIONS_DIR", tmp_path / "nope")
        assert await _cached_parse_sessions() == {}

    @pytest.mark.asyncio
    async def test_offloads_and_caches(self, tmp_path, monkeypatch):
        _reset_sessions_cache()
        sessions_dir = tmp_path / "cli"
        sessions_dir.mkdir()
        monkeypatch.setattr(usage_mod, "_SESSIONS_DIR", sessions_dir)

        calls = {"n": 0}

        def _fake_parse():
            calls["n"] += 1
            return {"total_sessions": 1, "daily_history": []}

        monkeypatch.setattr(usage_mod, "_parse_sessions", _fake_parse)
        first = await _cached_parse_sessions()
        second = await _cached_parse_sessions()
        assert first == {"total_sessions": 1, "daily_history": []}
        assert second == first
        # Second call served from the TTL cache — parse ran once.
        assert calls["n"] == 1
        _reset_sessions_cache()

    @pytest.mark.asyncio
    async def test_error_result_not_cached(self, tmp_path, monkeypatch):
        _reset_sessions_cache()
        sessions_dir = tmp_path / "cli"
        sessions_dir.mkdir()
        monkeypatch.setattr(usage_mod, "_SESSIONS_DIR", sessions_dir)
        monkeypatch.setattr(usage_mod, "_parse_sessions", lambda: {"error": "boom"})
        result = await _cached_parse_sessions()
        assert result == {"error": "boom"}
        # An error result must not poison the cache (stays unpopulated).
        assert usage_mod._SESSIONS_CACHE is None
        _reset_sessions_cache()

    @pytest.mark.asyncio
    async def test_empty_result_is_cached(self, tmp_path, monkeypatch):
        """A valid-but-empty {} parse must be cached and served from the fast
        path (sentinel check), not re-parsed on every call."""
        _reset_sessions_cache()
        sessions_dir = tmp_path / "cli"
        sessions_dir.mkdir()
        monkeypatch.setattr(usage_mod, "_SESSIONS_DIR", sessions_dir)

        calls = {"n": 0}

        def _fake_parse():
            calls["n"] += 1
            return {}  # valid, but empty (dir present, no session files)

        monkeypatch.setattr(usage_mod, "_parse_sessions", _fake_parse)
        assert await _cached_parse_sessions() == {}
        assert await _cached_parse_sessions() == {}
        # Empty dict is cached (sentinel is None, so {} is a hit) — parse once.
        assert calls["n"] == 1
        _reset_sessions_cache()


class TestBuildTokenRecordCredits:
    """_build_token_record persists per-turn credits (kiro) and defaults to 0."""

    def test_record_includes_credits(self):
        from types import SimpleNamespace

        event = SimpleNamespace(
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            credits=1.19,
            num_turns=0,
            duration_ms=0,
            cache_creation_tokens=0,
            cache_read_tokens=0,
        )
        rec = usage_mod._build_token_record(
            "chat-1", "claude-opus-4-8", event, "acp", datetime.now(timezone.utc)
        )
        assert rec["_type"] == "tokens"
        assert rec["credits"] == pytest.approx(1.19)
        assert rec["slot"] == "chat-1"
        assert rec["provider"] == "acp"

    def test_record_defaults_credits_to_zero_when_absent(self):
        from types import SimpleNamespace

        event = SimpleNamespace(input_tokens=10, output_tokens=5)
        rec = usage_mod._build_token_record(
            "s", "m", event, "claude_code", datetime.now(timezone.utc)
        )
        assert rec["credits"] == 0.0

    def test_record_coerces_non_numeric_credits_to_zero(self):
        from types import SimpleNamespace

        event = SimpleNamespace(
            input_tokens=0, output_tokens=0, credits="not-a-number"
        )
        rec = usage_mod._build_token_record(
            "s", "m", event, "acp", datetime.now(timezone.utc)
        )
        assert rec["credits"] == 0.0


# ── read_context_tokens / context-occupancy row fields (issue #647) ──────────


class TestReadContextTokens:
    """read_context_tokens() reads provider occupancy accessors defensively."""

    def test_returns_real_values_from_provider(self):
        from types import SimpleNamespace

        # A fake provider exposing both public accessors (as AcpProvider /
        # AcpSessionProvider do) returns their exact values.
        provider = SimpleNamespace(
            context_used_tokens=lambda: 12345,
            context_window_tokens=lambda: 1_000_000,
        )
        assert read_context_tokens(provider) == (12345, 1_000_000)

    def test_returns_zero_when_accessors_absent(self):
        # A plain object with neither accessor — e.g. a non-ACP provider or a
        # bare test double — records (0, 0) rather than raising.
        assert read_context_tokens(object()) == (0, 0)

    def test_returns_zero_when_accessor_raises(self):
        class _Boom:
            def context_used_tokens(self) -> int:
                raise RuntimeError("boom")

            def context_window_tokens(self) -> int:
                return 1_000_000

        assert read_context_tokens(_Boom()) == (0, 0)

    def test_returns_zero_when_only_one_accessor_present(self):
        from types import SimpleNamespace

        # Both accessors are required; a partial provider still yields (0, 0).
        partial = SimpleNamespace(context_used_tokens=lambda: 500)
        assert read_context_tokens(partial) == (0, 0)


class TestBuildTokenRecordContextFields:
    """_build_token_record emits the additive surface/agent/context_* fields."""

    @staticmethod
    def _event():
        from types import SimpleNamespace

        return SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            cost_usd=0.0,
            credits=0.0,
            num_turns=1,
            duration_ms=0,
        )

    def test_emits_new_keys(self):
        rec = usage_mod._build_token_record(
            "chat-1",
            "claude-opus-4-8",
            self._event(),
            "acp",
            datetime.now(timezone.utc),
            surface="dashboard",
            agent="kirocrew",
            context_used=44_000,
            context_window=1_000_000,
        )
        assert rec["surface"] == "dashboard"
        assert rec["agent"] == "kirocrew"
        assert rec["context_used"] == 44_000
        assert rec["context_window"] == 1_000_000

    def test_coerces_non_numeric_context_to_zero(self):
        # Defensive int coercion keeps the record json.dumps-safe.
        rec = usage_mod._build_token_record(
            "s",
            "m",
            self._event(),
            "acp",
            datetime.now(timezone.utc),
            context_used="not-a-number",
            context_window=None,
        )
        assert rec["context_used"] == 0
        assert rec["context_window"] == 0
        json.dumps(rec)  # must not raise

    def test_backcompat_defaults_when_no_kwargs(self):
        # Called positionally with no new kwargs (mirrors every legacy caller):
        # the original keys are unchanged and the new keys default to ""/0, so
        # old readers and old shards stay valid.
        rec = usage_mod._build_token_record(
            "chat-1", "opus", self._event(), "acp", datetime.now(timezone.utc)
        )
        for key in (
            "_type",
            "ts",
            "slot",
            "provider",
            "model",
            "input",
            "output",
            "cache_create",
            "cache_read",
            "cost",
            "credits",
            "turns",
            "duration_ms",
        ):
            assert key in rec
        assert rec["input"] == 10
        assert rec["provider"] == "acp"
        # New fields present with defaults.
        assert rec["surface"] == ""
        assert rec["agent"] == ""
        assert rec["context_used"] == 0
        assert rec["context_window"] == 0

    def test_persist_writes_new_fields_to_shard(self, tmp_path, monkeypatch):
        # End-to-end: the keyword-only params flow through persist_token_record
        # into the written JSONL row.
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        persist_token_record(
            "slot",
            "opus",
            self._event(),
            provider="acp",
            surface="dashboard",
            agent="kirocrew",
            context_used=44_000,
            context_window=1_000_000,
        )
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        record = json.loads((shard_dir / f"{today}.jsonl").read_text(encoding="utf-8").strip())
        assert record["surface"] == "dashboard"
        assert record["agent"] == "kirocrew"
        assert record["context_used"] == 44_000
        assert record["context_window"] == 1_000_000


class TestBuildTokenRecordCtxBlocks:
    """_build_token_record emits the per-turn injection breakdown + phase."""

    @staticmethod
    def _event():
        return SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            cost_usd=0.0,
            credits=0.0,
            num_turns=1,
            duration_ms=0,
        )

    def test_emits_ctx_blocks_and_phase(self):
        rec = usage_mod._build_token_record(
            "chat-1",
            "claude-opus-4-8",
            self._event(),
            "acp",
            datetime.now(timezone.utc),
            ctx_blocks={"memory": 1200, "lessons": 800},
            phase="session_start",
        )
        assert rec["ctx_blocks"] == {"memory": 1200, "lessons": 800}
        assert rec["phase"] == "session_start"

    def test_drops_non_positive_and_non_numeric_block_sizes(self):
        # A zero, a negative, and two non-numeric sizes are all dropped so the
        # row carries only real spans and stays json.dumps-safe.
        rec = usage_mod._build_token_record(
            "s",
            "m",
            self._event(),
            "acp",
            datetime.now(timezone.utc),
            ctx_blocks={"keep": 10, "zero": 0, "neg": -5, "text": "x", "none": None},
            phase="per_turn",
        )
        assert rec["ctx_blocks"] == {"keep": 10}
        assert rec["phase"] == "per_turn"
        json.dumps(rec)

    def test_backcompat_defaults_when_omitted(self):
        # Legacy callers pass neither: the fields still exist, defaulted, so old
        # readers and old shards stay valid.
        rec = usage_mod._build_token_record(
            "chat-1", "opus", self._event(), "acp", datetime.now(timezone.utc)
        )
        assert rec["ctx_blocks"] == {}
        assert rec["phase"] == ""


class _Inner:
    def __init__(self, model):
        self._model = model


class TestReadEffectiveModel:
    """read_effective_model: raw resolved model id, attribution only."""

    def test_reads_via_public_client_chain(self):
        src = type("P", (), {"client": _Inner("global.anthropic.claude-opus-4-8[1m]")})()
        assert read_effective_model(src) == "global.anthropic.claude-opus-4-8[1m]"

    def test_reads_via_private_client_chain(self):
        src = type("P", (), {"_client": _Inner("claude-opus-4.8")})()
        assert read_effective_model(src) == "claude-opus-4.8"

    def test_reads_via_handle_chain(self):
        src = type("P", (), {"_handle": _Inner("claude-haiku-4.5")})()
        assert read_effective_model(src) == "claude-haiku-4.5"

    def test_reads_direct_model_attr(self):
        assert read_effective_model(_Inner("claude-sonnet-4.5")) == "claude-sonnet-4.5"

    def test_skips_auto_sentinel(self):
        # "auto" means "backend chooses" — not a model, so not attribution data.
        assert read_effective_model(_Inner("auto")) == ""

    def test_returns_empty_when_absent(self):
        assert read_effective_model(object()) == ""

    def test_returns_empty_when_accessor_raises(self):
        class Boom:
            @property
            def _client(self):
                raise RuntimeError("boom")

        assert read_effective_model(Boom()) == ""

    def test_prefers_first_populated_chain(self):
        src = type("P", (), {"client": _Inner(""), "_client": _Inner("claude-opus-4.8")})()
        assert read_effective_model(src) == "claude-opus-4.8"

    def test_resolved_id_wins_over_model(self):
        # Mirrors AcpClient's own precedence: `_resolved_model_id or _model`.
        inner = _Inner("claude-opus-4.8")
        inner._resolved_model_id = "global.anthropic.claude-opus-4-8[1m]"
        src = type("P", (), {"_client": inner})()
        assert read_effective_model(src) == "global.anthropic.claude-opus-4-8[1m]"

    def test_default_model_turn_uses_resolved_id(self):
        # The common case: the caller asked for the default, so `_model` is left
        # at the "auto" sentinel while the backend's resolved id is the real one.
        inner = _Inner("auto")
        inner._resolved_model_id = "claude-opus-4.8"
        src = type("P", (), {"_handle": inner})()
        assert read_effective_model(src) == "claude-opus-4.8"

    def test_falls_back_to_model_when_resolved_id_blank(self):
        inner = _Inner("claude-haiku-4.5")
        inner._resolved_model_id = ""
        assert read_effective_model(inner) == "claude-haiku-4.5"

    def test_walks_nested_handle_two_levels_down(self):
        # The default Kiro turn shape: providers/acp.py assigns an
        # AcpSessionProvider to _client, which holds the handle on _handle, so
        # the resolved id sits at provider.client._handle.
        handle = _Inner("auto")
        handle._resolved_model_id = "claude-opus-4.8"
        mid = type("SessionProvider", (), {"_handle": handle})()
        outer = type("P", (), {"client": mid, "_client": mid})()
        assert read_effective_model(outer) == "claude-opus-4.8"

    def test_resolved_id_deep_beats_model_shallow(self):
        # A resolved id anywhere outranks a plain _model anywhere: _model may
        # still hold a pre-resolution request.
        handle = _Inner("")
        handle._resolved_model_id = "global.anthropic.claude-opus-4-8[1m]"
        outer = type("P", (), {"_model": "claude-opus-4.8", "_handle": handle})()
        assert read_effective_model(outer) == "global.anthropic.claude-opus-4-8[1m]"

    def test_self_referential_chain_terminates(self):
        node = type("Loop", (), {})()
        node._client = node
        node._model = "claude-opus-4.8"
        assert read_effective_model(node) == "claude-opus-4.8"


class TestReadEffectiveAgent:
    """The resolved agent, not the slot alias."""

    def test_reads_resolved_agent_off_client(self):
        inner = type("C", (), {"_agent": "kirocrew"})()
        assert read_effective_agent(inner) == "kirocrew"

    def test_walks_nested_handle(self):
        handle = type("H", (), {"_agent": "kirocrew-lite"})()
        mid = type("SessionProvider", (), {"_handle": handle})()
        outer = type("P", (), {"client": mid})()
        assert read_effective_agent(outer) == "kirocrew-lite"

    def test_blank_when_absent(self):
        assert read_effective_agent(object()) == ""

    def test_never_raises_on_exploding_attribute(self):
        class Boom:
            @property
            def _agent(self):
                raise RuntimeError("nope")

        assert read_effective_agent(Boom()) == ""

    def test_reads_agent_off_the_runtime(self):
        # The session-provider shape: the agent is held only by the spawned CLI
        # runtime (runtime.py:273), reached via provider -> _handle -> _runtime.
        runtime = type("Runtime", (), {"_agent": "kirocrew"})()
        handle = type("Handle", (), {"_runtime": runtime})()
        provider = type("SessionProvider", (), {"_handle": handle})()
        assert read_effective_agent(provider) == "kirocrew"

    def test_session_model_outranks_runtime_model(self):
        # _runtime is walked last so its process-level --model argument cannot
        # outrank the session handle's own model.
        runtime = type("Runtime", (), {"_model": "claude-haiku-4.5"})()
        handle = type("Handle", (), {"_model": "claude-opus-4.8", "_runtime": runtime})()
        provider = type("SessionProvider", (), {"_handle": handle})()
        assert read_effective_model(provider) == "claude-opus-4.8"


class TestModelSourceFallback:
    """model_source fills `model` only when the caller resolved none."""

    @staticmethod
    def _event():
        # Not a real TurnUsage, so _build_token_record reads credits off the
        # event itself — enough to exercise the model fallback.
        return SimpleNamespace(credits=1.0)

    def _row(self, shard_dir):
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        return json.loads((shard_dir / f"{today}.jsonl").read_text(encoding="utf-8").strip())

    def test_fallback_fills_empty_model(self, tmp_path, monkeypatch):
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        persist_token_record(
            "slot",
            "",
            self._event(),
            provider="acp",
            surface="webhook",
            model_source=_Inner("claude-opus-4.8"),
        )
        assert self._row(shard_dir)["model"] == "claude-opus-4.8"

    def test_explicit_model_wins(self, tmp_path, monkeypatch):
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        persist_token_record(
            "slot",
            "claude-haiku-4.5",
            self._event(),
            provider="acp",
            surface="cron",
            model_source=_Inner("claude-opus-4.8"),
        )
        assert self._row(shard_dir)["model"] == "claude-haiku-4.5"

    def test_no_source_leaves_model_empty(self, tmp_path, monkeypatch):
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        persist_token_record("slot", "", self._event(), provider="acp", surface="webhook")
        assert self._row(shard_dir)["model"] == ""

    def test_unusable_source_leaves_model_empty(self, tmp_path, monkeypatch):
        # A test double with no model attr must not break the write.
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        persist_token_record(
            "slot", "", self._event(), provider="acp", surface="webhook", model_source=object()
        )
        assert self._row(shard_dir)["model"] == ""

    def test_auto_sentinel_is_treated_as_unresolved(self, tmp_path, monkeypatch):
        # agent.model defaults to "auto" and the task runner forwards it verbatim.
        # "auto" is not a model, so the provider's resolved id must win.
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        persist_token_record(
            "slot",
            "auto",
            self._event(),
            provider="acp",
            surface="task_runner",
            model_source=_Inner("claude-opus-4.8"),
        )
        assert self._row(shard_dir)["model"] == "claude-opus-4.8"

    def test_auto_without_resolvable_source_records_auto(self, tmp_path, monkeypatch):
        # `auto` records the explicit backend-selection mode even when the
        # backend does not disclose the concrete model for this completed turn.
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        persist_token_record(
            "slot", "auto", self._event(), provider="acp", surface="task_runner"
        )
        assert self._row(shard_dir)["model"] == "auto"

    def test_auto_is_case_and_space_insensitive(self, tmp_path, monkeypatch):
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        persist_token_record(
            "slot", "  AUTO ", self._event(), provider="acp", surface="cron"
        )
        assert self._row(shard_dir)["model"] == "auto"

    def test_auto_source_fills_empty_model(self, tmp_path, monkeypatch):
        # Dashboard slots use an empty override for Auto while the live client
        # retains the request sentinel.
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        persist_token_record(
            "dashboard:auto",
            "",
            self._event(),
            provider="acp",
            surface="dashboard",
            model_source=_Inner("  AUTO "),
        )
        assert self._row(shard_dir)["model"] == "auto"

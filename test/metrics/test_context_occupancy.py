"""Drive the REAL context-occupancy aggregation over synthetic per-turn rows.

``persist_token_record`` writes ``context_used`` / ``context_window`` on every
turn, but nothing read them — the fields were write-only and the Telemetry page
had no context section at all. These tests cover the read side in production
(``handlers.usage.context_occupancy``) rather than restating its arithmetic.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from kiro_crew.dashboard.handlers import usage as usage_mod


@pytest.fixture(autouse=True)
def _isolated_shards(tmp_path, monkeypatch):
    """Point the row store at a temp dir and drop the module cache.

    The cache is keyed on (name, mtime, size) per shard, but it is module state:
    without resetting it a prior test's result could be served here.
    """
    monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
    monkeypatch.setattr(usage_mod, "_CONTEXT_CACHE", None)
    monkeypatch.setattr(usage_mod, "_CONTEXT_CACHE_KEY", None)
    monkeypatch.setattr(usage_mod, "_CONTEXT_CACHE_TS", 0.0)
    return tmp_path


def _row(slot, used, window=1_000_000, *, ago_hours=1, **extra):
    ts = datetime.now(timezone.utc) - timedelta(hours=ago_hours)
    row = {
        "_type": "tokens",
        "ts": ts.isoformat(),
        "slot": slot,
        "context_used": used,
        "context_window": window,
    }
    row.update(extra)
    return row


def _write(shard_dir, rows, day=None):
    day = day or datetime.now().astimezone().strftime("%Y-%m-%d")
    p = shard_dir / f"{day}.jsonl"
    with p.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return p


class TestContextOccupancyBasics:
    def test_empty_store_reports_no_turns(self, _isolated_shards):
        out = usage_mod.context_occupancy(14)
        assert out["turns"] == 0
        assert out["sessions"] == []

    def test_percentiles_and_peak(self, _isolated_shards):
        _write(_isolated_shards, [
            _row("chat-1", 100_000),
            _row("chat-1", 500_000),
            _row("chat-1", 900_000),
        ])
        out = usage_mod.context_occupancy(14)
        assert out["turns"] == 3
        assert out["p50_pct"] == 50.0
        assert out["max_pct"] == 90.0

    def test_peak_is_per_session_not_global(self, _isolated_shards):
        _write(_isolated_shards, [
            _row("chat-hot", 950_000),
            _row("chat-cool", 50_000),
        ])
        by_slot = {s["slot"]: s for s in usage_mod.context_occupancy(14)["sessions"]}
        assert by_slot["chat-hot"]["peak_pct"] == 95.0
        assert by_slot["chat-cool"]["peak_pct"] == 5.0

    def test_sessions_ranked_by_peak_descending(self, _isolated_shards):
        _write(_isolated_shards, [
            _row("low", 100_000),
            _row("high", 800_000),
            _row("mid", 400_000),
        ])
        ranked = [s["slot"] for s in usage_mod.context_occupancy(14)["sessions"]]
        assert ranked == ["high", "mid", "low"]


class TestContextOccupancySkips:
    """Rows that cannot yield a ratio are skipped, never defaulted."""

    def test_rows_without_the_fields_are_skipped(self, _isolated_shards):
        # Pre-feature rows: the fields simply are not there.
        _write(_isolated_shards, [{"_type": "tokens", "ts": datetime.now(timezone.utc).isoformat(), "slot": "old"}])
        assert usage_mod.context_occupancy(14)["turns"] == 0

    def test_zero_window_is_skipped(self, _isolated_shards):
        _write(_isolated_shards, [_row("chat-1", 500_000, window=0)])
        assert usage_mod.context_occupancy(14)["turns"] == 0

    def test_non_token_records_are_ignored(self, _isolated_shards):
        _write(_isolated_shards, [{"_type": "something_else", "context_used": 5, "context_window": 10}])
        assert usage_mod.context_occupancy(14)["turns"] == 0

    def test_malformed_line_does_not_abort_the_shard(self, _isolated_shards):
        p = _write(_isolated_shards, [_row("chat-1", 300_000)])
        with p.open("a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        _write(_isolated_shards, [_row("chat-2", 600_000)])
        assert usage_mod.context_occupancy(14)["turns"] == 2

    def test_rows_older_than_the_window_are_excluded(self, _isolated_shards):
        old_day = (datetime.now().astimezone() - timedelta(days=20)).strftime("%Y-%m-%d")
        _write(_isolated_shards, [_row("ancient", 900_000, ago_hours=20 * 24)], day=old_day)
        _write(_isolated_shards, [_row("recent", 100_000)])
        out = usage_mod.context_occupancy(14)
        assert out["turns"] == 1
        assert [s["slot"] for s in out["sessions"]] == ["recent"]


class TestContextOccupancyLatestWins:
    """Identity and absolute numbers describe the session's LATEST turn."""

    def test_latest_turn_supplies_model_and_agent(self, _isolated_shards):
        _write(_isolated_shards, [
            _row("chat-1", 900_000, ago_hours=5, model="old-model", agent="a1", surface="slack"),
            _row("chat-1", 200_000, ago_hours=1, model="new-model", agent="a2", surface="dashboard"),
        ])
        s = usage_mod.context_occupancy(14)["sessions"][0]
        assert s["model"] == "new-model"
        assert s["agent"] == "a2"
        assert s["surface"] == "dashboard"
        # Absolute numbers follow the latest turn...
        assert s["used"] == 200_000
        # ...while the peak still remembers how close the session got.
        assert s["peak_pct"] == 90.0
        assert s["turns"] == 2

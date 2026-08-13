"""Per-session per-turn injection breakdown: the read side.

``usage.context_trace(slot, days)`` reads the ``ctx_blocks`` / ``phase`` fields
``persist_token_record`` writes each turn and returns them chronologically with
per-block totals; ``telemetry.api_context_trace`` is the thin HTTP wrapper. These
drive the REAL reader over synthetic rows, mirroring test_context_occupancy's
temp-shard + cache-reset fixture, rather than restating its arithmetic.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import usage as usage_mod
from kiro_crew.dashboard.handlers.telemetry import api_context_trace


@pytest.fixture(autouse=True)
def _isolated_shards(tmp_path, monkeypatch):
    """Point the row store at a temp dir and drop the occupancy cache.

    context_trace itself is uncached, but it shares the shard directory and the
    module-level cache globals with context_occupancy; resetting them keeps a
    prior test from leaking a cached read into this one.
    """
    monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
    monkeypatch.setattr(usage_mod, "_CONTEXT_CACHE", None)
    monkeypatch.setattr(usage_mod, "_CONTEXT_CACHE_KEY", None)
    monkeypatch.setattr(usage_mod, "_CONTEXT_CACHE_TS", 0.0)
    return tmp_path


def _row(slot, blocks, *, ts=None, used=0, window=1_000_000, phase="per_turn", **extra):
    row = {
        "_type": "tokens",
        "ts": ts or datetime.now(timezone.utc).isoformat(),
        "slot": slot,
        "phase": phase,
        "context_used": used,
        "context_window": window,
    }
    # ``None`` means "omit the key entirely" — a pre-feature row.
    if blocks is not None:
        row["ctx_blocks"] = blocks
    row.update(extra)
    return row


def _write(shard_dir, rows, day=None):
    day = day or datetime.now().astimezone().strftime("%Y-%m-%d")
    p = shard_dir / f"{day}.jsonl"
    with p.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return p


class TestContextTrace:
    def test_chronological_order_across_two_shards(self, _isolated_shards):
        now = datetime.now(timezone.utc)
        t_early = (now - timedelta(hours=3)).isoformat()
        t_mid = (now - timedelta(hours=2)).isoformat()
        t_late = (now - timedelta(hours=1)).isoformat()
        yesterday = (datetime.now().astimezone() - timedelta(days=1)).strftime("%Y-%m-%d")
        # Deliberately unsorted within a shard AND split across two shards.
        _write(_isolated_shards, [
            _row("chat-1", {"memory": 100}, ts=t_late),
            _row("chat-1", {"memory": 50}, ts=t_mid),
        ])
        _write(_isolated_shards, [_row("chat-1", {"memory": 10}, ts=t_early)], day=yesterday)
        out = usage_mod.context_trace("chat-1", 14)
        assert [t["ts"] for t in out["turns"]] == [t_early, t_mid, t_late]

    def test_rows_without_ctx_blocks_are_skipped_not_zero_filled(self, _isolated_shards):
        _write(_isolated_shards, [
            _row("chat-1", {"memory": 100}),
            _row("chat-1", None),            # pre-feature row: no ctx_blocks key
            _row("chat-1", {}),              # present but empty
            _row("chat-1", {"memory": 0}),   # present but all non-positive
        ])
        out = usage_mod.context_trace("chat-1", 14)
        assert len(out["turns"]) == 1
        assert out["turns"][0]["blocks"] == {"memory": 100}

    def test_other_slots_are_excluded(self, _isolated_shards):
        _write(_isolated_shards, [
            _row("chat-1", {"memory": 100}),
            _row("chat-2", {"memory": 999}),
        ])
        out = usage_mod.context_trace("chat-1", 14)
        assert len(out["turns"]) == 1
        assert out["totals"] == {"memory": 100}

    def test_totals_accumulate_across_turns(self, _isolated_shards):
        _write(_isolated_shards, [
            _row("chat-1", {"memory": 100, "lessons": 50}),
            _row("chat-1", {"memory": 30, "skill_index": 20}),
        ])
        out = usage_mod.context_trace("chat-1", 14)
        assert out["totals"] == {"memory": 130, "lessons": 50, "skill_index": 20}
        assert out["injected_chars"] == 200

    def test_user_chars_comes_from_your_message_label(self, _isolated_shards):
        _write(_isolated_shards, [_row("chat-1", {"your_message": 42, "memory": 100})])
        assert usage_mod.context_trace("chat-1", 14)["user_chars"] == 42

    def test_user_chars_zero_when_no_your_message_label(self, _isolated_shards):
        _write(_isolated_shards, [_row("chat-1", {"memory": 100})])
        assert usage_mod.context_trace("chat-1", 14)["user_chars"] == 0

    def test_estimated_other_is_zero_when_occupancy_unknown(self, _isolated_shards):
        # No context_used recorded -> peak is 0 -> the remainder is unknowable.
        _write(_isolated_shards, [_row("chat-1", {"memory": 100}, used=0)])
        out = usage_mod.context_trace("chat-1", 14)
        assert out["peak_context_used"] == 0
        assert out["estimated_other_chars"] == 0

    def test_estimated_other_never_negative(self, _isolated_shards):
        # injected (100000 chars) dwarfs peak_used*4 (40) -> clamp to 0, not <0.
        _write(_isolated_shards, [_row("chat-1", {"memory": 100_000}, used=10)])
        assert usage_mod.context_trace("chat-1", 14)["estimated_other_chars"] == 0

    def test_estimated_other_uses_peak_reading_and_char_estimate(self, _isolated_shards):
        # peak is the MAX context_used across turns; estimate = peak*4 - injected.
        _write(_isolated_shards, [
            _row("chat-1", {"memory": 100}, used=200),
            _row("chat-1", {"memory": 100}, used=900),
        ])
        out = usage_mod.context_trace("chat-1", 14)
        assert out["peak_context_used"] == 900
        assert out["injected_chars"] == 200
        assert out["estimated_other_chars"] == int(900 * 4.0) - 200


class TestApiContextTrace:
    @staticmethod
    def _app():
        app = web.Application()
        app.router.add_get("/api/telemetry/context-trace", api_context_trace)
        return app

    @pytest.mark.asyncio
    async def test_missing_slot_returns_400(self, _isolated_shards):
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.get("/api/telemetry/context-trace")
            body = await resp.json()
            assert resp.status == 400
            # `code` is the contract the localized dashboard branches on; the
            # prose in `error` is advisory and untranslatable on its own.
            assert body["code"] == "slot_required"

    @pytest.mark.asyncio
    async def test_blank_slot_returns_400(self, _isolated_shards):
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.get("/api/telemetry/context-trace", params={"slot": "   "})
            assert resp.status == 400
            assert (await resp.json())["code"] == "slot_required"

    @pytest.mark.asyncio
    async def test_returns_trace_payload_for_slot(self, _isolated_shards):
        _write(_isolated_shards, [
            _row("chat-1", {"memory": 100, "your_message": 10}, used=1000),
        ])
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.get("/api/telemetry/context-trace", params={"slot": "chat-1"})
            assert resp.status == 200
            body = await resp.json()
            assert body["slot"] == "chat-1"
            assert len(body["turns"]) == 1
            assert body["turns"][0]["blocks"]["memory"] == 100
            assert body["user_chars"] == 10
            # Remainder estimate flows through the handler: peak*4 - injected.
            assert body["estimated_other_chars"] == int(1000 * 4.0) - 110

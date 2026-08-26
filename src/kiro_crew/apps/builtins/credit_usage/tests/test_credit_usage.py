"""Tests for the credit-usage builtin app."""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kiro_crew.apps.discovery import discover_builtin_apps
from kiro_crew.apps.manifest import AppManifest

_APP_DIR = Path(__file__).resolve().parent.parent
_server = importlib.import_module("kiro_crew.apps.builtins.credit_usage.server")


def test_manifest_is_valid() -> None:
    mf = AppManifest.from_json_file(_APP_DIR / "app.json")
    mf.validate()
    assert mf.name == "credit-usage"


def test_app_is_discovered() -> None:
    names = {a["name"] for a in discover_builtin_apps()}
    assert "credit-usage" in names


def test_manifest_declares_no_missing_assets() -> None:
    # The app ships no /app-assets/* files, so it must not reference any (else
    # test_builtin_app_assets would fail). Guard that here too.
    mf = json.loads((_APP_DIR / "app.json").read_text())
    for field in (
        "iconUrl",
        "heroImage",
        "heroImageDark",
        "heroImageDetail",
        "heroImageDetailDark",
    ):
        assert field not in mf, f"{field} references an asset that isn't shipped"


def _row(ts: str, credits: float, *, slot: str = "chat-1", model: str = "",
         surface: str = "dashboard", agent: str = "kirocrew") -> dict:
    return {
        "_type": "tokens",
        "ts": ts,
        "slot": slot,
        "provider": "acp",
        "model": model,
        "credits": credits,
        "turns": 1,
        "surface": surface,
        "agent": agent,
        "context_used": 100,
        "context_window": 1000,
        "phase": "per_turn",
        "stop_reason": "end_turn",
    }


def test_summary_sums_credits_and_buckets_by_day() -> None:
    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    today = now.replace(hour=12, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    rows = [
        _row(today.isoformat(), 2.5, model="a"),
        _row(today.isoformat(), 1.5, model="b", surface="subagent"),
        _row(yesterday.isoformat(), 4.0, model="a"),
    ]
    s = _server._summary(rows, days=7, tz_offset_min=480)
    assert s["totals"]["allTimeCredits"] == 8.0
    assert s["totals"]["today"] == 4.0
    assert s["windowDays"] == 7
    # trend covers exactly `days` buckets, ending today
    assert len(s["trend"]) == 7
    assert s["trend"][-1]["credits"] == 4.0
    # breakdowns sum correctly and sort desc by credits
    models = {m["name"]: m["credits"] for m in s["byModel"]}
    assert models["a"] == 6.5 and models["b"] == 1.5
    assert s["byModel"][0]["credits"] >= s["byModel"][-1]["credits"]
    surfaces = {m["name"]: m["credits"] for m in s["bySurface"]}
    assert surfaces["dashboard"] == 6.5 and surfaces["subagent"] == 1.5


def test_recent_returns_newest_first_and_maps_fields() -> None:
    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    rows = [
        _row((now - timedelta(minutes=2)).isoformat(), 1.0, slot="chat-1"),
        _row(now.isoformat(), 2.0, slot="chat-2", model="", surface="cron"),
    ]
    out = _server._recent(rows, limit=10)
    assert out[0]["slot"] == "chat-2"  # newest first
    assert out[0]["credits"] == 2.0
    assert out[0]["model"] == "auto"  # empty model normalized
    assert out[0]["surface"] == "cron"
    assert out[1]["slot"] == "chat-1"


def test_empty_history_is_safe() -> None:
    s = _server._summary([], days=30, tz_offset_min=0)
    assert s["totals"]["allTimeCredits"] == 0.0
    assert len(s["trend"]) == 30
    assert s["topSessions"] == []
    assert _server._recent([], limit=10) == []


def test_normalize_slot_strips_prefixes() -> None:
    assert _server._normalize_slot("dashboard:chat-12-999") == "chat-12-999"
    assert _server._normalize_slot("dashboard_chat-12-999") == "chat-12-999"
    assert _server._normalize_slot("chat-12-999") == "chat-12-999"
    # subagent slots are left intact (no transcript to match)
    assert _server._normalize_slot("subagent:abcd1234") == "subagent:abcd1234"


def test_title_resolution_from_sessions_dir(tmp_path, monkeypatch) -> None:
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    # explicit metadata title
    (sdir / "dashboard_chat-12-999.jsonl").write_text(
        json.dumps({"_type": "metadata", "title": "Credit dashboard work"}) + "\n",
        encoding="utf-8",
    )
    # no metadata title -> first user message fallback
    (sdir / "dashboard_chat-7-111.jsonl").write_text(
        json.dumps({"role": "user", "content": "help me fix the build"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(_server, "_sessions_dir", lambda: sdir)
    _server._title_sig = None  # bust the module cache
    _server._title_map = {}

    assert _server._title_for_slot("chat-12-999") == "Credit dashboard work"
    assert _server._title_for_slot("dashboard:chat-12-999") == "Credit dashboard work"
    assert _server._title_for_slot("chat-7-111") == "help me fix the build"
    # unknown slot falls back to the slot itself
    assert _server._title_for_slot("chat-99-000") == "chat-99-000"
    # subagent slots keep their slot label
    assert _server._title_for_slot("subagent:abcd1234") == "subagent:abcd1234"

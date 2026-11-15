"""A usage row with no slot belongs to no session on the Context occupancy card.

Pooling such rows under one made-up ``unknown`` slot listed a session that never
existed, with a peak, a turn count and a model mixed from unrelated turns.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import kiro_crew.dashboard.handlers.usage as usage_mod
from kiro_crew.dashboard.handlers import usage_context
from kiro_crew.testing.clock import ManualClock

#: One frozen instant: the rows' ``ts`` and the reader's window agree on it.
_NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def test_rows_with_no_slot_make_no_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / f"{_NOW:%Y-%m-%d}.jsonl"
    row = {"_type": "tokens", "ts": _NOW.isoformat(), "context_window": 200000}
    rows = [
        {**row, "context_used": 190000, "model": "a"},
        {**row, "slot": "", "context_used": 180000, "model": "b"},
        {**row, "slot": "chat-1-1", "context_used": 50000, "model": "m"},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
    monkeypatch.setattr(usage_mod, "_shards_in_window", lambda *a, **k: [path])
    monkeypatch.setattr(usage_mod, "_CONTEXT_CACHE", None)
    monkeypatch.setattr(usage_mod, "_CONTEXT_CACHE_KEY", None)
    ManualClock(start=_NOW.timestamp()).install(monkeypatch, usage_context, time=True)

    block = usage_mod.context_occupancy(7)

    assert [s["slot"] for s in block["sessions"]] == ["chat-1-1"]
    assert block["turns"] == 1

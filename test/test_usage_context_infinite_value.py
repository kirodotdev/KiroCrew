"""An infinite context value in a usage row reads as 0 instead of failing the reader.

``json.loads`` reads ``Infinity`` and ``1e999`` as ``float('inf')``, and ``int`` refuses
it with ``OverflowError``. ``_coerce_int`` is the one helper the context readers and the
row writer go through, so it answers 0 and the rest of the window is still counted.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import kiro_crew.dashboard.handlers.usage as usage_mod
from kiro_crew.dashboard.handlers import usage_context
from kiro_crew.dashboard.handlers.usage_store import _coerce_int
from kiro_crew.testing.clock import ManualClock

#: One frozen instant: the rows' ``ts`` and the reader's window agree on it.
_NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def _coerced(value: object) -> int | None:
    """``_coerce_int(value)``, or ``None`` when it raises instead of answering."""
    try:
        return _coerce_int(value)
    except OverflowError:
        return None


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_a_non_finite_value_coerces_to_zero(value: float) -> None:
    assert _coerced(value) == 0


def test_context_occupancy_keeps_the_window_when_one_row_is_infinite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / f"{_NOW:%Y-%m-%d}.jsonl"
    row = {"_type": "tokens", "ts": _NOW.isoformat(), "slot": "chat-1-1", "model": "m"}
    lines = [
        # The bare token json.dumps writes for inf, as an older shard can hold.
        json.dumps({**row, "context_window": 200000}).replace(
            '"context_window"', '"context_used": Infinity, "context_window"'
        ),
        json.dumps({**row, "context_used": 50000, "context_window": 200000}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
    monkeypatch.setattr(usage_mod, "_shards_in_window", lambda *a, **k: [path])
    monkeypatch.setattr(usage_mod, "_CONTEXT_CACHE", None)
    monkeypatch.setattr(usage_mod, "_CONTEXT_CACHE_KEY", None)
    ManualClock(start=_NOW.timestamp()).install(monkeypatch, usage_context, time=True)

    # The card is missing when the read raises, which is what the panel shows.
    try:
        block = usage_mod.context_occupancy(7)
    except OverflowError:
        block = None

    assert block is not None and block["turns"] == 1

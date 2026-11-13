"""A usage row whose number fields are not finite numbers is read as no number.

A null, a string, a list or an int wider than a double in one row must not raise
out of the token history (and with it ``/api/usage/kiro``, the whole Usage page),
the Telemetry cost breakdown or ``slot_spend`` (the session memory page). Each
reader reads such a field as 0, or skips the row, and keeps the rest.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import kiro_crew.dashboard.handlers.usage as usage_mod
from kiro_crew.dashboard.handlers import usage_cost, usage_history, usage_spend
from kiro_crew.testing.clock import ManualClock

_SLOT = "chat-1-1700000000"
#: One frozen instant: the rows' ``ts`` and every reader's window agree on it.
_NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)

_BAD_VALUES = [None, "12", [1], {"n": 1}, 10**400]


def _id(value: object) -> str:
    """A short test id: the huge int's repr is four hundred digits."""
    return "huge-int" if isinstance(value, int) and value > 2**64 else repr(value)


def _row(**fields: Any) -> dict[str, Any]:
    return {"_type": "tokens", "ts": _NOW.isoformat(), "slot": _SLOT, "model": "m", **fields}


@pytest.fixture
def shard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / f"{_NOW:%Y-%m-%d}.jsonl"
    monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
    monkeypatch.setattr(usage_mod, "_shards_in_window", lambda *a, **k: [path])
    clock = ManualClock(start=_NOW.timestamp())
    for module in (usage_history, usage_cost, usage_spend):
        clock.install(monkeypatch, module, time=True)
    monkeypatch.setattr(usage_mod, "_TOKEN_CACHE", {})
    monkeypatch.setattr(usage_mod, "_TOKEN_CACHE_KEY", None)
    monkeypatch.setattr(usage_mod, "_COST_CACHE", None)
    monkeypatch.setattr(usage_mod, "_COST_CACHE_KEY", None)
    monkeypatch.setattr(usage_mod, "_SLOT_SPEND_CACHE_SIG", ())

    def write(*rows: dict[str, Any]) -> None:
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    return write


@pytest.mark.parametrize("bad", _BAD_VALUES, ids=_id)
@pytest.mark.parametrize("field", ["input", "output", "cache_create", "cache_read", "cost"])
def test_the_token_history_reads_a_bad_field_as_zero(shard, field: str, bad: object) -> None:
    shard(_row(**{field: bad}), _row(input=10, output=5, cost=0.25))
    history = usage_mod._parse_token_history()
    assert (history["total_input"], history["total_output"]) == (10, 5)
    assert history["cost_usd"] == 0.25


@pytest.mark.parametrize("bad", ["x", [1], 10**400], ids=_id)
def test_the_cost_breakdown_skips_a_row_whose_credits_are_not_a_number(shard, bad) -> None:
    shard(_row(credits=bad), _row(credits=2.5))
    block = usage_mod.cost_breakdown(7)
    assert (block["credits"], block["turns"]) == (2.5, 1)


@pytest.mark.parametrize("bad", ["x", True, 10**400], ids=_id)
def test_slot_spend_skips_a_row_whose_credits_are_not_a_number(shard, bad) -> None:
    shard(_row(credits=bad), _row(credits=2.5))
    spend = usage_mod.slot_spend()
    assert spend[f"dashboard:{_SLOT}"] == {"credits": 2.5, "turns": 1}

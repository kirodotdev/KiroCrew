"""Spend recommendations derived from the token row store.

The guarded failure is an advisory that is not a measurement: every share on the
list has to be recomputable from rows the store already holds, and a rule whose
inputs are missing or too thin stays off the list instead of showing a guess.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from kiro_crew.dashboard.handlers import usage as usage_mod


def _row(*, slot="chat-1-1", model="m", credits=1.0, used=0, age_days=0.0):
    ts = datetime.now(timezone.utc) - timedelta(days=age_days)
    return {
        "_type": "tokens",
        "ts": ts.isoformat(),
        "slot": slot,
        "model": model,
        "credits": credits,
        "context_used": used,
        "context_window": 1_000_000,
        "surface": "dashboard",
        "agent": "kirocrew",
        "provider": "acp",
    }


@pytest.fixture(autouse=True)
def store(tmp_path, _floor_monkeypatch):
    """Point the aggregator at a throwaway row store; returns the writer."""

    def _write(rows):
        d = tmp_path / "usage" / ("to" + "kens")
        d.mkdir(parents=True, exist_ok=True)
        shard = d / "shard.jsonl"
        shard.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        _floor_monkeypatch.setattr(usage_mod, "_shards_in_window", lambda days, until=None: [shard])
        _floor_monkeypatch.setattr(usage_mod, "_COST_CACHE", None)
        _floor_monkeypatch.setattr(usage_mod, "_COST_CACHE_KEY", None)
        return shard

    return _write


def _advice(rows, store):
    store(rows)
    return {a["id"]: a for a in usage_mod.cost_breakdown(7)["advice"]}


def test_the_share_of_credits_on_turns_above_600k_is_measured_from_the_rows(store):
    advice = _advice(
        [
            _row(credits=31.0, used=650_000),
            _row(credits=69.0, used=100_000),
        ],
        store,
    )
    assert advice["high_context"]["share_pct"] == 31.0
    assert advice["high_context"]["tokens"] == 600_000


def test_a_turn_below_the_line_or_with_no_occupancy_is_not_counted(store):
    advice = _advice(
        [
            _row(credits=50.0, used=599_999),
            _row(credits=50.0, used=0),
        ],
        store,
    )
    assert "high_context" not in advice


def test_a_small_share_stays_off_the_list(store):
    advice = _advice(
        [_row(credits=5.0, used=700_000), _row(credits=95.0, used=100_000)],
        store,
    )
    assert advice == {}


def test_the_costly_model_needs_a_measured_cheaper_one_with_enough_turns(store):
    rows = [_row(model="big", credits=4.0) for _ in range(5)] + [
        _row(model="small", credits=1.0) for _ in range(5)
    ]
    advice = _advice(rows, store)
    entry = advice["costly_model"]
    assert entry["model"] == "big"
    assert entry["cheaper_model"] == "small"
    assert entry["ratio"] == 4.0
    assert entry["share_pct"] == 80.0


def test_a_model_seen_on_too_few_turns_is_not_compared(store):
    rows = [_row(model="big", credits=4.0) for _ in range(5)] + [
        _row(model="small", credits=1.0) for _ in range(4)
    ]
    assert "costly_model" not in _advice(rows, store)


def test_a_modest_price_gap_is_not_advice(store):
    rows = [_row(model="big", credits=1.5) for _ in range(5)] + [
        _row(model="small", credits=1.0) for _ in range(5)
    ]
    assert "costly_model" not in _advice(rows, store)


def test_an_unnamed_model_is_never_the_one_recommended_or_compared(store):
    rows = [_row(model="", credits=9.0) for _ in range(5)] + [
        _row(model="small", credits=1.0) for _ in range(5)
    ]
    assert "costly_model" not in _advice(rows, store)


def test_a_subagent_row_reaches_no_recommendation(store):
    rows = [_row(slot=f"subagent:{i:08x}", credits=50.0, used=700_000) for i in range(4)] + [
        _row(credits=1.0, used=10_000)
    ]
    store(rows)
    d = usage_mod.cost_breakdown(7)
    assert d["advice"] == []
    assert "subagent" not in json.dumps(d).lower()


def test_the_list_is_ordered_by_share_and_empty_for_a_window_with_no_spend(store):
    rows = [_row(model="big", credits=4.0, used=700_000) for _ in range(5)] + [
        _row(model="small", credits=1.0, used=100_000) for _ in range(5)
    ]
    store(rows)
    shares = [a["share_pct"] for a in usage_mod.cost_breakdown(7)["advice"]]
    assert shares == sorted(shares, reverse=True)
    assert len(shares) == 2
    store([_row(credits=0.0, used=700_000)])
    assert usage_mod.cost_breakdown(7)["advice"] == []


def test_the_share_does_not_depend_on_the_billing_unit(store):
    rows = [
        {**_row(credits=0.0, used=650_000), "cost": 3.0},
        {**_row(credits=0.0, used=100_000), "cost": 7.0},
    ]
    store(rows)
    d = usage_mod.cost_breakdown(7)
    assert d["unit"] == "usd"
    assert {a["id"]: a["share_pct"] for a in d["advice"]} == {"high_context": 30.0}


def test_a_mixed_window_measures_each_card_over_its_own_unit(store):
    """A window billed in both units carries one list per card, each over its own turns."""
    rows = [
        _row(credits=30.0, used=700_000),
        _row(credits=70.0, used=100_000),
        {**_row(credits=0.0, used=100_000), "cost": 1.0},
        {**_row(credits=0.0, used=100_000), "cost": 1.0},
    ]
    store(rows)
    d = usage_mod.cost_breakdown(7)
    assert d["unit"] == "credits"
    assert {a["id"]: a["share_pct"] for a in d["advice"]} == {"high_context": 30.0}
    assert d["usd"]["unit"] == "usd"
    assert d["usd"]["advice"] == []

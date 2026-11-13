"""Bounds on the numbers ``POST /api/memory/promote`` and ``PUT /api/memory/semantic`` read.

Promotion tombstones every row of a cluster it promotes, so a ``min_sim`` at or
below 0 or a ``min_count`` below 2 retired rows nobody asked to fold.
A semantic write stored ``confidence`` as sent (-3, 99, NaN), and a JSON
integer too large for a float raised ``OverflowError`` out of the request.
These tests drive the real handlers with a mocked store and pin what reaches it.
"""

from __future__ import annotations

import json
import logging
import math
from unittest.mock import MagicMock

import pytest
from handlers_memory_coverage_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _body,
    _make_request,
    _make_state,
    _reset_setup_status,
    _store,
)

import kiro_crew.dashboard.handlers.memory as mem_mod

# A JSON integer too large for a float. The parameters spell it ``HUGE`` so
# their test ids stay short; ``_loads`` expands it before parsing.
_HUGE_INT = "1" + "0" * 400


def _loads(raw: str) -> dict:
    return json.loads(raw.replace("HUGE", _HUGE_INT))


async def _promote(raw: str) -> tuple[int, MagicMock]:
    store = _store(promote_episodic_patterns=MagicMock(return_value=0))
    state = _make_state(vector_store=store)
    req = _make_request(state, method="POST", json_body=_loads(raw), session_key="dashboard:ui")
    resp = await mem_mod.api_memory_promote(req)
    return resp.status, store.promote_episodic_patterns


async def _write_confidence(raw: str) -> tuple[int, MagicMock]:
    store = _store()
    store.set_semantic = MagicMock(return_value=None)
    state = _make_state(vector_store=store)
    body = {"key": "k", "value": "v", **_loads(raw)}
    req = _make_request(state, method="PUT", json_body=body, session_key="dashboard:ui")
    resp = await mem_mod.api_memory_semantic_write(req)
    return resp.status, store.set_semantic


class TestPromoteBounds:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # A cluster of one is every row; the floor is 2.
            ('{"min_count": 1}', (2, 0.75)),
            ('{"min_count": 0}', (2, 0.75)),
            ('{"min_count": -2}', (2, 0.75)),
            ('{"min_count": -HUGE}', (2, 0.75)),
            ('{"min_count": true}', (2, 0.75)),
            # At or below 0: the default threshold, never "merge all".
            ('{"min_sim": 0}', (5, 0.75)),
            ('{"min_sim": -0.5}', (5, 0.75)),
            ('{"min_sim": -1e999}', (5, 0.75)),
        ],
    )
    async def test_unsafe_threshold_is_bounded(
        self, raw: str, expected: tuple[int, float], caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.handlers.memory"):
            status, promote = await _promote(raw)
        assert status == 200
        promote.assert_called_once_with(*expected)
        assert any("memory promote:" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("{}", (5, 0.75)),
            ('{"min_count": 2, "min_sim": 1}', (2, 1.0)),
            ('{"min_count": 3, "min_sim": 0.01}', (3, 0.01)),
            ('{"min_count": "4", "min_sim": "0.9"}', (4, 0.9)),
            # Above 1 already merges nothing; lowering it to 1 could merge
            # float32 near-duplicates whose dot product rounds above 1.
            ('{"min_sim": 1.5}', (5, 1.5)),
            ('{"min_sim": 1e999}', (5, float("inf"))),
        ],
    )
    async def test_threshold_in_bounds_is_forwarded_unchanged(
        self, raw: str, expected: tuple[int, float], caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.handlers.memory"):
            status, promote = await _promote(raw)
        assert status == 200
        promote.assert_called_once_with(*expected)
        assert not any("memory promote:" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw", ['{"min_sim": NaN}', '{"min_sim": "nan"}'])
    async def test_nan_min_sim_is_forwarded_unchanged(self, raw: str) -> None:
        # ``sim > nan`` is never true, so NaN merges no row, and the min_count
        # floor of 2 then promotes nothing: forwarding it is as safe as main.
        status, promote = await _promote(raw)
        assert status == 200
        min_count, min_sim = promote.call_args.args
        assert min_count == 5
        assert math.isnan(min_sim)

    @pytest.mark.asyncio
    async def test_non_numeric_threshold_is_400(self) -> None:
        status, promote = await _promote('{"min_sim": "high"}')
        assert status == 400
        promote.assert_not_called()


class TestSemanticConfidenceBounds:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ('{"confidence": -3}', 0.0),
            ('{"confidence": 99}', 1.0),
            ('{"confidence": -0.25}', 0.0),
            ('{"confidence": 1.5}', 1.0),
            ('{"confidence": 1e999}', 1.0),
            ('{"confidence": -1e999}', 0.0),
            # Too large for a float: float() raised OverflowError into a 500.
            ('{"confidence": HUGE}', 1.0),
            ('{"confidence": -HUGE}', 0.0),
            # NaN is not a confidence: the default a missing field gets.
            ('{"confidence": NaN}', 1.0),
        ],
    )
    async def test_out_of_range_confidence_is_clamped(self, raw: str, expected: float) -> None:
        status, set_semantic = await _write_confidence(raw)
        assert status == 200
        stored = set_semantic.call_args.args[2]
        assert stored == expected
        assert type(stored) is float

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("{}", 1.0),
            ('{"confidence": 0}', 0.0),
            ('{"confidence": 1}', 1.0),
            ('{"confidence": 0.42}', 0.42),
            # Booleans keep what main stored for them: float(true/false).
            ('{"confidence": true}', 1.0),
            ('{"confidence": false}', 0.0),
            ('{"confidence": "0.42"}', 1.0),
        ],
    )
    async def test_valid_or_absent_confidence_is_unchanged(self, raw: str, expected: float) -> None:
        status, set_semantic = await _write_confidence(raw)
        assert status == 200
        assert set_semantic.call_args.args[2] == expected

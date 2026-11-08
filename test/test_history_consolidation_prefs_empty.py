"""A preference-only pass with no usable answer keeps its window pending.

``maybe_consolidate`` advances the in-memory ``_prefs_offset`` once the
preference-only pass it scheduled completes. A pass whose turn came back with no
reply or an unparseable one, or could not be dispatched, extracted nothing, so it returns
``_CONSOLIDATION_EMPTY`` instead of the ``None`` a completed pass returns: the
offset stays put, the attempt cap of the history span is not charged, and the
same window is retried after an in-memory backoff that widens on each failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from history_consolidation_retry_helpers import KEY, _make_consolidator, _seed_log

from kiro_crew import history as history_mod
from kiro_crew import history_consolidation as consolidation_facade
from kiro_crew.history_consolidation import (
    _CONSOLIDATION_BACKOFF_BASE_SECS,
    _CONSOLIDATION_BACKOFF_MAX_SECS,
    _CONSOLIDATION_EMPTY,
    _CONSOLIDATION_REFUSED,
    _ConsolidationNotDispatched,
    _ConsolidationReplyUnusable,
    _no_pass_ran,
)
from kiro_crew.llm_helpers import parse_llm_json

THRESHOLD = history_mod._CONSOLIDATION_THRESHOLD
BASE = _CONSOLIDATION_BACKOFF_BASE_SECS
CEILING = _CONSOLIDATION_BACKOFF_MAX_SECS


class _Clock:
    """The ``time`` module with a ``time()`` the test sets, so deadlines are exact."""

    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now

    def __getattr__(self, name: str):
        return getattr(time, name)


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    fake = _Clock(1_000_000.0)
    monkeypatch.setattr(consolidation_facade, "_time", fake)
    return fake


async def _run_threshold(c) -> None:
    c.maybe_consolidate(KEY)
    await asyncio.gather(*list(c._tasks), return_exceptions=True)


def _expire_prefs_backoff(c, clock: _Clock) -> None:
    clock.now = c._prefs_retry[KEY][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["none", "unusable"])
async def test_an_empty_prefs_pass_returns_its_own_sentinel(tmp_path, failure):
    log = _seed_log(tmp_path, count=THRESHOLD)
    c = _make_consolidator(log)
    if failure == "none":
        call = AsyncMock(return_value=None)
    else:
        call = AsyncMock(side_effect=_ConsolidationReplyUnusable("not JSON"))

    with patch.object(c, "_call_llm", call) as spy:
        outcome = await c._consolidate(KEY, include_history=False)

    spy.assert_awaited_once()
    assert outcome is _CONSOLIDATION_EMPTY
    assert outcome is not _CONSOLIDATION_REFUSED
    assert _no_pass_ran(outcome)
    # The history span's attempt cap is not charged by a preference-only pass.
    assert log.consolidation_retry_state(KEY) == (0, 0.0)
    assert log.unconsolidated_count(KEY) == THRESHOLD


@pytest.mark.asyncio
async def test_a_parsed_empty_answer_is_a_completed_prefs_pass(tmp_path, clock):
    """The prefs-only prompt asks the model to omit unchanged keys, so ``{}`` means
    nothing changed: the window is done and no backoff is armed."""
    log = _seed_log(tmp_path, count=THRESHOLD)
    c = _make_consolidator(log)

    with patch.object(c, "_call_llm", AsyncMock(return_value={})) as spy:
        assert await c._consolidate(KEY, include_history=False) is None
        await _run_threshold(c)

    assert spy.await_count == 2
    assert c._prefs_offset.get(KEY) == THRESHOLD
    assert KEY not in c._prefs_retry


@contextlib.asynccontextmanager
async def _fake_turn(_sessions, **_kwargs):
    yield SimpleNamespace(served_model="m")


@pytest.mark.asyncio
async def test_a_streamed_empty_object_completes_the_prefs_pass(tmp_path, clock):
    """``{}`` read off the turn reaches ``_consolidate`` through the real
    ``_call_llm``, which must not drop the falsy dict as an unusable reply."""
    log = _seed_log(tmp_path, count=THRESHOLD)
    c = _make_consolidator(log, sessions=object())
    collect = AsyncMock(return_value=parse_llm_json("{}"))

    with (
        patch.object(consolidation_facade, "background_turn", _fake_turn),
        patch.object(history_mod, "stream_and_collect_json", collect),
    ):
        assert await c._call_llm("p") == {}
        await _run_threshold(c)

    assert c._prefs_offset.get(KEY) == THRESHOLD
    assert KEY not in c._prefs_retry


@pytest.mark.asyncio
async def test_an_undispatched_prefs_pass_returns_the_empty_sentinel(tmp_path):
    log = _seed_log(tmp_path, count=THRESHOLD)
    c = _make_consolidator(log)

    with patch.object(c, "_call_llm", AsyncMock(side_effect=_ConsolidationNotDispatched("no cli"))):
        outcome = await c._consolidate(KEY, include_history=False)

    assert outcome is _CONSOLIDATION_EMPTY
    assert log.consolidation_retry_state(KEY) == (0, 0.0)


@pytest.mark.asyncio
async def test_an_empty_history_pass_still_returns_none(tmp_path):
    """The history path keeps its durable accounting and its return value."""
    log = _seed_log(tmp_path, count=THRESHOLD)
    c = _make_consolidator(log)

    with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
        outcome = await c._consolidate(KEY, include_history=True)

    assert outcome is None
    assert log.consolidation_retry_state(KEY)[0] == 1


@pytest.mark.asyncio
async def test_an_empty_result_does_not_advance_the_prefs_offset(tmp_path, clock):
    log = _seed_log(tmp_path, count=THRESHOLD)
    c = _make_consolidator(log)

    with patch.object(c, "_call_llm", AsyncMock(return_value=None)) as spy:
        await _run_threshold(c)

    spy.assert_awaited_once()
    assert KEY not in c._prefs_offset, "an empty pass marked its window consolidated"
    assert KEY not in c._running
    assert c._prefs_retry[KEY] == (1, clock.now + BASE)


@pytest.mark.asyncio
async def test_an_empty_result_backs_off_then_retries_the_same_window(tmp_path, clock):
    log = _seed_log(tmp_path, count=THRESHOLD)
    c = _make_consolidator(log)
    with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
        await _run_threshold(c)

    # Inside the backoff the next turn schedules nothing, so a prompt that
    # fails every time is not re-sent on every turn.
    clock.now += BASE - 1
    with patch.object(c, "_call_llm", AsyncMock(return_value={"noop": True})) as spy:
        c.maybe_consolidate(KEY)
        assert not c._tasks, "a backed-off prefs window still scheduled a task"
    spy.assert_not_awaited()

    # Once it expires the SAME window is retried, with no new messages needed.
    _expire_prefs_backoff(c, clock)
    with patch.object(c, "_call_llm", AsyncMock(return_value={"noop": True})) as spy:
        await _run_threshold(c)

    spy.assert_awaited_once()
    assert c._prefs_offset.get(KEY) == THRESHOLD
    assert KEY not in c._prefs_retry, "a completed pass left the prefs backoff armed"


@pytest.mark.asyncio
async def test_consecutive_empty_results_widen_the_backoff_up_to_its_ceiling(tmp_path, clock):
    log = _seed_log(tmp_path, count=THRESHOLD)
    c = _make_consolidator(log)

    with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
        for failures in (1, 2, 3):
            started = clock.now
            await _run_threshold(c)
            assert c._prefs_retry[KEY] == (failures, started + BASE * 2 ** (failures - 1))
            _expire_prefs_backoff(c, clock)

        c._prefs_retry[KEY] = (64, clock.now)
        await _run_threshold(c)
    assert c._prefs_retry[KEY] == (65, clock.now + CEILING)
    assert KEY not in c._prefs_offset


@pytest.mark.asyncio
async def test_an_undispatched_prefs_pass_does_not_advance_the_prefs_offset(tmp_path):
    log = _seed_log(tmp_path, count=THRESHOLD)
    c = _make_consolidator(log)

    with patch.object(c, "_call_llm", AsyncMock(side_effect=_ConsolidationNotDispatched("no cli"))):
        await _run_threshold(c)

    assert KEY not in c._prefs_offset
    assert c._prefs_retry[KEY][0] == 1

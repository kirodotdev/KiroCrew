"""A preference-only pass the live chat outran does not restart on every turn.

A pass reads the window, the model answers, and the write boundary discards the
answer when a newer user row arrived meanwhile ("conversation changed during
extraction"). In a crewmate DM the person nearly always speaks again first, so
without a wait every later turn started (and billed) another pass that ended the
same way: 6 passes in 2 min, 5 discarded, in the R12 bench. The discard now
returns ``_CONSOLIDATION_STALE`` and the threshold caller waits
``_CONSOLIDATION_STALE_RETRY_SECS`` before the next turn may start one.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest
from history_consolidation_retry_helpers import KEY, _make_consolidator, _seed_log

from kiro_crew import history as history_mod
from kiro_crew import history_consolidation as consolidation_facade

THRESHOLD = history_mod._CONSOLIDATION_THRESHOLD


class _Clock:
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


def _append(log, role: str, text: str) -> None:
    with history_mod.allow_on_loop_persist():
        log.append(KEY, role, text)


async def _turn(c, log, i: int) -> None:
    """One finished chat turn: the reply lands, then the turn-end hook runs."""
    _append(log, "assistant", f"reply {i}")
    c.maybe_consolidate(KEY)
    await asyncio.gather(*list(c._tasks), return_exceptions=True)


def _outran_by_the_user(log):
    """A model call during which the person sends the next message."""
    sent = {"n": 0}

    async def call(*_args, **_kwargs):
        sent["n"] += 1
        _append(log, "user", f"next message {sent['n']}")
        return {"preferences": "likes tea"}

    return AsyncMock(side_effect=call)


@pytest.mark.asyncio
async def test_a_busy_chat_starts_one_pass_per_wait_not_one_per_turn(tmp_path, clock):
    log = _seed_log(tmp_path, count=THRESHOLD)
    c = _make_consolidator(log)
    call = _outran_by_the_user(log)

    with patch.object(c, "_call_llm", call):
        # Six turns, one every 20 s: the R12 bench pace (2 min).
        for i in range(6):
            await _turn(c, log, i)
            clock.now += 20

    assert call.await_count == 1
    # Nothing was written: the window stays pending for the next attempt.
    assert c._prefs_offset.get(KEY, 0) == 0


@pytest.mark.asyncio
async def test_the_window_is_tried_again_once_the_wait_is_over(tmp_path, clock):
    log = _seed_log(tmp_path, count=THRESHOLD)
    c = _make_consolidator(log)
    call = _outran_by_the_user(log)

    with patch.object(c, "_call_llm", call):
        await _turn(c, log, 0)
        assert call.await_count == 1
        failures, retry_at = c._prefs_retry[KEY]
        # Not a failure: the backoff count of an empty answer is not widened.
        assert failures == 0
        assert retry_at == clock.now + consolidation_facade._CONSOLIDATION_STALE_RETRY_SECS

    clock.now = retry_at
    with patch.object(c, "_call_llm", AsyncMock(return_value={})) as quiet:
        await _turn(c, log, 1)

    quiet.assert_awaited_once()
    assert c._prefs_offset.get(KEY, 0) > THRESHOLD
    assert KEY not in c._prefs_retry

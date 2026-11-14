"""A turn that reaches a provider releases the environment backoff it made stale.

An environment failure (nothing dispatched, nothing billed) widens a session's
retry deadline up to a day so a broken host does not retry every tick. Once a
consolidation turn reaches a provider again, that deadline is stale, and the
sessions refused while the host was broken must become eligible right away
instead of waiting out the rest of their day.

Shared helpers are in ``history_consolidation_retry_helpers.py``.
"""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest
from history_consolidation_retry_helpers import (
    KEY,
    _make_consolidator,
    _off_loop,
    _seed_log,
    _span,
)

from kiro_crew import history as history_mod
from kiro_crew.history import (
    _CONSOLIDATION_BACKOFF_BASE_SECS,
    _CONSOLIDATION_BACKOFF_MAX_SECS,
)
from kiro_crew.history_consolidation_span import _ConsolidationReplyUnusable

#: These tests start background helper turns, which are skipped while the host's
#: memory posture is critical (see ``ample_host_free_memory``).
pytestmark = pytest.mark.usefixtures("ample_host_free_memory")

OTHER = "dashboard:chat-other"


async def _not_dispatched(c, key: str) -> None:
    with patch.object(
        c,
        "_call_llm",
        AsyncMock(side_effect=history_mod._ConsolidationNotDispatched("lite spec refused")),
    ):
        await c._consolidate(key, include_history=True)


async def _drain(c) -> None:
    while c._tasks:
        await asyncio.gather(*list(c._tasks), return_exceptions=True)
        # A gather of done tasks does not yield; let their _tasks.discard run.
        await asyncio.sleep(0)


def test_drain_returns_when_a_finished_task_still_waits_for_its_discard(monkeypatch, tmp_path):
    """A finished task leaves ``_tasks`` on the loop's next pass, so ``_drain`` must yield."""
    real_gather = asyncio.gather
    calls: list[int] = []

    def counted(*aws, **kw):
        calls.append(1)
        assert len(calls) < 50, "_drain spun without yielding to the loop"
        return real_gather(*aws, **kw)

    async def go() -> None:
        async def noop() -> None:
            return None

        c = _make_consolidator(_seed_log(tmp_path))
        task = asyncio.get_running_loop().create_task(noop())
        c._tasks.add(task)
        task.add_done_callback(c._tasks.discard)
        await asyncio.sleep(0)  # the task finishes; its discard is queued, not run
        monkeypatch.setattr(asyncio, "gather", counted)
        await _drain(c)

    asyncio.run(go())


class TestClearingOneSession:
    @pytest.mark.asyncio
    async def test_an_environment_backoff_is_released(self, tmp_path):
        log = _seed_log(tmp_path)
        for _ in range(8):
            await _off_loop(
                lambda: log.record_consolidation_environment_failure(
                    KEY, _CONSOLIDATION_BACKOFF_BASE_SECS, _CONSOLIDATION_BACKOFF_MAX_SECS
                )
            )
        assert log.consolidation_retry_state(KEY)[1] > time.time() + 3600

        assert await _off_loop(lambda: log.clear_consolidation_environment_backoff(KEY)) is True

        attempts, retry_at = log.consolidation_retry_state(KEY)
        assert (attempts, retry_at) == (0, 0.0)
        meta = log.get_metadata(KEY)
        # The count is kept so a session that fails again resumes widening.
        assert meta.get("consolidation_env_failures") == 8
        assert meta.get("consolidation_env_released") is True
        assert log.unconsolidated_count(KEY) == 3, "the release retired unread messages"

    @pytest.mark.asyncio
    async def test_a_session_is_released_once_until_it_consolidates(self, tmp_path):
        log = _seed_log(tmp_path)

        def _fail():
            return log.record_consolidation_environment_failure(
                KEY, _CONSOLIDATION_BACKOFF_BASE_SECS, _CONSOLIDATION_BACKOFF_MAX_SECS
            )

        await _off_loop(_fail)
        assert await _off_loop(lambda: log.clear_consolidation_environment_backoff(KEY)) is True
        # The same session fails again on its own: the next release skips it,
        # and its wait widens from the kept count instead of restarting.
        failures, retry_at = await _off_loop(_fail)
        assert failures == 2
        assert await _off_loop(lambda: log.clear_consolidation_environment_backoff(KEY)) is False
        assert log.consolidation_retry_state(KEY)[1] == retry_at

        # A successful mark clears the accounting, so a later outage is released again.
        total = log.consolidation_counts(KEY)[0]
        await _off_loop(lambda: log.mark_consolidated(KEY, total))
        assert "consolidation_env_released" not in log.get_metadata(KEY)

    @pytest.mark.asyncio
    async def test_a_billed_count_from_a_gone_span_does_not_hold_it(self, tmp_path):
        log = _seed_log(tmp_path)
        await _off_loop(
            lambda: log.record_consolidation_failure(
                KEY, _CONSOLIDATION_BACKOFF_BASE_SECS, _CONSOLIDATION_BACKOFF_MAX_SECS, _span(log)
            )
        )
        # A rotation moves the generation, so that charge describes a span that is gone.
        generation = log.rotation_generation(KEY) + 1
        await _off_loop(lambda: log.update_metadata(KEY, {"rotation_generation": generation}))
        await _off_loop(
            lambda: log.record_consolidation_environment_failure(
                KEY, _CONSOLIDATION_BACKOFF_BASE_SECS, _CONSOLIDATION_BACKOFF_MAX_SECS
            )
        )
        assert log.consolidation_retry_state(KEY)[0] == 0

        assert await _off_loop(lambda: log.clear_consolidation_environment_backoff(KEY)) is True
        assert log.consolidation_retry_state(KEY)[1] == 0.0

    @pytest.mark.asyncio
    async def test_a_billed_backoff_holds(self, tmp_path):
        log = _seed_log(tmp_path)
        await _off_loop(
            lambda: log.record_consolidation_environment_failure(
                KEY, _CONSOLIDATION_BACKOFF_BASE_SECS, _CONSOLIDATION_BACKOFF_MAX_SECS
            )
        )
        await _off_loop(
            lambda: log.record_consolidation_failure(
                KEY, _CONSOLIDATION_BACKOFF_BASE_SECS, _CONSOLIDATION_BACKOFF_MAX_SECS, _span(log)
            )
        )
        before = log.consolidation_retry_state(KEY)

        assert await _off_loop(lambda: log.clear_consolidation_environment_backoff(KEY)) is False

        assert log.consolidation_retry_state(KEY) == before
        assert before[1] > time.time()

    @pytest.mark.asyncio
    async def test_a_session_without_an_environment_failure_is_untouched(self, tmp_path):
        log = _seed_log(tmp_path)
        assert await _off_loop(lambda: log.clear_consolidation_environment_backoff(KEY)) is False
        assert "consolidation_env_failures" not in log.get_metadata(KEY)

    @pytest.mark.asyncio
    async def test_a_deleted_session_is_not_resurrected(self, tmp_path):
        log = _seed_log(tmp_path)
        log._path(KEY).unlink()
        assert await _off_loop(lambda: log.clear_consolidation_environment_backoff(KEY)) is False
        assert not log._path(KEY).exists()


class TestAReachedProviderReleasesTheBackoff:
    @pytest.mark.asyncio
    async def test_sessions_refused_while_the_host_was_broken_become_eligible(self, tmp_path):
        log = _seed_log(tmp_path)
        with history_mod.allow_on_loop_persist():
            for i in range(3):
                log.append(OTHER, "user", f"o{i}")
        c = _make_consolidator(log)
        c._last_activity[KEY] = time.time()
        c._last_activity[OTHER] = time.time()

        await _not_dispatched(c, KEY)
        assert c.retry_eligible(KEY) is False
        await _drain(c)

        # A turn for another session reaches the provider and answers nothing usable.
        with patch.object(
            c,
            "_call_llm",
            AsyncMock(side_effect=_ConsolidationReplyUnusable("not JSON")),
        ):
            await c._consolidate(OTHER, include_history=True)
        await _drain(c)

        assert c.retry_eligible(KEY) is True
        assert log.unconsolidated_count(KEY) == 3
        # The session whose own turn was billed keeps its billed backoff.
        assert c.retry_eligible(OTHER) is False

    @pytest.mark.asyncio
    async def test_the_release_runs_once_per_recovery(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        c._last_activity[KEY] = time.time()

        calls = []
        real = log.clear_consolidation_environment_backoff

        def _counting(key):
            calls.append(key)
            return real(key)

        with patch.object(log, "clear_consolidation_environment_backoff", _counting):
            # The startup release, then nothing until another environment failure.
            c._release_environment_backoff()
            await _drain(c)
            c._release_environment_backoff()
            await _drain(c)
            assert calls == [KEY]

            await _not_dispatched(c, KEY)
            c._release_environment_backoff()
            await _drain(c)
            assert calls == [KEY, KEY]

    @pytest.mark.asyncio
    async def test_a_turn_that_raised_inside_the_stream_releases_nothing(self, tmp_path):
        """``_call_llm`` returns ``None`` for a turn that raised: no proof of a provider."""
        log = _seed_log(tmp_path)
        with history_mod.allow_on_loop_persist():
            for i in range(3):
                log.append(OTHER, "user", f"o{i}")
        c = _make_consolidator(log)
        c._last_activity[KEY] = time.time()
        c._last_activity[OTHER] = time.time()
        await _not_dispatched(c, KEY)
        await _drain(c)

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(OTHER, include_history=True)
        await _drain(c)

        assert c.retry_eligible(KEY) is False

    @pytest.mark.asyncio
    async def test_sessions_the_startup_scan_adds_late_are_still_released(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        await _not_dispatched(c, KEY)
        await _drain(c)
        # A live turn reaches a provider before the seed: KEY is not tracked yet.
        c._release_environment_backoff()
        await _drain(c)
        assert c.retry_eligible(KEY) is False

        with patch.object(c, "_scan_unconsolidated_transcripts", lambda: {KEY: time.time()}):
            await c._seed_last_activity()
        assert c._env_backoff_pending is True
        c._release_environment_backoff()
        await _drain(c)

        assert c.retry_eligible(KEY) is True

    @pytest.mark.asyncio
    async def test_a_turn_that_never_reached_the_provider_releases_nothing(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        c._last_activity[KEY] = time.time()

        await _not_dispatched(c, KEY)
        await _drain(c)

        assert c.retry_eligible(KEY) is False

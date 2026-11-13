"""A cron run that failed for good stops its sub-agent runs (``_end_failed_cron_turn``).

The attempt that fails the run calls the helper in its ``finally``, before it releases
the key. These tests drive it against the real ``SessionManager`` lease, with a child
teardown handler that records which runs a parent end cancels, once for each state the
key can be in when the run fails: the attempt still holds its session, the session was
torn down under it, the attempt never acquired one, or a session the attempt does not
hold is registered there.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest
from session_helpers import (  # noqa: F401 -- the fixture is requested by name
    _mock_provider_factory,
    _TestParentEndCancelsItsChildrenHarness,
    cfg_fixture,
)

from kiro_crew.session import SessionManager
from kiro_crew.slack.gateway import _end_failed_cron_turn
from kiro_crew.testing.wait import async_wait_until

_KEY = "cron:job-1"
#: Bounds a wait that a hang would otherwise make endless; never a barrier.
_HANG_CEILING_S = 30


class TestAFailedCronRunStopsItsSubAgents(_TestParentEndCancelsItsChildrenHarness):
    @pytest.mark.asyncio
    async def test_an_attempt_holding_its_session_ends_the_conversation(self, cfg):
        """The session goes with the runs, and the release the attempt owes the popped
        session afterwards is absorbed rather than over-releasing the permit."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, cancelled, handler = self._recorder()
        mgr.set_child_teardown_handler(handler)
        try:
            await mgr.get_or_create(_KEY)  # the failed attempt's own claim

            assert await _end_failed_cron_turn(mgr, _KEY) is True
            mgr.release(_KEY)

            assert cancelled == [("run-1",)]
            assert not mgr.has_session(_KEY)
            assert not mgr.is_busy(_KEY)
        finally:
            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_an_attempt_whose_session_was_torn_down_under_it_ends_the_keys_runs(self, cfg):
        """A recycling reset (a completion's timeout reset) tore the session down while
        the turn held it, and kept its runs. The failed attempt has no conversation left
        to end, so it ends the key's runs on their own."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, cancelled, handler = self._recorder()
        mgr.set_child_teardown_handler(handler)
        try:
            await mgr.get_or_create(_KEY)
            await mgr.reset(_KEY)
            assert cancelled == [], "the recycling reset was expected to keep the runs"

            assert await _end_failed_cron_turn(mgr, _KEY) is True
            mgr.release(_KEY)

            assert cancelled == [("run-1",)]
            assert not mgr.has_session(_KEY)
        finally:
            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_an_attempt_that_never_got_a_session_ends_the_keys_runs(self, cfg):
        """The run's last attempt failed before it claimed the key (an earlier attempt's
        reset left none), and starts nothing while ending the key's runs."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, cancelled, handler = self._recorder()
        mgr.set_child_teardown_handler(handler)
        try:
            assert await _end_failed_cron_turn(mgr, _KEY) is True

            assert cancelled == [("run-1",)]
            assert not mgr.has_session(_KEY)
        finally:
            await mgr.close_all()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("registered", ["held-by-another-turn", "released-for-a-retry"])
    async def test_a_session_the_attempt_does_not_hold_is_left_with_its_runs(
        self, cfg, registered: str
    ):
        """Runs are told apart by key only, so the runs of a session the attempt does not
        hold (another turn's, or one an earlier attempt gave back for a retry) may not
        be its own, and neither the session nor the runs are ended."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, cancelled, handler = self._recorder()
        mgr.set_child_teardown_handler(handler)
        holds, finish = asyncio.Event(), asyncio.Event()

        async def another_turn() -> None:
            await mgr.get_or_create(_KEY)
            holds.set()
            try:
                await finish.wait()
            finally:
                mgr.release(_KEY)

        other: asyncio.Task[None] | None = None
        try:
            if registered == "held-by-another-turn":
                other = asyncio.create_task(another_turn())
                await asyncio.wait_for(holds.wait(), _HANG_CEILING_S)
            else:
                await mgr.get_or_create(_KEY)
                mgr.release(_KEY)
            session = mgr._sessions[_KEY]

            assert await _end_failed_cron_turn(mgr, _KEY) is False

            assert cancelled == []
            assert mgr._sessions.get(_KEY) is session
            if other is not None:
                assert mgr.turn_holder(_KEY) is other
        finally:
            finish.set()
            if other is not None:
                await asyncio.wait_for(other, _HANG_CEILING_S)
            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_successor_registered_while_the_attempt_waits_for_the_lock_keeps_its_runs(
        self, cfg
    ):
        """No session is registered under the key when the failed attempt cleans up, but
        another teardown holds the registry lock across its own awaits, and a successor
        registers under the key before the attempt gets the lock; by then the successor's
        turn may have spawned a run. The attempt reads the registry in the lock hold that
        takes the snapshot, so it ends nothing."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, cancelled, handler = self._recorder()
        mgr.set_child_teardown_handler(handler)
        try:
            await mgr.get_or_create(_KEY)
            mgr.release(_KEY)
            successor = mgr._sessions.pop(_KEY)  # the key has no session
            async with mgr._lock:
                ending = asyncio.create_task(_end_failed_cron_turn(mgr, _KEY))
                await async_wait_until(
                    lambda: ending.done() or bool(mgr._lock._waiters),
                    describe=lambda: "the failed attempt waiting for the registry lock",
                )
                assert not ending.done()
                mgr._sessions[_KEY] = successor  # the successor registers first

            assert await asyncio.wait_for(ending, _HANG_CEILING_S) is False
            assert cancelled == []
        finally:
            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_failure_to_end_the_runs_is_logged_and_never_raised(self, cfg, caplog):
        """The helper runs in a ``finally`` that still owes the release, so it never
        raises over the run's own failure."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        try:
            with (
                patch.object(mgr, "end_children_for", AsyncMock(side_effect=RuntimeError("boom"))),
                caplog.at_level(logging.ERROR),
            ):
                assert await _end_failed_cron_turn(mgr, _KEY) is False

            assert "could not end the failed run's sub-agents" in caplog.text
        finally:
            await mgr.close_all()

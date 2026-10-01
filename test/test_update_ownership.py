"""Which update step this gateway is running owns a missing dashboard bundle."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import pytest

from kiro_crew import update_ownership
from kiro_crew.update_ownership import Step

_LOGGER = "kiro_crew.update_ownership"


def test_no_step_owns_no_gap():
    assert update_ownership.current_owner() is None


def test_a_step_owns_the_gap_while_it_runs():
    with update_ownership.step(Step.POLICY_APPLY):
        assert update_ownership.current_owner() == "the policy apply command"
    assert update_ownership.current_owner() is None


def test_a_step_that_raises_still_lets_go():
    with pytest.raises(RuntimeError):
        with update_ownership.step(Step.POLICY_APPLY):
            raise RuntimeError("apply failed")
    assert update_ownership.current_owner() is None


def test_the_innermost_step_names_the_owner():
    with update_ownership.step(Step.GIT_AUTO_UPDATE):
        with update_ownership.step(Step.RESTART):
            assert update_ownership.current_owner() == "the restart into an applied update"
        assert update_ownership.current_owner() == "the git auto-update"


@pytest.mark.asyncio
async def test_owning_owns_for_the_whole_coroutine_not_just_its_creation():
    seen = []

    @update_ownership.owning(Step.DASHBOARD_UPDATE)
    async def _update():
        await asyncio.sleep(0)
        seen.append(update_ownership.current_owner())

    pending = _update()
    assert update_ownership.current_owner() is None  # not yet running
    await pending
    assert seen == ["the dashboard update"]
    assert update_ownership.current_owner() is None


def test_a_step_past_its_maximum_stops_owning_and_says_so_once(caplog):
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    clock = {"t": 1000.0}
    with patch.object(update_ownership, "_now", lambda: clock["t"]):
        with update_ownership.step(Step.RESTART):
            clock["t"] += Step.RESTART.max_secs - 0.001
            assert update_ownership.current_owner() is not None
            clock["t"] += 0.001
            assert update_ownership.current_owner() is None
            assert update_ownership.current_owner() is None
    expired = [r for r in caplog.records if r.name == _LOGGER and "maximum" in r.getMessage()]
    assert len(expired) == 1
    assert "the restart into an applied update" in expired[0].getMessage()


def test_a_deferred_restart_is_owned_until_its_own_deadline():
    clock = {"t": 1000.0}
    with patch.object(update_ownership, "_now", lambda: clock["t"]):
        update_ownership.note_restart_deferred()
        assert update_ownership.current_owner() == "a deferred restart into an applied update"
        clock["t"] += update_ownership.DEFERRED_RESTART_MAX_SECS
        assert update_ownership.current_owner() is None


def test_a_restart_that_starts_does_not_end_the_deferral():
    """A restart can still coalesce or refuse; only one that commits ends it."""
    update_ownership.note_restart_deferred()
    with update_ownership.step(Step.RESTART):
        pass
    assert update_ownership.current_owner() == "a deferred restart into an applied update"


def test_a_committed_restart_ends_the_deferral():
    update_ownership.note_restart_deferred()
    update_ownership.restart_committed()
    assert update_ownership.current_owner() is None


def test_clearing_the_deferral_ends_it_and_a_later_deferral_rearms():
    """The no-interpreter refusal clears it; the next pending update opens its own window."""
    clock = {"t": 1000.0}
    with patch.object(update_ownership, "_now", lambda: clock["t"]):
        update_ownership.note_restart_deferred()
        update_ownership.clear_restart_deferral()
        assert update_ownership.current_owner() is None
        clock["t"] += update_ownership.DEFERRED_RESTART_MAX_SECS - 1
        update_ownership.note_restart_deferred()
        clock["t"] += 1
        assert update_ownership.current_owner() == "a deferred restart into an applied update"


def test_deferring_again_does_not_extend_the_deferral():
    """The coordinator retries every few minutes; a retry that defers keeps the first deadline."""
    clock = {"t": 1000.0}
    with patch.object(update_ownership, "_now", lambda: clock["t"]):
        update_ownership.note_restart_deferred()
        clock["t"] += update_ownership.DEFERRED_RESTART_MAX_SECS - 1
        update_ownership.note_restart_deferred()
        assert update_ownership.current_owner() is not None
        clock["t"] += 1
        assert update_ownership.current_owner() is None


@pytest.mark.asyncio
async def test_an_expired_restart_does_not_hide_a_live_step_another_task_opened():
    """``_live`` is process-wide: an expired restart next to a dashboard update still owned by it.

    No lock orders the dashboard's update worker against the coordinator's
    restart, so the entry before an expired restart can be unrelated work that
    a shutdown would cancel mid-install.
    """
    clock = {"t": 1000.0}
    update_open = asyncio.Event()
    finish_update = asyncio.Event()

    async def _dashboard_update():
        with update_ownership.step(Step.DASHBOARD_UPDATE):
            update_open.set()
            await finish_update.wait()

    with patch.object(update_ownership, "_now", lambda: clock["t"]):
        worker = asyncio.create_task(_dashboard_update())
        await asyncio.wait_for(update_open.wait(), timeout=5)
        try:
            with update_ownership.step(Step.RESTART):
                clock["t"] += Step.RESTART.max_secs
                assert update_ownership.current_owner() == "the dashboard update"
        finally:
            finish_update.set()
            await asyncio.wait_for(worker, timeout=5)
    assert update_ownership.current_owner() is None


def test_an_expired_step_falls_through_to_the_one_around_it():
    clock = {"t": 1000.0}
    with patch.object(update_ownership, "_now", lambda: clock["t"]):
        with update_ownership.step(Step.GIT_AUTO_UPDATE):
            with update_ownership.step(Step.POLICY_APPLY):
                clock["t"] += Step.POLICY_APPLY.max_secs
                assert update_ownership.current_owner() == "the git auto-update"


def test_two_steps_of_one_kind_in_one_clock_tick_each_remove_their_own_entry():
    """Equal kind and deadline: removal is by identity, not equality."""
    with patch.object(update_ownership, "_now", lambda: 1000.0):
        first = update_ownership.step(Step.RESTART)
        second = update_ownership.step(Step.RESTART)
        first.__enter__()
        second.__enter__()
        a_entry, b_entry = update_ownership._live
        second.__exit__(None, None, None)
        assert update_ownership._live[0] is a_entry
        a_entry.expired_logged = True  # what current_owner() does to an expired entry
        first.__exit__(None, None, None)
    assert update_ownership._live == []
    assert b_entry is not a_entry

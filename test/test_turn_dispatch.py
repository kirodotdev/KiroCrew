"""Tests for guarded chat-turn dispatch (``dashboard/turn_dispatch.py``).

The defect these pin: a turn wrapped in ``asyncio.wait_for`` whose only
done-callback was ``set.discard`` never had its ``TimeoutError`` retrieved, so
hitting the wall-clock ceiling produced no error card and no log the user would
find — the turn just stopped. Long babysit turns reach the ceiling routinely, so
the failure mode was ordinary rather than exotic.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

from kiro_crew.constants import CHAT_TURN_TIMEOUT
from kiro_crew.dashboard import turn_dispatch as td


def _slot() -> MagicMock:
    slot = MagicMock()
    slot.key = "chat-1-123"
    return slot


def _state() -> MagicMock:
    state = MagicMock()
    state._background_tasks = set()
    return state


def _patch_configured_ceiling(monkeypatch, value: object, *, acp: float = 7200.0) -> None:
    """Point ``chat_turn_timeout_secs`` at a synthetic config + ACP ceiling.

    ``KiroCrewConfig`` is imported at module scope, so the module attribute is
    what the code under test resolves. Passing ``value=None`` simulates config
    being unavailable entirely.
    """
    monkeypatch.setattr(td, "_acp_prompt_ceiling", lambda: acp)
    holder = MagicMock()
    if value is None:
        holder.load.side_effect = RuntimeError("no config here")
    else:
        cfg = MagicMock()
        cfg.agent.chat_turn_timeout_secs = value
        holder.load.return_value = cfg
    monkeypatch.setattr(td, "KiroCrewConfig", holder)


class TestBoundedIntCoercion:
    """A coercible value must not slip past the declared bounds.

    ``_clamp_security_bounds`` walks the raw config dict and deliberately skips
    anything that is not a real ``int``, so a numeric STRING passes through it
    untouched and is then coerced at construction. Without a clamp at the
    coercion site, ``"1"`` in config.json yields a 1-second turn ceiling (every
    turn dies immediately) or a 1-second loop-stall budget (the gateway
    hard-exits and systemd restart-loops it).
    """

    def test_numeric_string_below_the_floor_is_clamped(self) -> None:
        from kiro_crew.config.loader import (
            CHAT_TURN_TIMEOUT_MAX,
            CHAT_TURN_TIMEOUT_MIN,
            _safe_int,
        )

        assert (
            _safe_int("1", 7200, CHAT_TURN_TIMEOUT_MIN, CHAT_TURN_TIMEOUT_MAX)
            == CHAT_TURN_TIMEOUT_MIN
        )

    def test_numeric_string_above_the_ceiling_is_clamped(self) -> None:
        from kiro_crew.config.loader import (
            CHAT_TURN_TIMEOUT_MAX,
            CHAT_TURN_TIMEOUT_MIN,
            _safe_int,
        )

        assert (
            _safe_int("999999", 7200, CHAT_TURN_TIMEOUT_MIN, CHAT_TURN_TIMEOUT_MAX)
            == CHAT_TURN_TIMEOUT_MAX
        )

    def test_integral_float_is_clamped_too(self) -> None:
        from kiro_crew.config.loader import (
            LOOP_STALL_EXIT_AFTER_MAX,
            LOOP_STALL_EXIT_AFTER_MIN,
            _safe_int,
        )

        assert (
            _safe_int(1.0, 25, LOOP_STALL_EXIT_AFTER_MIN, LOOP_STALL_EXIT_AFTER_MAX)
            == LOOP_STALL_EXIT_AFTER_MIN
        )

    def test_in_range_value_is_untouched(self) -> None:
        from kiro_crew.config.loader import (
            CHAT_TURN_TIMEOUT_MAX,
            CHAT_TURN_TIMEOUT_MIN,
            _safe_int,
        )

        assert _safe_int(1800, 7200, CHAT_TURN_TIMEOUT_MIN, CHAT_TURN_TIMEOUT_MAX) == 1800

    def test_callers_without_bounds_are_unaffected(self) -> None:
        """The clamp is opt-in; the dozens of existing call sites must not shift."""
        from kiro_crew.config.loader import _safe_int

        assert _safe_int("1", 7200) == 1
        assert _safe_int("nonsense", 42) == 42
        assert _safe_int(True, 42) == 42


class TestCeilingResolution:
    def test_defaults_to_constant_when_config_unavailable(self, monkeypatch) -> None:
        """A config-less context (tests, early bootstrap) behaves like default config."""
        _patch_configured_ceiling(monkeypatch, None)
        assert td.chat_turn_timeout_secs() == CHAT_TURN_TIMEOUT

    def test_reads_configured_value(self, monkeypatch) -> None:
        _patch_configured_ceiling(monkeypatch, 1800)
        assert td.chat_turn_timeout_secs() == 1800.0

    def test_clamps_above_acp_prompt_timeout_and_logs(self, monkeypatch, caplog) -> None:
        """A ceiling above the transport's own timeout cannot take effect.

        The transport bounds the turn first, so honouring the larger number
        would advertise a limit the system does not enforce.
        """
        _patch_configured_ceiling(monkeypatch, 99999)
        with caplog.at_level(logging.WARNING, logger=td.logger.name):
            assert td.chat_turn_timeout_secs() == 7200.0
        assert "clamping" in caplog.text

    def test_non_positive_value_cannot_disable_the_backstop(self, monkeypatch) -> None:
        """0 would make wait_for raise immediately — the ceiling is not optional."""
        _patch_configured_ceiling(monkeypatch, 0)
        assert td.chat_turn_timeout_secs() == CHAT_TURN_TIMEOUT


class TestTimeoutCard:
    def test_names_the_limit_in_hours(self) -> None:
        assert "2-hour" in td.format_turn_timeout_card(7200.0)

    def test_names_the_limit_in_minutes_below_an_hour(self) -> None:
        assert "30-minute" in td.format_turn_timeout_card(1800.0)

    def test_reassures_that_work_survived(self) -> None:
        """The user's first question is "did I lose my work?"."""
        assert "written to disk is still there" in td.format_turn_timeout_card(7200.0)


class TestFinishTurnTask:
    @pytest.mark.asyncio
    async def test_timeout_renders_a_visible_card(self) -> None:
        """The headline regression: a ceiling hit must not be silent."""
        state, slot = _state(), _slot()

        async def _forever():
            await asyncio.sleep(3600)

        task = td.spawn_guarded_turn(state, slot, _forever(), timeout_secs=0.01)
        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await task
        # Give the done-callback a turn of the loop to run.
        await asyncio.sleep(0)
        roles = [c.args[0] for c in slot.append.call_args_list]
        assert "error" in roles, "a turn that hit the ceiling rendered no card"
        body = slot.append.call_args_list[0].args[1]
        assert "limit" in body and "⏱️" in body
        assert task not in state._background_tasks

    @pytest.mark.asyncio
    async def test_deadline_absorbed_by_the_turn_still_renders_a_card(self) -> None:
        """The real shape: ``_run_chat`` swallows the deadline's cancellation.

        ``_run_chat`` catches ``CancelledError`` to flush partial output and
        returns normally, so ``asyncio.wait_for`` hands back a value rather than
        raising — and a naive guard would treat the killed turn as a success and
        show nothing. This is the case that matters in production; a test
        coroutine that lets the cancellation propagate does not exercise it.
        """
        state, slot = _state(), _slot()

        async def _swallows_cancellation():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                return "partial output flushed"  # exactly what _run_chat does

        task = td.spawn_guarded_turn(state, slot, _swallows_cancellation(), timeout_secs=0.01)
        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await task
        await asyncio.sleep(0)
        roles = [c.args[0] for c in slot.append.call_args_list]
        assert "error" in roles, (
            "the turn absorbed the ceiling's cancellation and no card was shown — "
            "indistinguishable from the agent going quiet"
        )
        assert "limit" in slot.append.call_args_list[0].args[1]

    @pytest.mark.asyncio
    async def test_turn_finishing_just_under_the_ceiling_is_not_called_a_timeout(self) -> None:
        """The elapsed comparison must not mislabel a genuine completion."""
        state, slot = _state(), _slot()

        async def _quick():
            return "done"

        task = td.spawn_guarded_turn(state, slot, _quick(), timeout_secs=30)
        assert await task == "done"
        await asyncio.sleep(0)
        assert slot.append.call_count == 0

    @pytest.mark.asyncio
    async def test_exception_is_retrieved_so_asyncio_stays_quiet(self) -> None:
        """Nothing may be left for the collector to report as un-retrieved."""
        state, slot = _state(), _slot()

        async def _forever():
            await asyncio.sleep(3600)

        task = td.spawn_guarded_turn(state, slot, _forever(), timeout_secs=0.01)
        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await task
        await asyncio.sleep(0)
        # exception() returning without raising proves it was consumed.
        assert isinstance(task.exception(), (asyncio.TimeoutError, TimeoutError))

    @pytest.mark.asyncio
    async def test_cancellation_renders_no_card(self) -> None:
        """A user pressing Stop is already surfaced; a second card is noise."""
        state, slot = _state(), _slot()

        async def _forever():
            await asyncio.sleep(3600)

        task = td.spawn_guarded_turn(state, slot, _forever(), timeout_secs=60)
        await asyncio.sleep(0)  # let the task actually start before cancelling
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert slot.append.call_count == 0

    @pytest.mark.asyncio
    async def test_success_renders_no_card_and_deregisters(self) -> None:
        state, slot = _state(), _slot()

        async def _ok():
            return None

        task = td.spawn_guarded_turn(state, slot, _ok(), timeout_secs=60)
        await task
        await asyncio.sleep(0)
        assert slot.append.call_count == 0
        assert task not in state._background_tasks

    @pytest.mark.asyncio
    async def test_other_exception_is_logged_not_dropped(self, caplog) -> None:
        state, slot = _state(), _slot()

        async def _boom():
            raise ValueError("kaboom")

        with caplog.at_level(logging.ERROR, logger=td.logger.name):
            task = td.spawn_guarded_turn(state, slot, _boom(), timeout_secs=60)
            with pytest.raises(ValueError):
                await task
            await asyncio.sleep(0)
        assert "kaboom" in caplog.text

    @pytest.mark.asyncio
    async def test_card_render_failure_cannot_break_the_callback(self) -> None:
        """A raising callback would surface as "exception in callback" noise."""
        state, slot = _state(), _slot()
        slot.append.side_effect = RuntimeError("slot gone")

        async def _forever():
            await asyncio.sleep(3600)

        task = td.spawn_guarded_turn(state, slot, _forever(), timeout_secs=0.01)
        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await task
        await asyncio.sleep(0)  # must not raise

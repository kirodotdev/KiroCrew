"""The dashboard chat path's tool-approval window.

Three properties, one per failure mode observed in production:

1. The window is CONFIGURABLE and short by default. It used to be a literal
   ``7200.0`` in ``chat_runner``, identical to the turn ceiling.
2. It is CLAMPED below the turn ceiling. A window at or above the ceiling can
   never fire — the turn is cut first — so it is not a longer wait, it is a
   wait that never reports.
3. Its timeout says an APPROVAL went unanswered and to resend, rather than
   borrowing the generic turn-timeout wording.
"""

from __future__ import annotations

import asyncio
import inspect
import logging

import pytest

from kiro_crew.config.loader import (
    APPROVAL_TURN_MARGIN_SECS,
    TOOL_APPROVAL_TIMEOUT_MAX,
    TOOL_APPROVAL_TIMEOUT_MIN,
    AgentConfig,
    _clamp_security_bounds,
)
from kiro_crew.constants import CHAT_TURN_TIMEOUT, TOOL_APPROVAL_TIMEOUT
from kiro_crew.dashboard import turn_dispatch as td


class _Cfg:
    """Minimal stand-in for the loaded config the resolvers read."""

    def __init__(self, *, window: int, turn: int = 7200) -> None:
        self.agent = AgentConfig()
        self.agent.tool_approval_timeout_secs = window
        self.agent.chat_turn_timeout_secs = turn


@pytest.fixture
def cfg(monkeypatch: pytest.MonkeyPatch):
    """Point both resolvers at a synthetic config."""

    def _apply(*, window: int, turn: int = 7200) -> None:
        monkeypatch.setattr(
            td.KiroCrewConfig, "load", staticmethod(lambda: _Cfg(window=window, turn=turn))
        )

    return _apply


class TestDefaultsAreShort:
    def test_constant_and_config_default_agree(self) -> None:
        """The fallback constant and the config default must not drift apart.

        Two independent spellings of "600" exist by necessity — the constant
        serves config-less contexts — so pin them to each other.
        """
        assert TOOL_APPROVAL_TIMEOUT == float(AgentConfig().tool_approval_timeout_secs)

    def test_default_leaves_room_under_the_turn_ceiling(self) -> None:
        assert TOOL_APPROVAL_TIMEOUT <= CHAT_TURN_TIMEOUT - APPROVAL_TURN_MARGIN_SECS

    def test_no_hardcoded_window_left_in_the_runner(self) -> None:
        """The runner must resolve the window, not inline a literal.

        The bug was exactly an inlined ``7200.0`` here, invisible to config.
        """
        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner._run_chat)
        assert "wait_for(fut, timeout=7200.0)" not in src
        # Resolved into a local, not inlined into the await: the timeout card
        # has to report the SAME number the wait actually used.
        assert "_approval_window = tool_approval_timeout_secs()" in src


class TestResolver:
    def test_reads_config(self, cfg) -> None:
        cfg(window=300)
        assert td.tool_approval_timeout_secs() == 300.0

    def test_falls_back_when_config_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> None:
            raise RuntimeError("no config")

        monkeypatch.setattr(td.KiroCrewConfig, "load", staticmethod(_boom))
        assert td.tool_approval_timeout_secs() == TOOL_APPROVAL_TIMEOUT

    def test_non_positive_window_falls_back(self, cfg) -> None:
        """Zero would make wait_for raise at once and auto-decline every tool."""
        cfg(window=0)
        assert td.tool_approval_timeout_secs() == TOOL_APPROVAL_TIMEOUT

    def test_capped_under_the_resolved_turn_ceiling(
        self, cfg, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A window inside its config bound can still outlive a LOWERED ceiling.

        The loader clamps against the CONFIGURED ceiling; the resolved one can
        be lower (the ACP prompt timeout clamps it), which is why the resolver
        repeats the check instead of trusting load-time alone.
        """
        cfg(window=3600, turn=1200)
        with caplog.at_level(logging.WARNING, logger=td.logger.name):
            assert td.tool_approval_timeout_secs() == 1200.0 - APPROVAL_TURN_MARGIN_SECS
        assert "tool_approval_timeout_secs" in caplog.text

    def test_cap_never_falls_below_the_floor(self, cfg) -> None:
        cfg(window=3600, turn=60)
        assert td.tool_approval_timeout_secs() == float(TOOL_APPROVAL_TIMEOUT_MIN)


class TestLoadTimeClamp:
    def test_window_at_the_ceiling_is_clamped(self, caplog: pytest.LogCaptureFixture) -> None:
        data = {"agent": {"tool_approval_timeout_secs": 7200, "chat_turn_timeout_secs": 7200}}
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            _clamp_security_bounds(data)
        assert data["agent"]["tool_approval_timeout_secs"] == 7200 - APPROVAL_TURN_MARGIN_SECS
        assert "can never fire" in caplog.text

    def test_window_clamped_against_a_lowered_ceiling(self) -> None:
        data = {"agent": {"tool_approval_timeout_secs": 1800, "chat_turn_timeout_secs": 900}}
        _clamp_security_bounds(data)
        assert data["agent"]["tool_approval_timeout_secs"] == 900 - APPROVAL_TURN_MARGIN_SECS

    def test_default_window_survives_the_default_ceiling(self) -> None:
        """An in-range pair must be left byte-identical."""
        data = {"agent": {"tool_approval_timeout_secs": 600, "chat_turn_timeout_secs": 7200}}
        _clamp_security_bounds(data)
        assert data["agent"]["tool_approval_timeout_secs"] == 600

    def test_absent_ceiling_uses_the_field_default(self) -> None:
        """Omitting the ceiling must not disable the cross-field clamp."""
        data = {"agent": {"tool_approval_timeout_secs": 7200}}
        _clamp_security_bounds(data)
        assert data["agent"]["tool_approval_timeout_secs"] == 7200 - APPROVAL_TURN_MARGIN_SECS

    def test_static_bounds_applied_first(self) -> None:
        """The generic range clamp still runs on this field."""
        data = {"agent": {"tool_approval_timeout_secs": 1}}
        _clamp_security_bounds(data)
        assert data["agent"]["tool_approval_timeout_secs"] == TOOL_APPROVAL_TIMEOUT_MIN

        data = {"agent": {"tool_approval_timeout_secs": TOOL_APPROVAL_TIMEOUT_MAX * 10}}
        _clamp_security_bounds(data)
        # Static ceiling first, then the cross-field margin.
        assert (
            data["agent"]["tool_approval_timeout_secs"]
            == TOOL_APPROVAL_TIMEOUT_MAX - APPROVAL_TURN_MARGIN_SECS
        )

    def test_bool_window_is_left_to_dataclass_coercion(self) -> None:
        """``true`` is not a real window; the clamp must not arithmetic on it."""
        data = {"agent": {"tool_approval_timeout_secs": True}}
        _clamp_security_bounds(data)
        assert data["agent"]["tool_approval_timeout_secs"] is True

    def test_non_int_ceiling_falls_back_to_the_default(self) -> None:
        data = {"agent": {"tool_approval_timeout_secs": 7200, "chat_turn_timeout_secs": "lots"}}
        _clamp_security_bounds(data)
        assert data["agent"]["tool_approval_timeout_secs"] == 7200 - APPROVAL_TURN_MARGIN_SECS


class TestArmTimeBudget:
    """The window must fit the budget LEFT in the turn, not the full ceiling.

    A ceiling-relative bound alone still lets a prompt arming late in a long
    agentic turn outlive that turn — the same mislabeled turn timeout the whole
    change exists to prevent.
    """

    @pytest.mark.asyncio
    async def test_late_arming_prompt_is_shortened_to_fit(self, cfg) -> None:
        cfg(window=600, turn=7200)
        loop = asyncio.get_running_loop()
        # 300s left of a 2h turn: a 600s window would outlive it.
        token = td._TURN_DEADLINE.set(loop.time() + 300.0)
        try:
            got = td.tool_approval_timeout_secs()
        finally:
            td._TURN_DEADLINE.reset(token)
        assert got == pytest.approx(300.0 - APPROVAL_TURN_MARGIN_SECS, abs=1.0)

    @pytest.mark.asyncio
    async def test_no_budget_left_returns_zero(self, cfg) -> None:
        """Under the margin there is no window that can both wait and report."""
        cfg(window=600, turn=7200)
        loop = asyncio.get_running_loop()
        token = td._TURN_DEADLINE.set(loop.time() + 5.0)
        try:
            assert td.tool_approval_timeout_secs() == 0.0
        finally:
            td._TURN_DEADLINE.reset(token)

    @pytest.mark.asyncio
    async def test_early_prompt_keeps_the_configured_window(self, cfg) -> None:
        cfg(window=600, turn=7200)
        loop = asyncio.get_running_loop()
        token = td._TURN_DEADLINE.set(loop.time() + 7200.0)
        try:
            assert td.tool_approval_timeout_secs() == 600.0
        finally:
            td._TURN_DEADLINE.reset(token)

    def test_absent_deadline_falls_back_to_the_ceiling_bound(self, cfg) -> None:
        """Paths that don't go through _bounded_turn must still get a window."""
        cfg(window=600, turn=7200)
        assert td._TURN_DEADLINE.get() is None
        assert td.tool_approval_timeout_secs() == 600.0

    @pytest.mark.asyncio
    async def test_bounded_turn_publishes_then_clears_the_deadline(self) -> None:
        """The turn's own coroutine sees a deadline; the caller's context does not.

        The reset matters: `chat_orchestrator` awaits `_bounded_turn` directly,
        so a leaked spent deadline would starve every later approval dispatched
        in that same context.
        """
        seen: list[float | None] = []

        async def _turn() -> str:
            seen.append(td._turn_budget_remaining())
            return "done"

        assert await td._bounded_turn(_turn(), 120.0) == "done"
        # Remaining is computed as (t + 120.0) - t', so float rounding can put it
        # a hair ABOVE the timeout when both clock reads land on the same tick
        # (Windows' coarse timer makes that the common case). Assert the budget
        # is essentially the full window rather than pinning a strict bound.
        assert seen and seen[0] is not None
        assert seen[0] == pytest.approx(120.0, abs=1.0)
        assert td._TURN_DEADLINE.get() is None

    @pytest.mark.asyncio
    async def test_deadline_cleared_even_when_the_turn_raises(self) -> None:
        async def _boom() -> None:
            raise ValueError("nope")

        with pytest.raises(ValueError):
            await td._bounded_turn(_boom(), 120.0)
        assert td._TURN_DEADLINE.get() is None


class TestNoBudgetCard:
    def test_says_the_turn_had_no_time_and_to_resend(self) -> None:
        text = td.format_approval_no_budget_card()
        assert "approval" in text.lower()
        assert "again" in text.lower()

    def test_distinct_from_the_waited_timeout_card(self) -> None:
        assert td.format_approval_no_budget_card() != td.format_approval_timeout_card(600.0)

    def test_runner_declines_without_waiting_when_the_window_is_zero(self) -> None:
        """A zero window must skip the await entirely, not pass 0 to wait_for."""
        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner._run_chat)
        idx = src.index("_approval_window = tool_approval_timeout_secs()")
        branch = src[idx : idx + 1800]
        assert "if _approval_window <= 0:" in branch
        assert "format_approval_no_budget_card()" in branch
        # The await must live on the else side of that guard.
        assert branch.index("if _approval_window <= 0:") < branch.index("await asyncio.wait_for")


class TestCardsMatchRealRecovery:
    """Neither card may claim the turn stopped — the reject path continues it.

    `_run_chat`'s rejected branch calls `reject_tool` and `continue`s the event
    loop, so the agent is told the tool was denied and keeps working. Wording
    that says "stopped" tells the user to expect lost work that never happened.
    """

    def test_neither_card_claims_the_turn_stopped(self) -> None:
        for text in (td.format_approval_timeout_card(600.0), td.format_approval_no_budget_card()):
            assert "stopped" not in text.lower()
            assert "carried on" in text.lower()

    def test_reject_path_really_continues(self) -> None:
        """Guards the premise above: if the runner starts breaking, wording must change.

        Anchored on the GENERIC rejection (the one a timed-out approval takes),
        identified by its `_reject_label` append — not the invalid-tool-name or
        hook-error branches above it, which deliberately `break`.
        """
        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner._run_chat)
        idx = src.index('slot.append("tool", _reject_label, "msg msg-tool")')
        tail = src[idx : idx + 1600]
        assert "continue" in tail
        assert tail.index("continue") < (tail.index("break") if "break" in tail else len(tail))


class TestTimeoutCard:
    def test_names_the_approval_and_the_fix(self) -> None:
        text = td.format_approval_timeout_card(600.0)
        assert "approval" in text.lower()
        assert "10 minutes" in text
        assert "again" in text.lower()

    def test_distinct_from_the_turn_timeout_card(self) -> None:
        """The two must not be confusable — that confusion WAS the bug."""
        approval = td.format_approval_timeout_card(600.0)
        turn = td.format_turn_timeout_card(600.0)
        assert approval != turn
        assert "hit the" not in approval

    def test_hour_scale_wording(self) -> None:
        assert "1.5 hours" in td.format_approval_timeout_card(5400.0)

    def test_runner_renders_the_approval_card_on_timeout(self) -> None:
        """The timeout branch must append the card, not fall through silently."""
        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner._run_chat)
        idx = src.index("_approval_window = tool_approval_timeout_secs()")
        branch = src[idx : idx + 2000]
        assert "except asyncio.TimeoutError:" in branch
        assert "format_approval_timeout_card(_approval_window)" in branch

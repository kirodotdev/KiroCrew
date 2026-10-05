"""The dashboard chat path's tool-approval window.

Three properties, one per failure mode observed in production:

1. The window is CONFIGURABLE and short by default, not a literal
   ``7200.0`` in ``chat_runner`` equal to the turn ceiling.
2. It is CLAMPED below the turn ceiling. A window at or above the ceiling can
   never fire — the turn is cut first — so it is not a longer wait, it is a
   wait that never reports.
3. Its timeout says an APPROVAL went unanswered and to resend, rather than
   borrowing the generic turn-timeout wording.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from turn_harness import (
    APPROVED,
    REJECTED,
    REJECTED_ONCE,
    UNANSWERED,
    Emit,
    ScriptedProvider,
    SlotSpec,
    TurnContext,
    TurnRecord,
    TurnScript,
    Wait,
    run_turn,
)

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_STEER_CONSUMED,
    EVENT_TEXT_CHUNK,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.config.loader import (
    APPROVAL_TURN_MARGIN_SECS,
    TOOL_APPROVAL_TIMEOUT_MAX,
    TOOL_APPROVAL_TIMEOUT_MIN,
    AgentConfig,
    _clamp_security_bounds,
)
from kiro_crew.constants import (
    CHAT_TURN_TIMEOUT,
    DENY_CAUSE_APPROVAL_NO_BUDGET,
    DENY_CAUSE_APPROVAL_TIMEOUT,
    STEER_NOTICE_BOUND_SECS,
    TOOL_APPROVAL_TIMEOUT,
)
from kiro_crew.dashboard import turn_dispatch as td
from kiro_crew.dashboard.state import REFUSAL_RECOVERY_PREFIX
from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE


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


@pytest.fixture(autouse=True)
def _isolate_turn_deadline():
    """Give every test a clean ``_TURN_DEADLINE`` and put the outside value back.

    Residue travels through the main-thread context: an un-reset ``set()`` made
    there is inherited by every later test in the worker, because each async
    test's task context is copied from it (an async test's own writes die with
    its task copy — a leak seen at test start was already in the parent
    context). Baselining to ``None`` here is what makes this module's
    ``get() is None`` assertions deterministic under any ordering; restoring
    the snapshot afterwards keeps the fixture honest about state it did not
    create. Deliberately a sync fixture: an async one would run inside a copied
    task context and its writes would be discarded with it.

    Save/restore is by VALUE, not ``reset(token)``, mirroring
    ``turn_dispatch._bounded_turn``: test and shutdown harnesses may resume
    finalization in a copied Context (notably Windows xdist); ``reset(token)``
    then raises and can take down the whole worker because tokens are
    context-bound. The sites below follow the same pattern for the same reason.
    """
    prev = td._TURN_DEADLINE.get()
    td._TURN_DEADLINE.set(None)
    try:
        yield
    finally:
        td._TURN_DEADLINE.set(prev)


def test_token_based_restore_stays_banned_in_this_module() -> None:
    """No test here may restore ``_TURN_DEADLINE`` through a ContextVar token.

    The isolation fixture above masks exactly the failure it fixes: with every
    test baselined to ``None``, the ``get() is None`` assertions cannot
    catch a reintroduced token-based restore — the pattern that leaves the var
    set (or kills the worker) when finalization resumes in a copied Context,
    per the rationale at turn_dispatch.py:350-356. Pin the ban at the source
    level instead of relying on convention.
    """
    src = inspect.getsource(sys.modules[__name__])
    needle = "_TURN_DEADLINE" + ".reset("
    assert needle not in src, (
        "restore _TURN_DEADLINE by value (set the captured previous value), "
        "never through a ContextVar token"
    )


class TestDefaultsAreShort:
    def test_constant_and_config_default_agree(self) -> None:
        """The fallback constant and the config default must not drift apart.

        Two independent spellings of "600" exist by necessity — the constant
        serves config-less contexts — so pin them to each other.
        """
        assert TOOL_APPROVAL_TIMEOUT == float(AgentConfig().tool_approval_timeout_secs)

    def test_default_leaves_room_under_the_turn_ceiling(self) -> None:
        assert TOOL_APPROVAL_TIMEOUT <= CHAT_TURN_TIMEOUT - APPROVAL_TURN_MARGIN_SECS


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

    def test_absent_ceiling_uses_the_field_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Omitting the ceiling must not disable the cross-field clamp.

        With the shipped default ceiling the static ``TOOL_APPROVAL_TIMEOUT_MAX``
        binds first, so the cross-field clamp can only be SEEN to consult the
        field default by lowering that default below the static max.
        """
        from kiro_crew.config import loader as loader_mod

        monkeypatch.setattr(loader_mod, "_DEFAULT_CHAT_TURN_TIMEOUT_SECS", 1200)
        data = {"agent": {"tool_approval_timeout_secs": 7200}}
        _clamp_security_bounds(data)
        assert data["agent"]["tool_approval_timeout_secs"] == 1200 - APPROVAL_TURN_MARGIN_SECS

    def test_static_bounds_applied_first(self) -> None:
        """The generic range clamp still runs on this field."""
        data = {"agent": {"tool_approval_timeout_secs": 1}}
        _clamp_security_bounds(data)
        assert data["agent"]["tool_approval_timeout_secs"] == TOOL_APPROVAL_TIMEOUT_MIN

        data = {"agent": {"tool_approval_timeout_secs": TOOL_APPROVAL_TIMEOUT_MAX * 10}}
        _clamp_security_bounds(data)
        # Static ceiling first. The default turn ceiling sits above the static
        # max by more than the margin, so the cross-field clamp leaves the
        # statically-clamped value alone.
        assert data["agent"]["tool_approval_timeout_secs"] == TOOL_APPROVAL_TIMEOUT_MAX
        # With a ceiling INSIDE the static max the cross-field margin binds after it.
        data = {
            "agent": {
                "tool_approval_timeout_secs": TOOL_APPROVAL_TIMEOUT_MAX * 10,
                "chat_turn_timeout_secs": TOOL_APPROVAL_TIMEOUT_MAX,
            }
        }
        _clamp_security_bounds(data)
        assert (
            data["agent"]["tool_approval_timeout_secs"]
            == TOOL_APPROVAL_TIMEOUT_MAX - APPROVAL_TURN_MARGIN_SECS
        )

    def test_approval_max_is_decoupled_from_a_raised_turn_ceiling(self) -> None:
        """Raising the turn ceiling must NOT raise the approval window's max.

        The approval suites hold a flat 2h runtime window
        (``DashboardState._APPROVAL_TIMEOUT``); a config max that follows the
        24h turn-ceiling max would accept windows the runtime silently never
        honours. So with a 24h ceiling configured, an oversized window still
        clamps to the static 7200 — the cross-field margin (86340s) is no
        longer the binding limit.
        """
        assert TOOL_APPROVAL_TIMEOUT_MAX == 7200
        data = {
            "agent": {
                "tool_approval_timeout_secs": 86400,
                "chat_turn_timeout_secs": 86400,
            }
        }
        _clamp_security_bounds(data)
        assert data["agent"]["tool_approval_timeout_secs"] == TOOL_APPROVAL_TIMEOUT_MAX

    def test_bool_window_is_left_to_dataclass_coercion(self) -> None:
        """``true`` is not a real window; the clamp must not arithmetic on it."""
        data = {"agent": {"tool_approval_timeout_secs": True}}
        _clamp_security_bounds(data)
        assert data["agent"]["tool_approval_timeout_secs"] is True

    def test_non_int_ceiling_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.config import loader as loader_mod

        # Lowered below the static max so the fallback is observable (see
        # test_absent_ceiling_uses_the_field_default).
        monkeypatch.setattr(loader_mod, "_DEFAULT_CHAT_TURN_TIMEOUT_SECS", 1200)
        data = {"agent": {"tool_approval_timeout_secs": 7200, "chat_turn_timeout_secs": "lots"}}
        _clamp_security_bounds(data)
        assert data["agent"]["tool_approval_timeout_secs"] == 1200 - APPROVAL_TURN_MARGIN_SECS


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
        prev = td._TURN_DEADLINE.get()
        td._TURN_DEADLINE.set(loop.time() + 300.0)
        try:
            got = td.tool_approval_timeout_secs()
        finally:
            td._TURN_DEADLINE.set(prev)
        assert got == pytest.approx(300.0 - APPROVAL_TURN_MARGIN_SECS, abs=1.0)

    @pytest.mark.asyncio
    async def test_no_budget_left_returns_zero(self, cfg) -> None:
        """Under the margin there is no window that can both wait and report."""
        cfg(window=600, turn=7200)
        loop = asyncio.get_running_loop()
        prev = td._TURN_DEADLINE.get()
        td._TURN_DEADLINE.set(loop.time() + 5.0)
        try:
            assert td.tool_approval_timeout_secs() == 0.0
        finally:
            td._TURN_DEADLINE.set(prev)

    @pytest.mark.asyncio
    async def test_early_prompt_keeps_the_configured_window(self, cfg) -> None:
        cfg(window=600, turn=7200)
        loop = asyncio.get_running_loop()
        prev = td._TURN_DEADLINE.get()
        td._TURN_DEADLINE.set(loop.time() + 7200.0)
        try:
            assert td.tool_approval_timeout_secs() == 600.0
        finally:
            td._TURN_DEADLINE.set(prev)

    def test_absent_deadline_falls_back_to_the_ceiling_bound(self, cfg) -> None:
        """Paths that don't go through _bounded_turn must still get a window."""
        cfg(window=600, turn=7200)
        assert td._TURN_DEADLINE.get() is None
        assert td.tool_approval_timeout_secs() == 600.0

    @pytest.mark.asyncio
    async def test_bounded_turn_publishes_then_clears_the_deadline(self) -> None:
        """The turn's own coroutine sees a deadline; the caller's context does not.

        The restore matters: `chat_orchestrator` awaits `_bounded_turn` directly,
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

    @pytest.mark.asyncio
    async def test_bounded_turn_restores_the_previous_deadline_by_value(self) -> None:
        """A non-None prior deadline comes back after the turn — not None.

        Pins the restore-by-value contract documented in ``_bounded_turn``
        (turn_dispatch.py): the finally writes back the CAPTURED previous
        value. No other test armed a non-None prior value, so the two
        ``get() is None`` neighbours above only ever exercised the None case —
        which is how a residue inherited from another test's context read as
        this module's product bug.
        """
        prev = td._TURN_DEADLINE.get()
        armed = asyncio.get_running_loop().time() + 999.0
        td._TURN_DEADLINE.set(armed)
        try:

            async def _turn() -> str:
                return "done"

            assert await td._bounded_turn(_turn(), 120.0) == "done"
            assert td._TURN_DEADLINE.get() == armed
        finally:
            td._TURN_DEADLINE.set(prev)


class TestNoBudgetCard:
    def test_says_the_turn_had_no_time_and_to_resend(self) -> None:
        text = td.format_approval_no_budget_card()
        assert "approval" in text.lower()
        assert "again" in text.lower()

    def test_distinct_from_the_waited_timeout_card(self) -> None:
        assert td.format_approval_no_budget_card() != td.format_approval_timeout_card(600.0)


class TestCardsMatchRealRecovery:
    """Neither card may claim the turn stopped — the reject path continues it.

    A declined prompt is answered with ``reject_tool`` and the turn keeps going,
    so the agent is told the tool was denied and keeps working (pinned by
    ``TestTheRunnersApprovalWindow.test_a_declined_prompt_carries_the_turn_on``).
    Wording that says "stopped" tells the user to expect lost work that never
    happened.
    """

    def test_neither_card_claims_the_turn_stopped(self) -> None:
        for text in (td.format_approval_timeout_card(600.0), td.format_approval_no_budget_card()):
            assert "stopped" not in text.lower()
            assert "carried on" in text.lower()


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


# ── the window as the runner applies it, through one real turn ───────────────

_PROMPT = AcpEvent(
    kind=EVENT_PERMISSION_REQUEST, request_id="req-1", title="fs_write", tool_kind="edit"
)
_AFTER = AcpEvent(kind=EVENT_TEXT_CHUNK, text="after the denial")
_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
#: A turn ceiling the config loader keeps as written (a stored value equal to an
#: earlier default is adopted back to the current one), short enough that a
#: prompt arming late in it has less than the configured window left.
_CEILING = 3600


def _prompt_turn(*before: Any, answers: dict[str, str] | None = None) -> TurnScript:
    return TurnScript(events=[*before, _PROMPT, _AFTER, _DONE], answers=answers or {})


def _declined_at(record: TurnRecord) -> float:
    [reject] = [call for call in record.calls("reject_tool") if call.args == ("req-1",)]
    return reject.at


def _cards(record: TurnRecord) -> list[str]:
    return [row["content"] for row in record.rows("error")]


def _steers(record: TurnRecord) -> list[str]:
    return [call.args[0] for call in record.calls("steer")]


class _SteerNeverReturns(ScriptedProvider):
    """A provider whose steer write never completes (a backpressured stdin)."""

    async def steer(self, message: str) -> bool:
        self.record("steer", message)
        await asyncio.Event().wait()
        return True


class _BlocksToolA:
    """A PreToolUse hook that blocks ``tool_a`` and lets everything else through."""

    async def fire(self, event: str, *args: Any, **kwargs: Any) -> list[Any]:
        if event == HOOK_EVENT_PRE_TOOL_USE and kwargs.get("tool_name") == "tool_a":
            return [
                SimpleNamespace(
                    exit_code=2, stdout="", stderr="blocked by rule X", error="", hook_name="rule"
                )
            ]
        return []


def _echo_last_steer(ctx: TurnContext) -> AcpEvent:
    """The backend's ``steering_consumed`` echo of the steer the turn just sent."""
    assert ctx.provider is not None
    return AcpEvent(kind=EVENT_STEER_CONSUMED, text=ctx.provider.recorded("steer")[-1].args[0])


class TestTheRunnersApprovalWindow:
    """What a dashboard turn does with a prompt nobody answers, end to end.

    Each runs one real ``_run_chat`` turn (``turn_harness.run_turn``) on virtual
    time, so a 600 s window expires at exactly 600 s in microseconds. The window
    the turn waits is the MINIMUM of the slot's own (the 180 s deny-fast of an
    unattended app worker, the attended 7200 s) and ``tool_approval_timeout_secs``
    (the configured window, bounded by the turn ceiling and by the budget left in
    the running turn) -- one bound alone either ignores the configuration or
    outlives the turn.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("window", [300, 900])
    async def test_an_unanswered_prompt_declines_at_the_configured_window(
        self, window: int
    ) -> None:
        record = await run_turn(
            _prompt_turn(), config={"agent": {"tool_approval_timeout_secs": window}}
        )
        assert _declined_at(record) == window
        assert _cards(record) == [td.format_approval_timeout_card(float(window))]

    @pytest.mark.asyncio
    async def test_an_unattended_slot_takes_the_deny_fast_window(self) -> None:
        record = await run_turn(_prompt_turn(), slot=SlotSpec(key="worker-1", app="issue-radar"))
        assert _declined_at(record) == 180
        assert _cards(record) == [td.format_approval_timeout_card(180.0)]
        # The agent is told too: a denial it cannot read makes it retry forever.
        [notice] = [
            row["content"]
            for row in record.rows("assistant")
            if "running unattended" in row["content"]
        ]
        assert "no one answered within 180s" in notice

    @pytest.mark.asyncio
    async def test_a_late_prompt_is_shortened_to_the_budget_left(self) -> None:
        # 300 s of a 3600 s turn left: a 600 s window would outlive the turn,
        # which would then die as a turn timeout with no approval card at all.
        left = 300
        record = await run_turn(
            _prompt_turn(Wait(_CEILING - left)),
            config={"agent": {"chat_turn_timeout_secs": _CEILING}},
        )
        window = left - APPROVAL_TURN_MARGIN_SECS
        assert _declined_at(record) == _CEILING - left + window
        assert _cards(record) == [td.format_approval_timeout_card(float(window))]
        assert record.stop_reason == STOP_REASON_END_TURN

    @pytest.mark.asyncio
    async def test_no_budget_declines_without_waiting(self) -> None:
        # Under the margin no window can both wait and report.
        armed_at = _CEILING - 5
        record = await run_turn(
            _prompt_turn(Wait(armed_at)),
            slot=SlotSpec(autonudge=True),
            config={"agent": {"chat_turn_timeout_secs": _CEILING}},
        )
        assert _declined_at(record) == armed_at
        assert _cards(record) == [td.format_approval_no_budget_card()]
        assert record.approval_decisions[0]["cause"] == DENY_CAUSE_APPROVAL_NO_BUDGET
        # Nothing waited, so nothing stalled: no monitoring loop is told one did.
        assert record.notify_approval_stalled == []
        [steer] = _steers(record)
        assert "the turn had no budget left to host its approval prompt" in steer

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("slot", "window"),
        [
            (SlotSpec(key="chat-1", autonudge=True), 600),
            (SlotSpec(key="worker-1", app="issue-radar", autonudge=True), 180),
        ],
        ids=["attended", "unattended"],
    )
    async def test_an_expired_prompt_tells_the_loop_bound_to_the_slot(
        self, slot: SlotSpec, window: int
    ) -> None:
        # Loops are armed in ATTENDED slots too (a babysit loop a person armed),
        # so the signal must not depend on the slot being unattended.
        record = await run_turn(_prompt_turn(), slot=slot)
        assert record.notify_approval_stalled == [(slot.key, window)]

    @pytest.mark.asyncio
    async def test_an_answered_prompt_is_not_a_stall(self) -> None:
        record = await run_turn(
            _prompt_turn(answers={"req-1": REJECTED}), slot=SlotSpec(autonudge=True)
        )
        assert record.notify_approval_stalled == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "slot",
        [SlotSpec(key="chat-1"), SlotSpec(key="worker-1", app="issue-radar")],
        ids=["attended", "unattended"],
    )
    async def test_the_agent_is_told_in_band_once_before_the_reject(self, slot: SlotSpec) -> None:
        # kiro-cli reports the auto-decline as "User denied tool execution"; the
        # notice corrects that attribution, once, while the request is still open.
        record = await run_turn(_prompt_turn(), slot=slot)
        [steer] = _steers(record)
        assert "its approval prompt expired unanswered" in steer
        assert "This was NOT a user action" in steer
        assert "safety policy" not in steer
        window = 600 if not slot.app else 180
        assert f"fs_write: the approval prompt went unanswered for {window}s" in steer
        names = [call.name for call in record.provider_calls]
        assert names.index("steer") < names.index("reject_tool")
        [decision] = record.approval_decisions
        assert decision == {
            "approval_id": "req-1",
            "decision": "rejected",
            "by": "host",
            "cause": DENY_CAUSE_APPROVAL_TIMEOUT,
        }

    @pytest.mark.asyncio
    async def test_the_batch_cascade_is_told_the_hosts_reason(self) -> None:
        second = AcpEvent(
            kind=EVENT_PERMISSION_REQUEST, request_id="req-2", title="tool_b", tool_kind="edit"
        )
        record = await run_turn(TurnScript(events=[_PROMPT, second, _DONE]))
        first, cascade = _steers(record)
        assert "every remaining call in its batch" in cascade
        assert "the approval prompt went unanswered for 600s" in cascade
        assert [call.args for call in record.calls("reject_tool")] == [("req-1",), ("req-2",)]

    @pytest.mark.asyncio
    async def test_a_steer_that_never_returns_still_lets_the_reject_through(self) -> None:
        # Unbounded, a backpressured stdin would hold the steer until the turn
        # ceiling and skip both the reject and its audit.
        record = await run_turn(
            TurnScript(events=[_PROMPT, _AFTER, _DONE], provider=_SteerNeverReturns)
        )
        assert _declined_at(record) == 600 + STEER_NOTICE_BOUND_SECS
        assert record.audits(request_id="req-1", outcome="rejected")
        assert record.stop_reason == STOP_REASON_END_TURN

    @pytest.mark.asyncio
    async def test_the_host_decline_notice_stays_out_of_the_recovery_ledger(self) -> None:
        # tool_a is hook-blocked (a refusal: notice steered, reason recorded) and
        # its notice is echoed back as consumed; tool_b then expires. Its notice
        # pairs with no refusal reason, so it must not count against the ledger
        # that decides whether a recovery continuation is still owed.
        def _blocking_hooks(ctx: TurnContext) -> None:
            ctx.state._hook_store = _BlocksToolA()

        tool_a = AcpEvent(
            kind=EVENT_PERMISSION_REQUEST, request_id="req-a", title="tool_a", tool_kind="edit"
        )
        tool_b = AcpEvent(
            kind=EVENT_PERMISSION_REQUEST, request_id="req-b", title="tool_b", tool_kind="edit"
        )
        explained = await run_turn(
            TurnScript(
                events=[tool_a, Emit(_echo_last_steer), tool_b, _DONE],
                answers={"req-a": APPROVED},
                setup=_blocking_hooks,
            )
        )
        assert len(_steers(explained)) == 2
        assert explained.successors == ()
        # Control: with the refusal's notice NOT echoed, the continuation is owed,
        # so the empty successor list above is the ledger's answer, not silence.
        unexplained = await run_turn(
            TurnScript(
                events=[tool_a, tool_b, _DONE], answers={"req-a": APPROVED}, setup=_blocking_hooks
            )
        )
        [recovery] = unexplained.successors
        assert recovery.args[0].startswith(REFUSAL_RECOVERY_PREFIX)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("answer", [UNANSWERED, REJECTED, REJECTED_ONCE])
    async def test_a_declined_prompt_carries_the_turn_on(self, answer: str) -> None:
        # Both cards say the turn "carried on from the denial"; this is that.
        record = await run_turn(_prompt_turn(answers={"req-1": answer}))
        assert [row["content"] for row in record.rows("assistant")][-1] == "after the denial"
        assert record.stop_reason == STOP_REASON_END_TURN


def test_the_bounded_steer_is_never_wrapped_again_at_a_call_site() -> None:
    """Kept as a source pin: it guards duplication, which no turn can observe.

    The steer's bound lives inside ``_steer_policy_notice`` so every deny path
    inherits it and a new one cannot be added without it. A call site wrapping
    the helper in its own ``asyncio.wait_for`` behaves identically (same or a
    larger bound) -- the behavioural half is
    ``test_a_steer_that_never_returns_still_lets_the_reject_through`` -- so the
    one thing left to guard is the duplicated bound itself. Scanned over the
    runner and every ``chat_turn`` owner, so a move between them keeps it.
    Counted in ``test_source_pin_budget.py``.
    """
    from kiro_crew.dashboard import chat_runner, chat_turn

    sources = [Path(chat_runner.__file__)] + sorted(Path(chat_turn.__file__).parent.glob("*.py"))
    rewrapped = [
        f"{path.name}:{node.lineno}"
        for path in sources
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and ast.unparse(node.func) in ("asyncio.wait_for", "wait_for")
        and node.args
        and isinstance(node.args[0], ast.Call)
        and ast.unparse(node.args[0].func) == "_steer_policy_notice"
    ]
    assert rewrapped == [], f"the steer notice is bounded twice: {rewrapped}"


class TestAnswerReleasesTheApprovalHold:
    """A person's answer on the dashboard resumes a loop paused for approval.

    Gated on the host attribution the crew-log closer uses, so a timeout -- a host
    decision -- never releases it. One real turn each, through the harness.
    """

    @staticmethod
    def _releases(record: TurnRecord) -> list[str]:
        return [
            call.args[0] for call in record.autonudge_calls if call.name == "release_approval_hold"
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("answer", [APPROVED, REJECTED])
    async def test_a_persons_answer_releases_the_hold(self, answer: str) -> None:
        slot = SlotSpec(key="chat-1", autonudge=True)
        record = await run_turn(_prompt_turn(answers={"req-1": answer}), slot=slot)
        assert self._releases(record) == ["chat-1"]

    @pytest.mark.asyncio
    async def test_an_expired_prompt_does_not_release_it(self) -> None:
        slot = SlotSpec(key="chat-1", autonudge=True)
        record = await run_turn(_prompt_turn(), slot=slot)
        assert self._releases(record) == []
        assert record.notify_approval_stalled == [("chat-1", 600)]

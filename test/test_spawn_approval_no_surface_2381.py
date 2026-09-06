"""Regression tests for issue #2381 — a spawn approval nobody can answer must
be refused now, not held until the reaper.

The reported shape: a turn that originated on a channel (Telegram) calls
``spawn_run`` on a headless install. None of the four auto-approve rungs in
``subagent_manager/admission.py`` match, so the gate raises an interactive
prompt — and the only two surfaces that can render one are a Slack owner DM and
an attached dashboard client. With neither, the prompt is broadcast to nobody,
the run sits registered at ``turn 0`` with ``pid: null``, and the caller learns
nothing for ~30 minutes until the reaper kills it.

What this change fixes is only the *hang*. The approval callback now reports
"there is no surface here" by raising ``SpawnApprovalUnreachable`` at the exact
point it would have parked, and the spawn gate turns that into an immediate,
actionable refusal naming the rungs that would have let the spawn through.

Deliberately NOT covered, because the change does not make them: delivering the
prompt to the originating channel's own inline keyboard (the ideal fix, and much
larger — every channel renderer plus its approval decider), the per-agent
``auto_approve_spawn`` rung proposed on the issue, and any new tombstone field.
Both of the latter are open maintainer decisions on #2381.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.subagent import SpawnApprovalUnreachable, SubagentManager

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


# --------------------------------------------------------------------------
# Manager-level doubles (same posture as test_subagent_spawn_approval_parked_6484)
# --------------------------------------------------------------------------


def _mock_sessions() -> MagicMock:
    """SessionManager double with a DEFAULT install's posture: no session trust."""
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    provider = AsyncMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.context_usage_pct = lambda: 0.0

    async def _empty_stream(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        return
        yield  # noqa: unreachable — makes this an async generator

    provider.stream = MagicMock(side_effect=lambda *a, **kw: _empty_stream())
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_approval_policy = MagicMock(return_value="ask")
    return sessions


def _mock_ctx_builder() -> MagicMock:
    """ContextBuilder double with the default hooks posture (every rung off)."""
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = False
    return ctx


def _manager(approval, on_done=None) -> SubagentManager:  # type: ignore[no-untyped-def]
    return SubagentManager(
        sessions=_mock_sessions(),
        ctx_builder=_mock_ctx_builder(),
        on_spawn_approval=approval,
        on_done=on_done,
        is_yolo=lambda: False,
    )


async def _settle(mgr: SubagentManager, info) -> None:  # type: ignore[no-untyped-def]
    """Let the approval task run to its terminal state."""
    for _ in range(50):
        if info.done:
            return
        await asyncio.sleep(0)


async def _unreachable(_rid: str, _desc: str, _parent: str = "") -> bool:
    """Approval callback with nowhere to raise the prompt."""
    raise SpawnApprovalUnreachable("spawn:test")


async def _declined(_rid: str, _desc: str, _parent: str = "") -> bool:
    """Approval callback a human answered with "no"."""
    return False


class _ParkedApproval:
    """Approval callback that parks until released — a prompt that WAS delivered."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.calls = 0

    async def __call__(self, _rid: str, _desc: str, _parent: str = "") -> bool:
        self.calls += 1
        await self.gate.wait()
        return True


class TestUnansweredablePromptIsRefusedNow:
    """The bug: the gate waited out the reaper on a prompt nobody received."""

    @pytest.mark.asyncio
    async def test_refusal_is_immediate_and_actionable(self) -> None:
        """The whole report, in one assertion set.

        Red before this change: the raise fell into the gate's generic
        ``except Exception`` and became the same ``"spawn rejected"`` a human
        decline produces, so a caller could neither tell the two apart nor learn
        what to do. (On main the prompt does not raise at all: it parks, which is
        the hang itself.)
        """
        mgr = _manager(_unreachable)
        info = mgr.spawn("Return only the result of 1+1", parent_session_key="telegram:1")
        assert info is not None
        await _settle(mgr, info)

        assert info.done is True, "the spawn must not be left registered and waiting"
        assert "no surface could show the approval prompt" in info.error
        # Actionable for the agent means "who to ask", not "which key to flip" --
        # see the security ratchet below for why.
        assert "ask the operator" in info.error.lower()

    @pytest.mark.asyncio
    async def test_the_refusal_quotes_the_raiser_on_which_surface_was_missing(self) -> None:
        """The gate owns the rungs; the raiser owns the surfaces.

        Keeping the split is what stops the sentence going stale when a channel
        learns to deliver the prompt itself — the gate never names a surface it
        does not know about.
        """

        async def _named(_r: str, _d: str, _p: str = "") -> bool:
            raise SpawnApprovalUnreachable("no carrier pigeon is attached")

        mgr = _manager(_named)
        info = mgr.spawn("Return only the result of 1+1")
        assert info is not None
        await _settle(mgr, info)
        assert "no carrier pigeon is attached" in info.error

    @pytest.mark.asyncio
    async def test_a_bare_signal_still_reads_as_a_sentence(self) -> None:
        """A raiser that supplies no detail must not produce "()" in the prose."""

        async def _bare(_r: str, _d: str, _p: str = "") -> bool:
            raise SpawnApprovalUnreachable()

        mgr = _manager(_bare)
        info = mgr.spawn("Return only the result of 1+1")
        assert info is not None
        await _settle(mgr, info)
        assert "()" not in info.error
        assert "no interactive surface is attached" in info.error

    @pytest.mark.asyncio
    async def test_refusal_releases_the_slot(self) -> None:
        """A refused spawn must not hold a concurrency slot, or the next one queues."""
        mgr = _manager(_unreachable)
        info = mgr.spawn("Return only the result of 1+1")
        assert info is not None
        await _settle(mgr, info)
        assert mgr.count == 0

    @pytest.mark.asyncio
    async def test_refusal_reaches_the_caller(self) -> None:
        """The calling agent learns by completion event, so it must be announced."""
        seen: list[object] = []

        async def _on_done(info) -> None:  # type: ignore[no-untyped-def]
            seen.append(info)

        mgr = _manager(_unreachable, on_done=_on_done)
        info = mgr.spawn("Return only the result of 1+1")
        assert info is not None
        await _settle(mgr, info)
        for _ in range(20):
            if seen:
                break
            await asyncio.sleep(0)
        assert seen and seen[0] is info

    @pytest.mark.asyncio
    async def test_refusal_is_audited_with_its_own_reason(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """An auditor must be able to separate a decline from an undeliverable prompt.

        ``outcome`` keeps its existing value on purpose — this IS a rejection —
        so the distinguishing fact has to be in the metadata.
        """
        import kiro_crew.subagent as subagent_mod

        calls: list[dict] = []
        recorder = MagicMock()
        recorder.log_tool_invocation = MagicMock(side_effect=lambda **kw: calls.append(kw))
        monkeypatch.setattr(subagent_mod, "sel", lambda: recorder)

        mgr = _manager(_unreachable)
        info = mgr.spawn("Return only the result of 1+1")
        assert info is not None
        await _settle(mgr, info)

        rejects = [c for c in calls if c.get("outcome") == "rejected"]
        assert rejects, f"no rejection was audited: {calls!r}"
        assert (rejects[-1].get("metadata") or {}).get("reason") == "no_approval_surface"

    @pytest.mark.asyncio
    async def test_it_says_so_in_the_log(self, caplog) -> None:  # type: ignore[no-untyped-def]
        """An operator grepping for the run id finds the cause, not just the effect.

        Captured at the root logger: the coordinators are rebound onto
        ``kiro_crew.subagent``'s namespace (``bind_component_globals``), so the
        record is emitted under that name rather than under ``admission``.
        """
        mgr = _manager(_unreachable)
        with caplog.at_level(logging.WARNING):
            info = mgr.spawn("Return only the result of 1+1")
            assert info is not None
            await _settle(mgr, info)
        keyed = [r.getMessage() for r in caplog.records if info.id in r.getMessage()]
        assert keyed, f"no log record names run {info.id}"
        assert any("surface" in m for m in keyed), keyed

    @pytest.mark.asyncio
    async def test_the_operator_log_names_every_rung(self, caplog) -> None:  # type: ignore[no-untyped-def]
        """The how-to has an audience, and it is the human reading the logs.

        Every rung that would have admitted this spawn must be somewhere, or the
        refusal tells an operator on a channel nothing they can act on. It goes
        in the log rather than in ``info.error`` for the reason the ratchet below
        records.
        """
        mgr = _manager(_unreachable)
        with caplog.at_level(logging.WARNING):
            info = mgr.spawn("Return only the result of 1+1")
            assert info is not None
            await _settle(mgr, info)
        logged = " ".join(r.getMessage() for r in caplog.records if info.id in r.getMessage())
        for rung in (
            'approval_mode="auto"',
            "Trust",
            "hooks.auto_approve_subagent_spawn",
            "hooks.auto_approve_sources",
        ):
            assert rung in logged, f"operator log does not name {rung!r}: {logged!r}"

    @pytest.mark.asyncio
    async def test_the_agent_facing_refusal_carries_no_bypass_recipe(self) -> None:
        """The refusal must not teach the agent how to remove its own gate.

        Design Review r3 on PR #8914. ``info.error`` reaches the calling agent as
        a completion event -- automation input -- and two of the four rungs are
        ``config.json`` edits. ``security.py`` records that ``config.json`` is
        "writable by any auto-approved agent shell", so naming those keys here
        hands the party this gate CONSTRAINS the recipe for removing it: an
        unattended or prompt-injected agent hits the refusal, follows the
        instruction, and the human spawn-approval gate is gone with nobody ever
        having seen a prompt.

        The operator still gets the full how-to (test above); this pins that the
        agent does not.
        """
        mgr = _manager(_unreachable)
        info = mgr.spawn("Return only the result of 1+1")
        assert info is not None
        await _settle(mgr, info)
        for followable in (
            "config.json",
            "hooks.auto_approve_subagent_spawn",
            "hooks.auto_approve_sources",
            'approval_mode="auto"',
        ):
            assert (
                followable not in info.error
            ), f"agent-facing refusal leaks a self-grant recipe: {followable!r}"


class TestTheOtherTwoOutcomesAreUnchanged:
    """The refusal must not swallow a human decline, nor a prompt that landed."""

    @pytest.mark.asyncio
    async def test_a_human_decline_keeps_its_plain_refusal(self) -> None:
        mgr = _manager(_declined)
        info = mgr.spawn("Return only the result of 1+1")
        assert info is not None
        await _settle(mgr, info)
        assert info.done is True
        assert info.error == "spawn rejected"
        assert info.error_code == "", "a decline is a decision, not a misconfiguration"

    @pytest.mark.asyncio
    async def test_a_delivered_prompt_still_waits_for_its_answer(self) -> None:
        """The 1800s deadline is still correct when a surface DID receive the prompt."""
        approval = _ParkedApproval()
        mgr = _manager(approval)
        try:
            info = mgr.spawn("Return only the result of 1+1")
            assert info is not None
            for _ in range(20):
                await asyncio.sleep(0)
            assert approval.calls == 1
            assert info.done is False
            assert info._awaiting_approval is True
            assert mgr.count == 1
        finally:
            approval.gate.set()
            for t in list(mgr._tasks.values()):
                t.cancel()
            await asyncio.sleep(0)


# --------------------------------------------------------------------------
# The probe, and the callback that consults it
# --------------------------------------------------------------------------


def _probe(state: object, *, slack: object = MagicMock()) -> bool:
    """Run ``_dashboard_client_attached`` against a hand-built gateway."""
    from kiro_crew.slack.gateway import GatewayOrchestrator

    fake = SimpleNamespace(slack=slack, _owner_id="U123", dashboard_state=state)
    return GatewayOrchestrator._dashboard_client_attached(fake)  # type: ignore[arg-type]


def _hub_count(sockets: list[dict]) -> int:
    """Run the real ``dashboard_user_ws_count`` over hand-built sockets."""
    from kiro_crew.dashboard.websocket_hub import WebSocketHub

    class _Sock:
        def __init__(self, *, closed: bool, dashboard_user: bool) -> None:
            self.closed = closed
            self._d = {"_is_dashboard_user": dashboard_user}

        def get(self, key: str, default: object = None) -> object:
            return self._d.get(key, default)

    owner = SimpleNamespace(
        _ws_clients=[
            _Sock(closed=s.get("closed", False), dashboard_user=s["dashboard_user"])
            for s in sockets
        ]
    )
    hub = WebSocketHub.__new__(WebSocketHub)
    object.__setattr__(hub, "_owner", owner)
    return WebSocketHub.dashboard_user_ws_count(hub)


class TestDashboardUserSocketCount:
    """Only a dashboard USER's socket is somebody who could answer.

    GPT round 2 on PR #8914. An app token registers on ``/api/ws`` as well, with
    ``_is_dashboard_user`` False, and the broadcast chokepoint
    (``_ws_client_allowed``) sends it an owner-surface frame only if its manifest
    declared that event — so counting it reports a surface that received nothing,
    which is round 1's Slack defect in a second location.
    """

    def test_an_app_token_socket_does_not_count(self) -> None:
        assert _hub_count([{"dashboard_user": False}]) == 0

    def test_a_dashboard_user_socket_counts(self) -> None:
        assert _hub_count([{"dashboard_user": True}]) == 1

    def test_an_app_socket_beside_a_dashboard_user_does_not_inflate_the_count(self) -> None:
        assert _hub_count([{"dashboard_user": False}, {"dashboard_user": True}]) == 1

    def test_a_closed_socket_does_not_count(self) -> None:
        """The registry prunes lazily, on the next broadcast."""
        assert _hub_count([{"dashboard_user": True, "closed": True}]) == 0


class TestDashboardClientProbe:
    """What counts as a client that could answer the prompt."""

    def test_no_attached_client_is_no_surface(self) -> None:
        state = MagicMock()
        state.dashboard_user_ws_count = MagicMock(return_value=0)
        assert _probe(state) is False

    def test_an_attached_client_is_a_surface(self) -> None:
        state = MagicMock()
        state.dashboard_user_ws_count = MagicMock(return_value=1)
        assert _probe(state) is True

    def test_the_probe_asks_for_dashboard_users_not_every_socket(self) -> None:
        """A raw ``ws_client_count`` would count an app token as a human."""
        state = MagicMock()
        state.dashboard_user_ws_count = MagicMock(return_value=0)
        state.ws_client_count = MagicMock(return_value=7)
        assert _probe(state) is False
        state.ws_client_count.assert_not_called()

    def test_a_configured_slack_owner_dm_does_not_make_it_reachable(self) -> None:
        """The probe's one call site is reached only AFTER Slack has had its turn.

        A Slack term here reports a surface that demonstrably did not receive the
        prompt — either it was never configured, or posting to it raised — which
        is the park this whole change removes. Both a configured and an absent
        Slack must therefore read the same.
        """
        state = MagicMock()
        state.dashboard_user_ws_count = MagicMock(return_value=0)
        assert _probe(state, slack=MagicMock()) is False
        assert _probe(state, slack=None) is False

    def test_no_dashboard_state_is_no_surface(self) -> None:
        assert _probe(None) is False

    def test_an_unreadable_client_count_is_treated_as_attached(self) -> None:
        """Fail toward today's behaviour: a broken probe must not refuse a spawn."""
        state = MagicMock()
        state.dashboard_user_ws_count = MagicMock(side_effect=RuntimeError("boom"))
        assert _probe(state) is True


def _callback(  # type: ignore[no-untyped-def]
    monkeypatch, *, clients: int, raise_when_unreachable: bool, slack: object = None
):
    """Build one real ``_interactive_approval`` closure over a fake gateway."""
    from kiro_crew.slack import gateway as gateway_mod

    monkeypatch.setattr(
        gateway_mod, "safety_override", lambda: SimpleNamespace(is_active=lambda: False)
    )
    state = MagicMock()
    state._slots = {}
    state.dashboard_user_ws_count = MagicMock(return_value=clients)
    state.request_approval = AsyncMock(return_value=True)
    fake = SimpleNamespace(
        slack=slack,
        _owner_id="U123" if slack is not None else "",
        dashboard_state=state,
        _cfg=SimpleNamespace(hooks={}),
        _approval_mode="interactive",
        autonudge_svc=None,
        sessions=None,
    )
    # The closure calls ``self._dashboard_client_attached()``; bind the REAL one so
    # the probe under test is production code, not a stand-in.
    fake._dashboard_client_attached = (  # type: ignore[attr-defined]
        lambda: gateway_mod.GatewayOrchestrator._dashboard_client_attached(fake)  # type: ignore[arg-type]
    )
    cb = gateway_mod.GatewayOrchestrator._interactive_approval(
        fake,  # type: ignore[arg-type]
        "subagent",
        raise_when_unreachable=raise_when_unreachable,
    )
    return cb, state


def _event():  # type: ignore[no-untyped-def]
    from kiro_crew.providers.base import LLMEvent

    return LLMEvent(kind="permission_request", request_id="spawn:abc123", title="spawn_run(x)")


class TestOnlyTheSpawnGateRaises:
    """The signal is opt-in, and it fires exactly where the park would have been."""

    @pytest.mark.asyncio
    async def test_spawn_gate_raises_instead_of_parking(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        cb, state = _callback(monkeypatch, clients=0, raise_when_unreachable=True)
        with pytest.raises(SpawnApprovalUnreachable):
            await cb(_event())
        state.request_approval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_spawn_gate_still_prompts_an_attached_dashboard(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        cb, state = _callback(monkeypatch, clients=1, raise_when_unreachable=True)
        assert await cb(_event()) is True
        state.request_approval.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_failed_slack_post_still_fails_fast(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """A configured owner DM that cannot be posted to is not a surface.

        GPT round 1 on PR #8914. The Slack branch is wrapped in its own
        ``except`` and falls through to the dashboard-only fallback, so a Slack
        outage on a channel install used to land right back in the park this
        change exists to remove.
        """
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D123")
        slack.post_blocks = AsyncMock(side_effect=RuntimeError("slack is down"))
        cb, state = _callback(monkeypatch, clients=0, raise_when_unreachable=True, slack=slack)
        with pytest.raises(SpawnApprovalUnreachable):
            await cb(_event())
        state.request_approval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_tool_approval_never_raises(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """A mid-run tool prompt has no terminal path, so it must keep parking.

        Same zero-client posture that makes the spawn gate raise above.
        """
        cb, state = _callback(monkeypatch, clients=0, raise_when_unreachable=False)
        assert await cb(_event()) is True
        state.request_approval.assert_awaited_once()

    def test_tool_approvals_stay_wired_to_the_non_raising_instance(self) -> None:
        """Source ratchet: the two callbacks must not collapse back into one.

        Wiring ``on_tool_approval`` to the raising instance would silently convert
        every unanswered mid-run tool prompt into a hard failure — a behavioural
        test on the manager cannot see which closure the gateway passed.
        """
        from pathlib import Path

        import kiro_crew.slack.gateway as gateway_mod

        src = Path(gateway_mod.__file__).read_text(encoding="utf-8")
        assert "on_tool_approval=_approve_subagent," in src
        assert "return await _approve_spawn_gate(event, parent_session_key)" in src
        assert src.count("raise_when_unreachable=True") == 1

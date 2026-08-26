"""Liveness-attributed stall detection (issue #3920).

Separate module so the pre-existing idle-time tests in
``test_subagent_stall.py`` stay a readable record of the fallback behaviour.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.liveness import (
    VERDICT_DEAD,
    VERDICT_STUCK_INPUT,
    VERDICT_UNKNOWN,
    VERDICT_WORKING,
)
from kiro_crew.subagent import SubagentInfo, SubagentManager


def _make_manager(stall_idle_secs: int = 120) -> SubagentManager:
    mgr = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        stall_idle_secs=stall_idle_secs,
    )
    mgr._fire_event = AsyncMock()
    return mgr


def _info(**overrides) -> SubagentInfo:
    info = SubagentInfo(id="a1b2c3d4", task="t", agent="")
    for k, v in overrides.items():
        setattr(info, k, v)
    return info


def _event(*, title="Running: sleep 600", is_shell=True, tool_input='{"command": "sleep 600"}'):
    """Minimal stand-in for the AcpEvent the subagent loop receives."""
    ev = MagicMock()
    ev.title = title
    ev.tool_input = tool_input
    ev.is_shell = is_shell
    ev.tool_name = "execute_bash" if is_shell else "some_mcp_tool"
    return ev


def _oracle(verdict: str, evidence: str = "ev") -> MagicMock:
    oracle = MagicMock()
    oracle.check_tool.return_value = (verdict, evidence)
    oracle.fresh.return_value = oracle
    return oracle


# ── the plumbing that was missing ────────────────────────────────────


def test_dispatch_snapshot_captures_the_trusted_shell_fields():
    """The loop used to keep only ``title`` and drop the rest of the event.

    That discarded exactly what the oracle needs to attribute evidence: the
    command to cmdline-match against, and the TRUSTED ``is_shell`` /
    ``tool_name`` from ``_meta.kiro`` (never the LLM-authored title).
    """
    info = _info()
    SubagentManager._note_tool_dispatch(info, _event())
    tool = info._inflight_tool
    assert tool is not None
    assert tool.is_shell is True
    assert tool.command == '{"command": "sleep 600"}'
    assert tool.tool_name == "execute_bash"
    assert tool.dispatch_ts > 0


def test_tool_result_clears_the_snapshot():
    """A returned tool must not be judged against on a later idle stretch."""
    info = _info()
    SubagentManager._note_tool_dispatch(info, _event())
    assert info._inflight_tool is not None
    SubagentManager._clear_tool_dispatch(info)
    assert info._inflight_tool is None


def test_a_new_dispatch_retires_the_oracle_rather_than_clearing_it():
    """A walk still running against the previous command holds the old instance,
    so clearing in place would let its late write become the new baseline."""
    info = _info()
    old = _oracle(VERDICT_WORKING)
    info._stall_oracle = old
    SubagentManager._note_tool_dispatch(info, _event())
    old.fresh.assert_called_once()


# ── _stall_verdict attribution gates ─────────────────────────────────


@pytest.mark.asyncio
async def test_declines_when_no_tool_is_in_flight():
    """Idle with nothing dispatched is a model-wait, and the oracle's model-wait
    branch reads the whole runtime subtree — not attributable on a shared
    runtime, so decline rather than guess."""
    mgr = _make_manager()
    info = _info(_pid=4242, _inflight_tool=None)
    verdict, evidence = await mgr._stall_verdict(info)
    assert verdict == VERDICT_UNKNOWN
    assert "no tool in flight" in evidence


@pytest.mark.asyncio
async def test_declines_for_a_non_shell_tool():
    """A non-shell MCP tool has no child process to cmdline-match, so the oracle
    could only offer the same unattributable subtree aggregate."""
    mgr = _make_manager()
    info = _info(_pid=4242)
    SubagentManager._note_tool_dispatch(info, _event(is_shell=False))
    verdict, evidence = await mgr._stall_verdict(info)
    assert verdict == VERDICT_UNKNOWN
    assert "not attributable" in evidence


@pytest.mark.asyncio
async def test_consult_failure_degrades_to_unknown():
    """A blown-up /proc read must not take down the reaper sweep.

    The degrade is ``consult_offloaded``'s, not this module's -- the evidence
    string is asserted as that shared guard's, so re-copying the guard here
    would fail this test rather than pass it silently.
    """
    mgr = _make_manager()
    info = _info(_pid=4242)
    SubagentManager._note_tool_dispatch(info, _event())
    boom = MagicMock()
    boom.check_tool.side_effect = RuntimeError("proc read blew up")
    with patch("kiro_crew.subagent.LivenessOracle", return_value=boom):
        verdict, evidence = await mgr._stall_verdict(info)
    assert verdict == VERDICT_UNKNOWN
    assert evidence == "oracle offload error"


@pytest.mark.asyncio
async def test_consult_runs_off_the_event_loop():
    """The consult is a synchronous /proc walk (``iter_descendants`` plus
    ``os.readlink`` on ``/proc/<pid>/fd/*``, which can block on the very wedged
    fd being investigated). The reaper runs on the loop that also serves every
    chat turn and the heartbeat, so running it inline would freeze the gateway
    until the loop-stall watchdog killed it. Assert it is handed to a worker
    thread, not called on the loop.
    """
    mgr = _make_manager()
    info = _info(_pid=4242)
    SubagentManager._note_tool_dispatch(info, _event())

    loop_thread = threading.get_ident()
    ran_on: list[int] = []

    def _probe(_pid, _tool):
        ran_on.append(threading.get_ident())
        return VERDICT_WORKING, "shell child 5 alive"

    oracle = MagicMock()
    oracle.check_tool.side_effect = _probe
    with patch("kiro_crew.subagent.LivenessOracle", return_value=oracle):
        verdict, _ = await mgr._stall_verdict(info)

    assert verdict == VERDICT_WORKING
    assert ran_on and ran_on[0] != loop_thread, "consult must not run on the event loop"


@pytest.mark.asyncio
async def test_only_one_consult_outstanding_per_agent():
    """A permanently wedged /proc read must not leave a new blocked worker behind
    on every reaper sweep — that would starve the shared subprocess pool that
    teardown also draws from. While a walk is still in flight the next sweep
    answers UNKNOWN instead of submitting another.
    """
    mgr = _make_manager()
    info = _info(_pid=4242)
    SubagentManager._note_tool_dispatch(info, _event())

    release = threading.Event()
    started = threading.Event()

    def _wedged(_pid, _tool):
        started.set()
        release.wait(timeout=30)
        return VERDICT_WORKING, "eventually"

    oracle = MagicMock()
    oracle.check_tool.side_effect = _wedged
    try:
        with patch("kiro_crew.subagent.LivenessOracle", return_value=oracle):
            # First sweep: submits the walk, then times out waiting on it.
            # The bounded await lives in ``consult_offloaded``, so the seam is
            # patched there rather than in this module.
            with patch("kiro_crew.acp.liveness.asyncio.wait_for", side_effect=asyncio.TimeoutError):
                first, _ = await mgr._stall_verdict(info)
            assert first == VERDICT_UNKNOWN
            # The walk is genuinely in a worker thread and still wedged there.
            assert started.wait(timeout=10), "consult never reached the worker"

            # Second sweep, walk still wedged: declines without submitting again.
            second, evidence = await mgr._stall_verdict(info)

            # Retiring the snapshot must NOT free the in-flight handle. The
            # generation bump invalidates a stale verdict, but the walk is still
            # pinned to a worker thread, so a later sweep under a NEW dispatch
            # must keep declining rather than strand a second blocked worker on
            # the same wedged fd.
            SubagentManager._clear_tool_dispatch(info)
            SubagentManager._note_tool_dispatch(info, _event())
            third, third_evidence = await mgr._stall_verdict(info)
        assert second == VERDICT_UNKNOWN
        assert "prior consult still in flight" in evidence
        assert third == VERDICT_UNKNOWN
        assert "prior consult still in flight" in third_evidence
        assert oracle.check_tool.call_count == 1
    finally:
        release.set()


# ── _maybe_flag_stall decisions ──────────────────────────────────────


@pytest.mark.asyncio
async def test_working_child_is_not_flagged():
    """The regression this issue exists for: a subagent whose own child is alive
    is silent, not stalled. The earlier whole-subtree attempt got this backwards
    — it read kiro-cli's background traffic as progress and never flagged a
    genuinely wedged agent; here the evidence is the matched child itself."""
    mgr = _make_manager(stall_idle_secs=10)
    now = 1_000.0
    # Idle past the threshold but BELOW the suppression ceiling, which is the
    # window where a WORKING reading is trusted (see the ceiling test below).
    info = _info(turns=1, _pid=4242, last_activity=now - 25, _stall_suspect_at=now - 60)
    SubagentManager._note_tool_dispatch(info, _event())
    with (
        patch(
            "kiro_crew.subagent.LivenessOracle",
            return_value=_oracle(VERDICT_WORKING, "shell child 5150 alive"),
        ),
        patch("kiro_crew.subagent.record_slow_command") as rec,
    ):
        await mgr._maybe_flag_stall("a1b2c3d4", info, now)
    assert info.stalled is False
    mgr._fire_event.assert_not_called()
    rec.assert_not_called()
    # Suspicion stays OPEN so the badge appears the moment that child stops.
    assert info._stall_suspect_at > 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verdict,evidence",
    [
        (VERDICT_DEAD, "shell child 5150 exited 12s ago, no result frame"),
        (VERDICT_STUCK_INPUT, "child 5150 blocked reading tty"),
    ],
)
async def test_wedged_verdict_flags_immediately_without_two_sweep(verdict, evidence):
    """DEAD/STUCK_INPUT is positive evidence about THIS agent's child, not a
    guess from elapsed silence, so it must not wait for the second sweep."""
    mgr = _make_manager(stall_idle_secs=10)
    now = 1_000.0
    # _stall_suspect_at is 0.0 — no prior sweep has suspected this agent.
    info = _info(turns=1, _pid=4242, last_activity=now - 200)
    SubagentManager._note_tool_dispatch(info, _event())
    with (
        patch("kiro_crew.subagent.LivenessOracle", return_value=_oracle(verdict, evidence)),
        patch("kiro_crew.subagent.record_slow_command"),
    ):
        await mgr._maybe_flag_stall("a1b2c3d4", info, now)
    assert info.stalled is True
    _etype, _info_arg, extra = mgr._fire_event.await_args.args
    assert extra["stalled"] is True
    assert extra["idle_secs"] >= 200
    # The verdict/evidence are deliberately kept off the wire (no consumer reads
    # them and the event is app-sdk-forwarded); the log line carries them.
    assert "verdict" not in extra
    assert "evidence" not in extra


@pytest.mark.asyncio
async def test_unknown_still_needs_the_two_sweep_confirmation():
    """With no attributable evidence the gate must behave exactly as before."""
    mgr = _make_manager(stall_idle_secs=10)
    now = 1_000.0
    info = _info(turns=1, _pid=4242, last_activity=now - 200)
    SubagentManager._note_tool_dispatch(info, _event(is_shell=False))  # -> UNKNOWN
    with patch("kiro_crew.subagent.record_slow_command") as rec:
        await mgr._maybe_flag_stall("a1b2c3d4", info, now)
        # Sweep 1: suspect only.
        assert info.stalled is False and info._stall_suspect_at > 0
        mgr._fire_event.assert_not_called()
        rec.assert_not_called()
        # Sweep 2: now it flags.
        await mgr._maybe_flag_stall("a1b2c3d4", info, now + 60)
    assert info.stalled is True
    _etype, _info_arg, extra = mgr._fire_event.await_args.args
    assert extra["stalled"] is True


@pytest.mark.asyncio
async def test_wedged_verdict_still_never_reaps():
    """Surface-only is unchanged: escalating DEAD to an early kill would be a
    change to reap semantics and is deliberately not part of this."""
    mgr = _make_manager(stall_idle_secs=10)
    mgr._force_reap = AsyncMock()
    now = 1_000.0
    info = _info(turns=1, _pid=4242, last_activity=now - 200)
    SubagentManager._note_tool_dispatch(info, _event())
    with (
        patch("kiro_crew.subagent.LivenessOracle", return_value=_oracle(VERDICT_DEAD, "gone")),
        patch("kiro_crew.subagent.record_slow_command"),
    ):
        await mgr._maybe_flag_stall("a1b2c3d4", info, now)
    assert info.stalled is True
    assert info.done is False
    mgr._force_reap.assert_not_called()


@pytest.mark.asyncio
async def test_activity_clears_the_flag_and_retires_the_oracle():
    mgr = _make_manager(stall_idle_secs=10)
    info = _info(turns=1, _pid=4242, stalled=True)
    oracle = _oracle(VERDICT_WORKING)
    info._stall_oracle = oracle
    await mgr._touch_activity(info)
    assert info.stalled is False
    assert info._stall_suspect_at == 0.0
    oracle.fresh.assert_called_once()


@pytest.mark.asyncio
async def test_working_cannot_suppress_the_badge_forever():
    """Attribution is not infallible: under ``session_sharing`` a wedged agent can
    cmdline-match a SIBLING's live child and read WORKING indefinitely. Unbounded
    that turns a case the old idle-time path DID badge into a permanent false
    negative — worse than what it replaces, since the badge is self-clearing and a
    missing badge is not. Past the ceiling the badge wins."""
    from kiro_crew.subagent import _SUPPRESS_CEILING

    mgr = _make_manager(stall_idle_secs=10)
    now = 1_000.0
    oracle = _oracle(VERDICT_WORKING, "shell child 5150 alive")

    # Below the ceiling: WORKING suppresses, as designed.
    below = _info(turns=1, _pid=4242, last_activity=now - 10 * _SUPPRESS_CEILING + 5)
    SubagentManager._note_tool_dispatch(below, _event())
    with (
        patch("kiro_crew.subagent.LivenessOracle", return_value=oracle),
        patch("kiro_crew.subagent.record_slow_command"),
    ):
        await mgr._maybe_flag_stall("a1b2c3d4", below, now)
    assert below.stalled is False, "WORKING should still suppress below the ceiling"

    # Past the ceiling: the same WORKING reading no longer holds the badge back.
    over = _info(
        turns=1,
        _pid=4242,
        last_activity=now - 10 * _SUPPRESS_CEILING - 50,
        _stall_suspect_at=now - 60,
    )
    SubagentManager._note_tool_dispatch(over, _event())
    with (
        patch("kiro_crew.subagent.LivenessOracle", return_value=oracle),
        patch("kiro_crew.subagent.record_slow_command"),
    ):
        await mgr._maybe_flag_stall("a1b2c3d4", over, now)
    assert over.stalled is True, "a WORKING reading suppressed the badge past the ceiling"


# ── round-2 review findings ──────────────────────────────────────────


def test_a_non_final_tool_result_keeps_the_snapshot():
    """``EVENT_TOOL_RESULT`` also carries non-completed progress updates
    (``_dispatch`` sets ``tool_final = status == "completed"``). Retiring the
    snapshot on one of those would drop attribution mid-command and judge the
    rest of a long silent tool on idle time alone — badging a healthy agent."""
    info = _info(_pid=4242)
    SubagentManager._note_tool_dispatch(info, _event())
    gen = info._stall_gen

    progress = _event()
    progress.tool_final = False
    SubagentManager._note_tool_result(info, progress)
    assert info._inflight_tool is not None, "a non-final result dropped the snapshot"
    assert info._stall_gen == gen

    final = _event()
    final.tool_final = True
    SubagentManager._note_tool_result(info, final)
    assert info._inflight_tool is None
    assert info._stall_gen == gen + 1


@pytest.mark.asyncio
async def test_a_verdict_superseded_mid_consult_is_discarded():
    """The offloaded ``/proc`` walk is awaited, so the snapshot it was submitted
    for can be retired while it runs. Applying that verdict afterwards would let
    a DEAD reading — which skips the two-sweep confirmation — flag an agent that
    has already resumed working."""
    mgr = _make_manager(stall_idle_secs=10)
    info = _info(turns=1, _pid=4242)
    SubagentManager._note_tool_dispatch(info, _event())

    oracle = MagicMock()
    oracle.fresh.return_value = oracle

    # check_tool runs in the executor; while it "walks", the agent goes active.
    def _walk_then_activity(_pid, _tool):
        info._stall_gen += 1  # what _touch_activity / a final result would do
        return (VERDICT_DEAD, "shell child 5150 exited, no result frame")

    oracle.check_tool.side_effect = _walk_then_activity
    with patch("kiro_crew.subagent.LivenessOracle", return_value=oracle):
        verdict, evidence = await mgr._stall_verdict(info)
    assert verdict == VERDICT_UNKNOWN, "a stale DEAD verdict was applied"
    assert evidence == "superseded mid-consult"


@pytest.mark.asyncio
async def test_activity_bumps_the_generation():
    mgr = _make_manager()
    info = _info(_pid=4242)
    before = info._stall_gen
    await mgr._touch_activity(info)
    assert info._stall_gen == before + 1


# ── the wedged-skip is withdrawn when a sibling could be the measured child ──


async def _flag_with_dead(mgr, info, now):
    """Run one sweep with the oracle returning DEAD, and report whether it flagged."""
    SubagentManager._note_tool_dispatch(info, _event())
    with (
        patch(
            "kiro_crew.subagent.LivenessOracle",
            return_value=_oracle(VERDICT_DEAD, "shell child 5150 exited, no result frame"),
        ),
        patch("kiro_crew.subagent.record_slow_command"),
    ):
        await mgr._maybe_flag_stall("a1b2c3d4", info, now)
    return info.stalled


def _register(mgr, *infos):
    """Put agents in the manager's registry so _live_shared_count can see them."""
    for i, info in enumerate(infos):
        mgr._agents[f"agent{i}"] = info


@pytest.mark.asyncio
async def test_dead_does_not_skip_two_sweep_when_a_sibling_shares_the_runtime():
    """A DEAD verdict rides the same fallible cmdline match that `_SUPPRESS_CEILING`
    exists to bound. With a live sibling on the same runtime pid, that sibling's
    child exiting would otherwise raise an immediate badge on a healthy agent —
    skipping the very dampening that keeps the badge trustworthy at scale. So the
    skip is withdrawn and the verdict must hold across two sweeps."""
    mgr = _make_manager(stall_idle_secs=10)
    now = 1_000.0
    victim = _info(turns=1, _pid=4242, last_activity=now - 25, _session_sharing=True)
    sibling = _info(turns=1, _pid=4242, _session_sharing=True)
    sibling.id = "sibling00"
    _register(mgr, victim, sibling)
    assert mgr._live_shared_count(4242) == 2, "test setup: siblings must share the pid"

    # Sweep 1: no immediate flag — it only becomes a suspect.
    assert await _flag_with_dead(mgr, victim, now) is False, "DEAD skipped dampening"
    assert victim._stall_suspect_at > 0
    mgr._fire_event.assert_not_called()

    # Sweep 2: the reading held, so now it flags.
    assert await _flag_with_dead(mgr, victim, now + 60) is True


@pytest.mark.asyncio
async def test_dead_does_not_skip_two_sweep_for_a_lone_agent_in_a_shared_runtime():
    """A LONE session-sharing subagent must also lose the fast path.

    The confusable co-tenant is not only a sibling. ``_create_shared_session``
    puts the subagent on the PARENT's AcpRuntime — one process hosts everything —
    so ``info._pid`` is the parent's process and the parent's own tool children
    are descendants of it. ``_live_shared_count`` iterates the subagent registry
    and cannot see the parent, so gating on a sibling count > 1 left this agent
    on the fast path while it could still cmdline-match the parent's child and
    flag the instant that child exited. A shared runtime always contains the
    parent, so every session-sharing agent takes the two-sweep path.
    """
    mgr = _make_manager(stall_idle_secs=10)
    now = 1_000.0
    info = _info(turns=1, _pid=4242, last_activity=now - 25, _session_sharing=True)
    _register(mgr, info)
    assert mgr._live_shared_count(4242) == 1, "test setup: no live sibling, parent unseen"

    # Sweep 1: the sibling count says "alone", but the parent shares the pid.
    assert await _flag_with_dead(mgr, info, now) is False, "lone shared agent kept fast path"
    assert info._stall_suspect_at > 0

    # Sweep 2: held across two sweeps, so now it flags.
    assert await _flag_with_dead(mgr, info, now + 60) is True


@pytest.mark.asyncio
async def test_dead_still_skips_two_sweep_without_session_sharing():
    """Unshared runtime: the pid is the agent's own, so the match cannot land on
    anyone else's child — no sibling's and no parent's — and the immediate flag
    keeps its original warrant. This is the branch that still earns the fast
    path, and the reason the gate is not simply unconditional."""
    mgr = _make_manager(stall_idle_secs=10)
    now = 1_000.0
    info = _info(turns=1, _pid=4242, last_activity=now - 25, _session_sharing=False)
    _register(mgr, info)
    assert await _flag_with_dead(mgr, info, now) is True


@pytest.mark.asyncio
async def test_a_done_sibling_does_not_restore_the_fast_path():
    """A finished sibling holds no child that could be matched, but the parent
    still does, so a session-sharing agent does not get the fast path back just
    because its siblings retired."""
    mgr = _make_manager(stall_idle_secs=10)
    now = 1_000.0
    info = _info(turns=1, _pid=4242, last_activity=now - 25, _session_sharing=True)
    finished = _info(turns=1, _pid=4242, _session_sharing=True, done=True)
    finished.id = "finished0"
    _register(mgr, info, finished)
    assert mgr._live_shared_count(4242) == 1, "a done sibling must not count as live"
    assert await _flag_with_dead(mgr, info, now) is False, "shared agent kept fast path"

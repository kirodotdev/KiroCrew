"""Reaping a wedged RUNNING wave member so a pending wave closes promptly.

``_maybe_flag_stall`` is surface-only: a running member judged DEAD/STUCK_INPUT
keeps its concurrency slot until the multi-hour wall-clock reaper. When that
member belongs to the pending wave, the stuck-wave reconciler skips the wave
(it has a non-done member), so ``batch_members_pending`` stays True and the
orchestrator hangs for the whole ``subagent_timeout_secs``. Failing only the
queued tail cannot release a same-wave wedged member; settling the RUNNING
member is what releases the wave.

``_maybe_reap_wedged_member`` is the terminating path. It fires only on a
TRUSTED positive wedge (``_wedge_verdict``, never idle time, never a
``session_sharing`` match), confirmed across two sweeps, respecting the user's
stop controls, and only when the wedge genuinely strands a pending wave (no
healthy sibling slot would close it). Everything else falls through to the
wall-clock reaper unchanged.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.subagent import (
    VERDICT_DEAD,
    VERDICT_STUCK_INPUT,
    VERDICT_UNKNOWN,
    VERDICT_WORKING,
    SubagentInfo,
    SubagentManager,
)


def _make_manager(stall_idle_secs: int = 120) -> SubagentManager:
    mgr = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        stall_idle_secs=stall_idle_secs,
    )
    mgr._fire_event = AsyncMock()
    mgr._force_reap = AsyncMock()
    # The confirming sweep re-consults the oracle live; default it to a wedge so
    # a test exercising the reap path reaches it. Tests that need the re-consult
    # to disagree override this.
    mgr._stall_verdict = AsyncMock(return_value=(VERDICT_STUCK_INPUT, "flat subtree, stdin"))
    return mgr


def _info(agent_id: str = "a1b2c3d4", **overrides) -> SubagentInfo:
    info = SubagentInfo(id=agent_id, task="t", agent="")
    for k, v in overrides.items():
        setattr(info, k, v)
    return info


def _wedged_member(agent_id: str, batch_id: str = "w1", **overrides) -> SubagentInfo:
    """A flagged, trusted-wedge RUNNING wave member — the state the surface
    flag leaves behind for a DEAD/STUCK_INPUT verdict off a shared runtime."""
    base = dict(
        turns=1,
        started=1_000.0,
        last_activity=1_000.0,
        batch_id=batch_id,
        batch_total=4,
        stalled=True,
        _wedge_verdict=VERDICT_STUCK_INPUT,
        _session_sharing=False,
    )
    base.update(overrides)
    return _info(agent_id, **base)


# ── the terminating path, in isolation ────────────────────────────────────


@pytest.mark.asyncio
async def test_wedged_wave_member_reaped_after_two_sweeps():
    """A trusted-wedge running member stranding its wave is reaped — but only
    on the SECOND confirming sweep (a terminating path never trusts one)."""
    mgr = _make_manager()
    info = _wedged_member("m1")
    mgr._agents[info.id] = info
    now = 5_000.0

    reaped = await mgr._maybe_reap_wedged_member(info.id, info, now)
    assert reaped is False  # sweep 1: suspect only
    assert info._wedge_reap_suspect_at == now
    mgr._force_reap.assert_not_called()

    reaped = await mgr._maybe_reap_wedged_member(info.id, info, now + 60)
    assert reaped is True
    mgr._force_reap.assert_awaited_once()
    args, kwargs = mgr._force_reap.await_args
    assert args[0] == info.id
    assert kwargs["reason"] == "wedged"
    # The confirming sweep RE-CONSULTED the oracle rather than trusting the
    # cached verdict.
    mgr._stall_verdict.assert_awaited()


@pytest.mark.asyncio
async def test_not_reaped_when_reconsult_reads_working():
    """A run fed input between sweeps resumes silent work: the confirming sweep
    re-consults and reads WORKING, so it is spared. The FULL stall state is
    cleared (stalled + suspect) so the flag path can re-stamp a later wedge."""
    mgr = _make_manager()
    info = _wedged_member("m1", _wedge_reap_suspect_at=4_900.0, _stall_suspect_at=4_900.0)
    mgr._agents[info.id] = info
    mgr._stall_verdict = AsyncMock(return_value=(VERDICT_WORKING, "live matched child"))
    assert await mgr._maybe_reap_wedged_member(info.id, info, 5_000.0) is False
    mgr._force_reap.assert_not_called()
    # Full stall state cleared so _maybe_flag_stall (which consults only when
    # `not stalled`) can stamp a fresh verdict if it wedges again.
    assert info.stalled is False
    assert info._stall_suspect_at == 0.0
    assert info._wedge_verdict == ""
    assert info._wedge_reap_suspect_at == 0.0


@pytest.mark.asyncio
async def test_not_reaped_when_reconsult_reads_unknown():
    """Evidence not attributable on the confirming sweep (UNKNOWN) is not
    positive proof, so the run is not reaped — but the wedge verdict is KEPT so
    the NEXT sweep retries confirmation (an UNKNOWN can be transient). Clearing
    it would demote the member to a healthy sibling and stall the whole wave."""
    mgr = _make_manager()
    info = _wedged_member("m1", _wedge_reap_suspect_at=4_900.0)
    mgr._agents[info.id] = info
    mgr._stall_verdict = AsyncMock(return_value=(VERDICT_UNKNOWN, "consult in flight"))
    assert await mgr._maybe_reap_wedged_member(info.id, info, 5_000.0) is False
    mgr._force_reap.assert_not_called()
    assert info.stalled is True
    assert info._wedge_verdict == VERDICT_STUCK_INPUT  # kept for the retry
    assert info._wedge_reap_suspect_at == 0.0  # only the sweep clock reset


@pytest.mark.asyncio
async def test_unknown_then_dead_reaps_on_the_retry():
    """After a transient UNKNOWN the next sweep re-marks the suspect, and a
    following sweep whose re-consult reads DEAD reaps — the member is never lost."""
    mgr = _make_manager()
    info = _wedged_member("m1", _wedge_reap_suspect_at=4_900.0)
    mgr._agents[info.id] = info
    mgr._stall_verdict = AsyncMock(return_value=(VERDICT_UNKNOWN, "offload error"))
    assert await mgr._maybe_reap_wedged_member(info.id, info, 5_000.0) is False  # UNKNOWN
    # Next sweep: suspect re-marked (verdict still set).
    assert await mgr._maybe_reap_wedged_member(info.id, info, 5_060.0) is False
    assert info._wedge_reap_suspect_at == 5_060.0
    # Following sweep: re-consult reads DEAD -> reap.
    mgr._stall_verdict = AsyncMock(return_value=(VERDICT_DEAD, "child exited"))
    assert await mgr._maybe_reap_wedged_member(info.id, info, 5_120.0) is True
    mgr._force_reap.assert_awaited_once()


@pytest.mark.asyncio
async def test_native_child_snapshot_is_never_reaped():
    """A DEAD verdict read against an id-less native-child snapshot (whose
    terminal frame never reaches the sub-agent loop) must not terminate: the
    child exiting reads DEAD while the member itself is healthy and thinking."""
    from kiro_crew.acp.liveness import InFlightToolTracker

    mgr = _make_manager()
    info = _wedged_member("m1", _wedge_verdict=VERDICT_DEAD, _wedge_reap_suspect_at=4_900.0)
    # Make the tracker's judged call id-less (a native-child dispatch).
    tracker = InFlightToolTracker()
    tracker.dispatch("", object())  # id-less -> current_is_idless is True
    info._tool_tracker = tracker
    assert info._inflight_is_native_child is True
    mgr._agents[info.id] = info
    mgr._stall_verdict = AsyncMock(return_value=(VERDICT_DEAD, "child exited"))
    assert await mgr._maybe_reap_wedged_member(info.id, info, 5_000.0) is False
    mgr._force_reap.assert_not_called()
    assert info._wedge_verdict == ""  # suspicion dropped so a fresh wedge re-earns it


@pytest.mark.asyncio
async def test_reconsult_still_dead_reaps():
    """A still-DEAD re-consult on the confirming sweep reaps."""
    mgr = _make_manager()
    info = _wedged_member("m1", _wedge_verdict=VERDICT_DEAD, _wedge_reap_suspect_at=4_900.0)
    mgr._agents[info.id] = info
    mgr._stall_verdict = AsyncMock(return_value=(VERDICT_DEAD, "child exited, no result"))
    assert await mgr._maybe_reap_wedged_member(info.id, info, 5_000.0) is True
    mgr._force_reap.assert_awaited_once()


@pytest.mark.asyncio
async def test_idle_time_flag_is_never_reaped():
    """A stall flagged by idle time (UNKNOWN verdict, empty _wedge_verdict) is
    surface-only and must never be reaped, however long it sits."""
    mgr = _make_manager()
    info = _wedged_member("m1", _wedge_verdict="")  # idle-time flag, not a wedge
    mgr._agents[info.id] = info
    assert await mgr._maybe_reap_wedged_member(info.id, info, 5_000.0) is False
    assert await mgr._maybe_reap_wedged_member(info.id, info, 5_060.0) is False
    mgr._force_reap.assert_not_called()


@pytest.mark.asyncio
async def test_session_sharing_wedge_is_never_reaped():
    """On a shared runtime the cmdline match can land on a sibling's/parent's
    child, so a wedge verdict there must never terminate."""
    mgr = _make_manager()
    info = _wedged_member("m1", _session_sharing=True)
    mgr._agents[info.id] = info
    assert await mgr._maybe_reap_wedged_member(info.id, info, 5_000.0) is False
    assert await mgr._maybe_reap_wedged_member(info.id, info, 5_060.0) is False
    mgr._force_reap.assert_not_called()


@pytest.mark.asyncio
async def test_user_stop_and_approval_are_respected():
    """A run the user stopped, already ending, or parked at an approval prompt
    is left alone even with a confirmed wedge."""
    mgr = _make_manager()
    for field, value in (
        ("user_stopped", True),
        ("_reap_started", True),
        ("_awaiting_approval", True),
        ("done", True),
    ):
        info = _wedged_member("m1", _wedge_reap_suspect_at=4_900.0, **{field: value})
        mgr._agents = {info.id: info}
        assert await mgr._maybe_reap_wedged_member(info.id, info, 5_000.0) is False
    mgr._force_reap.assert_not_called()


@pytest.mark.asyncio
async def test_member_with_other_outstanding_calls_is_not_reaped():
    """The verdict attributes to one call (``_inflight_tool``'s subtree). With a
    second tool call still outstanding — a silent build running beside a later
    stdin-blocked command — a wedge on the latest proves nothing about the other,
    and ``_force_reap`` resets the whole session. The member is left alone so the
    sibling call's work is not destroyed."""
    mgr = _make_manager()
    info = _wedged_member("m1", _wedge_reap_suspect_at=4_900.0)
    info._active_tool_calls["build"] = object()
    info._active_tool_calls["repl"] = object()
    mgr._agents[info.id] = info
    assert await mgr._maybe_reap_wedged_member(info.id, info, 5_000.0) is False
    mgr._force_reap.assert_not_called()


@pytest.mark.asyncio
async def test_second_call_dispatched_during_the_consult_spares_the_member():
    """A second tool call can be dispatched while the awaited liveness consult is
    in flight. The post-consult re-check counts the calls again and declines, so
    the member is not reaped even though the oracle still reads wedged."""
    mgr = _make_manager()
    info = _wedged_member("m1", _wedge_reap_suspect_at=4_900.0)
    info._active_tool_calls["repl"] = object()  # one call at the first guard
    mgr._agents[info.id] = info

    async def _dispatch_second_then_report_wedged(_info):
        _info._active_tool_calls["build"] = object()  # a second call appears
        return (VERDICT_STUCK_INPUT, "flat subtree, stdin")

    mgr._stall_verdict = AsyncMock(side_effect=_dispatch_second_then_report_wedged)
    assert await mgr._maybe_reap_wedged_member(info.id, info, 5_000.0) is False
    mgr._force_reap.assert_not_called()


@pytest.mark.asyncio
async def test_slot_released_member_is_never_reaped():
    """A member that intentionally gave up its lane slot while keeping its
    process (``lane_wait_for_status`` under ``interactive_command_policy="wait"``)
    is left alone even on a confirmed, wave-stranding wedge: it holds no slot
    that gates the wave, and force-reaping it would destroy the process state it
    is deliberately preserving."""
    mgr = _make_manager()
    # Confirmed wedge, suspect already stamped so a member holding its slot is
    # reaped on this sweep — the only difference here is the released slot.
    info = _wedged_member("m1", _slot_released=True, _wedge_reap_suspect_at=4_900.0)
    mgr._agents[info.id] = info
    assert await mgr._maybe_reap_wedged_member(info.id, info, 5_000.0) is False
    mgr._force_reap.assert_not_called()


@pytest.mark.asyncio
async def test_slot_released_during_the_awaited_consult_spares_the_member():
    """The slot can be released concurrently with the awaited live re-consult
    (another coroutine runs while ``_stall_verdict`` awaits). The post-consult
    re-check must see the fresh ``_slot_released`` and decline, so the member is
    not reaped even though the oracle still reads wedged."""
    mgr = _make_manager()
    info = _wedged_member("m1", _wedge_reap_suspect_at=4_900.0)
    mgr._agents[info.id] = info

    async def _release_slot_then_report_wedged(_info):
        # Model the slot being yielded while the consult is in flight.
        _info._slot_released = True
        return (VERDICT_STUCK_INPUT, "flat subtree, stdin")

    mgr._stall_verdict = AsyncMock(side_effect=_release_slot_then_report_wedged)
    assert await mgr._maybe_reap_wedged_member(info.id, info, 5_000.0) is False
    mgr._force_reap.assert_not_called()


@pytest.mark.asyncio
async def test_standalone_wedged_run_is_not_reaped_here():
    """A wedged run outside a wave harms only itself; it is left to the
    wall-clock reaper, not this wave backstop."""
    mgr = _make_manager()
    info = _wedged_member("m1", batch_id="", _wedge_reap_suspect_at=4_900.0)
    mgr._agents[info.id] = info
    assert await mgr._maybe_reap_wedged_member(info.id, info, 5_000.0) is False
    mgr._force_reap.assert_not_called()


# ── gate-saturation guard: a healthy sibling closes the wave on its own ────


@pytest.mark.asyncio
async def test_not_reaped_while_a_healthy_sibling_still_runs():
    """Gate saturation: a lone STUCK_INPUT member beside a
    healthy running sibling is NOT reaped — the healthy slot closes the wave
    on its own, so the wedge is not what strands it."""
    mgr = _make_manager()
    wedged = _wedged_member("m1", _wedge_reap_suspect_at=4_900.0)
    healthy = _info("m2", batch_id="w1", batch_total=4, turns=1, stalled=False)
    mgr._agents = {wedged.id: wedged, healthy.id: healthy}
    assert await mgr._maybe_reap_wedged_member(wedged.id, wedged, 5_000.0) is False
    mgr._force_reap.assert_not_called()


@pytest.mark.asyncio
async def test_reaped_when_every_live_sibling_is_also_wedged():
    """When every non-done member of the wave is itself confirmed-wedged,
    nothing but a reap will ever close it — so the wedge IS reaped."""
    mgr = _make_manager()
    w1 = _wedged_member("m1", _wedge_reap_suspect_at=4_900.0)
    w2 = _wedged_member("m2", _wedge_reap_suspect_at=4_900.0)
    done = _info("m3", batch_id="w1", batch_total=4, done=True)
    mgr._agents = {w1.id: w1, w2.id: w2, done.id: done}
    assert await mgr._maybe_reap_wedged_member(w1.id, w1, 5_000.0) is True
    mgr._force_reap.assert_awaited_once()


# ── the flag path stamps the trusted wedge; idle/shared paths do not ───────


@pytest.mark.asyncio
async def test_flag_stamps_wedge_verdict_for_a_trusted_wedge():
    """A trusted DEAD/STUCK_INPUT flag records _wedge_verdict so the reaper can
    act on positive evidence, not an idle-time guess."""
    mgr = _make_manager()
    now = 1_000.0
    info = _info(turns=1, last_activity=now - 5_000, batch_id="w1")
    with (
        patch.object(
            mgr, "_stall_verdict", AsyncMock(return_value=(VERDICT_STUCK_INPUT, "flat subtree"))
        ),
        patch("kiro_crew.subagent.record_slow_command"),
    ):
        await mgr._maybe_flag_stall(info.id, info, now)
    assert info.stalled is True
    assert info._wedge_verdict == VERDICT_STUCK_INPUT


@pytest.mark.asyncio
async def test_flag_does_not_stamp_wedge_on_session_sharing():
    """On a shared runtime the wedge verdict is withdrawn (two-sweep idle path),
    so _wedge_verdict stays empty and the reaper never acts."""
    mgr = _make_manager()
    now = 1_000.0
    info = _info(
        turns=1,
        last_activity=now - 5_000,
        batch_id="w1",
        _session_sharing=True,
        _stall_suspect_at=now - 60,  # sweep 1 already past (2-sweep idle rule)
    )
    with (
        patch.object(
            mgr, "_stall_verdict", AsyncMock(return_value=(VERDICT_STUCK_INPUT, "flat subtree"))
        ),
        patch("kiro_crew.subagent.record_slow_command"),
    ):
        await mgr._maybe_flag_stall(info.id, info, now)
    assert info.stalled is True
    assert info._wedge_verdict == ""


@pytest.mark.asyncio
async def test_touch_activity_clears_wedge_markers():
    """Fresh activity withdraws the wedge suspicion so the reaper starts over."""
    mgr = _make_manager()
    info = _wedged_member("m1", _wedge_reap_suspect_at=4_900.0)
    await mgr._touch_activity(info)
    assert info._wedge_verdict == ""
    assert info._wedge_reap_suspect_at == 0.0

"""Tests for subagent idle-stall detection and activity tracking (subagent.py).

Covers ``SubagentManager._maybe_flag_stall`` (surface-only: after
``_stall_idle_secs`` of no stream activity it marks the subagent stalled,
emits a ``subagent_stalled`` UI signal, and records the slow command for
later analysis — but NEVER terminates the agent) and ``_touch_activity``
(records activity and clears a prior stalled flag).

The main-agent watchdog stack does not govern spawned subagents, so the flag is
raised by the reaper. Idle time alone cannot separate a hung tool call from a
slow silent one, so the flag is gated on a ``LivenessOracle`` consult
(``_stall_verdict``) that attributes evidence to the subagent's OWN child
process by cmdline match — which works even on a session-shared runtime, where a
whole-subtree aggregate would be dominated by kiro-cli's background traffic.
``WORKING`` suppresses the badge; ``DEAD``/``STUCK_INPUT`` flag immediately
(positive evidence needs no two-sweep dampening); ``UNKNOWN`` falls back to
idle-time-only, the pre-existing behaviour. It remains surface-only: a
genuinely-hung subagent is still closed by the user, never auto-reaped.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


# ── _maybe_flag_stall ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_not_flagged_before_start():
    """A subagent with no turn and no runtime pid is pre-start — never stalled."""
    mgr = _make_manager(stall_idle_secs=120)
    now = 1_000.0
    info = _info(turns=0, _pid=None, last_activity=now - 5_000)
    await mgr._maybe_flag_stall("a1b2c3d4", info, now)
    assert info.stalled is False
    mgr._fire_event.assert_not_called()


@pytest.mark.asyncio
async def test_flagged_when_idle_beyond_threshold():
    """Two-sweep confirmation (scale dampening): the first sweep past the
    idle threshold marks a suspect (no event); the second consecutive sweep
    flags stalled and emits the event + slow-command record."""
    mgr = _make_manager(stall_idle_secs=120)
    now = 1_000.0
    info = _info(turns=1, last_activity=now - 200)
    with patch("kiro_crew.subagent.record_slow_command") as rec:
        await mgr._maybe_flag_stall("a1b2c3d4", info, now)
        # Sweep 1: suspect only — no flag, no event, no record.
        assert info.stalled is False and info._stall_suspect_at > 0
        mgr._fire_event.assert_not_called()
        rec.assert_not_called()
        await mgr._maybe_flag_stall("a1b2c3d4", info, now + 60)
    assert info.stalled is True  # surfaced, but not terminated
    mgr._fire_event.assert_awaited_once()
    etype, _info_arg, extra = mgr._fire_event.await_args.args
    assert etype == "subagent_stalled"
    assert extra["stalled"] is True
    assert extra["idle_secs"] >= 200
    rec.assert_called_once()  # slow command recorded for analysis


@pytest.mark.asyncio
async def test_not_flagged_within_idle_window():
    mgr = _make_manager(stall_idle_secs=120)
    now = 1_000.0
    info = _info(_pid=4242, last_activity=now - 30)
    await mgr._maybe_flag_stall("a1b2c3d4", info, now)
    assert info.stalled is False
    mgr._fire_event.assert_not_called()


@pytest.mark.asyncio
async def test_not_re_emitted_when_already_stalled():
    mgr = _make_manager(stall_idle_secs=120)
    now = 1_000.0
    info = _info(turns=1, last_activity=now - 300, stalled=True)
    with patch("kiro_crew.subagent.record_slow_command") as rec:
        await mgr._maybe_flag_stall("a1b2c3d4", info, now)
    mgr._fire_event.assert_not_called()  # already flagged; no duplicate event
    rec.assert_not_called()  # and no duplicate record


@pytest.mark.asyncio
async def test_not_flagged_while_awaiting_approval():
    """A subagent blocked on a human approval prompt is healthy, not stalled —
    even well past the idle threshold it must not be flagged or recorded."""
    mgr = _make_manager(stall_idle_secs=120)
    now = 1_000.0
    info = _info(turns=1, last_activity=now - 700, _awaiting_approval=True)
    with patch("kiro_crew.subagent.record_slow_command") as rec:
        await mgr._maybe_flag_stall("a1b2c3d4", info, now)
    assert info.stalled is False
    mgr._fire_event.assert_not_called()
    rec.assert_not_called()


@pytest.mark.asyncio
async def test_slow_command_record_fields():
    """The recorded slow command carries the diagnostic fields for analysis."""
    mgr = _make_manager(stall_idle_secs=120)
    now = 1_000.0
    info = _info(
        turns=3, tool_count=5, last_tool="fs_read", last_activity=now - 200,
        started=now - 260, parent_session_key="dashboard:main",
        _stall_suspect_at=now - 60,  # sweep 1 already suspected (2-sweep rule)
    )
    with patch("kiro_crew.subagent.record_slow_command") as rec:
        await mgr._maybe_flag_stall("a1b2c3d4", info, now)
    rec.assert_called_once()
    kwargs = rec.call_args.kwargs
    assert kwargs["last_tool"] == "fs_read"
    assert kwargs["tool_count"] == 5
    assert kwargs["turns"] == 3
    assert kwargs["idle_secs"] >= 200
    assert kwargs["parent_session"] == "dashboard:main"


@pytest.mark.asyncio
async def test_flag_never_reaps():
    """Surface-only: flagging a stall must never force-reap the agent."""
    mgr = _make_manager(stall_idle_secs=120)
    mgr._force_reap = AsyncMock()
    now = 1_000.0
    info = _info(turns=1, started=now - 5_000, last_activity=now - 5_000,
                 _stall_suspect_at=now - 60)  # sweep 1 already suspected (2-sweep rule)
    with patch("kiro_crew.subagent.record_slow_command"):
        await mgr._maybe_flag_stall("a1b2c3d4", info, now)
    assert info.stalled is True
    assert info.done is False
    mgr._force_reap.assert_not_called()


# ── _touch_activity ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_touch_activity_updates_timestamp():
    mgr = _make_manager()
    info = _info(last_activity=0.0)
    await mgr._touch_activity(info)
    assert info.last_activity > 0.0
    mgr._fire_event.assert_not_called()  # was not stalled -> no event


@pytest.mark.asyncio
async def test_touch_activity_clears_stalled_and_notifies():
    mgr = _make_manager()
    info = _info(stalled=True, last_activity=0.0)
    await mgr._touch_activity(info)
    assert info.stalled is False
    mgr._fire_event.assert_awaited_once()
    etype, _info_arg, extra = mgr._fire_event.await_args.args
    assert etype == "subagent_stalled"
    assert extra["stalled"] is False

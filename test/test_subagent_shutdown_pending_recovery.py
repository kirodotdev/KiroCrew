"""A run the shutdown ends while its cancel-recovery is pending is left for the next start.

An unexpected cancel hands the run to ``<id>:recovery`` (``_resume_guarded`` in
``subagent_manager/cancellation.py``), which waits for the original task's teardown and
then for a free slot before it respawns. Two arms can end the run in that window:

* ``cancel_all`` cancels the pending recovery (its ``CancelledError`` arm, report-free);
* the respawn cannot happen while the in-app restart's ``close_all`` runs (its failure
  arm, which reports through the finalize claim).

Like ``_run``'s arms, each asks ``_leave_for_next_start`` before it writes a tombstone:
under a dashboard tab the run gets none, its report skips the parent, and the next
start's orphan sweep announces it. A cron parent and a user stop keep the tombstone.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from subagent_turn_resilience_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _close_subagent_managers,
    _context_overflow_error,
    _mock_ctx_builder,
    _mock_sessions,
    _text_event,
)

from kiro_crew import session_surface
from kiro_crew import subagent_persistence as sp
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.testing.wait import await_until, default_timeout

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

_DASHBOARD_PARENT = "dashboard:chat-7"


def _hanging_stream_factory(started: asyncio.Event):
    def stream_factory(msg: str, *a, **kw):
        async def _gen():
            yield _text_event("partial work ")
            started.set()
            await asyncio.Event().wait()  # hang until cancelled

        return _gen()

    return stream_factory


def _manager_with_recorder(sessions: MagicMock) -> tuple[SubagentManager, AsyncMock]:
    on_done = AsyncMock()
    mgr = SubagentManager(sessions=sessions, ctx_builder=_mock_ctx_builder(), on_done=on_done)
    mgr._should_use_session_sharing = MagicMock(return_value=False)
    return mgr, on_done


async def _cancel_unexpectedly(mgr: SubagentManager, started: asyncio.Event, parent: str):
    """Spawn under *parent* and cancel it from outside; return it with its pending recovery.

    The recovery is registered in the same step that hands the run over, so it is in
    ``_tasks`` once the cancel has been delivered.
    """
    info = mgr.spawn("job whose recovery is pending", parent_session_key=parent)
    assert info is not None
    await await_until(started, what="the stream's first yield")
    assert info._folder_seeded is True
    original = mgr._tasks[info.id]
    original.cancel()  # not a user stop, not the shutdown
    return info, original


async def _until_ended(task: asyncio.Task) -> None:  # type: ignore[type-arg]
    # ``asyncio.wait`` resumes the test in the same loop turn the task ends, before the
    # recovery's first capacity check; ``await_until`` would add a turn.
    done, _ = await asyncio.wait({task}, timeout=default_timeout())
    assert task in done, "the cancelled run never ended"


def _recovery_of(mgr: SubagentManager, info: SubagentInfo) -> asyncio.Task:  # type: ignore[type-arg]
    recovery = mgr._tasks.get(f"{info.id}:recovery")
    assert recovery is not None, f"no pending recovery; tasks={sorted(mgr._tasks)}"
    return recovery


async def _shutdown_cancels_the_pending_recovery(
    parent: str, *, user_stopped: bool = False
) -> tuple[SubagentInfo, AsyncMock]:
    """The original task's teardown hangs in its reset, so the recovery is still waiting
    on it when ``cancel_all`` cancels every task."""
    started = asyncio.Event()
    in_reset = asyncio.Event()
    sessions = _mock_sessions(_hanging_stream_factory(started))

    async def _hanging_reset(_key, **_kw):
        in_reset.set()
        await asyncio.Event().wait()

    sessions.reset = AsyncMock(side_effect=_hanging_reset)
    mgr, on_done = _manager_with_recorder(sessions)
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info, _ = await _cancel_unexpectedly(mgr, started, parent)
        await await_until(in_reset, what="the original task's teardown reset")
        recovery = _recovery_of(mgr, info)
        assert info._recovering is True and not recovery.done()
        info.user_stopped = user_stopped
        await await_until(asyncio.ensure_future(mgr.cancel_all()), what="cancel_all")
    assert recovery.cancelled()
    assert info.done is True
    assert info.error == "cancelled"
    return info, on_done


@pytest.mark.asyncio
async def test_a_pending_recovery_the_shutdown_cancels_is_announced_by_the_next_start(
    monkeypatch,
):
    monkeypatch.setattr(session_surface, "_dashboard_surfaced", frozenset())
    info, on_done = await _shutdown_cancels_the_pending_recovery(_DASHBOARD_PARENT)

    assert info._left_for_next_start is True
    on_done.assert_not_awaited()  # the arm stays report-free
    assert sp.read_tombstone(info.id) is None
    assert [state["id"] for state in sp.list_orphans()] == [info.id]

    notify = AsyncMock(return_value=True)
    next_start = SubagentManager(sessions=MagicMock(), ctx_builder=None, on_orphan_notify=notify)
    next_start._is_pid_alive = MagicMock(return_value=False)
    with patch("kiro_crew.subagent.sel"):
        await await_until(asyncio.ensure_future(next_start._reconcile_orphans()), what="the sweep")

    notify.assert_awaited_once()
    notified_parent, msg = notify.await_args.args[:2]
    assert notified_parent == _DASHBOARD_PARENT
    assert f"Agent `{info.id}` ⚠️ cut off mid-turn by gateway restart" in msg


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parent", "user_stopped"),
    [
        pytest.param("cron:nightly-digest", False, id="cron-parent"),
        pytest.param(_DASHBOARD_PARENT, True, id="user-stopped"),
    ],
)
async def test_a_pending_recovery_the_shutdown_cancels_keeps_its_tombstone(
    monkeypatch, parent, user_stopped
):
    monkeypatch.setattr(session_surface, "_dashboard_surfaced", frozenset({parent}))
    info, on_done = await _shutdown_cancels_the_pending_recovery(parent, user_stopped=user_stopped)

    assert info._left_for_next_start is False
    on_done.assert_not_awaited()  # report-free, as before
    assert sp.read_tombstone(info.id)["cause"] == "cancelled"
    assert sp.list_orphans() == []


async def _recovery_fails_during_the_in_app_restart(parent: str) -> tuple[SubagentInfo, AsyncMock]:
    """The pool stays full while ``close_all`` runs without ``cancel_all``, so the
    recovery's slot wait gives up and ends in its failure arm."""
    started = asyncio.Event()
    sessions = _mock_sessions(_hanging_stream_factory(started))
    sessions.final_drain_started = True  # read only by the leave-for-next-start rule here
    mgr, on_done = _manager_with_recorder(sessions)
    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent._RECOVERY_SLOT_WAIT_SECS", 0.4),
    ):
        info, original = await _cancel_unexpectedly(mgr, started, parent)
        # Filled as soon as the original task's teardown ends, before the recovery's
        # first capacity check (the order the slot-wait tests rely on).
        await _until_ended(original)
        mgr._max_concurrent = 1
        mgr._running_count = 1
        recovery = _recovery_of(mgr, info)
        await await_until(asyncio.gather(recovery, return_exceptions=True), what="the recovery")
        # The terminal report runs as its own shielded task; let it settle.
        await await_until(
            asyncio.gather(*list(mgr._report_tasks), return_exceptions=True),
            what="the terminal report",
        )
        mgr._running_count = 0
    assert mgr._shutting_down is False
    assert info.done is True
    assert info.error == "cancelled (recovery failed)"
    return info, on_done


@pytest.mark.asyncio
async def test_a_recovery_the_in_app_restart_fails_is_left_for_the_next_start(monkeypatch):
    monkeypatch.setattr(session_surface, "_dashboard_surfaced", frozenset())
    info, on_done = await _recovery_fails_during_the_in_app_restart(_DASHBOARD_PARENT)

    assert info._left_for_next_start is True
    on_done.assert_not_awaited()  # the report skips the closing tab
    assert sp.read_tombstone(info.id) is None
    await _assert_the_next_start_announces(info)


async def _assert_the_next_start_announces(info: SubagentInfo) -> None:
    assert [state["id"] for state in sp.list_orphans()] == [info.id]
    notify = AsyncMock(return_value=True)
    next_start = SubagentManager(sessions=MagicMock(), ctx_builder=None, on_orphan_notify=notify)
    next_start._is_pid_alive = MagicMock(return_value=False)
    with patch("kiro_crew.subagent.sel"):
        await await_until(asyncio.ensure_future(next_start._reconcile_orphans()), what="the sweep")
    notify.assert_awaited_once()
    notified_parent, msg = notify.await_args.args[:2]
    assert notified_parent == _DASHBOARD_PARENT
    assert f"Agent `{info.id}` ⚠️ cut off mid-turn by gateway restart" in msg


@pytest.mark.asyncio
async def test_a_context_overflow_give_up_during_the_in_app_restart_is_reported_now(monkeypatch):
    # The dedicated-session recovery failing closed is the run's own outcome, not the
    # shutdown's: it keeps its error tombstone and its report even under a tab.
    monkeypatch.setattr(session_surface, "_dashboard_surfaced", frozenset())
    sessions = _mock_sessions(lambda _msg: None)
    sessions.final_drain_started = True
    sessions.forget_conversation_if_sid = MagicMock(return_value=(False, "successor-sid"))
    mgr, on_done = _manager_with_recorder(sessions)
    shared_provider = MagicMock()
    shared_provider.shutdown = AsyncMock()

    async def run_inner(info: SubagentInfo, _session_key: str) -> None:
        info._session_sharing = True
        info._shared_provider = shared_provider
        info._session_id = "rejected-sid"
        raise _context_overflow_error()

    mgr._run_inner = AsyncMock(side_effect=run_inner)
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("overflowing job", parent_session_key=_DASHBOARD_PARENT)
        assert info is not None and info._folder_seeded is True
        original = mgr._tasks[info.id]
        await _until_ended(original)
        await await_until(
            asyncio.gather(_recovery_of(mgr, info), return_exceptions=True), what="the recovery"
        )
        await await_until(
            asyncio.gather(*list(mgr._report_tasks), return_exceptions=True),
            what="the terminal report",
        )

    assert "recovery could not start" in info.error
    assert info._left_for_next_start is False
    on_done.assert_awaited_once_with(info)
    assert sp.read_tombstone(info.id)["cause"] == "error"


@pytest.mark.asyncio
async def test_a_recovery_the_in_app_restart_fails_under_a_cron_parent_is_reported_now(
    monkeypatch,
):
    monkeypatch.setattr(session_surface, "_dashboard_surfaced", frozenset())
    info, on_done = await _recovery_fails_during_the_in_app_restart("cron:nightly-digest")

    assert info._left_for_next_start is False
    on_done.assert_awaited_once_with(info)
    assert sp.read_tombstone(info.id)["cause"] == "cancelled"

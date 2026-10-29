"""Tests for sub-agent turn resilience (subagent.py).

Covers the three guard-parity fixes that bring sub-agents in line with the
main agent's turn-resilience ladder:

1. Transient-backend retry inside ``_run_inner`` (``_stream_with_transient_retry``):
   pre-token re-prompt / post-token CONTINUE, budget-capped, ``subagent_retrying``
   UI event. Mirrors chat_runner B1/B2 (PR #91).
2. User-stop semantics (``cancel``): neutral terminal state — partial output
   preserved, ``user_stop`` tombstone, ``subagent_done`` carries ``stopped: true``.
3. Unexpected-cancel one-shot auto-continue (``_schedule_cancel_recovery``):
   a non-user, non-shutdown task cancellation respawns the run exactly once.
   Mirrors the main path's cancel recovery.
4. Orphan-notification wiring: ``_try_inject_orphan_notification`` /
   ``_send_orphan_slack_dm`` delegate to the gateway-wired callbacks instead of
   being stubs.

The cases under the ``1.`` and ``1b.`` section banners, and the default-budget case, are in
``test_subagent_turn_resilience_retry.py``; the context-window overflow cases are in
``test_subagent_turn_resilience_context_overflow.py``; the helpers the three modules share
are in ``subagent_turn_resilience_helpers.py``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from subagent_turn_resilience_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _close_subagent_managers,
    _complete_event,
    _FatalError,
    _manager,
    _mock_ctx_builder,
    _mock_sessions,
    _spawn_and_wait,
    _text_event,
    _TransientError,
)

from kiro_crew import session_surface
from kiro_crew import subagent_persistence as sp
from kiro_crew.llm_helpers import TRANSIENT_RETRIES
from kiro_crew.subagent import (
    _TRANSIENT_CONTINUE_MSG,
    SubagentInfo,
    SubagentManager,
)
from kiro_crew.testing.wait import async_wait_until, await_until, default_timeout

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")


# Subagent-registry isolation is provided globally by the autouse
# ``_isolate_subagents_dir`` fixture in ``conftest.py``.

# POSITIVE WAITS here -- the mocked stream reaching its first yield, a cancelled run's
# replacement task appearing in ``_tasks``, a recovery task ending -- ride on the
# event, task or state the code emits (``await_until`` / ``async_wait_until``), under
# the helpers' lost-run bound: half the suite's ``--timeout=120``, so a real hang
# fails by name instead of as an opaque pytest-timeout kill, quoting the tasks the
# manager holds and ``info.error``. The fixed ``asyncio.sleep`` windows that prove a
# respawn has NOT fired yet are deliberately left alone -- those are negative
# assertions, where the duration IS the test.

# An UPPER BOUND, not a positive wait -- not a lost-run bound to widen.
# ``test_cancel_recovery_failure_emits_done_and_delivers`` patches the production
# give-up (``subagent._RECOVERY_SLOT_WAIT_SECS``, normally 60.0) down to 0.4s, and
# this bound is what asserts the patch actually took effect: the recovery must fail
# FAST. Widening it opens a band in which a refactor that stops reading that module
# global -- inlining the literal, moving it onto the instance, renaming it -- leaves
# every terminal assertion still passing, for the wrong reason.
_GIVE_UP_BOUND = 10.0


async def _respawned_task(mgr: SubagentManager, info: SubagentInfo, original: asyncio.Task):
    """The replacement task registered under the run's id, once it is not *original*.

    A recovery that never respawns fails here by name, quoting the tasks the manager
    holds and the run's error.
    """

    # A truth value, not the task: the helper awaits an awaitable a predicate returns.
    def respawned() -> bool:
        task = mgr._tasks.get(info.id)
        return task is not None and task is not original

    await async_wait_until(
        respawned, describe=lambda: f"tasks={sorted(mgr._tasks)}; error={info.error!r}"
    )
    return mgr._tasks[info.id]


# ── 2. User-stop semantics ───────────────────────────────────────────


def _hanging_stream_factory(started: asyncio.Event):
    def stream_factory(msg: str, *a, **kw):
        async def _gen():
            yield _text_event("partial work ")
            started.set()
            await asyncio.Event().wait()  # hang until cancelled
            yield _complete_event()

        return _gen()

    return stream_factory


@pytest.mark.asyncio
async def test_user_cancel_is_neutral_stopped_with_partial():
    started = asyncio.Event()
    mgr = _manager(_mock_sessions(_hanging_stream_factory(started)))
    events: list[tuple[str, dict]] = []

    async def _spy(etype, info, extra=None):
        events.append((etype, extra or {}))

    mgr._fire_event = _spy

    with patch("kiro_crew.subagent.Stats") as stats, patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("long job")
        assert info is not None
        await await_until(started, what="the stream's first yield")
        cancelled = await mgr.cancel(info.id)
        await asyncio.gather(*mgr._tasks.values(), return_exceptions=True)

    assert cancelled is True
    assert info.user_stopped is True
    assert info.done is True
    # Neutral semantics live in the RECORD: no error for a user stop, so
    # reconnect snapshots / tombstones / API listings all derive "stopped".
    assert not info.error
    assert "partial work" in info.result  # streamed partial preserved
    done_events = [e for e in events if e[0] == "subagent_done"]
    # Exactly ONE terminal event (from _force_reap) — cancel() must not
    # emit a duplicate. It is stopped-aware and error-free.
    assert len(done_events) == 1
    assert done_events[-1][1].get("stopped") is True
    assert done_events[-1][1].get("error") is None
    # Neutral outcome: user stop is not counted as a failure.
    stats.return_value.inc_subagent_failed.assert_not_called()


# ── 3. Unexpected-cancel one-shot auto-continue ──────────────────────


@pytest.mark.asyncio
async def test_unexpected_cancel_auto_continues_once():
    started = asyncio.Event()
    mgr = _manager(_mock_sessions(_hanging_stream_factory(started)))

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("interruptible job")
        assert info is not None
        await await_until(started, what="the stream's first yield")
        task1 = mgr._tasks[info.id]
        task1.cancel()  # UNEXPECTED cancel (not via mgr.cancel, not shutdown)
        await asyncio.gather(task1, return_exceptions=True)

        # One-shot recovery: not terminal, retry budget consumed.
        assert info.done is False
        assert info._cancel_retry_used is True

        # Recovery respawns on a fresh task AFTER the original task's
        # teardown fully completes (explicit handshake, not a timed sleep).
        started.clear()
        task2 = await _respawned_task(mgr, info, task1)
        assert task1.done()  # respawn never races the original teardown

        # Second unexpected cancel → terminal (budget spent).
        await await_until(started, what="the stream's first yield")
        task2.cancel()
        await asyncio.gather(task2, return_exceptions=True)

    assert info.done is True
    assert info.error == "cancelled"


@pytest.mark.asyncio
async def test_unexpected_cancel_hand_off_is_visible_to_a_wait_for_waiter():
    """A waiter whose ``wait_for`` deadline cancels the run must see
    ``TimeoutError``, and the original task must end cancelled, because the run
    has moved to the ``:recovery`` task and has not finished. A task that
    swallowed the cancel made ``wait_for`` return normally instead."""
    started = asyncio.Event()
    mgr = _manager(_mock_sessions(_hanging_stream_factory(started)))

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("interruptible job")
        assert info is not None
        await await_until(started, what="the stream's first yield")
        task1 = mgr._tasks[info.id]
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(task1, timeout=0.05)

        assert task1.cancelled()
        assert info.done is False
        assert info._cancel_retry_used is True

        # The continuation still happens on a fresh task.
        started.clear()
        await await_until(started, what="the continuation's first yield")
        task2 = mgr._tasks.get(info.id)
        assert task2 is not None and task2 is not task1
        await mgr.cancel_all()

    assert info.done is True


@pytest.mark.asyncio
async def test_unexpected_cancel_after_tool_activity_finalizes_without_respawn():
    """Once ANY tool has executed, an unexpected cancel must NOT auto-respawn:
    the respawn would run on a fresh session with no tool ledger, so the model
    cannot verify which side effects already happened — a preamble alone
    cannot make re-running safe (Arbiter item 1 / Design finding 1). The run
    is finalized with an explicit suppression error instead."""
    from kiro_crew.acp.client import EVENT_TOOL_CALL

    started = asyncio.Event()
    stream_calls: list[str] = []

    def stream_factory(msg: str, *a, **kw):
        stream_calls.append(msg)

        async def _gen():
            yield SimpleNamespace(
                kind=EVENT_TOOL_CALL,
                title="Running: write_file",
                tool_kind="edit",
                tool_call_id="tc1",
                tool_input={},
                runtime_global=False,
            )
            started.set()
            await asyncio.Event().wait()  # hang until cancelled — NO text ever
            yield _complete_event()

        return _gen()

    mgr = _manager(_mock_sessions(stream_factory))
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("side-effecting job")
        assert info is not None
        await await_until(started, what="the stream's first yield")
        assert info.tool_count > 0 and info.streaming_text == ""
        task1 = mgr._tasks[info.id]
        task1.cancel()  # UNEXPECTED cancel after tool ran
        await asyncio.gather(task1, return_exceptions=True)

        # Terminal immediately — no recovery scheduled, no respawn task.
        assert info.done is True
        assert info._recovering is False
        assert "auto-continue suppressed" in info.error
        assert info.outcome == "failed"
        assert mgr._tasks.get(f"{info.id}:recovery") is None
        # Give the loop a beat: no second stream call may ever appear.
        await asyncio.sleep(0.2)
        assert len(stream_calls) == 1


@pytest.mark.asyncio
async def test_cancel_recovery_text_only_respawn_gets_resume_preamble():
    """Text-only activity is safe to resume: the respawned prompt must carry
    the interruption preamble so the model continues instead of restarting."""
    from kiro_crew.subagent import _CANCEL_RESUME_PREFIX

    started = asyncio.Event()
    mgr = _manager(_mock_sessions(_hanging_stream_factory(started)))
    build_msgs: list[str] = []
    orig_build = mgr._ctx_builder.build_message

    def _capture(msg, *a, **kw):
        build_msgs.append(msg)
        return orig_build(msg, *a, **kw)

    mgr._ctx_builder.build_message = MagicMock(side_effect=_capture)

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("resumable job")
        assert info is not None
        await await_until(started, what="the stream's first yield")
        assert info.streaming_text and info.tool_count == 0
        task1 = mgr._tasks[info.id]
        task1.cancel()  # UNEXPECTED cancel after text, no tools
        await asyncio.gather(task1, return_exceptions=True)

        # The respawn's build_message call is the second entry.
        await async_wait_until(
            lambda: len(build_msgs) >= 2,
            describe=lambda: f"{len(build_msgs)} build_message call(s); error={info.error!r}",
        )

        # First attempt: bare prompt. Respawn: preamble present.
        assert not build_msgs[0].startswith(_CANCEL_RESUME_PREFIX)
        assert build_msgs[1].startswith(_CANCEL_RESUME_PREFIX)

        task2 = mgr._tasks.get(info.id)
        if task2 is not None:
            task2.cancel()
            await asyncio.gather(task2, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_cancel_does_not_auto_continue():
    started = asyncio.Event()
    mgr = _manager(_mock_sessions(_hanging_stream_factory(started)))

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("job at shutdown")
        assert info is not None
        await await_until(started, what="the stream's first yield")
        await mgr.cancel_all()

    assert mgr._shutting_down is True
    assert info._cancel_retry_used is False  # no recovery attempted
    assert info.done is True


# ── 3b. A shutdown cancel under a dashboard parent waits for the next start ──

_DASHBOARD_PARENT = "dashboard:chat-7"
_CHANNEL_PARENT = "slack:C0123ABCD:1700000000.000100"


async def _cancelled_by_the_shutdown(
    parent: str, *, user_stopped: bool = False, folder_seeded: bool = True
) -> tuple[SubagentInfo, AsyncMock]:
    """One run under *parent* the shutdown cancels mid-stream; the parent consumer
    (``on_done``) is a recorder. ``folder_seeded=False`` stands for a run whose
    folder write failed."""
    started = asyncio.Event()
    on_done = AsyncMock()
    mgr = SubagentManager(
        sessions=_mock_sessions(_hanging_stream_factory(started)),
        ctx_builder=_mock_ctx_builder(),
        on_done=on_done,
    )
    mgr._should_use_session_sharing = MagicMock(return_value=False)
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("job at shutdown", parent_session_key=parent)
        assert info is not None
        await await_until(started, what="the stream's first yield")
        # A Stop the user pressed before the shutdown reached the run.
        info.user_stopped = user_stopped
        assert info._folder_seeded is True  # the spawn wrote the run's folder
        info._folder_seeded = folder_seeded
        await mgr.cancel_all()
    assert info.done is True
    return info, on_done


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parent", "open_tabs"),
    [
        pytest.param(_DASHBOARD_PARENT, frozenset(), id="dashboard-chat"),
        pytest.param(_CHANNEL_PARENT, frozenset({_CHANNEL_PARENT}), id="channel-tab"),
    ],
)
async def test_a_shutdown_cancel_under_a_tab_is_announced_by_the_next_start(
    monkeypatch, parent, open_tabs
):
    # The report made during the shutdown could only queue in the tab's memory or
    # start a turn the shutdown refuses, so the next start's sweep announces it. A
    # channel conversation's tab is back by then: the gateway holds the sweep's
    # notices until the channel reconciler's first complete pass.
    monkeypatch.setattr(session_surface, "_dashboard_surfaced", open_tabs)
    info, on_done = await _cancelled_by_the_shutdown(parent)

    on_done.assert_not_awaited()
    assert sp.read_tombstone(info.id) is None
    assert [state["id"] for state in sp.list_orphans()] == [info.id]

    notify = AsyncMock(return_value=True)
    next_start = SubagentManager(sessions=MagicMock(), ctx_builder=None, on_orphan_notify=notify)
    next_start._is_pid_alive = MagicMock(return_value=False)
    with patch("kiro_crew.subagent.sel"):
        await next_start._reconcile_orphans()

    notify.assert_awaited_once()
    notified_parent, msg = notify.await_args.args[:2]
    assert notified_parent == parent
    assert f"Agent `{info.id}` ⚠️ cut off mid-turn by gateway restart" in msg
    assert "Partial output saved at:" in msg  # the text it streamed before the cancel
    assert sp.list_orphans() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parent", "user_stopped"),
    [
        pytest.param(_DASHBOARD_PARENT, True, id="user-stopped"),
        pytest.param(_CHANNEL_PARENT, False, id="channel-parent"),
        pytest.param("cron:nightly-digest", False, id="cron-parent"),
    ],
)
async def test_a_shutdown_cancel_with_a_stop_or_no_tab_is_reported_now(
    monkeypatch, parent, user_stopped
):
    # A channel or cron parent's report goes to a thread or the bell, and a user's
    # Stop is its own ending: both keep the cancelled tombstone and the report.
    monkeypatch.setattr(session_surface, "_dashboard_surfaced", frozenset())
    info, on_done = await _cancelled_by_the_shutdown(parent, user_stopped=user_stopped)

    on_done.assert_awaited_once_with(info)
    assert sp.read_tombstone(info.id)["cause"] == "cancelled"
    assert sp.list_orphans() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parent",
    ["cron:nightly-digest", "subagent:p1", "taskrunner:t1:chat:tok1"],
    ids=["cron", "subagent", "task-review"],
)
async def test_a_shutdown_cancel_under_another_kind_of_tab_is_reported_now(monkeypatch, parent):
    # Only a dashboard chat or a channel conversation is left to the next start's
    # sweep: any other kind of parent keeps the report with its tab open.
    monkeypatch.setattr(session_surface, "_dashboard_surfaced", frozenset({parent}))
    assert session_surface.has_dashboard_surface(parent)

    info, on_done = await _cancelled_by_the_shutdown(parent)

    on_done.assert_awaited_once_with(info)
    assert sp.read_tombstone(info.id)["cause"] == "cancelled"
    assert sp.list_orphans() == []


@pytest.mark.asyncio
async def test_a_shutdown_cancel_of_a_run_whose_folder_was_never_written_is_reported_now():
    # The sweep finds runs by their folder, so a run whose folder write failed is
    # not left to it, and the shutdown decides that from memory, not from disk.
    info, on_done = await _cancelled_by_the_shutdown(_DASHBOARD_PARENT, folder_seeded=False)

    on_done.assert_awaited_once_with(info)
    assert sp.read_tombstone(info.id)["cause"] == "cancelled"


# ── 4. Orphan-notification wiring ────────────────────────────────────


@pytest.mark.asyncio
async def test_orphan_injection_delegates_to_callback():
    notify = AsyncMock(return_value=True)
    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=None, on_orphan_notify=notify)
    with patch("kiro_crew.subagent.sel"):
        ok = await mgr._try_inject_orphan_notification("dashboard:main", "msg")
    assert ok is True
    # The structured completion facts are forwarded as a third arg;
    # a direct call with no meta passes None through unchanged.
    notify.assert_awaited_once_with("dashboard:main", "msg", None)


@pytest.mark.asyncio
async def test_orphan_injection_false_without_callback():
    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=None)
    assert await mgr._try_inject_orphan_notification("dashboard:main", "msg") is False


@pytest.mark.asyncio
async def test_orphan_injection_callback_error_returns_false():
    notify = AsyncMock(side_effect=RuntimeError("dashboard unavailable"))
    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=None, on_orphan_notify=notify)

    assert await mgr._try_inject_orphan_notification("dashboard:main", "msg") is False
    notify.assert_awaited_once_with("dashboard:main", "msg", None)


@pytest.mark.asyncio
async def test_delivered_orphan_survives_audit_and_tombstone_failures():
    notify = AsyncMock(return_value=True)
    audit = MagicMock()
    audit.log_api_access.side_effect = RuntimeError("audit unavailable")
    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=None, on_orphan_notify=notify)
    state = {
        "id": "orphan-1",
        "task": "recover work",
        "parent_session": "dashboard:main",
    }

    with (
        patch("kiro_crew.subagent.has_dashboard_surface", return_value=True),
        patch("kiro_crew.subagent.sel", return_value=audit),
        patch(
            "kiro_crew.subagent.write_tombstone", side_effect=OSError("disk unavailable")
        ) as write_tombstone,
    ):
        result = await mgr._notify_orphan("orphan-1", state, False)

    assert result is None
    notify.assert_awaited_once()
    audit.log_api_access.assert_called_once_with(
        caller="dashboard:main",
        operation="subagent.orphan_notification_injected",
        outcome="ok",
        source="subagent",
    )
    write_tombstone.assert_called_once_with(
        "orphan-1",
        cause="gateway_restart",
        recovery_action="delivered",
        pid=None,
        turns=0,
        last_tool="",
    )


@pytest.mark.asyncio
async def test_orphan_dm_delegates_to_callback(caplog):
    dm = AsyncMock(return_value=True)
    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=None, on_orphan_dm=dm)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.subagent"):
        await mgr._send_orphan_slack_dm("orphan msg")
    dm.assert_awaited_once_with("orphan msg")
    # A delivered notice does not also fall through to the undelivered log line.
    assert [r.getMessage() for r in caplog.records if r.name == "kiro_crew.subagent"] == []


@pytest.mark.asyncio
async def test_orphan_dm_callback_error_is_swallowed(caplog):
    dm = AsyncMock(side_effect=RuntimeError("slack down"))
    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=None, on_orphan_dm=dm)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.subagent"):
        await mgr._send_orphan_slack_dm("orphan msg")  # must not raise
    dm.assert_awaited_once_with("orphan msg")
    # The failed delivery falls through to the log line, so the notice is not lost.
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.name == "kiro_crew.subagent" and r.levelno == logging.WARNING
    ]
    assert warnings == ["Orphan notification (no delivery channel wired): orphan msg"]


@pytest.mark.asyncio
async def test_undelivered_orphan_log_keeps_result_path_and_whole_worktree(caplog, tmp_path):
    """With no channel, the log line is the notice: it must carry every field.

    A task long enough to fill the 100-character preview puts the result path
    past the 200th character, and the worktree it names is cut at the preview.
    """
    worktree = "/home/user/.kiro/crew/scratch/runtime-3c7d0aeb/wt-orphan-notice-0123456789"
    state = {
        "id": "2f2cb4b5",
        "task": f"Fix the open review on the feature branch in the worktree {worktree}",
        "parent_session": "",
        "result_complete": True,
        "cwd": worktree,
    }
    run_dir = tmp_path / "subagents" / "2f2cb4b5"
    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=None)
    with (
        patch("kiro_crew.subagent.has_dashboard_surface", return_value=False),
        patch("kiro_crew.subagent.agent_dir_for_display", return_value=run_dir),
    ):
        msg = await mgr._notify_orphan("2f2cb4b5", state, True)
    assert msg is not None
    assert f"Worktree: `{worktree}`" in msg.splitlines()
    assert f"Result saved at: `{run_dir / 'result.txt'}`" in msg.splitlines()

    with caplog.at_level(logging.WARNING, logger="kiro_crew.subagent"):
        await mgr._send_orphan_slack_dm(msg)
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.name == "kiro_crew.subagent" and r.levelno == logging.WARNING
    ]
    assert warnings == [f"Orphan notification (no delivery channel wired): {msg}"]


@pytest.mark.asyncio
async def test_orphan_notice_without_recorded_cwd_has_no_worktree_line():
    state = {"id": "orphan-2", "task": "recover work", "parent_session": ""}
    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=None)
    with patch("kiro_crew.subagent.has_dashboard_surface", return_value=False):
        msg = await mgr._notify_orphan("orphan-2", state, False)
    assert msg is not None
    assert not any(line.startswith("Worktree:") for line in msg.splitlines())
    assert "No result was captured before the restart." in msg


@pytest.mark.asyncio
async def test_cancel_recovery_waits_for_slow_teardown():
    """Respawn must not race a slow session reset in the original finally.

    The original run's finally awaits ``sessions.reset`` (up to 30s in prod).
    The recovery handshake is explicit: the respawn only happens after the
    original task object has fully completed, so a 0.5s-slow reset must delay
    the respawn past it — never fire mid-teardown.
    """
    started = asyncio.Event()
    sessions = _mock_sessions(_hanging_stream_factory(started))

    reset_done = asyncio.Event()

    async def _slow_reset(key, **_):
        await asyncio.sleep(0.5)
        reset_done.set()

    sessions.reset = AsyncMock(side_effect=_slow_reset)
    mgr = _manager(sessions)

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("slow teardown job")
        assert info is not None
        await await_until(started, what="the stream's first yield")
        task1 = mgr._tasks[info.id]
        task1.cancel()
        await asyncio.gather(task1, return_exceptions=True)

        # Poll for the respawn; when it appears, teardown MUST already be done.
        task2 = await _respawned_task(mgr, info, task1)
        assert reset_done.is_set()  # respawn strictly after the slow reset
        assert task1.done()
        # The new task is tracked — a user Stop can still find and cancel it.
        assert mgr._tasks.get(info.id) is task2
        task2.cancel()
        await asyncio.gather(task2, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_recovery_waits_for_free_slot():
    """The respawn re-acquires capacity — never exceeds max_concurrent."""
    started = asyncio.Event()
    mgr = _manager(_mock_sessions(_hanging_stream_factory(started)))

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("capacity job")
        assert info is not None
        await await_until(started, what="the stream's first yield")
        task1 = mgr._tasks[info.id]

        # Simulate the freed slot being immediately taken by a queued spawn.
        mgr._max_concurrent = 1

        async def _occupy_after_teardown():
            await asyncio.gather(task1, return_exceptions=True)
            mgr._running_count = 1  # drained spawn holds the only slot

        occupier = asyncio.create_task(_occupy_after_teardown())
        task1.cancel()
        await occupier

        # Recovery must WAIT while the pool is full.
        await asyncio.sleep(0.6)
        assert mgr._running_count <= mgr._max_concurrent
        assert mgr._tasks.get(info.id) in (None, task1)

        # Free the slot — recovery proceeds and count never exceeds the cap.
        mgr._running_count = 0
        task2 = await _respawned_task(mgr, info, task1)
        assert mgr._running_count <= mgr._max_concurrent
        task2.cancel()
        await asyncio.gather(task2, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_recovery_failure_emits_done_and_delivers():
    """A failed recovery still finalizes: subagent_done fires and _on_done runs.

    If the respawn can't happen (no capacity within the deadline), the UI must
    not be left on a running card and the parent must still be notified.
    """
    started = asyncio.Event()
    on_done = AsyncMock()
    sessions = _mock_sessions(_hanging_stream_factory(started))
    mgr = SubagentManager(sessions=sessions, ctx_builder=_mock_ctx_builder(), on_done=on_done)
    mgr._should_use_session_sharing = MagicMock(return_value=False)
    events: list[tuple[str, dict]] = []

    async def _spy(etype, info, extra=None):
        events.append((etype, extra or {}))

    mgr._fire_event = _spy

    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent._RECOVERY_SLOT_WAIT_SECS", 0.4),
    ):
        info = mgr.spawn("doomed recovery job")
        assert info is not None
        await await_until(started, what="the stream's first yield")
        task1 = mgr._tasks[info.id]
        task1.cancel()
        await asyncio.gather(task1, return_exceptions=True)

        # Keep the pool permanently full so the slot wait times out.
        mgr._max_concurrent = 1
        mgr._running_count = 1

        # Deterministic: await the registered pending-recovery task itself.
        # Polling info.done races CI schedulers — done flips before the
        # failure path's subagent_done emit and on_done delivery run.
        rec = mgr._tasks.get(f"{info.id}:recovery")
        assert rec is not None, "pending recovery must be registered in _tasks"
        await asyncio.wait_for(  # flake-ok: _GIVE_UP_BOUND asserts the patched give-up took effect
            asyncio.gather(rec, return_exceptions=True), timeout=_GIVE_UP_BOUND
        )

    assert info.done is True
    assert info.error == "cancelled (recovery failed)"
    done_events = [e for e in events if e[0] == "subagent_done"]
    assert done_events, "recovery failure must emit subagent_done"
    assert done_events[-1][1].get("error")
    on_done.assert_awaited_once_with(info)


class _WallSteppedClock:
    """The ``time`` module with its wall clock offset and its monotonic clock real.

    ``reads`` counts the clock reads made through it: the slot wait reads the clock
    on every poll, so a test waits for its next polls instead of sleeping.
    """

    def __init__(self) -> None:
        self.wall = 0.0
        self.reads = 0

    def time(self) -> float:
        self.reads += 1
        return time.time() + self.wall

    def monotonic(self) -> float:
        self.reads += 1
        return time.monotonic()

    def __getattr__(self, name: str) -> object:
        return getattr(time, name)


@pytest.mark.asyncio
async def test_cancel_recovery_slot_wait_survives_a_forward_wall_clock_step():
    """The slot wait is measured on the monotonic clock: a wall-clock step forward
    (an NTP step, a manual change, a resume from suspend) does not end it early and
    fail the run as "cancelled (recovery failed)"."""
    started = asyncio.Event()
    mgr = _manager(_mock_sessions(_hanging_stream_factory(started)))
    clock = _WallSteppedClock()

    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent.time", clock),
    ):
        info = mgr.spawn("recovery under a wall-clock step")
        assert info is not None
        await await_until(started, what="the stream's first yield")
        task1 = mgr._tasks[info.id]
        task1.cancel()
        # Waited on directly, under the helpers' lost-run bound: the test must resume
        # right after the run ends, before the recovery's first capacity check, and
        # ``await_until`` adds a loop turn (its waiter on a ``gather``) that let the
        # recovery respawn first.
        done, _ = await asyncio.wait({task1}, timeout=default_timeout())
        assert task1 in done, "the cancelled run never ended"

        # Keep the pool full so the recovery waits for a slot.
        mgr._max_concurrent = 1
        mgr._running_count = 1
        rec = mgr._tasks.get(f"{info.id}:recovery")
        assert rec is not None, "pending recovery must be registered in _tasks"
        polled = clock.reads
        await async_wait_until(lambda: clock.reads >= polled + 2 or info.done)
        assert not info.done

        # Two minutes forward on the wall clock, none on the monotonic one: a
        # recovery measured on the wall clock gives up on its next 0.25 s poll.
        clock.wall = 120.0
        polled = clock.reads
        await async_wait_until(lambda: clock.reads >= polled + 2 or info.done)
        assert not info.done, f"the slot wait ended on a wall-clock step: {info.error!r}"

        # Free the slot: the recovery respawns the run.
        mgr._running_count = 0
        await await_until(asyncio.gather(rec, return_exceptions=True), what="the recovery ending")
        task2 = mgr._tasks.get(info.id)
        assert task2 is not None and task2 is not task1
        task2.cancel()
        await await_until(
            asyncio.gather(task2, return_exceptions=True), what="the cancelled replacement ending"
        )


@pytest.mark.asyncio
async def test_cancel_recovery_slot_wait_without_a_clock_step_gives_up_at_its_deadline():
    """Control for the test above: with no clock step, a slot wait on a pool that
    stays full still ends at its deadline, now read on the monotonic clock, and the
    run fails as "cancelled (recovery failed)", as before."""
    started = asyncio.Event()
    mgr = _manager(_mock_sessions(_hanging_stream_factory(started)))
    clock = _WallSteppedClock()
    give_up = 0.4

    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent.time", clock),
        patch("kiro_crew.subagent._RECOVERY_SLOT_WAIT_SECS", give_up),
    ):
        info = mgr.spawn("recovery that never finds a slot")
        assert info is not None
        await await_until(started, what="the stream's first yield")
        task1 = mgr._tasks[info.id]
        # Taken before the cancel, so it precedes the deadline the recovery sets.
        t0 = time.monotonic()
        task1.cancel()
        # Waited on directly, under the helpers' lost-run bound: the test must resume
        # right after the run ends, before the recovery's first capacity check, and
        # ``await_until`` adds a loop turn (its waiter on a ``gather``) that let the
        # recovery respawn first.
        done, _ = await asyncio.wait({task1}, timeout=default_timeout())
        assert task1 in done, "the cancelled run never ended"

        # Keep the pool permanently full so the slot wait reaches its deadline.
        mgr._max_concurrent = 1
        mgr._running_count = 1
        rec = mgr._tasks.get(f"{info.id}:recovery")
        assert rec is not None, "pending recovery must be registered in _tasks"
        await asyncio.wait_for(  # flake-ok: _GIVE_UP_BOUND asserts the patched give-up took effect
            asyncio.gather(rec, return_exceptions=True), timeout=_GIVE_UP_BOUND
        )
        waited = time.monotonic() - t0

    assert info.done is True
    assert info.error == "cancelled (recovery failed)"
    assert waited >= give_up, f"gave up after {waited:.2f} s, before its {give_up} s deadline"


@pytest.mark.asyncio
async def test_cancel_all_reaches_pending_recovery_and_finalizes():
    """A pending cancel-recovery is registered in _tasks so cancel_all cancels
    it, and the cancelled recovery finalizes the record terminally (never
    respawns, never leaves _recovering limbo). Locks in the arbiter's
    shutdown-reachability requirement for the recovery branch."""
    started = asyncio.Event()
    on_done = AsyncMock()
    sessions = _mock_sessions(_hanging_stream_factory(started))
    mgr = SubagentManager(sessions=sessions, ctx_builder=_mock_ctx_builder(), on_done=on_done)
    mgr._should_use_session_sharing = MagicMock(return_value=False)
    mgr._fire_event = AsyncMock()

    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
    ):
        info = mgr.spawn("job interrupted by shutdown")
        assert info is not None
        await await_until(started, what="the stream's first yield")
        task1 = mgr._tasks[info.id]
        task1.cancel()
        await asyncio.gather(task1, return_exceptions=True)

        # Recovery is pending and MUST be reachable by cancel_all.
        rec = mgr._tasks.get(f"{info.id}:recovery")
        assert rec is not None, "pending recovery must be registered in _tasks"

        await mgr.cancel_all()

    assert rec.done()
    assert info._recovering is False
    assert info.done is True
    assert info.error == "cancelled"
    # The respawned run never started: the original task was the only one.
    assert mgr._tasks == {}


@pytest.mark.asyncio
async def test_transient_error_after_tool_call_sends_continue_prompt():
    """A transient error after TOOL activity (no text yet) must send CONTINUE —
    replaying the full prompt could re-execute the mutating tool that already
    ran (duplicate writes/messages)."""
    from kiro_crew.acp.client import EVENT_TOOL_CALL

    calls: list[str] = []

    def stream_factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            if len(calls) == 1:
                yield SimpleNamespace(
                    kind=EVENT_TOOL_CALL,
                    title="Running: write_file",
                    tool_kind="edit",
                    tool_call_id="tc1",
                    tool_input={},
                    runtime_global=False,
                )
                raise _TransientError("500 before first token")
            yield _text_event("done after tool")
            yield _complete_event()

        return _gen()

    mgr = _manager(_mock_sessions(stream_factory))
    with patch("kiro_crew.subagent.transient_retry_delay", return_value=0.0):
        info = await _spawn_and_wait(mgr)

    assert info.error == ""
    assert "done after tool" in info.result
    assert calls[0] == "built_message"
    # Tool activity counts as post-activity: CONTINUE, never a full replay.
    assert calls[1] == _TRANSIENT_CONTINUE_MSG


@pytest.mark.asyncio
async def test_post_activity_retry_is_one_shot():
    """After ANY observed activity, transient recovery gets exactly ONE
    continuation turn — a second post-activity transient error must propagate,
    matching the main path's ``_posttoken_retry_used`` rule (each continuation
    after a mutating tool is an independent chance to repeat side effects)."""
    calls: list[str] = []

    def stream_factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            yield _text_event("some output ")
            raise _TransientError("500 mid-stream")

        return _gen()

    mgr = _manager(_mock_sessions(stream_factory))
    with patch("kiro_crew.subagent.transient_retry_delay", return_value=0.0):
        info = await _spawn_and_wait(mgr)

    # Attempt 1 (original) → activity → ONE continuation → second post-activity
    # error propagates. Never a third turn, even with TRANSIENT_RETRIES > 1.
    assert TRANSIENT_RETRIES > 1  # the rule must be stricter than the budget
    assert len(calls) == 2
    assert calls[1] == _TRANSIENT_CONTINUE_MSG
    assert info.error != ""
    assert info.outcome == "failed"


def test_no_raw_cancel_outside_chokepoint():
    """Source scan: every raw ``.cancel()`` on a managed run task in
    subagent.py must route through ``_cancel_task_intentionally`` (the
    mechanical enforcement of the intentional-cancel marker contract).
    Allowed raw sites: the chokepoint body itself, the reaper-loop task, and a
    pending cancel-recovery scheduler task — none of the latter two are managed
    runs, so the marker contract (and recovery) never applies to them."""
    import inspect
    from pathlib import Path

    import kiro_crew.subagent as subagent_mod

    source_root = Path(subagent_mod.__file__).resolve().parent
    source_paths = [Path(subagent_mod.__file__).resolve()]
    source_paths.extend(sorted((source_root / "subagent_manager").glob("*.py")))
    source_paths.extend(sorted((source_root / "subagent_parts").glob("*.py")))
    raw_sites = [
        (path.relative_to(source_root).as_posix(), i + 1, line.strip())
        for path in source_paths
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines())
        if ".cancel()" in line
        and not line.strip().startswith("#")
        and "``" not in line  # docstring mentions, not call sites
    ]
    allowed_substrings = (
        "task.cancel()",  # chokepoint body — verified below to be unique
        "self._reaper_task.cancel()",
        # A reap supersedes a pending respawn; the recovery task schedules the
        # respawn and is NOT a managed run, so no terminal marker applies.
        "recovery_task.cancel()",
        # Shielded terminal-report tasks drained at shutdown — also not managed
        # runs; cancelling them cannot trigger a respawn.
        "report_task.cancel()",
        # follow_up watchers (spawn_steer mode="follow_up") — observers, not
        # managed runs: no terminal marker applies, and cancelling one cannot
        # trigger a respawn (it only ever DISPATCHES via continue_conversation,
        # which cancel_all pre-empts by cancelling watchers first).
        "followup_watcher.cancel()",
        # The pending async OPEN of the durable task store, cancelled by ``close()``.
        # It is a store-open task, not a managed run: no terminal marker applies and
        # cancelling it cannot trigger a respawn. Left pending it would complete after
        # the close and re-attach the connection this method exists to release.
        "taskq_open_task.cancel()",
    )
    chokepoint_src = inspect.getsource(subagent_mod.SubagentManager._cancel_task_intentionally)
    assert "task.cancel()" in chokepoint_src
    for rel, lineno, line in raw_sites:
        assert any(s in line for s in allowed_substrings), (
            f"raw .cancel() at {rel}:{lineno} ({line!r}) — route it "
            "through _cancel_task_intentionally with a terminal marker"
        )
    # The generic 'task.cancel()' form must appear ONLY inside the chokepoint.
    generic = [
        (rel, n, line)
        for rel, n, line in raw_sites
        if "task.cancel()" in line
        and "_reaper_task" not in line
        and "recovery_task" not in line
        and "report_task" not in line
        and "taskq_open_task" not in line
    ]
    assert len(generic) == 1, (
        f"expected exactly one raw task.cancel() (the chokepoint body), " f"found: {generic}"
    )


def test_chokepoint_unmarked_cancel_consumes_recovery_budget():
    """An intentional cancel issued WITHOUT a terminal marker must not be able
    to zombie-respawn: the chokepoint consumes the recovery budget
    defensively (and still cancels)."""
    mgr = _manager(_mock_sessions(lambda *a, **kw: None))
    info = SubagentInfo(id="sa-test", task="t", started=0.0)
    task = MagicMock()
    assert info._cancel_retry_used is False
    mgr._cancel_task_intentionally(task, info, reason="test-unmarked")
    task.cancel.assert_called_once()
    assert info._cancel_retry_used is True  # recovery can never fire now

    # Marked path: budget untouched.
    info2 = SubagentInfo(id="sa-test2", task="t", started=0.0)
    info2.user_stopped = True
    task2 = MagicMock()
    mgr._cancel_task_intentionally(task2, info2, reason="test-marked")
    task2.cancel.assert_called_once()
    assert info2._cancel_retry_used is False


def test_outcome_property_is_canonical_three_way():
    """SubagentInfo.outcome is THE single classification source: stopped wins
    over error-nullability; error means failed; neither means completed."""
    from kiro_crew.subagent import SubagentInfo

    stopped = SubagentInfo(id="o1", task="t")
    stopped.user_stopped = True
    assert stopped.outcome == "stopped"

    failed = SubagentInfo(id="o2", task="t")
    failed.error = "boom"
    assert failed.outcome == "failed"

    completed = SubagentInfo(id="o3", task="t")
    assert completed.outcome == "completed"


@pytest.mark.asyncio
async def test_reconcile_multiple_orphans_sends_single_digest_dm():
    """N orphans on the DM-fallback path produce ONE digest, never N pings."""
    dm = AsyncMock(return_value=True)
    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=None, on_orphan_dm=dm)

    orphans = [
        {"id": "orph-1", "pid": None, "parent_session": "", "task": "task one"},
        {"id": "orph-2", "pid": None, "parent_session": "", "task": "task two"},
        {"id": "orph-3", "pid": None, "parent_session": "", "task": "task three"},
    ]
    with (
        patch("kiro_crew.subagent.list_orphans", return_value=orphans),
        patch("kiro_crew.subagent.write_tombstone"),
        patch("kiro_crew.subagent.sel"),
    ):
        await mgr._reconcile_orphans()

    dm.assert_awaited_once()
    digest = dm.await_args.args[0]
    assert "3 subagent(s)" in digest
    for aid in ("orph-1", "orph-2", "orph-3"):
        assert aid in digest


@pytest.mark.asyncio
async def test_reconcile_single_orphan_dm_is_not_wrapped_in_digest():
    """A lone orphan's DM keeps the plain per-agent message (no digest header)."""
    dm = AsyncMock(return_value=True)
    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=None, on_orphan_dm=dm)

    orphans = [{"id": "solo-1", "pid": None, "parent_session": "", "task": "solo task"}]
    with (
        patch("kiro_crew.subagent.list_orphans", return_value=orphans),
        patch("kiro_crew.subagent.write_tombstone"),
        patch("kiro_crew.subagent.sel"),
    ):
        await mgr._reconcile_orphans()

    dm.assert_awaited_once()
    msg = dm.await_args.args[0]
    assert "solo-1" in msg
    assert "restart digest" not in msg


def _streams_then_fails(error: Exception, text: str = "the answer "):
    def stream_factory(msg: str, *a, **kw):
        async def _gen():
            if text:
                yield _text_event(text)
            raise error

        return _gen()

    return stream_factory


_GENERATE_FAILED = "The model failed to generate a response (transient error)."


@pytest.mark.asyncio
@pytest.mark.parametrize("keep", ["head", "tail"])
async def test_generate_failed_after_output_keeps_the_output_with_a_warning(keep):
    """Output already streamed survives a transient generate failure the retry cannot fix.

    Longer than the keep cap, so a warning added before the cap would be cut off."""
    error, text = _TransientError(_GENERATE_FAILED), "x" * 4000
    calls: list[str] = []
    factory = _streams_then_fails(error, text)

    def _recording(msg: str, *a, **kw):
        calls.append(msg)
        return factory(msg, *a, **kw)

    mgr = _manager(_mock_sessions(_recording))
    mgr.update_completion_keep(keep, 3000)
    with patch("kiro_crew.subagent.transient_retry_delay", return_value=0.0):
        info = await _spawn_and_wait(mgr)

    assert info.outcome == "completed"
    assert not info.error
    assert info.partial is True
    assert info.result.startswith("_Warning: the backend failed to generate")
    assert "xxxx" in info.result
    # The output is kept only after the one continue turn was tried.
    assert len(calls) == 2 and calls[1] == _TRANSIENT_CONTINUE_MSG
    # The disk copy, read by spawn_status / spawn_run, carries the warning too.
    from pathlib import Path

    assert info.result_path
    assert "_Warning: the backend failed to generate" in Path(info.result_path).read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "text"),
    [
        (_TransientError("500 mid-stream"), "the answer "),  # transient, not generate-failed
        (_FatalError(_GENERATE_FAILED), "the answer "),  # not transient
        (_TransientError(_GENERATE_FAILED), " "),  # whitespace-only output
    ],
)
async def test_other_failures_after_output_still_fail(error, text):
    mgr = _manager(_mock_sessions(_streams_then_fails(error, text)))
    with patch("kiro_crew.subagent.transient_retry_delay", return_value=0.0):
        info = await _spawn_and_wait(mgr)

    assert info.outcome == "failed"
    assert info.error


@pytest.mark.asyncio
async def test_control_tag_only_output_still_fails():
    """Output that is only an [OPTIONS: ...] tag is empty once the tag is stripped."""
    calls: list[str] = []

    def stream_factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            if len(calls) == 1:
                yield _text_event("[OPTIONS: Retry | Stop]")
            raise _TransientError(_GENERATE_FAILED)

        return _gen()

    mgr = _manager(_mock_sessions(stream_factory))
    with patch("kiro_crew.subagent.transient_retry_delay", return_value=0.0):
        info = await _spawn_and_wait(mgr)

    assert info.outcome == "failed"
    assert info.error


@pytest.mark.asyncio
async def test_subagent_tool_frames_carry_the_tool_call_id():
    """Both ``subagent_tool`` fire sites send the call's ``tool_call_id``.

    The Subagents card lists each call once by matching that id: a gated call's
    ``EVENT_TOOL_CALL`` frame and its permission-request frame share it and the
    same ``tool_count``, while a backend child's permission request has its own
    id but an unchanged count. Without the id on the wire the card can only
    compare counts, and the child's request overwrites the parent's last call.
    """
    from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, EVENT_TOOL_CALL, LLMEvent

    async def stream(*_args, **_kwargs):
        yield LLMEvent(
            kind=EVENT_TOOL_CALL, title="Terminal", tool_call_id="call-1", tool_kind="execute"
        )
        yield LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title="Running: git status",
            request_id=1,
            tool_call_id="call-1",
            tool_kind="execute",
        )
        yield _text_event("done")
        yield _complete_event()

    mgr = _manager(_mock_sessions(stream))
    frames: list[dict] = []
    _orig_fire = mgr._fire_event

    async def _spy(etype, info, extra=None):
        if etype == "subagent_tool":
            frames.append(dict(extra or {}))
        await _orig_fire(etype, info, extra)

    mgr._fire_event = _spy
    info = await _spawn_and_wait(mgr)

    assert info.error == ""
    assert [f.get("tool_call_id") for f in frames] == ["call-1", "call-1"]
    # Same call, same count: the second frame must not look like a new call.
    assert frames[0]["tool_count"] == frames[1]["tool_count"] == 1

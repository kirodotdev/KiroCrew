"""Tests for spawn_steer mode="follow_up" — non-interrupting delivery.

Unlike the default interrupt steer (inject into the running turn), follow_up
queues the message on the run and a per-run watcher dispatches the whole
queue as ONE continuation on the run's own conversation after the run
completes. The watcher deliberately observes completion (``info.done`` + task
popped) instead of hooking ``_run``'s multi-path finalization.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from kiro_crew.subagent import SubagentInfo, SubagentManager

# Subagent-registry isolation is provided globally by the autouse
# ``_isolate_subagents_dir`` fixture in ``conftest.py``.


def _mock_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.mark_continuable = MagicMock()
    sessions.resumable_sid = MagicMock(return_value="sid-123")
    sessions.seed_conversation = MagicMock()
    sessions.conversation_provider = MagicMock(return_value="acp")
    sessions.get_provider = MagicMock(return_value=None)
    sessions.set_continuable_fallback = MagicMock()
    return sessions


def _mock_ctx_builder() -> MagicMock:
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    return ctx


def _manager() -> SubagentManager:
    return SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx_builder())


def _fast(mgr: SubagentManager, monkeypatch) -> None:
    """Shrink the watcher's poll/retry clocks so tests run in milliseconds."""
    monkeypatch.setattr(SubagentManager, "_FOLLOWUP_POLL_SECS", 0.01)
    monkeypatch.setattr(SubagentManager, "_FOLLOWUP_BUSY_RETRY_SECS", 0.01)


class TestFollowUpQueueing:
    @pytest.mark.asyncio
    async def test_unknown_run_is_not_found(self) -> None:
        mgr = _manager()
        ok, detail = await mgr.follow_up_run("ghost", "msg")
        assert ok is False and detail == "not_found"

    @pytest.mark.asyncio
    async def test_finished_run_is_not_running(self) -> None:
        """A done run points at spawn_continue — follow_up is for in-flight runs."""
        mgr = _manager()
        mgr._agents["r1"] = SubagentInfo(id="r1", task="t", done=True)
        ok, detail = await mgr.follow_up_run("r1", "msg")
        assert ok is False and detail.startswith("not_running")

    @pytest.mark.asyncio
    async def test_queue_accumulates_and_arms_one_watcher(self, monkeypatch) -> None:
        """Repeated follow-ups accumulate on the run; only ONE watcher is
        armed, and it is registered in the MANAGER-owned dict (reachable by
        cancel_all), not the global fire-and-forget set."""
        mgr = _manager()
        info = SubagentInfo(id="r2", task="t")
        mgr._agents["r2"] = info
        ok1, d1 = await mgr.follow_up_run("r2", "first")
        ok2, d2 = await mgr.follow_up_run("r2", "second")
        assert (ok1, d1) == (True, "queued") and (ok2, d2) == (True, "queued")
        assert info.pending_followups == ["first", "second"]
        assert set(mgr._followup_watchers) == {"r2"}, "one watcher per run, manager-owned"
        mgr._followup_watchers["r2"].cancel()


class TestFollowUpDelivery:
    @pytest.mark.asyncio
    async def test_dispatches_one_continuation_after_completion(self, monkeypatch) -> None:
        """Queued messages drain as ONE continue_conversation call, joined in
        arrival order, only after done AND the task is popped (teardown over)."""
        mgr = _manager()
        _fast(mgr, monkeypatch)
        info = SubagentInfo(id="r3", task="t", parent_session_key="dash:1", agent="coder")
        mgr._agents["r3"] = info
        info.pending_followups = ["fix the tests", "also update the docs"]
        continues: list = []
        monkeypatch.setattr(
            mgr,
            "continue_conversation",
            lambda cid, task, **kw: (
                continues.append((cid, task, kw)),
                SubagentInfo(id="child", task=task),
            )[1],
        )
        # Simulate the run finishing while the watcher polls.
        info.done = True  # task never in mgr._tasks → both conditions met
        await asyncio.wait_for(mgr._deliver_followups(info), timeout=2)
        assert len(continues) == 1
        cid, task, kw = continues[0]
        assert cid == "r3"
        assert task == "fix the tests\n\n---\n\nalso update the docs"
        assert kw["parent_session_key"] == "dash:1"
        assert kw["agent"] == "coder"
        assert info.pending_followups == []

    @pytest.mark.asyncio
    async def test_waits_for_task_pop_before_dispatch(self, monkeypatch) -> None:
        """done=True alone is not enough — teardown (task pop) must finish first."""
        mgr = _manager()
        _fast(mgr, monkeypatch)
        info = SubagentInfo(id="r4", task="t", done=True)
        mgr._agents["r4"] = info
        mgr._tasks["r4"] = MagicMock()  # teardown not finished yet
        info.pending_followups = ["msg"]
        continues: list = []
        monkeypatch.setattr(
            mgr,
            "continue_conversation",
            lambda cid, task, **kw: (
                continues.append(cid),
                SubagentInfo(id="child", task=task),
            )[1],
        )

        async def _pop_later():
            await asyncio.sleep(0.05)
            mgr._tasks.pop("r4")

        pop_task = asyncio.ensure_future(_pop_later())
        await asyncio.wait_for(mgr._deliver_followups(info), timeout=2)
        await pop_task
        assert continues == ["r4"]

    @pytest.mark.asyncio
    async def test_busy_conversation_is_retried(self, monkeypatch) -> None:
        """A residual conversation_busy right after done gets a bounded retry."""
        mgr = _manager()
        _fast(mgr, monkeypatch)
        info = SubagentInfo(id="r5", task="t", done=True)
        mgr._agents["r5"] = info
        info.pending_followups = ["msg"]
        results = [
            SubagentInfo(id="x", task="msg", done=True, error="conversation_busy: in flight"),
            SubagentInfo(id="child", task="msg"),
        ]
        calls: list = []
        monkeypatch.setattr(
            mgr,
            "continue_conversation",
            lambda cid, task, **kw: (calls.append(cid), results.pop(0))[1],
        )
        await asyncio.wait_for(mgr._deliver_followups(info), timeout=2)
        assert len(calls) == 2, "busy result must be retried"

    @pytest.mark.asyncio
    async def test_watcher_deadline_drops_queue_without_dispatch(self, monkeypatch) -> None:
        """A run that never completes cannot strand an immortal watcher — the
        deadline expires, the queue is dropped, nothing is dispatched, and the
        parent receives a SYNTHETIC failure completion event (it was promised
        an event; SEL-only failure would leave it waiting forever)."""
        mgr = _manager()
        _fast(mgr, monkeypatch)
        mgr._default_timeout = -301  # deadline already in the past
        announced: list = []

        async def _on_done(i):
            announced.append(i)

        mgr._on_done = _on_done
        info = SubagentInfo(id="r6", task="t", parent_session_key="dash:9")  # never done
        mgr._agents["r6"] = info
        info.pending_followups = ["msg"]
        continues: list = []
        monkeypatch.setattr(
            mgr,
            "continue_conversation",
            lambda cid, task, **kw: (
                continues.append(cid),
                SubagentInfo(id="child", task=task),
            )[1],
        )
        await asyncio.wait_for(mgr._deliver_followups(info), timeout=2)
        assert continues == []
        assert len(announced) == 1
        assert announced[0].done and "expired" in announced[0].error
        assert announced[0].parent_session_key == "dash:9"

    @pytest.mark.asyncio
    async def test_user_stopped_run_suppresses_followups(self, monkeypatch) -> None:
        """OUTCOME-AWARE: a run the user explicitly stopped must not be
        resurrected by a queued follow-up — suppressed, with a synthetic
        failure event so the parent is not left waiting."""
        mgr = _manager()
        _fast(mgr, monkeypatch)
        announced: list = []

        async def _on_done(i):
            announced.append(i)

        mgr._on_done = _on_done
        info = SubagentInfo(id="r7", task="t", done=True, user_stopped=True)
        mgr._agents["r7"] = info
        info.pending_followups = ["keep going"]
        continues: list = []
        monkeypatch.setattr(
            mgr,
            "continue_conversation",
            lambda cid, task, **kw: (
                continues.append(cid),
                SubagentInfo(id="child", task=task),
            )[1],
        )
        await asyncio.wait_for(mgr._deliver_followups(info), timeout=2)
        assert continues == [], "user-stopped work must not be resurrected"
        assert len(announced) == 1 and "suppressed" in announced[0].error

    @pytest.mark.asyncio
    async def test_dispatch_failure_announces_the_typed_error(self, monkeypatch) -> None:
        """conversation_gone at dispatch time reaches the parent as a real
        completion event carrying the typed failure, not just a SEL row."""
        mgr = _manager()
        _fast(mgr, monkeypatch)
        announced: list = []

        async def _on_done(i):
            announced.append(i)

        mgr._on_done = _on_done
        info = SubagentInfo(id="r8", task="t", done=True)
        mgr._agents["r8"] = info
        info.pending_followups = ["msg"]
        failure = SubagentInfo(
            id="x", task="msg", done=True, error="conversation_gone: files pruned"
        )
        monkeypatch.setattr(mgr, "continue_conversation", lambda cid, task, **kw: failure)
        await asyncio.wait_for(mgr._deliver_followups(info), timeout=2)
        assert announced == [failure]

    @pytest.mark.asyncio
    async def test_cancel_all_cancels_watchers_before_dispatch(self, monkeypatch) -> None:
        """SHUTDOWN CONTAINMENT: cancel_all() must reach the watcher — a
        queued follow-up must never mint a new run in a shutting-down
        gateway — and the queued message must NOT die silently: the parent
        was promised an event, so shutdown announces a synthetic failure for
        each non-empty queue before cancelling."""
        mgr = _manager()
        _fast(mgr, monkeypatch)
        announced: list = []

        async def _on_done(i):
            announced.append(i)

        mgr._on_done = _on_done
        info = SubagentInfo(id="r9", task="t", parent_session_key="dash:7")
        mgr._agents["r9"] = info
        ok, _ = await mgr.follow_up_run("r9", "later")
        assert ok and "r9" in mgr._followup_watchers
        watcher = mgr._followup_watchers["r9"]
        continues: list = []
        monkeypatch.setattr(
            mgr,
            "continue_conversation",
            lambda cid, task, **kw: (
                continues.append(cid),
                SubagentInfo(id="child", task=task),
            )[1],
        )
        await mgr.cancel_all()
        # The run completing after shutdown must not trigger a dispatch.
        info.done = True
        await asyncio.sleep(0.05)
        assert watcher.cancelled() or watcher.done()
        assert mgr._followup_watchers == {}
        assert continues == []
        # The accepted message was announced dropped, not silently discarded.
        assert len(announced) == 1
        assert "shutting down" in announced[0].error
        assert announced[0].parent_session_key == "dash:7"
        assert info.pending_followups == []

    @pytest.mark.asyncio
    async def test_follow_up_refused_during_shutdown(self, monkeypatch) -> None:
        """A shutting-down gateway refuses new follow-ups with a typed error
        instead of accepting a message it cannot deliver."""
        mgr = _manager()
        mgr._shutting_down = True
        info = SubagentInfo(id="r12", task="t")
        mgr._agents["r12"] = info
        ok, detail = await mgr.follow_up_run("r12", "msg")
        assert ok is False and detail.startswith("shutting_down")
        assert info.pending_followups == []
        assert "r12" not in mgr._followup_watchers

    @pytest.mark.asyncio
    async def test_shutdown_during_busy_retry_still_announces_the_message(
        self, monkeypatch
    ) -> None:
        """SHUTDOWN-MID-RETRY RACE (GPT review): the watcher must not DRAIN the
        queue before the outcome settles — shutdown landing during a
        conversation_busy retry sleep used to find an empty queue, cancel the
        watcher, and lose the message with no event. Messages now stay queued
        until dispatched-or-announced, so cancel_all()'s sweep announces them."""
        mgr = _manager()
        _fast(mgr, monkeypatch)
        announced: list = []

        async def _on_done(i):
            announced.append(i)

        mgr._on_done = _on_done
        info = SubagentInfo(id="r13", task="t", parent_session_key="dash:3")  # alive
        mgr._agents["r13"] = info
        # Dispatch always answers conversation_busy, parking the watcher in
        # its retry sleep with the message still pending.
        busy = SubagentInfo(id="x", task="m", done=True, error="conversation_busy: in flight")
        monkeypatch.setattr(mgr, "continue_conversation", lambda cid, task, **kw: busy)
        # Slow the retry sleep so shutdown reliably lands mid-retry.
        monkeypatch.setattr(SubagentManager, "_FOLLOWUP_BUSY_RETRY_SECS", 5.0)
        ok, _ = await mgr.follow_up_run("r13", "important correction")
        assert ok
        info.done = True  # run completes; watcher proceeds to the busy dispatch
        await asyncio.sleep(0.1)  # watcher enters the busy-retry sleep
        assert info.pending_followups == ["important correction"], (
            "the queue must not be drained before the outcome settles"
        )
        await mgr.cancel_all()
        # The shutdown sweep announced the still-queued message.
        assert len(announced) == 1
        assert "shutting down" in announced[0].error
        assert info.pending_followups == []

    @pytest.mark.asyncio
    async def test_watcher_latch_resets_when_the_watcher_exits(self, monkeypatch) -> None:
        """GPT P1: a watcher that dies at its deadline (run still alive) must
        reset the one-watcher latch — otherwise every later follow_up_run
        answers 'queued' while nothing will ever deliver. A fresh follow-up
        after the reset arms a NEW watcher that can dispatch."""
        mgr = _manager()
        _fast(mgr, monkeypatch)
        mgr._default_timeout = -301  # first watcher's deadline already past
        announced: list = []

        async def _on_done(i):
            announced.append(i)

        mgr._on_done = _on_done
        info = SubagentInfo(id="r10", task="t")  # alive: not done
        mgr._agents["r10"] = info
        ok, _ = await mgr.follow_up_run("r10", "first")
        assert ok
        first_watcher = mgr._followup_watchers.get("r10")
        if first_watcher is not None:  # may have finished + popped itself already
            await asyncio.wait_for(first_watcher, timeout=2)
        for _ in range(50):  # let the done-callback run
            if not info._followup_watcher:
                break
            await asyncio.sleep(0.02)
        assert info._followup_watcher is False, "latch must reset with the task"
        assert "r10" not in mgr._followup_watchers
        # A later follow-up arms a NEW watcher and delivers once the run ends.
        mgr._default_timeout = 3600
        dispatched: list = []
        monkeypatch.setattr(
            mgr,
            "continue_conversation",
            lambda cid, task, **kw: (
                dispatched.append(task),
                SubagentInfo(id="child", task=task),
            )[1],
        )
        ok, _ = await mgr.follow_up_run("r10", "second")
        assert ok and "r10" in mgr._followup_watchers
        info.done = True
        await asyncio.wait_for(mgr._followup_watchers["r10"], timeout=2)
        assert dispatched == ["second"]

    @pytest.mark.asyncio
    async def test_message_queued_during_expiry_announce_gets_a_new_watcher(
        self, monkeypatch
    ) -> None:
        """EXPIRY RACE (GPT review): a follow-up accepted while the previous
        watcher is inside its final awaits (latch still true, so no new
        watcher is armed) must be picked up by the done-callback's re-arm —
        never stranded as 'queued' with no watcher."""
        mgr = _manager()
        _fast(mgr, monkeypatch)
        mgr._default_timeout = -301  # first watcher expires immediately
        announced: list = []

        async def _on_done(i):
            announced.append(i)

        mgr._on_done = _on_done
        info = SubagentInfo(id="r11", task="t")  # alive
        mgr._agents["r11"] = info
        dispatched: list = []
        monkeypatch.setattr(
            mgr,
            "continue_conversation",
            lambda cid, task, **kw: (
                dispatched.append(task),
                SubagentInfo(id="child", task=task),
            )[1],
        )
        # Make the announce path block so we can queue mid-announce.
        slow_gate = asyncio.Event()
        real_announce = mgr._announce_followup_failure

        async def _slow_announce(*a, **kw):
            await slow_gate.wait()
            return await real_announce(*a, **kw)

        monkeypatch.setattr(mgr, "_announce_followup_failure", _slow_announce)
        ok, _ = await mgr.follow_up_run("r11", "first")
        assert ok
        first_watcher = mgr._followup_watchers["r11"]
        await asyncio.sleep(0.05)  # let the watcher reach the gated announce
        assert not first_watcher.done(), "watcher should be parked on the announce"
        # Queue a message MID-ANNOUNCE: latch is still true → no new watcher.
        # "first" is deliberately STILL queued here — messages are not drained
        # until their outcome settles (shutdown-race fix).
        ok, _ = await mgr.follow_up_run("r11", "second")
        assert ok and info.pending_followups == ["first", "second"]
        # Release the announce; the done-callback must re-arm for "second".
        mgr._default_timeout = 3600  # the re-armed watcher gets a real deadline
        slow_gate.set()
        await asyncio.wait_for(first_watcher, timeout=2)
        for _ in range(100):
            if "r11" in mgr._followup_watchers:
                break
            await asyncio.sleep(0.01)
        assert "r11" in mgr._followup_watchers, "done-callback must re-arm for the survivor"
        # The re-armed watcher delivers once the run completes.
        info.done = True
        await asyncio.wait_for(mgr._followup_watchers["r11"], timeout=2)
        assert dispatched == ["second"]


class TestFollowUpRestApi:
    @pytest.mark.asyncio
    async def test_mode_follow_up_routes_to_follow_up_run(self, tmp_path, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.handlers.messaging import api_spawn_steer

        subagents = MagicMock()
        subagents.follow_up_run = AsyncMock(return_value=(True, "queued"))
        subagents.steer_run = AsyncMock(return_value=(True, "ok"))
        state = MagicMock()
        state.subagents = subagents
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/spawn/{agent_id}/steer", api_spawn_steer)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/spawn/abc/steer", json={"message": "later", "mode": "follow_up"}
            )
            assert resp.status == 200
            assert (await resp.json())["status"] == "follow_up_queued"
            subagents.follow_up_run.assert_awaited_once_with("abc", "later")
            subagents.steer_run.assert_not_awaited()

            # Default stays interrupt, byte-for-byte.
            resp = await client.post("/api/spawn/abc/steer", json={"message": "now"})
            assert resp.status == 200
            assert (await resp.json())["status"] == "steered"
            subagents.steer_run.assert_awaited_once_with("abc", "now")

            # An unknown mode is a 400, not a silent interrupt.
            resp = await client.post(
                "/api/spawn/abc/steer", json={"message": "x", "mode": "sideways"}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_mode"

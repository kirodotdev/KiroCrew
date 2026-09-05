"""Scale plumbing tests (PR-4: 60-100 concurrent sub-agents).

Covers:
1. ``SubagentEventCoalescer``: pass-through below the activation threshold
   (small spawns byte-identical to legacy), absorption + one-frame flush
   above it, chunk concatenation, flush-before-lifecycle ordering, close().
2. Batch identity: ``spawn(batch_id=...)`` threads onto ``SubagentInfo``,
   survives the queue, and fires ``spawn_batch_started`` exactly once.
3. Stall two-sweep confirmation: one idle sweep marks a suspect (no event),
   the second flags stalled; activity between sweeps resets the suspicion.
4. Wave-digest completion injection: waves above the digest threshold hold
   per-agent injections and deliver ONE consolidated digest on the last
   member; ``batch_finished`` fires with correct counts.
5. ``POST /api/spawn/{id}/retry`` gating: only terminal FAILED agents.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_scale import SubagentEventCoalescer

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")

# Subagent-registry isolation is provided globally by the autouse
# ``_isolate_subagents_dir`` fixture in ``conftest.py``.


# ── 1. Coalescer ─────────────────────────────────────────────────────


def _coalescer(active: int, tick: float = 0.02):
    all_frames: list[tuple[str, dict]] = []
    sub_frames: list[tuple[str, dict]] = []
    c = SubagentEventCoalescer(
        lambda t, d: all_frames.append((t, d)),
        lambda t, d: sub_frames.append((t, d)),
        lambda: active,
        threshold=8,
        tick_secs=tick,
    )
    return c, all_frames, sub_frames


class TestCoalescer:
    def test_below_threshold_passes_through(self):
        c, all_frames, sub_frames = _coalescer(active=3)
        assert c.handle("subagent_tool", {"id": "a1", "slot": "s", "tool": "Read"}) is False
        assert c.handle("subagent_chunk", {"id": "a1", "slot": "s", "text": "x"}) is False
        assert all_frames == [] and sub_frames == []  # caller forwards, not us

    def test_lifecycle_events_never_absorbed(self):
        c, _, _ = _coalescer(active=50)
        for etype in ("subagent_spawn", "subagent_done", "subagent_recovering",
                      "subagent_injection_failed", "spawn_batch_started", "batch_finished"):
            assert c.handle(etype, {"id": "a1", "slot": "s"}) is False

    @pytest.mark.asyncio
    async def test_tool_merge_clears_stale_retrying_attempt(self):
        """A tool delta after a retrying delta means work RESUMED — the merged
        entry must not carry the stale `attempt` (the frontend would leave the
        row marked retrying after recovery)."""
        c, all_frames, _ = _coalescer(active=50)
        c.handle("subagent_retrying", {"id": "a1", "slot": "s", "attempt": 1})
        c.handle("subagent_tool", {"id": "a1", "slot": "s", "tool": "Read", "tool_count": 2})
        await asyncio.sleep(0.06)
        (etype, data), = all_frames
        entry = data["updates"][0]
        assert entry["tool"] == "Read"
        assert "attempt" not in entry

    @pytest.mark.asyncio
    async def test_above_threshold_absorbs_and_flushes_one_frame(self):
        c, all_frames, _ = _coalescer(active=50)
        assert c.handle("subagent_tool", {"id": "a1", "slot": "s", "tool": "Read", "tool_count": 1}) is True
        assert c.handle("subagent_tool", {"id": "a2", "slot": "s", "tool": "Grep", "tool_count": 3}) is True
        # Latest state wins per agent
        assert c.handle("subagent_tool", {"id": "a1", "slot": "s", "tool": "Write", "tool_count": 2}) is True
        assert all_frames == []  # nothing until the tick
        await asyncio.sleep(0.06)
        assert len(all_frames) == 1
        etype, data = all_frames[0]
        assert etype == "subagent_batch_update"
        by_id = {u["id"]: u for u in data["updates"]}
        assert by_id["a1"]["tool"] == "Write" and by_id["a1"]["tool_count"] == 2
        assert by_id["a2"]["tool"] == "Grep"

    @pytest.mark.asyncio
    async def test_chunks_concatenate_and_go_to_subscribers(self):
        c, all_frames, sub_frames = _coalescer(active=50)
        assert c.handle("subagent_chunk", {"id": "a1", "slot": "s", "text": "hello "}) is True
        assert c.handle("subagent_chunk", {"id": "a1", "slot": "s", "text": "world"}) is True
        await asyncio.sleep(0.06)
        assert all_frames == []
        assert len(sub_frames) == 1
        etype, data = sub_frames[0]
        assert etype == "subagent_batch_chunks"
        assert data["chunks"] == [{"id": "a1", "slot": "s", "text": "hello world"}]

    @pytest.mark.asyncio
    async def test_done_flushes_buffered_state_first(self):
        """A done event between ticks must not overtake the agent's buffered
        deltas — the buffer flushes synchronously before the done forwards."""
        c, all_frames, sub_frames = _coalescer(active=50, tick=5.0)
        c.handle("subagent_chunk", {"id": "a1", "slot": "s", "text": "tail text"})
        c.handle("subagent_tool", {"id": "a1", "slot": "s", "tool": "Read"})
        assert c.handle("subagent_done", {"id": "a1", "slot": "s"}) is False
        # Flushed synchronously at the done boundary, before any tick.
        assert len(all_frames) == 1 and all_frames[0][0] == "subagent_batch_update"
        assert len(sub_frames) == 1 and sub_frames[0][0] == "subagent_batch_chunks"

    @pytest.mark.asyncio
    async def test_close_flushes_and_stops(self):
        c, all_frames, _ = _coalescer(active=50, tick=5.0)
        c.handle("subagent_tool", {"id": "a1", "slot": "s", "tool": "Read"})
        c.close()
        assert len(all_frames) == 1
        assert c.handle("subagent_tool", {"id": "a2", "slot": "s", "tool": "X"}) is False


# ── 2. Batch identity ────────────────────────────────────────────────


def _mock_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.get_agent = MagicMock(return_value="")
    sessions.has_session = MagicMock(return_value=True)
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    return sessions


def _mock_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    return ctx


class TestBatchIdentity:
    def test_digest_chunk_size_env_guarded(self):
        """A malformed KIROCREW_SUBAGENT_DIGEST_CHUNK_SIZE must never crash
        gateway import — guarded parse falls back to the default and clamps
        to a positive range (a zero/negative chunk size would flush forever)."""
        import os
        from unittest.mock import patch as _patch

        from kiro_crew.slack.gateway import _digest_chunk_size

        with _patch.dict(os.environ, {"KIROCREW_SUBAGENT_DIGEST_CHUNK_SIZE": "foo"}):
            assert _digest_chunk_size() == 10
        with _patch.dict(os.environ, {"KIROCREW_SUBAGENT_DIGEST_CHUNK_SIZE": "-5"}):
            assert _digest_chunk_size() == 1  # clamped to positive
        with _patch.dict(os.environ, {"KIROCREW_SUBAGENT_DIGEST_CHUNK_SIZE": "25"}):
            assert _digest_chunk_size() == 25

    def test_batch_members_pending_scoped_to_batch(self):
        """Wave completion must count THIS batch only: unrelated running
        agents don't hold it; queued (unregistered) members DO hold it; a
        spawn-failed member (never registered) doesn't wedge it forever."""
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())
        member = SubagentInfo(id="m1", task="t", batch_id="w1", batch_total=3)
        unrelated = SubagentInfo(id="u1", task="t")  # no batch
        mgr._agents = {"m1": member, "u1": unrelated}
        assert mgr.batch_members_pending("w1") is True  # m1 still running
        member.done = True
        # unrelated still running, but the WAVE is complete
        assert mgr.batch_members_pending("w1") is False
        # A queued member of the wave holds completion
        mgr._queue.append({"task": "t2", "batch_id": "w1", "batch_total": 3})
        assert mgr.batch_members_pending("w1") is True
        mgr._queue.clear()
        assert mgr.batch_members_pending("") is False

    def test_pending_while_submissions_in_flight(self):
        """A fast-failing first member must NOT finalize the wave while
        sibling POSTs are still in flight (Arbiter item 2): the pending
        predicate holds until every expected submission has arrived, so no
        partial digest / duplicate batch_finished can be emitted."""
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())
        # First member submitted and already terminal; 2 more expected.
        mgr._batch_submitted["w2"] = [1, 3]
        done_member = SubagentInfo(id="m1", task="t", batch_id="w2", batch_total=3)
        done_member.done = True
        mgr._agents = {"m1": done_member}
        assert mgr.batch_members_pending("w2") is True  # submissions in flight
        # Remaining submissions arrive (spawn-failed: never registered).
        mgr._batch_submitted["w2"] = [3, 3]
        assert mgr.batch_members_pending("w2") is False  # wave truly complete
        # finalize_batch prunes per-wave bookkeeping (bounded growth).
        mgr._seen_batches.add("w2")
        mgr.finalize_batch("w2")
        assert "w2" not in mgr._seen_batches
        assert "w2" not in mgr._batch_submitted

    @pytest.mark.asyncio
    async def test_a_drained_rejection_is_announced(self):
        """A drained spawn has no synchronous reader, so a terminal rejection there
        used to vanish: no completion event, and the caller still believed the run
        was going (crew left the topic `running` forever). `_announce_rejection`
        gates on batch_id because a DIRECT caller reads the error off the return
        value -- that does not hold for a timer-driven drain.
        """
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())
        announced: list[SubagentInfo] = []

        async def _on_done(info):  # type: ignore[no-untyped-def]
            announced.append(info)

        mgr._on_done = _on_done
        mgr._queue = [{"task": "waited too long", "_preassigned_id": "q-reject",
                       "parent_session_key": "dashboard:chat-1", "batch_id": ""}]
        mgr._running_count = 0
        mgr._spawn_stagger_secs = 0.0
        mgr._last_spawn_ts = 0.0
        mgr._emit_queue_depth = MagicMock()
        rejected = SubagentInfo(id="q-reject", task="waited too long", done=True,
                                error="cwd does not exist or is not a directory")
        mgr.spawn = lambda **kw: rejected

        mgr._drain_queue()
        assert "reject-q-reject" in mgr._tasks, (
            "a rejection at drain time was dropped on the floor"
        )
        await mgr._tasks["reject-q-reject"]
        assert [i.id for i in announced] == ["q-reject"]

    def test_a_drained_batch_rejection_is_not_double_announced(self):
        """`_announce_rejection` announces batch members ITSELF, from inside spawn.

        So the drain must cover only the set it skips -- non-batch runs. Announcing
        regardless counted a queued batch rejection twice: the wave's accounting
        closed early and emitted a duplicate or incomplete digest.
        """
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())
        mgr._on_done = AsyncMock()
        mgr._queue = [{"task": "wave member", "_preassigned_id": "q-batch",
                       "parent_session_key": "dashboard:chat-1", "batch_id": "wv"}]
        mgr._running_count = 0
        mgr._spawn_stagger_secs = 0.0
        mgr._last_spawn_ts = 0.0
        mgr._emit_queue_depth = MagicMock()
        # Rejected AND a batch member: spawn's own `_announce_rejection` owns this.
        mgr.spawn = lambda **kw: SubagentInfo(
            id="q-batch", task="wave member", done=True, batch_id="wv",
            error="cwd does not exist or is not a directory",
        )

        mgr._drain_queue()
        assert "reject-q-batch" not in mgr._tasks, (
            "the drain announced a batch rejection that spawn already announced"
        )

    def test_a_drained_success_is_not_announced_twice(self):
        """The announce is for TERMINAL rejections only -- a run that actually
        started reports through its own completion path."""
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())
        mgr._on_done = AsyncMock()
        mgr._queue = [{"task": "fine", "_preassigned_id": "q-ok",
                       "parent_session_key": "dashboard:chat-1", "batch_id": ""}]
        mgr._running_count = 0
        mgr._spawn_stagger_secs = 0.0
        mgr._last_spawn_ts = 0.0
        mgr._emit_queue_depth = MagicMock()
        mgr.spawn = lambda **kw: SubagentInfo(id="q-ok", task="fine")

        mgr._drain_queue()
        assert "reject-q-ok" not in mgr._tasks

    def test_a_queued_run_cancelled_while_waiting_never_starts(self):
        """A waiting run has NO `_agents` record: `spawn` returns its queued
        SubagentInfo without registering it. So cancelling one has to unqueue it --
        the earlier drain-side guard keyed on the info and was therefore dead code
        for exactly the state it was meant to cover, which a test that seeded
        `_agents` by hand could not reveal.
        """
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())
        mgr._queue = [
            {"task": "cancelled while waiting", "_preassigned_id": "q-stopped",
             "parent_session_key": "dashboard:chat-1", "batch_id": ""},
            {"task": "still wanted", "_preassigned_id": "q-live",
             "parent_session_key": "dashboard:chat-1", "batch_id": ""},
        ]
        mgr._running_count = 0
        mgr._spawn_stagger_secs = 0.0
        mgr._last_spawn_ts = 0.0
        mgr._emit_queue_depth = MagicMock()
        assert "q-stopped" not in mgr._agents, "premise: a queued run is unregistered"

        assert asyncio.run(mgr.cancel("q-stopped")) is True, (
            "cancel reported failure for a run it can still prevent"
        )
        assert [p["_preassigned_id"] for p in mgr._queue] == ["q-live"]
        # The chip must stop counting a run that will never start.
        assert mgr._emit_queue_depth.called

        spawned: list[str] = []
        mgr.spawn = lambda **kw: spawned.append(str(kw.get("_preassigned_id")))
        mgr._drain_queue()
        assert spawned == ["q-live"], spawned

    def test_cancel_still_reports_false_for_an_unknown_id(self):
        """Unqueueing must not turn every unknown id into a successful cancel.

        Asserted against a NON-EMPTY queue: with an empty one, a broken filter that
        drops everything is indistinguishable from a correct one, so the obvious
        version of this test cannot see the mutation it exists to catch.
        """
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())
        mgr._queue = [
            {"task": "someone else's work", "_preassigned_id": "q-other",
             "parent_session_key": "dashboard:chat-1", "batch_id": ""},
        ]
        mgr._emit_queue_depth = MagicMock()
        assert asyncio.run(mgr.cancel("never-existed")) is False
        assert [p["_preassigned_id"] for p in mgr._queue] == ["q-other"], (
            "cancelling an unknown id evicted an unrelated queued run"
        )
        assert not mgr._emit_queue_depth.called
        mgr._queue = []
        assert asyncio.run(mgr.cancel("never-existed")) is False

    @pytest.mark.asyncio
    async def test_queued_cancel_reports_a_neutral_stopped_terminal(self):
        events: list[tuple[str, SubagentInfo, dict]] = []
        announced: list[SubagentInfo] = []

        async def on_event(kind, info, extra):
            events.append((kind, info, extra))

        async def on_done(info):
            announced.append(info)

        mgr = SubagentManager(
            sessions=_mock_sessions(),
            ctx_builder=_mock_ctx(),
            on_event=on_event,
            on_done=on_done,
        )
        mgr._queue = [
            {
                "task": "queued task",
                "_preassigned_id": "q-stop",
                "parent_session_key": "dashboard:one",
                "batch_id": "wave",
                "batch_total": 2,
            }
        ]
        mgr._emit_queue_depth = MagicMock()

        assert await mgr.cancel("q-stop") is True
        await asyncio.gather(*list(mgr._report_tasks))

        assert announced and announced[0].user_stopped is True
        assert announced[0].task == "queued task"
        assert announced[0].error == ""
        assert events and events[0][0] == "subagent_done"
        assert events[0][2]["outcome"] == "stopped"

    @pytest.mark.asyncio
    async def test_stop_parent_removes_its_queued_agents_before_start(self):
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())
        mgr._queue = [
            {"task": "q1", "_preassigned_id": "q1", "parent_session_key": "dashboard:one"},
            {"task": "q2", "_preassigned_id": "q2", "parent_session_key": "dashboard:one"},
            {"task": "other", "_preassigned_id": "q3", "parent_session_key": "dashboard:two"},
        ]
        mgr._emit_queue_depth = MagicMock()
        mgr._report_queued_stop = MagicMock()
        mgr.cancel = AsyncMock(return_value=True)

        running, queued = await mgr.cancel_for_parent("dashboard:one")

        assert (running, queued) == (0, 2)
        assert [item["_preassigned_id"] for item in mgr._queue] == ["q3"]
        assert mgr._report_queued_stop.call_count == 2
        mgr.cancel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_parent_does_not_recancel_pending_queued_records(self):
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())
        mgr._queue = [
            {"task": "q1", "_preassigned_id": "q1", "parent_session_key": "dashboard:one"},
            {"task": "q2", "_preassigned_id": "q2", "parent_session_key": "dashboard:one"},
        ]
        mgr._emit_queue_depth = MagicMock()
        mgr._spawn_terminal_report = MagicMock()
        mgr.cancel = AsyncMock(return_value=True)

        assert await mgr.cancel_for_parent("dashboard:one") == (0, 2)

        assert all(mgr._agents[agent_id].queued for agent_id in ("q1", "q2"))
        assert all(not mgr._agents[agent_id].done for agent_id in ("q1", "q2"))
        mgr.cancel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_parent_keeps_batch_pending_until_last_queued_report(self):
        observed: list[tuple[str, bool]] = []
        mgr: SubagentManager

        async def on_done(info):  # type: ignore[no-untyped-def]
            observed.append((info.id, mgr.batch_members_pending(info.batch_id)))

        mgr = SubagentManager(
            sessions=_mock_sessions(), ctx_builder=_mock_ctx(), on_done=on_done
        )
        mgr._batch_submitted["wave"] = [2, 2]
        mgr._queue = [
            {
                "task": "q1",
                "_preassigned_id": "q1",
                "parent_session_key": "dashboard:one",
                "batch_id": "wave",
                "batch_total": 2,
            },
            {
                "task": "q2",
                "_preassigned_id": "q2",
                "parent_session_key": "dashboard:one",
                "batch_id": "wave",
                "batch_total": 2,
            },
        ]
        mgr._emit_queue_depth = MagicMock()

        assert await mgr.cancel_for_parent("dashboard:one") == (0, 2)
        await asyncio.gather(*list(mgr._report_tasks))

        assert observed == [("q1", True), ("q2", False)]
        assert all(mgr._agents[agent_id].done for agent_id in ("q1", "q2"))

    @pytest.mark.asyncio
    async def test_stop_parent_leaves_spawn_approval_waits_pending(self):
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())
        running = SubagentInfo(id="run", task="run", parent_session_key="dashboard:one")
        pending = SubagentInfo(id="approval", task="wait", parent_session_key="dashboard:one")
        pending._awaiting_approval = True
        pending._exec_started = None
        other = SubagentInfo(id="other", task="other", parent_session_key="dashboard:two")
        mgr._agents = {info.id: info for info in (running, pending, other)}
        mgr.cancel = AsyncMock(return_value=True)

        stopped, queued = await mgr.cancel_for_parent("dashboard:one")

        assert (stopped, queued) == (1, 0)
        mgr.cancel.assert_awaited_once_with("run")

    @pytest.mark.asyncio
    async def test_spawn_counts_submissions_once_per_member(self):
        """spawn() increments the submission counter exactly once per member —
        a queued member re-entering via _drain_queue must not double-count,
        and a REJECTED member (refused before registration) MUST still count,
        or batch_members_pending would hold the wave forever and the digest
        would never fire (GPT 5.6 round-5 HIGH)."""
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())
        mgr._spawn_stagger_secs = 0.0
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"), \
                patch.object(SubagentManager, "_run", new=AsyncMock()):
            mgr.spawn("t1", batch_id="wv", batch_total=3)
            mgr.spawn("t2", batch_id="wv", batch_total=3)
            # Drain re-entry must not bump the counter.
            mgr.spawn("t2", batch_id="wv", batch_total=3, _from_queue=True)
            # Rejected member (empty task — refused before registration)
            # still counts as submitted: it will never register or complete.
            rejected = mgr.spawn("   ", batch_id="wv", batch_total=3)
        assert rejected is not None and rejected.error
        assert mgr._batch_submitted["wv"] == [3, 3]
        # With all 3 submissions accounted (one rejected, never registered),
        # a wave whose registered members are done is COMPLETE — the digest
        # is not stranded by the rejected member.
        for a in mgr._agents.values():
            a.done = True
        assert mgr.batch_members_pending("wv") is False

    @pytest.mark.asyncio
    async def test_rejected_batch_member_announces_terminal_state(self):
        """A rejected BATCH member must flow through the done callback with
        its batch identity intact (GPT 5.6 HIGH): counting it as submitted
        is not enough — when the rejection is the wave's FINAL submission,
        no later completion event re-evaluates the wave, so without an
        announce the gateway never runs its batch accounting and every
        sibling result already held for the digest strands forever. A
        NON-batch rejection must NOT announce (the caller already gets the
        error synchronously; injecting a turn would double-report)."""
        announced: list = []

        async def _on_done(info):
            announced.append(info)

        mgr = SubagentManager(
            sessions=_mock_sessions(), ctx_builder=_mock_ctx(), on_done=_on_done
        )
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            rejected = mgr.spawn("   ", batch_id="wv9", batch_total=2)
            plain = mgr.spawn("   ")  # non-batch rejection: no announce
        await asyncio.sleep(0)  # let the scheduled announce run
        assert rejected is not None and rejected.error
        assert plain is not None and plain.error
        assert len(announced) == 1
        got = announced[0]
        assert got.batch_id == "wv9" and got.batch_total == 2
        assert got.done and got.error
        assert got.outcome == "failed"

    @pytest.mark.asyncio
    async def test_no_approval_rejection_announces_batch_member(self):
        """The hooks-path 'no approval mechanism' rejection (hooks present,
        auto_approve_subagent_spawn disabled, no approval callback) is a
        REGISTERED rejection: the member sits done=True in _agents, so
        batch_members_pending() counts it as complete — but without an
        announce the gateway's wave accounting never runs, and a wave whose
        FINAL member lands here closes with no completion event, stranding
        every held sibling digest (GPT 5.6 HIGH). It must route through
        _announce_rejection like the other rejection paths."""
        announced: list = []

        async def _on_done(info):
            announced.append(info)

        ctx = MagicMock()
        ctx.hooks.auto_approve_subagent_spawn = False  # hooks exist, gate closed
        mgr = SubagentManager(
            sessions=_mock_sessions(), ctx_builder=ctx, on_done=_on_done
        )
        mgr._is_yolo = None
        mgr._on_spawn_approval = None  # no approval callback configured
        mgr._spawn_stagger_secs = 0.0
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            rejected = mgr.spawn("do work", batch_id="wvA", batch_total=2)
        await asyncio.sleep(0)
        assert rejected is not None and rejected.done
        assert "no approval mechanism" in (rejected.error or "")
        assert len(announced) == 1
        got = announced[0]
        assert got.batch_id == "wvA" and got.batch_total == 2
        assert got.outcome == "failed"

    @pytest.mark.asyncio
    async def test_record_lost_submission_reconciles_and_announces(self):
        """A batch member whose spawn POST never reached spawn() is counted
        as submitted AND announced as a synthetic terminal failure, so the
        wave's count-driven pending predicate can close and held sibling
        results deliver (Opus MEDIUM + Design Review CONCERN 1)."""
        announced: list = []

        async def _on_done(info):
            announced.append(info)

        mgr = SubagentManager(
            sessions=_mock_sessions(), ctx_builder=_mock_ctx(), on_done=_on_done
        )
        # Wave of 3: 2 submissions arrived (members done), 1 POST was lost.
        mgr._batch_submitted["wvL"] = [2, 3]
        m1 = SubagentInfo(id="m1", task="t", batch_id="wvL", batch_total=3)
        m1.done = True
        mgr._agents = {"m1": m1}
        assert mgr.batch_members_pending("wvL") is True  # wedged pre-fix
        with patch("kiro_crew.subagent.sel"):
            mgr.record_lost_submission(
                "wvL", 3, "connection refused", parent_session_key="dashboard:main"
            )
        await asyncio.sleep(0)
        assert mgr._batch_submitted["wvL"] == [3, 3]
        assert mgr.batch_members_pending("wvL") is False  # wave can close
        assert len(announced) == 1
        got = announced[0]
        assert got.batch_id == "wvL" and got.done and got.error
        assert "submission lost" in got.error
        assert got.outcome == "failed"

    @pytest.mark.asyncio
    async def test_reaper_stuck_wave_sweep_reconciles(self):
        """The reaper backstop force-reconciles a wave with lost submissions:
        submitted < expected, all registered members terminal, nothing
        queued, no progress for _WAVE_STUCK_SECS. Waves inside the grace
        window, with live members, or with queued members are left alone."""
        import time as _time

        from kiro_crew.subagent import _WAVE_STUCK_SECS

        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())
        now = _time.time()
        done_m = SubagentInfo(id="d1", task="t", batch_id="stuck", batch_total=2)
        done_m.done = True
        live_m = SubagentInfo(id="l1", task="t", batch_id="alive", batch_total=2)
        mgr._agents = {"d1": done_m, "l1": live_m}
        mgr._batch_submitted = {
            "stuck": [1, 2],   # lost submission, member done, stale -> reconcile
            "alive": [1, 2],   # lost submission but a member still RUNS -> skip
            "fresh": [1, 2],   # within the grace window -> skip
            "full": [2, 2],    # complete -> skip
        }
        stale = now - _WAVE_STUCK_SECS - 60
        mgr._batch_progress_ts = {
            "stuck": stale, "alive": stale, "fresh": now, "full": stale,
        }
        with patch("kiro_crew.subagent.sel"), \
                patch.object(mgr, "record_lost_submission") as rec:
            mgr._sweep_stuck_waves(now)
        assert rec.call_count == 1
        assert rec.call_args.args[0] == "stuck"
        # finalize_batch prunes the liveness timestamp too (bounded growth).
        mgr.finalize_batch("stuck")
        assert "stuck" not in mgr._batch_progress_ts

    def test_http_error_body_preserves_counted_flag(self):
        """api_spawn marks in-process rejections with counted=True; the MCP
        client's error-body flattening must preserve it, or spawn_run would
        double-reconcile counted rejections and close waves early."""
        import io
        import urllib.error

        from kiro_crew.mcp_core import _http_error_body

        def _err(payload: bytes):
            return urllib.error.HTTPError(
                "http://x/api/spawn", 400, "Bad Request", {},  # type: ignore[arg-type]
                io.BytesIO(payload),
            )

        counted = _http_error_body(_err(b'{"error": "spawn refused", "counted": true}'))
        assert counted.get("counted") is True and "spawn refused" in counted["error"]
        uncounted = _http_error_body(_err(b'{"error": "task is required"}'))
        assert "counted" not in uncounted

    @pytest.mark.asyncio
    async def test_batch_fields_set_and_started_event_fires_once(self):
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())
        mgr._spawn_stagger_secs = 0.0  # no stagger queueing in this test
        events: list[tuple[str, dict]] = []

        async def _spy(etype, info, extra=None):
            events.append((etype, extra or {}))

        mgr._on_event = _spy
        # Skip actual execution — spawn creates the task; cancel it right away.
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"), \
                patch.object(SubagentManager, "_run", new=AsyncMock()):
            i1 = mgr.spawn("t1", batch_id="wave1", batch_total=3)
            i2 = mgr.spawn("t2", batch_id="wave1", batch_total=3)
            i3 = mgr.spawn("t3", batch_id="wave1", batch_total=3)
            await asyncio.sleep(0.05)  # let the fire-and-forget event task run

        assert i1.batch_id == "wave1" and i1.batch_total == 3
        assert i2.batch_id == "wave1" and i3.batch_id == "wave1"
        started = [e for e in events if e[0] == "spawn_batch_started"]
        assert len(started) == 1
        assert started[0][1] == {"batch_id": "wave1", "count": 3}

    @pytest.mark.asyncio
    async def test_standalone_spawn_has_no_batch(self):
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())
        mgr._spawn_stagger_secs = 0.0
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"), \
                patch.object(SubagentManager, "_run", new=AsyncMock()):
            info = mgr.spawn("solo task")
            await asyncio.sleep(0)
        assert info.batch_id == "" and info.batch_total == 0


# ── 3. Stall two-sweep confirmation ──────────────────────────────────


class TestStallDampening:
    def _info(self, idle_for: float) -> SubagentInfo:
        info = SubagentInfo(id="s1", task="t")
        info.turns = 1
        info.last_activity = time.time() - idle_for
        return info

    @pytest.mark.asyncio
    async def test_first_sweep_suspects_second_flags(self):
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())
        events: list[str] = []

        async def _spy(etype, info, extra=None):
            events.append(etype)

        mgr._on_event = _spy
        info = self._info(idle_for=mgr._stall_idle_secs + 10)
        now = time.time()
        await mgr._maybe_flag_stall("s1", info, now)
        assert info.stalled is False and info._stall_suspect_at > 0  # suspect only
        assert "subagent_stalled" not in events
        await mgr._maybe_flag_stall("s1", info, now + 60)
        assert info.stalled is True
        assert "subagent_stalled" in events

    @pytest.mark.asyncio
    async def test_activity_between_sweeps_resets_suspicion(self):
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())
        mgr._on_event = AsyncMock()
        info = self._info(idle_for=mgr._stall_idle_secs + 10)
        await mgr._maybe_flag_stall("s1", info, time.time())
        assert info._stall_suspect_at > 0
        await mgr._touch_activity(info)  # stream event lands
        assert info._stall_suspect_at == 0.0
        # Next sweep starts the confirmation over (fresh idle needed).
        await mgr._maybe_flag_stall("s1", info, time.time())
        assert info.stalled is False


# ── 4. Wave-digest completion injection ──────────────────────────────


def _make_orchestrator():
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.slack.gateway import GatewayOrchestrator

    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
        return GatewayOrchestrator(cfg, no_dashboard=False, no_crons=True, no_open=True)


def _mock_dashboard_state():
    ds = MagicMock()
    ds._slots = {}
    ds._yolo = False
    ds.notify = MagicMock()
    ds.push_slots_update = MagicMock()
    ds.push_refresh = MagicMock()
    ds.broadcast_ws = MagicMock()
    ds.broadcast_ws_subagent_subscribers = MagicMock()
    ds.request_approval = AsyncMock(return_value=True)
    ds.resolve_approval = MagicMock()
    ds.resolve_slot = MagicMock(return_value=None)
    ds.get_slot = MagicMock(return_value=None)
    ds.get_or_create_slot = MagicMock()
    ds.close_all_ws = AsyncMock()
    ds._background_tasks = set()
    return ds


async def _settle(predicate, timeout: float = 5.0) -> None:
    """Poll until *predicate* is truthy (bounded) — create_task'd injection
    turns need real event-loop time on slow CI shards, not one sleep(0)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate() and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.02)


def _wire_hold_settlement(orch, slot, mgr):
    """Wire the slot's delivery ledger and the manager's async settle seam.

    The direct-injection branch owes a flushing digest's held ids to the
    turn's CONSUMPTION through the slot's content-keyed delivery ledger and
    settles them through ``SubagentManager.settle_queued_delivery`` — the same
    machinery the queue drain uses (#2233 via the #4839 ledger). The MagicMock
    slot needs a real mini-ledger for that flow to be observable, and the
    mocked manager's settle must hand back a real coroutine or the settlement
    path skips it (the stubbed-manager guard in
    ``_arm_queued_delivery_settlement``). Production wires
    ``DashboardState(subagents=<the manager>)``; mirror it.

    Returns ``(ledger, settled)``: the content-keyed debts still parked, and
    the id batches the manager was asked to settle.
    """
    ledger: dict[str, list[str]] = {}
    slot.note_pending_subagent_delivery = MagicMock(
        side_effect=lambda content, ids: ledger.setdefault(content, []).extend(ids)
    )
    slot.take_pending_subagent_deliveries = MagicMock(
        side_effect=lambda contents: [i for c in contents for i in ledger.pop(c, [])]
    )
    settled: list[list[str]] = []

    async def _record_settle(ids):
        settled.append(list(ids))

    mgr.settle_queued_delivery = MagicMock(side_effect=_record_settle)
    orch.dashboard_state.subagents = mgr
    return ledger, settled


class TestWaveDigest:
    def _capture_on_done(self, orch):
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
                mock_sm_inst = MagicMock()
                mock_sm_inst.start_reaper = MagicMock()
                mock_sm.return_value = mock_sm_inst
                orch._init_subagents()
                orch.subagent_mgr = mock_sm_inst
                return mock_sm_inst, mock_sm.call_args.kwargs["on_done"]

    def _member(self, i: int, total: int, *, error: str = "") -> SubagentInfo:
        info = SubagentInfo(
            id=f"w{i}",
            task=f"wave task {i}",
            parent_session_key="dashboard:main",
            batch_id="bigwave",
            batch_total=total,
        )
        info.done = True
        info.error = error
        info.result = f"result {i}"
        info.result_path = f"/tmp/w{i}/result.txt"
        return info

    @pytest.mark.asyncio
    async def test_large_wave_delivers_chunked_digests(self):
        """Chunked queue-style delivery: 12 agents with chunk size 10 produce
        exactly TWO digest injections — one when the 10th member completes
        (with do-NOT-spawn guidance while the wave runs) and one final chunk
        on wave close (with the release guidance). Never 12 per-agent turns,
        and never one straggler-gated mega-digest."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        slot = MagicMock()
        slot.mode = "chat"
        slot.running = False
        slot.task = None
        slot._orch_tracker = None
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        total = 12  # chunk size 10 -> chunks of 10 + 2
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text, *, _directive_user_origin, **_kw):
            assert _directive_user_origin is False
            injected.append(text)

        with patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat), \
                patch("kiro_crew.subagent_persistence.mark_delivered"):
            for i in range(total):
                # running_agents_for: members still pending until the last one
                mgr.batch_members_pending = MagicMock(
                    return_value=i != total - 1
                )
                err = "boom" if i == 2 else ""
                await on_done(self._member(i, total, error=err))
                await asyncio.sleep(0)
            await _settle(lambda: len(injected) >= 2)

        # TWO chunk injections for 12 members — not 12, not 1.
        assert len(injected) == 2
        first, final = injected
        assert first.startswith("[Subagent batch completion event]")
        assert final.startswith("[Subagent batch completion event]")
        # Chunk 1: incremental delivery + spawn-discipline guidance.
        assert "Batch results 1/2" in first
        assert "10 of 12 delivered, 2 still running" in first
        assert "do NOT spawn new sub-agents yet" in first
        # Chunk 1 carries the first 10 members' lines, exception-first.
        assert first.index("w2") < first.index("w0")
        assert "/tmp/w0/result.txt" in first
        # Chunk 2 (final): summary counts + release guidance, and ONLY the
        # remaining members' lines (chunk buffers reset between flushes).
        assert "Batch results 2/2" in final
        assert "11 ✅" in final and "1 ❌" in final and "of 12 agents" in final
        assert "before spawning any follow-up" in final
        assert "/tmp/w10/result.txt" in final and "/tmp/w11/result.txt" in final
        assert "/tmp/w0/result.txt" not in final  # already delivered in chunk 1

    @pytest.mark.asyncio
    async def test_wave_digest_text_carries_member_model_provenance(self):
        """Issue #5337: the per-member SERVED model must be visible in the
        PARENT-READ digest body (built from ok_lines/fail_lines), not only in
        the injected meta dict. Only the served id is printed — never a
        "(requested …)" qualifier, since a raw requested-vs-resolved inequality
        would false-amber every member of a normal auto-pinned wave (maintainer
        kyleseaman) — and a member with no served model prints no tag, matching
        the card."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        slot = MagicMock()
        slot.mode = "chat"
        slot.running = False
        slot.task = None
        slot._orch_tracker = None
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        total = 2
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text, *, _directive_user_origin, **_kw):
            injected.append(text)

        with patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat), \
                patch("kiro_crew.subagent_persistence.mark_delivered"):
            # Member 0: served model present. Member 1: requested set but served
            # DIFFERS — the served id is still all that prints (no downgrade
            # qualifier, no false amber).
            m0 = self._member(0, total)
            m0.resolved_model = "claude-opus-4.8"
            m1 = self._member(1, total)
            m1.requested_model = "claude-opus-4.8"
            m1.resolved_model = "claude-opus-4.7"
            mgr.batch_members_pending = MagicMock(return_value=True)
            await on_done(m0)
            await asyncio.sleep(0)
            mgr.batch_members_pending = MagicMock(return_value=False)
            await on_done(m1)
            await asyncio.sleep(0)
            await _settle(lambda: len(injected) >= 1)

        body = "\n".join(injected)
        # Each member shows its SERVED id inline.
        assert "model claude-opus-4.8" in body
        assert "model claude-opus-4.7" in body
        # The "(requested …)" downgrade qualifier is never printed.
        assert "requested" not in body

    @pytest.mark.asyncio
    async def test_wave_digest_no_model_tag_when_served_model_absent(self):
        """Maintainer kyleseaman: when resolved_model is empty the card shows
        nothing, so the digest line must not label the pin as `model
        {requested}` either — no tag at all."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        slot = MagicMock()
        slot.mode = "chat"
        slot.running = False
        slot.task = None
        slot._orch_tracker = None
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text, *, _directive_user_origin, **_kw):
            injected.append(text)

        with patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat), \
                patch("kiro_crew.subagent_persistence.mark_delivered"):
            m0 = self._member(0, 1)
            m0.requested_model = "auto"  # a pin, but nothing served
            m0.resolved_model = ""
            mgr.batch_members_pending = MagicMock(return_value=False)
            await on_done(m0)
            await asyncio.sleep(0)
            await _settle(lambda: len(injected) >= 1)

        body = "\n".join(injected)
        assert "· model" not in body
        assert "auto" not in body

    @pytest.mark.asyncio
    async def test_wave_digest_model_tag_is_redacted(self):
        """GPT 5.6 (backend-security-controls): model values are
        caller-influenceable (spawn_run.model), so a credential-shaped value
        must not reach the digest text broadcast to the dashboard/channels.
        The inline model tag is redacted through the display context."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        slot = MagicMock()
        slot.mode = "chat"
        slot.running = False
        slot.task = None
        slot._orch_tracker = None
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text, *, _directive_user_origin, **_kw):
            injected.append(text)

        secret = "AKIAIOSFODNN7EXAMPLE"
        with patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat), \
                patch("kiro_crew.subagent_persistence.mark_delivered"):
            m0 = self._member(0, 1)
            m0.resolved_model = secret
            mgr.batch_members_pending = MagicMock(return_value=False)
            await on_done(m0)
            await asyncio.sleep(0)
            await _settle(lambda: len(injected) >= 1)

        body = "\n".join(injected)
        # The raw credential-shaped value must not appear verbatim in the
        # broadcast digest text.
        assert secret not in body

    @pytest.mark.asyncio
    async def test_digest_chunks_inject_in_fifo_order_despite_delayed_dispatch_hop(self):
        """A later digest chunk must never overtake an earlier one whose
        dispatched injection is still inside ``bounded_chat_turn``'s off-loop
        timeout resolution (issue #3273). The first chunk's hop is held
        deterministically: it releases the moment a later chunk's injection
        lands (the overtake this test forbids) or after a bounded deadline
        (the fixed code parks the later chunk behind the live ``slot.task``
        claim, so nothing can land while it is held). No machine-load
        dependence: without the widened busy predicate the overtake is forced
        every run; with it the order is FIFO every run."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        slot = MagicMock()
        slot.mode = "chat"
        slot.running = False
        slot.task = None
        slot._orch_tracker = None
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        total = 12  # chunk size 10 -> chunk 1/2 at member 10, final 2/2 on close
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text, *, _directive_user_origin, **_kw):
            assert _directive_user_origin is False
            injected.append(text)

        hop_calls: list[int] = []

        def _held_resolver() -> float:
            # Runs inside asyncio.to_thread, standing in for the config read
            # bounded_chat_turn resolves off-loop. Thread-side bounded poll
            # (not a timing guess): exits the instant an overtaking injection
            # is observed, and the deadline only pays out on the fixed path,
            # where the later chunk is parked and can never land here.
            hop_calls.append(1)
            if len(hop_calls) == 1:
                deadline = time.monotonic() + 2.0
                while not injected and time.monotonic() < deadline:
                    time.sleep(0.01)
            return 60.0

        with patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat), \
                patch("kiro_crew.subagent_persistence.mark_delivered"), \
                patch(
                    "kiro_crew.dashboard.turn_dispatch.chat_turn_timeout_secs",
                    _held_resolver,
                ):
            for i in range(total):
                mgr.batch_members_pending = MagicMock(return_value=i != total - 1)
                await on_done(self._member(i, total))
                await asyncio.sleep(0)
            await _settle(lambda: len(injected) >= 2)

        assert len(injected) == 2
        # FIFO: the observed order is [chunk 1/2, chunk 2/2] — the escalation
        # guidance lives in chunk 1, so a reader must meet it first.
        assert "Batch results 1/2" in injected[0]
        assert "Batch results 2/2" in injected[1]

    @pytest.mark.asyncio
    async def test_batch_finished_event_carries_counts(self):
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        slot = MagicMock()
        slot.mode = "chat"
        slot.running = False
        slot.task = None
        slot._orch_tracker = None
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        total = 12
        with patch("kiro_crew.slack.gateway._run_chat", new_callable=AsyncMock):
            for i in range(total):
                mgr.batch_members_pending = MagicMock(
                    return_value=i != total - 1
                )
                await on_done(self._member(i, total, error="boom" if i < 2 else ""))
                await asyncio.sleep(0)
        finished = [
            c for c in orch.dashboard_state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "batch_finished"
        ]
        assert len(finished) == 1
        payload = finished[0].args[1]
        assert payload["total"] == 12 and payload["ok"] == 10
        assert payload["err"] == 2 and payload["stopped"] == 0

    @pytest.mark.asyncio
    async def test_held_members_marked_delivered_only_at_digest(self):
        """Restart safety (Arbiter item 1 + GPT round-5 HIGH): held members
        are flagged ``_digest_held`` (the run loop skips its own
        mark_delivered — the result is NOT in the parent's context yet and a
        delivered tombstone would hide it from orphan reconciliation after a
        restart). The gateway must NOT settle them at chunk COMPOSITION
        either (routing could still fail); instead it stashes each chunk's
        held OK ids on that chunk's FLUSHING member (``_digest_settle_ids``)
        and settlement waits for the route that owns the hand-off: the
        dashboard route below detaches the ids when the injection turn is
        launched and owes them to the turn's CONSUMPTION through the slot's
        delivery ledger (#2233); for routes whose ``_on_done`` return really
        is the confirmation it is the run loop, after ``_on_done`` — routing
        included — returns cleanly."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        slot = MagicMock()
        slot.mode = "chat"
        slot.running = False
        slot.task = None
        slot._orch_tracker = None
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        ledger, settled = _wire_hold_settlement(orch, slot, mgr)
        total = 12
        members = [
            self._member(i, total, error="boom" if i == 2 else "")
            for i in range(total)
        ]

        async def _consuming_run_chat(_state, _slot, _text, *, _on_consumed=None, **_kw):
            # The model consumed the injected digest — the one condition that
            # settles this route's holds (#2233).
            if _on_consumed is not None:
                _on_consumed()

        marked: list[str] = []
        with patch("kiro_crew.slack.gateway._run_chat", _consuming_run_chat), \
                patch("kiro_crew.subagent_persistence.mark_delivered",
                      side_effect=marked.append):
            for i, m in enumerate(members):
                mgr.batch_members_pending = MagicMock(return_value=i != total - 1)
                await on_done(m)
                await asyncio.sleep(0)
                # Let each flush's injection turn finish before the next member
                # reports, so both flushes take the direct (idle-slot) branch.
                await _settle(lambda: slot.task is None)
            # Both chunks' injection turns must report consumption before their
            # holds can settle — the settle is owed to the turn, not to the
            # `_on_done` return (#2233).
            await _settle(lambda: len(settled) >= 2)
        # Members 0-8 are held for chunk 1; member 9 (the 10th) flushes it.
        # Members 10 is held for chunk 2; member 11 (wave close) flushes it.
        held_idx = list(range(9)) + [10]
        flush_idx = [9, 11]
        assert all(members[i]._digest_held for i in held_idx)
        assert all(members[i]._digest_held is False for i in flush_idx)
        # NOTHING is tombstoned at composition time — a crash between
        # composing and routing must leave held results orphan-recoverable.
        assert marked == []
        # Each FLUSHING member's settle list is its own chunk's held OK members
        # only (chunk buffers reset between flushes). On THIS route the list is
        # detached when the injection turn is launched and settled through the
        # manager once the turn consumed the digest, so what is asserted is the
        # hand-off, not a residue left on the member (#2233): the member is
        # left clean and the ids reach the manager exactly once, per chunk.
        assert members[9]._digest_settle_ids == []
        assert members[11]._digest_settle_ids == []
        # Each debt leads with the FLUSHING member's own id: its tombstone is
        # deferred to the same consumption (`_delivery_queued`), closing the
        # identical loss window for the flusher's own result.
        assert members[9]._delivery_queued is True
        assert members[11]._delivery_queued is True
        assert settled == [
            [members[9].id] + [members[i].id for i in range(9) if not members[i].error],
            [members[11].id, members[10].id],
        ]
        # Per-wave bookkeeping pruned once the wave finished.
        mgr.finalize_batch.assert_called_once_with("bigwave")

    @pytest.mark.asyncio
    async def test_run_loop_settles_held_ids_after_on_done(self):
        """``_settle_digest_holds`` marks held ids delivered and is invoked in
        ``_run`` ONLY inside the try-block after ``_on_done`` succeeds — an
        _on_done failure must leave every held member undelivered
        (orphan-recoverable)."""
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())
        marked: list[str] = []
        info = SubagentInfo(id="last", task="t")
        info._digest_settle_ids = ["h1", "h2"]
        with patch("kiro_crew.subagent.mark_delivered", side_effect=marked.append):
            mgr._settle_digest_holds(info)
        assert marked == ["h1", "h2"]
        assert info._digest_settle_ids == []  # idempotent re-entry safe
        # Structural guarantee: the settle call sits AFTER the awaited
        # _on_done inside the same try-block, so an _on_done exception
        # (routing failure / crash) skips it entirely. The terminal report
        # (subagent_done + _on_done + settle) now lives in _report_terminal,
        # which `_run` runs on a shielded task — the ordering invariant is
        # unchanged, only its owning function moved.
        import inspect

        from kiro_crew.subagent_manager.terminal import TerminalCoordinator
        src = inspect.getsource(TerminalCoordinator._report_terminal_impl)
        on_done_pos = src.index("await asyncio.wait_for(self._manager._on_done(info)")
        settle_pos = src.index("self._manager._settle_digest_holds(info)")
        assert settle_pos > on_done_pos

    @pytest.mark.asyncio
    async def test_holds_settle_only_after_the_injection_turn_confirms(self):
        """Ownership (#2233): the dashboard route hands off asynchronously, so a
        bare ``_on_done`` return is not proof the digest reached the parent.

        ``_report_terminal`` settles ``info._digest_settle_ids`` right after
        ``_on_done`` returns. On the dashboard branch that return happens while
        the injection turn is still a *pending task* — so a shutdown or a
        cancelled slot turn between the two leaves the held siblings carrying
        ``delivered`` tombstones for a digest the parent never saw. A tombstone
        is exactly what ``list_orphans()`` uses to EXCLUDE a run folder from the
        next start's reconciliation, so those complete ``result.txt`` files
        become permanently invisible: no error, no notification, just N results
        the parent never receives and recovery will never offer again.

        The fix moves settlement to the side that actually owns the hand-off.
        The flushing member's settle ids are DETACHED from ``info`` when the
        injection task is launched — which makes the run loop's settle a no-op
        for this route — and owed to the turn's CONSUMPTION through the slot's
        delivery ledger, the same debt shape the queue branch records (#2233).
        Not even the turn's clean completion settles them: ``_run_chat``
        returns normally on several non-delivery paths (signed-out CLI, dead
        provider, exhausted retries, a first empty response), so only the
        consumption report — the model actually has the prompt — confirms the
        hand-off.

        The turn is gated on an ``asyncio.Event`` rather than timed: the state
        under test is "task created, consumption not yet reported", which a
        sleep can only approximate.
        """
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        slot = MagicMock()
        slot.mode = "chat"
        slot.running = False
        slot.task = None
        slot._orch_tracker = None
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        ledger, settled = _wire_hold_settlement(orch, slot, mgr)
        total = 12
        members = [self._member(i, total) for i in range(total)]

        turn_started = asyncio.Event()
        release_consume = asyncio.Event()

        async def _gated_run_chat(_state, _slot, _text, *, _on_consumed=None, **_kw):
            turn_started.set()
            await release_consume.wait()
            if _on_consumed is not None:
                _on_consumed()

        marked: list[str] = []
        with patch("kiro_crew.slack.gateway._run_chat", _gated_run_chat), \
                patch("kiro_crew.subagent_persistence.mark_delivered",
                      side_effect=marked.append):
            for i, m in enumerate(members[:10]):
                mgr.batch_members_pending = MagicMock(return_value=True)
                await on_done(m)
                await asyncio.sleep(0)

            # Preconditions: nine siblings are held, the tenth flushed chunk 1,
            # and its injection turn is RUNNING but has not consumed the digest.
            held_ids = [members[i].id for i in range(9)]
            owed = [members[9].id] + held_ids
            assert all(members[i]._digest_held for i in range(9)), (
                "precondition: the first nine members must be held for the chunk"
            )
            await _settle(turn_started.is_set)
            assert turn_started.is_set(), "precondition: the injection turn started"
            assert slot.task is not None and not slot.task.done(), (
                "precondition: the hand-off is still in flight — this is the "
                "window in which the current contract settles"
            )

            flusher = members[9]
            assert flusher._digest_settle_ids == [], (
                "the flushing member must not still be carrying the settle ids "
                "while the hand-off is unconfirmed: the run loop settles that "
                "list as soon as _on_done returns, which is now"
            )
            assert list(ledger.values()) == [owed], (
                "the ids — the flusher's own tombstone included — are parked "
                "in the slot's delivery ledger, owed, not settled: a process "
                "death here leaves them tombstone-free and recoverable by "
                "orphan reconciliation"
            )
            assert settled == [] and marked == [], (
                "nothing may be tombstoned before the hand-off lands"
            )

            # The model consumes the digest — NOW the hand-off is confirmed.
            release_consume.set()
            await _settle(lambda: bool(settled))

        # Settled through the manager by the side that owns the hand-off, once,
        # with exactly this chunk's held members. (``settle_queued_delivery`` is
        # mocked here; its real tombstone write and teardown gate are pinned by
        # test_subagent_delivery_ttl_anchor.py.)
        assert settled == [owed]
        assert marked == [], (
            "and never through the run loop's settle, which this route detached"
        )

    @pytest.mark.asyncio
    async def test_a_queued_hand_off_is_not_confirmed_until_the_turn_runs(self):
        """The same root cause one branch up (#2233, First Principles CONCERNS).

        When the parent slot is busy the digest is appended to ``slot._queue``
        and ``_subagent_done`` returns — so the run loop would settle on that
        bare return, exactly as it did for the direct branch.

        ``slot._queue`` is a plain in-memory list (``state.py``): the
        ``"queued"`` role is in ``chat_persistence._TRANSIENT_ROLES`` and no
        producer writes it to disk. ``_run_chat``'s ``finally`` drains it on any
        exit path *within the process*, which is why the enqueue looks durable —
        but a shutdown before the drain loses the announce entirely, and by then
        the held siblings would already carry ``delivered`` tombstones.
        Enqueueing is a local routing success, not evidence the parent received
        anything.

        ``_defer_queued_delivery`` therefore owes the held ids (together with
        the flushing member's own) to the drain through the slot's delivery
        ledger, keyed on the announce itself — the run loop's settle is a no-op
        here too, and settlement waits for a turn to actually consume the
        announce (the #4839 machinery; one debt shape for both routes).

        This test never drains the queue: that IS the process-loss window.
        """
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        slot = MagicMock()
        slot.mode = "chat"
        # Busy: a turn already owns the slot, so the completion is QUEUED rather
        # than dispatched. `task = None` keeps the shield-await a no-op.
        slot.running = True
        slot.task = None
        slot._orch_tracker = None
        slot._subagent_deliveries_inflight = 0
        slot._subagents_inline_collected = set()
        queued: list[dict] = []
        slot.queue_append = MagicMock(
            side_effect=lambda content, kind="", meta=None: (
                queued.append({"content": content, "kind": kind, "meta": meta}) or "qid"
            )
        )
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        ledger, settled = _wire_hold_settlement(orch, slot, mgr)
        total = 12
        members = [self._member(i, total) for i in range(total)]

        marked: list[str] = []
        with patch("kiro_crew.slack.gateway._run_chat", new_callable=AsyncMock), \
                patch("kiro_crew.subagent_persistence.mark_delivered",
                      side_effect=marked.append):
            for i, m in enumerate(members[:10]):
                mgr.batch_members_pending = MagicMock(return_value=True)
                await on_done(m)
                await asyncio.sleep(0)

        assert len(queued) == 1, (
            "precondition: the flushing chunk must have been QUEUED, not dispatched"
        )
        # The queue is deliberately never drained — the process died here.
        assert settled == [] and marked == [], (
            "an announce sitting in an in-memory queue is not a hand-off: a "
            "shutdown here loses the digest, and a delivered tombstone would "
            "hide the held results from orphan reconciliation forever"
        )
        assert members[9]._digest_settle_ids == [], (
            "the ids must have left the flushing member, so the run loop's "
            "settle on the bare _on_done return is a no-op for this route too"
        )
        assert members[9]._delivery_queued is True, (
            "the flushing member's own tombstone is deferred to the drain with "
            "the same debt (issue #4839)"
        )
        # The debt is parked in the ledger, keyed on the queued announce, and
        # names the flushing member itself plus its held siblings — so the
        # drain settles them all once a turn actually consumes the announce.
        held = [members[i].id for i in range(9)]
        assert list(ledger.keys()) == [queued[0]["content"]]
        assert ledger[queued[0]["content"]] == [members[9].id] + held

    @pytest.mark.asyncio
    async def test_an_auth_required_turn_is_not_a_confirmed_hand_off(self):
        """The third state: the turn ended cleanly and delivered nothing (#2233).

        ``_run_chat`` CATCHES ``AcpAuthRequired`` — a signed-out CLI is
        non-retryable, so it records the outcome on the slot, holds the queue
        intact for post-login resume, and returns NORMALLY. The injection task
        therefore completes with no exception and no cancellation, which is
        indistinguishable from a delivered digest if "the task finished" is the
        confirmation.

        It is not delivered: the digest never reached the LLM. Settling here
        tombstones results the parent has not seen — the exact loss this fix
        exists to close, re-entered through a narrower door.

        CONSUMPTION is the signal that cannot make this mistake: a signed-out
        CLI fails before the model sees a single token, so ``_run_chat`` never
        reports the prompt consumed and the debt stays parked in the ledger —
        no per-outcome flag inspection required.
        """
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        slot = MagicMock()
        slot.mode = "chat"
        slot.running = False
        slot.task = None
        slot._orch_tracker = None
        slot._subagent_deliveries_inflight = 0
        # Real attribute, not a MagicMock truthy stub: the stub below flips it
        # exactly as _run_chat does on a signed-out CLI.
        slot._queue_held = False
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        ledger, settled = _wire_hold_settlement(orch, slot, mgr)
        total = 12
        members = [self._member(i, total) for i in range(total)]

        async def _auth_required_run_chat(_state, _slot, _text, *, _on_consumed=None, **_kw):
            # Exactly what _run_chat does on a signed-out CLI: record it and
            # return. No raise, no cancellation — and no consumption report,
            # because the model never saw the prompt.
            _slot._queue_held = True

        marked: list[str] = []
        with patch("kiro_crew.slack.gateway._run_chat", _auth_required_run_chat), \
                patch("kiro_crew.subagent_persistence.mark_delivered",
                      side_effect=marked.append):
            for i, m in enumerate(members[:10]):
                mgr.batch_members_pending = MagicMock(return_value=True)
                await on_done(m)
                await asyncio.sleep(0)
            await _settle(lambda: slot.task is None)

        assert slot._queue_held is True, (
            "precondition: the turn must have ended in the auth-required state"
        )
        assert settled == [] and marked == [], (
            "a signed-out CLI never received the digest — the held siblings' "
            "results are still only on disk"
        )
        assert members[9]._digest_settle_ids == [], (
            "and the run loop must not settle them either: the ids left the "
            "flushing member when the turn was launched"
        )
        held = [members[i].id for i in range(9)]
        assert list(ledger.values()) == [[members[9].id] + held], (
            "the debt — the flusher's own tombstone included — stays owed, "
            "tombstone-free and recoverable, rather than settled on a clean "
            "return that delivered nothing"
        )

    @pytest.mark.asyncio
    async def test_a_failed_injection_turn_leaves_holds_recoverable(self):
        """The deliberate asymmetry (#2233): an unconfirmed hand-off must leave
        holds UNsettled rather than settle them.

        A duplicate digest after a restart is visible to the parent and
        recoverable; a lost one is neither. So when the injection turn raises
        before the model consumed the prompt, the held siblings keep no
        tombstone and stay visible to ``list_orphans()`` — the same direction
        ``_digest_held`` itself encodes. The debt stays parked in the slot's
        ledger, so a recovery replay of the announce can still claim it.
        """
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        slot = MagicMock()
        slot.mode = "chat"
        slot.running = False
        slot.task = None
        slot._orch_tracker = None
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        ledger, settled = _wire_hold_settlement(orch, slot, mgr)
        total = 12
        members = [self._member(i, total) for i in range(total)]

        async def _failing_run_chat(_state, _slot, _text, *, _on_consumed=None, **_kw):
            raise RuntimeError("injection turn died")

        marked: list[str] = []
        with patch("kiro_crew.slack.gateway._run_chat", _failing_run_chat), \
                patch("kiro_crew.subagent_persistence.mark_delivered",
                      side_effect=marked.append):
            for i, m in enumerate(members[:10]):
                mgr.batch_members_pending = MagicMock(return_value=True)
                await on_done(m)
                await asyncio.sleep(0)
            await _settle(lambda: slot.task is None)

        assert settled == [] and marked == [], (
            "a failed hand-off must not tombstone the held siblings — their "
            "results are still only on disk"
        )
        assert members[9]._digest_settle_ids == [], (
            "and the run loop must not settle them either: the ids left the "
            "flushing member when the turn was launched"
        )
        held = [members[i].id for i in range(9)]
        assert list(ledger.values()) == [[members[9].id] + held], (
            "the debt stays parked for a recovery replay to claim"
        )

    @pytest.mark.asyncio
    async def test_guard_msgs_from_all_members_fold_into_digest(self):
        """Orchestration escalations from HELD mid-wave members must survive
        into the digest (Arbiter item 3) — not just the last member's."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        slot = MagicMock()
        slot.mode = "orchestrator"
        # Pre-seeded tracker: every failure trips the escalation ceiling.
        tracker = MagicMock()
        tracker.stopped = False
        tracker.record_failure = MagicMock(return_value=True)
        tracker.failure_count = MagicMock(return_value=2)
        tracker.record_success = MagicMock()
        tracker.record_round = MagicMock(return_value=False)
        slot._orch_tracker = tracker
        slot.running = False
        slot.task = None
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        mgr.running_agents_for = MagicMock(return_value=["still-running"])
        total = 12
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text, *, _directive_user_origin, **_kw):
            assert _directive_user_origin is False
            injected.append(text)

        with patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat), \
                patch("kiro_crew.subagent_persistence.mark_delivered"):
            for i in range(total):
                mgr.batch_members_pending = MagicMock(return_value=i != total - 1)
                # Mid-wave failure (held member) trips the ceiling; the LAST
                # member succeeds, so its own guard_msg is empty.
                await on_done(self._member(i, total, error="boom" if i == 2 else ""))
                await asyncio.sleep(0)
            await _settle(lambda: len(injected) >= 2)
        assert len(injected) == 2  # chunked: 10 + 2
        combined = "\n".join(injected)
        # The held member's escalation instruction reached the parent, in the
        # chunk that contains that member…
        assert "You MUST ask the user for guidance" in injected[0]
        # …exactly once across the whole wave (deduped within the chunk, and
        # chunk buffers reset between flushes — no bleed into later chunks).
        assert combined.count("You MUST ask the user for guidance") == 1

    @pytest.mark.asyncio
    async def test_small_wave_delivers_single_chunk_digest(self):
        """Small multi-task waves (2-10 agents) get ONE consolidated chunk
        digest on wave close — chunking is uniform for every multi-task
        spawn, not gated on wave size. A 3-agent wave = 1 injection turn
        labelled 1/1 with the final release guidance, never 3 per-agent
        turns. (Single-task spawns have no batch identity and keep the plain
        per-agent injection — see test_single_spawn_keeps_per_agent below.)"""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        slot = MagicMock()
        slot.mode = "chat"
        slot.running = False
        slot.task = None
        slot._orch_tracker = None
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        total = 3
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text, *, _directive_user_origin, **_kw):
            assert _directive_user_origin is False
            injected.append(text)

        with patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat), \
                patch("kiro_crew.subagent_persistence.mark_delivered"):
            for i in range(total):
                mgr.batch_members_pending = MagicMock(
                    return_value=i != total - 1
                )
                await on_done(self._member(i, total))
                await asyncio.sleep(0)
            await _settle(lambda: len(injected) >= 1)
        assert len(injected) == 1  # one chunk digest, not 3 per-agent turns
        digest = injected[0]
        assert digest.startswith("[Subagent batch completion event]")
        assert "Batch results 1/1" in digest
        assert "3 ✅" in digest and "of 3 agents" in digest
        assert "before spawning any follow-up" in digest

    @pytest.mark.asyncio
    async def test_single_spawn_keeps_per_agent_injection(self):
        """A single-task spawn has no batch identity — its completion keeps
        the plain per-agent injection turn, untouched by chunking."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        slot = MagicMock()
        slot.mode = "chat"
        slot.running = False
        slot.task = None
        slot._orch_tracker = None
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        mgr.running_agents_for = MagicMock(return_value=[])
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text, *, _directive_user_origin, **_kw):
            assert _directive_user_origin is False
            injected.append(text)

        solo = SubagentInfo(
            id="solo", task="one-off task",
            parent_session_key="dashboard:main",
        )
        solo.done = True
        solo.result = "solo result"
        with patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat):
            await on_done(solo)
            await _settle(lambda: len(injected) >= 1)
        assert len(injected) == 1
        assert injected[0].startswith("[Subagent completion event]")
        assert "Batch results" not in injected[0]


# ── 4b. Hold deadline (straggler escape hatch, issue #2215) ──────────


class TestDigestHoldDeadline:
    """The chunk COUNT trigger cannot fire for a wave smaller than the chunk
    size, so wave close is its only flush — every sibling's finished result is
    withheld for the slowest member's remaining runtime, and for a member that
    HANGS rather than fails, for the full 30-minute reap. The reaper's
    hold-deadline sweep is the LATENCY trigger that releases them.
    """

    def _held_member(self, i: int, *, batch: str = "wv", total: int = 3) -> SubagentInfo:
        info = SubagentInfo(
            id=f"h{i}",
            task=f"held task {i}",
            parent_session_key="dashboard:main",
            batch_id=batch,
            batch_total=total,
        )
        info.done = True
        return info

    def _mgr(self, *, pending: bool = True) -> SubagentManager:
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())
        mgr._on_done = AsyncMock()
        mgr.batch_members_pending = MagicMock(return_value=pending)
        return mgr

    def test_hold_within_deadline_is_not_flushed(self):
        """A wave whose members finish close together must still deliver ONE
        consolidated digest — the deadline is a latency cap, not a per-member
        flush. Regression guard against re-introducing the chunk-size=1
        behavior (which floods the parent with N turns at scale)."""
        mgr = self._mgr()
        now = time.time()
        m = self._held_member(0)
        m._digest_held_at = now - 5.0
        mgr._agents["h0"] = m
        with patch.object(mgr, "force_digest_flush") as forced:
            mgr._sweep_digest_holds(now)
        forced.assert_not_called()

    def test_expired_hold_forces_flush(self):
        """THE BUG (#2215): two members finished, the third is still running, so
        neither chunk trigger can fire. Once the oldest hold ages past the
        deadline the sweep forces the partial digest out instead of waiting for
        the straggler (up to 30 min for a hang)."""
        from kiro_crew.subagent import DIGEST_HOLD_SECS

        mgr = self._mgr(pending=True)
        now = time.time()
        for i in (0, 1):
            m = self._held_member(i)
            m._digest_held_at = now - (DIGEST_HOLD_SECS + 10 - i)
            mgr._agents[m.id] = m
        with patch.object(mgr, "force_digest_flush") as forced:
            mgr._sweep_digest_holds(now)
        forced.assert_called_once()
        batch_id, parent, total, age = forced.call_args.args
        assert batch_id == "wv"
        assert parent == "dashboard:main"
        assert total == 3
        # Aged from the OLDEST hold in the wave, not the newest — the deadline
        # must describe the worst wait the parent actually suffered.
        assert age >= DIGEST_HOLD_SECS + 10 - 1

    def test_closing_wave_is_not_force_flushed(self):
        """When no member is outstanding the real wave-close digest (counts +
        release guidance) is already in flight; forcing a partial one here would
        race it and could double-deliver the same members."""
        from kiro_crew.subagent import DIGEST_HOLD_SECS

        mgr = self._mgr(pending=False)
        now = time.time()
        m = self._held_member(0)
        m._digest_held_at = now - (DIGEST_HOLD_SECS + 60)
        mgr._agents["h0"] = m
        with patch.object(mgr, "force_digest_flush") as forced:
            mgr._sweep_digest_holds(now)
        forced.assert_not_called()

    def test_deadline_zero_disables_sweep(self):
        """``KIROCREW_SUBAGENT_DIGEST_HOLD_SECS=0`` is the documented opt-out
        back to count-trigger-only behavior."""
        mgr = self._mgr()
        now = time.time()
        m = self._held_member(0)
        m._digest_held_at = now - 100_000.0
        mgr._agents["h0"] = m
        with patch("kiro_crew.subagent.DIGEST_HOLD_SECS", 0.0), \
                patch.object(mgr, "force_digest_flush") as forced:
            mgr._sweep_digest_holds(now)
        forced.assert_not_called()

    def test_unheld_members_never_trip_the_sweep(self):
        """``_digest_held_at`` is the sweep's ONLY input: a delivered member
        (hold cleared at flush) must not re-trigger a flush forever."""
        from kiro_crew.subagent import DIGEST_HOLD_SECS

        mgr = self._mgr()
        now = time.time()
        m = self._held_member(0)
        m._digest_held = True  # restart-safety flag stays set after the flush…
        m._digest_held_at = 0.0  # …but the hold clock was stopped
        mgr._agents["h0"] = m
        with patch.object(mgr, "force_digest_flush") as forced:
            mgr._sweep_digest_holds(now + DIGEST_HOLD_SECS * 10)
        forced.assert_not_called()

    def test_force_digest_flush_builds_flush_only_record(self):
        mgr = self._mgr()
        announced: list[SubagentInfo] = []

        async def _cap(info):
            announced.append(info)

        mgr._on_done = _cap
        mgr.force_digest_flush("wv", "dashboard:main", 3, 200.0)
        assert mgr._tasks  # scheduled
        asyncio.get_event_loop().run_until_complete(
            asyncio.gather(*mgr._tasks.values())
        )
        (rec,) = announced
        assert rec._digest_flush_only is True
        assert rec.batch_id == "wv" and rec.batch_total == 3
        assert rec.done is True and rec.error == ""
        assert "200s" in rec.task

    @pytest.mark.asyncio
    async def test_flush_only_settles_holds_only_after_on_done(self):
        """Same restart-safety contract as ``_run``: a routing failure must
        leave held members undelivered so orphan reconciliation can recover
        them. The flush-only path has no run loop, so it enforces it itself."""
        mgr = self._mgr()
        info = SubagentInfo(id="flush", task="t", batch_id="wv")
        info._digest_flush_only = True
        info._digest_settle_ids = ["h0", "h1"]

        marked: list[str] = []
        mgr._on_done = AsyncMock(side_effect=RuntimeError("routing blew up"))
        with patch("kiro_crew.subagent.mark_delivered", side_effect=marked.append):
            await mgr._announce_digest_flush(info)
        assert marked == []  # failure → nothing tombstoned
        assert info._digest_settle_ids == ["h0", "h1"]

        mgr._on_done = AsyncMock()
        with patch("kiro_crew.subagent.mark_delivered", side_effect=marked.append):
            await mgr._announce_digest_flush(info)
        assert marked == ["h0", "h1"]

    @pytest.mark.asyncio
    async def test_straggler_wave_delivers_partial_digest_end_to_end(self):
        """REPRO for #2215, end to end through the real sweep.

        A 3-member wave: two members finish, the third keeps running. Neither
        chunk trigger can fire — the COUNT trigger needs 10 pending completions
        and the wave has not closed — so on main the parent receives NOTHING
        until the straggler ends (up to the 30-minute reap if it hangs). After
        the fix the reaper's hold-deadline sweep releases the two finished
        results as a labelled partial digest."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        slot = MagicMock()
        slot.mode = "chat"
        slot.running = False
        slot.task = None
        slot._orch_tracker = None
        slot._subagent_deliveries_inflight = 0
        slot._subagents_inline_collected = set()
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        gw_mgr, on_done = TestWaveDigest()._capture_on_done(orch)
        gw_mgr.batch_members_pending = MagicMock(return_value=True)  # straggler alive
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text, *, _directive_user_origin, **_kw):
            assert _directive_user_origin is False
            injected.append(text)

        # Real manager for the sweep, wired to the gateway's own consumer.
        real = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())
        real._on_done = on_done
        real.batch_members_pending = MagicMock(return_value=True)

        finished = []
        for i in range(2):
            m = SubagentInfo(
                id=f"e{i}", task=f"task {i}",
                parent_session_key="dashboard:main",
                batch_id="e2e", batch_total=3,
            )
            m.done = True
            m.result = f"result {i}"
            m.result_path = f"/tmp/e{i}/result.txt"
            finished.append(m)
            real._agents[m.id] = m

        with patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat), \
                patch("kiro_crew.subagent_persistence.mark_delivered"), \
                patch("kiro_crew.subagent.mark_delivered"):
            for m in finished:
                await on_done(m)
                await asyncio.sleep(0)
            # Held: this is the reported symptom — two complete results on disk,
            # zero signal to the parent.
            assert injected == []

            # Advance past the hold deadline and run the sweep the reaper runs.
            # getattr keeps the failure BEHAVIORAL on unfixed code (no injection)
            # instead of an AttributeError.
            hold = getattr(__import__(
                "kiro_crew.subagent", fromlist=["DIGEST_HOLD_SECS"]
            ), "DIGEST_HOLD_SECS", 120.0)
            sweep = getattr(real, "_sweep_digest_holds", lambda _now: None)
            sweep(time.time() + hold + 5)
            await _settle(lambda: len(injected) >= 1)

        assert len(injected) == 1, "straggler withheld both finished siblings"
        digest = injected[0]
        assert "/tmp/e0/result.txt" in digest and "/tmp/e1/result.txt" in digest
        assert "2 of 3 delivered, 1 still running" in digest
        assert "PARTIAL result set" in digest

    @pytest.mark.asyncio
    async def test_gateway_flush_only_releases_held_results(self):
        """End of the chain: a flush-only record makes the gateway deliver the
        held siblings' digest WITHOUT inventing an agent — no terminal WS
        event, no done/ok counter bump, and the wave-close chunk still to come.
        This is the assertion that fails on main (2 of 3 → zero injections)."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        slot = MagicMock()
        slot.mode = "chat"
        slot.running = False
        slot.task = None
        slot._orch_tracker = None
        slot._subagent_deliveries_inflight = 0
        slot._subagents_inline_collected = set()
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = TestWaveDigest()._capture_on_done(orch)
        ledger, settled = _wire_hold_settlement(orch, slot, mgr)
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text, *, _directive_user_origin, _on_consumed=None, **_kw):
            assert _directive_user_origin is False
            injected.append(text)
            # The model consumed the flushed digest — the condition that
            # settles the held siblings on this route (#2233).
            if _on_consumed is not None:
                _on_consumed()

        members = [
            SubagentInfo(
                id=f"s{i}", task=f"task {i}",
                parent_session_key="dashboard:main",
                batch_id="strag", batch_total=3,
            )
            for i in range(2)
        ]
        for i, m in enumerate(members):
            m.done = True
            m.result = f"result {i}"
            m.result_path = f"/tmp/s{i}/result.txt"

        with patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat), \
                patch("kiro_crew.subagent_persistence.mark_delivered"):
            mgr.batch_members_pending = MagicMock(return_value=True)
            for m in members:
                await on_done(m)
                await asyncio.sleep(0)
            # Pre-fix behavior: nothing delivered — the count trigger (10) is
            # unreachable and the wave has not closed.
            assert injected == []
            assert all(m._digest_held for m in members)
            assert all(m._digest_held_at > 0 for m in members)

            flush = SubagentInfo(
                id="ff", task="(wave digest flush — results held 200s)",
                parent_session_key="dashboard:main",
                batch_id="strag", batch_total=3,
            )
            flush.done = True
            flush._digest_flush_only = True
            await on_done(flush)
            await _settle(lambda: len(injected) >= 1)

        assert len(injected) == 1
        digest = injected[0]
        assert digest.startswith("[Subagent batch completion event]")
        # Both finished siblings' results are in the parent's context now.
        assert "/tmp/s0/result.txt" in digest and "/tmp/s1/result.txt" in digest
        # Honest labelling: a partial release, wave-close chunk still to come.
        assert "Batch results 1/2" in digest
        assert "2 of 3 delivered, 1 still running" in digest
        assert "PARTIAL result set" in digest
        assert "wave finished" not in digest
        # The synthetic record invented no agent: no terminal WS event for it,
        # and it was not counted as a wave member.
        _done_ids = [
            c.args[1].get("id")
            for c in orch.dashboard_state.broadcast_ws.call_args_list
            if c.args[0] == "subagent_status"
        ]
        assert "ff" not in _done_ids
        assert "wave digest flush" not in digest
        # Hold clocks stopped, so the sweep cannot force a duplicate flush.
        assert all(m._digest_held_at == 0.0 for m in members)
        # Tombstones settle after routing — and on this route "after
        # routing" means after the model CONSUMED the injected digest, not
        # after `_on_done` returned: the ids left the flushing record when
        # the turn was launched, owed to the turn's consumption through the
        # slot's delivery ledger (#2233). The forced hold-deadline flush is
        # one of the settle callers, so it inherits the same ownership rule
        # without a second code path.
        assert flush._digest_settle_ids == []
        await _settle(lambda: bool(settled))
        assert settled == [["s0", "s1"]]

    @pytest.mark.asyncio
    async def test_flush_only_noop_when_nothing_held(self):
        """A sweep that races the wave-close flush must not emit a second,
        empty digest."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        orch.dashboard_state.get_slot = MagicMock(return_value=None)
        _mgr, on_done = TestWaveDigest()._capture_on_done(orch)
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text, *, _directive_user_origin, **_kw):
            assert _directive_user_origin is False
            injected.append(text)

        flush = SubagentInfo(
            id="ff", task="(wave digest flush — results held 200s)",
            parent_session_key="dashboard:main",
            batch_id="gone", batch_total=3,
        )
        flush.done = True
        flush._digest_flush_only = True
        with patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat):
            await on_done(flush)
            await asyncio.sleep(0.05)
        assert injected == []
        # And no phantom "agent completed" notification for the synthetic record.
        orch.dashboard_state.notify.assert_not_called()


# ── 5. Retry endpoint gating ─────────────────────────────────────────


class TestRetryGating:
    def _mgr_with(self, info: SubagentInfo) -> MagicMock:
        mgr = MagicMock()
        mgr.get = MagicMock(return_value=info)
        return mgr

    def _request(self, mgr, agent_id: str):
        req = MagicMock()
        req.app = {"state": MagicMock(subagents=mgr)}
        req.match_info = {"agent_id": agent_id}
        return req

    @pytest.mark.asyncio
    async def test_retry_rejects_running_and_stopped(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn_retry

        running = SubagentInfo(id="r1", task="t")
        resp = await api_spawn_retry(self._request(self._mgr_with(running), "r1"))
        assert resp.status == 409

        stopped = SubagentInfo(id="r2", task="t")
        stopped.done = True
        stopped.user_stopped = True
        resp = await api_spawn_retry(self._request(self._mgr_with(stopped), "r2"))
        assert resp.status == 409

    @pytest.mark.asyncio
    async def test_retry_respawns_failed_with_original_task(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn_retry

        failed = SubagentInfo(id="f1", task="redacted task", parent_session_key="dashboard:m")
        failed.done = True
        failed.error = "boom"
        failed._raw_task = "original raw task"
        mgr = self._mgr_with(failed)
        new_info = SubagentInfo(id="n1", task="original raw task")
        mgr.spawn = MagicMock(return_value=new_info)
        resp = await api_spawn_retry(self._request(mgr, "f1"))
        assert resp.status == 200
        assert mgr.spawn.call_args.args[0] == "original raw task"
        assert mgr.spawn.call_args.kwargs["parent_session_key"] == "dashboard:m"

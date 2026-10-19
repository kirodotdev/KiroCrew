"""The spawn side of a wave: event coalescing, batch identity, a second stop, stall dampening.

Items 1-3 of ``test_subagent_scale.py``'s list: ``SubagentEventCoalescer`` below and above
its activation threshold; a batch's identity through ``spawn``, the queue, rejections,
cancels, a parent's stop, lost submissions and the stuck-wave sweep; a second Stop of a run
stopped while it waited; and the two-sweep stall confirmation. The mocks and the autouse
manager close come from ``subagent_scale_helpers.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from overload_fakes import ManagerHarness, wait_taskq_open
from subagent_scale_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _ANNOUNCE_WAIT_SECS,
    _close_subagent_managers,
    _mock_ctx,
    _mock_sessions,
)

from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_scale import SubagentEventCoalescer

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")


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
        for etype in (
            "subagent_spawn",
            "subagent_done",
            "subagent_recovering",
            "subagent_injection_failed",
            "spawn_batch_started",
            "batch_finished",
        ):
            assert c.handle(etype, {"id": "a1", "slot": "s"}) is False

    @pytest.mark.asyncio
    async def test_tool_merge_clears_stale_retrying_attempt(self):
        """A tool delta after a retrying delta means work RESUMED — the merged
        entry must not carry the stale `attempt` (the frontend would leave the
        row marked retrying after recovery)."""
        c, all_frames, _ = _coalescer(active=50)
        c.handle("subagent_retrying", {"id": "a1", "slot": "s", "attempt": 1})
        c.handle("subagent_tool", {"id": "a1", "slot": "s", "tool": "Read", "tool_count": 2})
        # flake-ok: pre-existing 60 ms wait past the 20 ms flush tick -- moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 60 ms wait past the 20 ms flush tick moved verbatim
        await asyncio.sleep(0.06)
        ((etype, data),) = all_frames
        entry = data["updates"][0]
        assert entry["tool"] == "Read"
        assert "attempt" not in entry

    @pytest.mark.asyncio
    async def test_above_threshold_absorbs_and_flushes_one_frame(self):
        c, all_frames, _ = _coalescer(active=50)
        assert (
            c.handle("subagent_tool", {"id": "a1", "slot": "s", "tool": "Read", "tool_count": 1})
            is True
        )
        assert (
            c.handle("subagent_tool", {"id": "a2", "slot": "s", "tool": "Grep", "tool_count": 3})
            is True
        )
        # Latest state wins per agent
        assert (
            c.handle("subagent_tool", {"id": "a1", "slot": "s", "tool": "Write", "tool_count": 2})
            is True
        )
        assert all_frames == []  # nothing until the tick
        # flake-ok: pre-existing 60 ms wait past the 20 ms flush tick -- moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 60 ms wait past the 20 ms flush tick moved verbatim
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
        # flake-ok: pre-existing 60 ms wait past the 20 ms flush tick -- moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 60 ms wait past the 20 ms flush tick moved verbatim
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

    def test_wave_has_live_nested_spawns_detects_member_descendants(self):
        """A wave member that spawned nested work is DONE (its own turn ended),
        so batch_members_pending is False and the wave-close digest fires — but
        a child the member spawned is still running. The nested child mints its
        OWN batch_id and its parent_session_key is the member's session key
        (``subagent:<member.id>``), so it counts against neither this wave's
        total nor batch_members_pending. This is the state where the digest
        must not claim completion, and this method reports True for it.

        The scope is narrow on purpose: an unrelated sibling wave under the SAME
        grandparent is NOT a child of this wave's members, so this method
        reports False for it — a sibling wave cannot hold this digest hostage.
        """
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())
        # Wave "wv": both direct members finished.
        m0 = SubagentInfo(
            id="m0", task="t", batch_id="wv", batch_total=2, parent_session_key="dashboard:main"
        )
        m0.done = True
        m1 = SubagentInfo(
            id="m1", task="t", batch_id="wv", batch_total=2, parent_session_key="dashboard:main"
        )
        m1.done = True
        # A nested child spawned BY m1 — its own batch, parent is m1's session.
        nested = SubagentInfo(
            id="n1",
            task="nested",
            batch_id="nestwave",
            batch_total=2,
            parent_session_key="subagent:m1",
        )
        # An unrelated sibling wave under the SAME grandparent (dashboard:main),
        # NOT a child of any wv member.
        sibling = SubagentInfo(
            id="s1",
            task="sib",
            batch_id="sibwave",
            batch_total=2,
            parent_session_key="dashboard:main",
        )
        mgr._agents = {"m0": m0, "m1": m1, "n1": nested, "s1": sibling}

        # Direct members all done -> the wave would close by the count.
        assert mgr.batch_members_pending("wv") is False
        # But a member's nested spawn is still live -> digest cannot claim done.
        assert mgr.wave_has_live_nested_spawns("wv") is True
        # The unrelated sibling wave must NOT be attributed to wv (no hostage).
        assert mgr.wave_has_live_nested_spawns("sibwave") is False

        # A queued (not-yet-registered) nested spawn of a member also counts.
        mgr._agents = {"m0": m0, "m1": m1}
        assert mgr.wave_has_live_nested_spawns("wv") is False
        mgr._queue.append(
            {"task": "queued nested", "batch_id": "nestwave2", "parent_session_key": "subagent:m0"}
        )
        assert mgr.wave_has_live_nested_spawns("wv") is True
        mgr._queue.clear()

        # When the nested child finishes, the method goes False.
        nested.done = True
        mgr._agents = {"m0": m0, "m1": m1, "n1": nested}
        assert mgr.wave_has_live_nested_spawns("wv") is False
        assert mgr.wave_has_live_nested_spawns("") is False

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
        would vanish: no completion event, and the caller still believed the run
        was going (crew left the topic `running` forever). `_announce_rejection`
        gates on batch_id because a DIRECT caller reads the error off the return
        value -- that does not hold for a timer-driven drain.
        """
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())
        announced: list[SubagentInfo] = []
        registered: list[bool] = []

        async def _on_done(info):  # type: ignore[no-untyped-def]
            announced.append(info)
            registered.append(f"reject-{info.id}" in mgr._tasks)

        mgr._on_done = _on_done
        mgr._queue = [
            {
                "task": "waited too long",
                "_preassigned_id": "q-reject",
                "parent_session_key": "dashboard:chat-1",
                "batch_id": "",
            }
        ]
        mgr._running_count = 0
        mgr._spawn_stagger_secs = 0.0
        mgr._last_spawn_ts = 0.0
        mgr._emit_queue_depth = MagicMock()
        rejected = SubagentInfo(
            id="q-reject",
            task="waited too long",
            done=True,
            error="cwd does not exist or is not a directory",
        )
        mgr.spawn = lambda **kw: rejected

        # The pump is a coroutine on a running loop; await one pass.
        await mgr._drain_queue_async()
        # flake-ok: pre-existing zero-length yield for a scheduled task or done callback -- moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)  # the announce runs
        # flake-ok: pre-existing zero-length yield for a scheduled task or done callback -- moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)  # its done callback runs
        assert [i.id for i in announced] == ["q-reject"], "a rejection at drain time was dropped"
        assert registered == [True]  # held in _tasks while in flight, so shutdown can cancel it
        assert "reject-q-reject" not in mgr._tasks, "a finished announce stayed registered"

    def test_a_drained_batch_rejection_is_not_double_announced(self):
        """`_announce_rejection` announces batch members ITSELF, from inside spawn.

        So the drain must cover only the set it skips -- non-batch runs. Announcing
        regardless counted a queued batch rejection twice: the wave's accounting
        closed early and emitted a duplicate or incomplete digest.
        """
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())
        mgr._on_done = AsyncMock()
        mgr._queue = [
            {
                "task": "wave member",
                "_preassigned_id": "q-batch",
                "parent_session_key": "dashboard:chat-1",
                "batch_id": "wv",
            }
        ]
        mgr._running_count = 0
        mgr._spawn_stagger_secs = 0.0
        mgr._last_spawn_ts = 0.0
        mgr._emit_queue_depth = MagicMock()
        # Rejected AND a batch member: spawn's own `_announce_rejection` owns this.
        mgr.spawn = lambda **kw: SubagentInfo(
            id="q-batch",
            task="wave member",
            done=True,
            batch_id="wv",
            error="cwd does not exist or is not a directory",
        )

        mgr._drain_queue()
        assert (
            "reject-q-batch" not in mgr._tasks
        ), "the drain announced a batch rejection that spawn already announced"

    def test_a_drained_success_is_not_announced_twice(self):
        """The announce is for TERMINAL rejections only -- a run that actually
        started reports through its own completion path."""
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())
        mgr._on_done = AsyncMock()
        mgr._queue = [
            {
                "task": "fine",
                "_preassigned_id": "q-ok",
                "parent_session_key": "dashboard:chat-1",
                "batch_id": "",
            }
        ]
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
            {
                "task": "cancelled while waiting",
                "_preassigned_id": "q-stopped",
                "parent_session_key": "dashboard:chat-1",
                "batch_id": "",
            },
            {
                "task": "still wanted",
                "_preassigned_id": "q-live",
                "parent_session_key": "dashboard:chat-1",
                "batch_id": "",
            },
        ]
        mgr._running_count = 0
        mgr._spawn_stagger_secs = 0.0
        mgr._last_spawn_ts = 0.0
        mgr._emit_queue_depth = MagicMock()
        assert "q-stopped" not in mgr._agents, "premise: a queued run is unregistered"

        assert (
            asyncio.run(mgr.cancel("q-stopped")) is True
        ), "cancel reported failure for a run it can still prevent"
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
            {
                "task": "someone else's work",
                "_preassigned_id": "q-other",
                "parent_session_key": "dashboard:chat-1",
                "batch_id": "",
            },
        ]
        mgr._emit_queue_depth = MagicMock()
        assert asyncio.run(mgr.cancel("never-existed")) is False
        assert [p["_preassigned_id"] for p in mgr._queue] == [
            "q-other"
        ], "cancelling an unknown id evicted an unrelated queued run"
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

        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx(), on_done=on_done)
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
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch.object(SubagentManager, "_run", new=AsyncMock()),
        ):
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

        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx(), on_done=_on_done)
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            rejected = mgr.spawn("   ", batch_id="wv9", batch_total=2)
            plain = mgr.spawn("   ")  # non-batch rejection: no announce
        # flake-ok: pre-existing zero-length yield for a scheduled task or done callback -- moved verbatim; tracked for a de-flake pass
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
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=ctx, on_done=_on_done)
        await mgr.wait_taskq_ready()
        mgr._is_yolo = None
        mgr._on_spawn_approval = None  # no approval callback configured
        mgr._spawn_stagger_secs = 0.0
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            rejected = mgr.spawn("do work", batch_id="wvA", batch_total=2)
        # flake-ok: pre-existing zero-length yield for a scheduled task or done callback -- moved verbatim; tracked for a de-flake pass
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

        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx(), on_done=_on_done)
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
        # flake-ok: pre-existing zero-length yield for a scheduled task or done callback -- moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)
        assert mgr._batch_submitted["wvL"] == [3, 3]
        assert mgr.batch_members_pending("wvL") is False  # wave can close
        assert len(announced) == 1
        got = announced[0]
        assert got.batch_id == "wvL" and got.done and got.error
        assert "submission lost" in got.error
        assert got.outcome == "failed"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("exit_kind", ["returns", "raises", "cancelled"])
    async def test_record_lost_submission_announce_leaves_task_registry(self, exit_kind):
        """The synthetic member's announce stays in ``_tasks`` while it is in
        flight, so shutdown can cancel it, and leaves on every exit: the
        announce returns, ``on_done`` raises, or the task is cancelled."""
        release = asyncio.Event()

        async def _on_done(info):
            await asyncio.wait_for(release.wait(), _ANNOUNCE_WAIT_SECS)
            if exit_kind == "raises":
                raise RuntimeError("delivery failed")

        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx(), on_done=_on_done)
        with patch("kiro_crew.subagent.sel"):
            mgr.record_lost_submission("wvL", 2, "connection refused")
        (task,) = mgr._tasks.values()
        # flake-ok: pre-existing zero-length yield for a scheduled task or done callback -- moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)
        assert not task.done()  # in flight and registered
        if exit_kind == "cancelled":
            task.cancel()
        else:
            release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), _ANNOUNCE_WAIT_SECS)
        # flake-ok: pre-existing zero-length yield for a scheduled task or done callback -- moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)  # done callbacks run on the next loop pass
        assert mgr._tasks == {}

    @pytest.mark.asyncio
    async def test_finished_announce_keeps_a_newer_task_under_its_key(self):
        """The done callback drops the key only while it still maps to the
        finished task, so it never drops a task registered later under it."""
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())
        first = asyncio.ensure_future(asyncio.sleep(0))
        mgr._waves._hold_announce_task("lost-x", first)
        # flake-ok: pre-existing never-set Event as a stand-in task that the test cancels under a bounded gather -- moved verbatim; tracked for a de-flake pass
        newer = asyncio.ensure_future(asyncio.Event().wait())
        mgr._tasks["lost-x"] = newer
        await asyncio.wait_for(first, _ANNOUNCE_WAIT_SECS)
        # flake-ok: pre-existing zero-length yield for a scheduled task or done callback -- moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)  # done callbacks run on the next loop pass
        assert mgr._tasks == {"lost-x": newer}
        newer.cancel()
        await asyncio.wait_for(asyncio.gather(newer, return_exceptions=True), _ANNOUNCE_WAIT_SECS)

    @pytest.mark.asyncio
    async def test_reaper_stuck_wave_sweep_reconciles(self):
        """The reaper backstop force-reconciles a wave with lost submissions:
        submitted < expected, all registered members terminal, nothing
        queued, no progress for _WAVE_STUCK_SECS. Waves inside the grace
        window, with live members, or with queued members are left alone."""
        import time as _time

        from kiro_crew.subagent import _WAVE_STUCK_SECS

        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())
        # flake-ok: pre-existing wall-clock stamp handed to the sweep -- every wave's stamp is set relative to it -- moved verbatim; tracked for a de-flake pass
        now = _time.time()
        done_m = SubagentInfo(id="d1", task="t", batch_id="stuck", batch_total=2)
        done_m.done = True
        live_m = SubagentInfo(id="l1", task="t", batch_id="alive", batch_total=2)
        mgr._agents = {"d1": done_m, "l1": live_m}
        mgr._batch_submitted = {
            "stuck": [1, 2],  # lost submission, member done, stale -> reconcile
            "alive": [1, 2],  # lost submission but a member still RUNS -> skip
            "fresh": [1, 2],  # within the grace window -> skip
            "full": [2, 2],  # complete -> skip
        }
        stale = now - _WAVE_STUCK_SECS - 60
        mgr._batch_progress_ts = {
            "stuck": stale,
            "alive": stale,
            "fresh": now,
            "full": stale,
        }
        with patch("kiro_crew.subagent.sel"), patch.object(mgr, "record_lost_submission") as rec:
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
                "http://x/api/spawn",
                400,
                "Bad Request",
                {},  # type: ignore[arg-type]
                io.BytesIO(payload),
            )

        counted = _http_error_body(_err(b'{"error": "spawn refused", "counted": true}'))
        assert counted.get("counted") is True and "spawn refused" in counted["error"]
        uncounted = _http_error_body(_err(b'{"error": "task is required"}'))
        assert "counted" not in uncounted

    @pytest.mark.asyncio
    async def test_batch_fields_set_and_started_event_fires_once(self):
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())
        await mgr.wait_taskq_ready()
        mgr._spawn_stagger_secs = 0.0  # no stagger queueing in this test
        events: list[tuple[str, dict]] = []

        async def _spy(etype, info, extra=None):
            events.append((etype, extra or {}))

        mgr._on_event = _spy
        # Skip actual execution — spawn creates the task; cancel it right away.
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch.object(SubagentManager, "_run", new=AsyncMock()),
        ):
            i1 = mgr.spawn("t1", batch_id="wave1", batch_total=3)
            i2 = mgr.spawn("t2", batch_id="wave1", batch_total=3)
            i3 = mgr.spawn("t3", batch_id="wave1", batch_total=3)
            # flake-ok: pre-existing 50 ms wait for the fire-and-forget event task -- moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 50 ms wait for the fire-and-forget event task moved verbatim
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
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch.object(SubagentManager, "_run", new=AsyncMock()),
        ):
            info = mgr.spawn("solo task")
            # flake-ok: pre-existing zero-length yield for a scheduled task or done callback -- moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)
        assert info.batch_id == "" and info.batch_total == 0


@contextlib.contextmanager
def _record_running_count(mgr: SubagentManager) -> Iterator[list[int]]:
    """Record every value written to ``mgr._running_count`` while the block runs."""
    writes: list[int] = []
    current = {"count": mgr.__dict__.pop("_running_count")}

    def _get(self: SubagentManager) -> int:
        return current["count"] if self is mgr else self.__dict__["_running_count"]

    def _set(self: SubagentManager, value: int) -> None:
        if self is mgr:
            writes.append(value)
            current["count"] = value
        else:
            self.__dict__["_running_count"] = value

    with patch.object(SubagentManager, "_running_count", property(_get, _set), create=True):
        try:
            yield writes
        finally:
            mgr.__dict__["_running_count"] = current["count"]


class TestSecondStopOfAQueuedRun:
    """A second Stop of a run stopped while it waited frees no slot and reaps nothing.

    The first Stop registers the queued stop's terminal record (``queued=True``,
    ``done=False`` until its report task runs). A second Stop in that window, the
    same tick (a double click) or the next await, must answer "not running"
    instead of reaping that record as a live run, which would free a lane slot
    two running agents still hold and let the pump start a third.
    """

    CAP = 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("concurrent", [True, False], ids=["concurrent", "sequential"])
    async def test_second_stop_frees_no_slot_and_starts_nothing(self, concurrent: bool) -> None:
        h = ManagerHarness(max_concurrent=self.CAP)
        reports: list[str] = []

        async def on_done(info):  # type: ignore[no-untyped-def]
            reports.append(info.id)

        h.mgr._on_done = on_done
        try:
            await wait_taskq_open(h.mgr)
            running = [h.spawn(f"running-{i}") for i in range(self.CAP)]
            waiting = h.spawn("waiting")
            await h.settle()
            assert h.started == [info.id for info in running], "premise: two run, one waits"
            assert h.mgr.running_count == self.CAP

            with _record_running_count(h.mgr) as writes:
                if concurrent:
                    answers = list(
                        await asyncio.gather(h.mgr.cancel(waiting.id), h.mgr.cancel(waiting.id))
                    )
                else:
                    answers = [await h.mgr.cancel(waiting.id), await h.mgr.cancel(waiting.id)]
                await h.settle()
                assert h.mgr.running_count == self.CAP, "two runs still hold both slots"
                assert answers == [True, False], "a second Stop of a stopped row is not running"
                assert h.mgr._release_slot(h.mgr._agents[waiting.id]) is False

                extra = h.spawn("extra")
                await h.settle()
                assert extra.id not in h.started, "a third run started with a cap of two"

                for info in running:
                    await h.end(info)
                await h.end(extra)
                await asyncio.gather(*list(h.mgr._report_tasks))

            assert h.started == [*(info.id for info in running), extra.id]
            assert h.mgr.running_count == 0
            assert writes and 0 <= min(writes) and max(writes) <= self.CAP, writes
            assert reports.count(waiting.id) == 1, reports
            h.mgr._sessions.reset.assert_not_awaited()  # no run was reaped
        finally:
            h.close()

    @pytest.mark.asyncio
    async def test_second_stop_makes_no_store_call(self) -> None:
        """The second Stop answers from the registered record alone.

        Its row already ended with the first Stop, so there is nothing for the
        store to cancel, and a store call here would run on the event loop.
        """
        h = ManagerHarness(max_concurrent=self.CAP)
        try:
            await wait_taskq_open(h.mgr)
            running = [h.spawn(f"running-{i}") for i in range(self.CAP)]
            waiting = h.spawn("waiting")
            await h.settle()
            assert await h.mgr.cancel(waiting.id) is True
            assert h.mgr._agents[waiting.id].queued, "premise: the stop's record is registered"

            admission_type = type(h.mgr._admission)
            real_cancel = admission_type.taskq_cancel_queued
            store_cancels: list[str] = []

            def _counting(self: object, agent_id: str, *args: object, **kwargs: object) -> object:
                store_cancels.append(agent_id)
                return real_cancel(self, agent_id, *args, **kwargs)

            loop_calls = h.store.loop_thread_calls
            with patch.object(admission_type, "taskq_cancel_queued", _counting):
                assert await h.mgr.cancel(waiting.id) is False
            assert store_cancels == [], "the second Stop asked the store to cancel the row again"
            assert h.store.loop_thread_calls == loop_calls, "a store call ran on the event loop"

            for info in running:
                await h.end(info)
            await asyncio.gather(*list(h.mgr._report_tasks))
            assert h.mgr.running_count == 0
        finally:
            h.close()


# ── 3. Stall two-sweep confirmation ──────────────────────────────────


class TestStallDampening:
    def _info(self, idle_for: float) -> SubagentInfo:
        info = SubagentInfo(id="s1", task="t")
        info.turns = 1
        # flake-ok: pre-existing wall-clock idle stamp with a 10 s margin over the stall threshold -- moved verbatim; tracked for a de-flake pass
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
        # flake-ok: pre-existing wall-clock idle stamp with a 10 s margin over the stall threshold -- moved verbatim; tracked for a de-flake pass
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
        # flake-ok: pre-existing wall-clock idle stamp with a 10 s margin over the stall threshold -- moved verbatim; tracked for a de-flake pass
        await mgr._maybe_flag_stall("s1", info, time.time())
        assert info._stall_suspect_at > 0
        await mgr._touch_activity(info)  # stream event lands
        assert info._stall_suspect_at == 0.0
        # Next sweep starts the confirmation over (fresh idle needed).
        # flake-ok: pre-existing wall-clock sweep time read right after the activity touch -- the idle span is near zero -- moved verbatim; tracked for a de-flake pass
        await mgr._maybe_flag_stall("s1", info, time.time())
        assert info.stalled is False

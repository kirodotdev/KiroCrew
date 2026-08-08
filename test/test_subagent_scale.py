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
        slot._orch_tracker = None
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        total = 12  # chunk size 10 -> chunks of 10 + 2
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text):
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
    async def test_batch_finished_event_carries_counts(self):
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        slot = MagicMock()
        slot.mode = "chat"
        slot.running = False
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
        and the run loop settles them only after ``_on_done`` — routing
        included — returns cleanly."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        slot = MagicMock()
        slot.mode = "chat"
        slot.running = False
        slot._orch_tracker = None
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        total = 12
        members = [
            self._member(i, total, error="boom" if i == 2 else "")
            for i in range(total)
        ]
        marked: list[str] = []
        with patch("kiro_crew.slack.gateway._run_chat", new_callable=AsyncMock), \
                patch("kiro_crew.subagent_persistence.mark_delivered",
                      side_effect=marked.append):
            for i, m in enumerate(members):
                mgr.batch_members_pending = MagicMock(return_value=i != total - 1)
                await on_done(m)
                await asyncio.sleep(0)
        # Members 0-8 are held for chunk 1; member 9 (the 10th) flushes it.
        # Members 10 is held for chunk 2; member 11 (wave close) flushes it.
        held_idx = list(range(9)) + [10]
        flush_idx = [9, 11]
        assert all(members[i]._digest_held for i in held_idx)
        assert all(members[i]._digest_held is False for i in flush_idx)
        # NOTHING is tombstoned at composition time — a crash between
        # composing and routing must leave held results orphan-recoverable.
        assert marked == []
        # Each FLUSHING member carries its own chunk's settle list: the held
        # OK members of that chunk only (chunk buffers reset between flushes).
        assert sorted(members[9]._digest_settle_ids) == sorted(
            members[i].id for i in range(9) if not members[i].error
        )
        assert members[11]._digest_settle_ids == [members[10].id]
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

        from kiro_crew import subagent as _mod
        src = inspect.getsource(_mod.SubagentManager._report_terminal)
        on_done_pos = src.index("await asyncio.wait_for(self._on_done(info)")
        settle_pos = src.index("self._settle_digest_holds(info)")
        assert settle_pos > on_done_pos

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
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        mgr.running_agents_for = MagicMock(return_value=["still-running"])
        total = 12
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text):
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
        slot._orch_tracker = None
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        total = 3
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text):
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
        slot._orch_tracker = None
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        mgr.running_agents_for = MagicMock(return_value=[])
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text):
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
        slot._orch_tracker = None
        slot._subagent_deliveries_inflight = 0
        slot._subagents_inline_collected = set()
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        gw_mgr, on_done = TestWaveDigest()._capture_on_done(orch)
        gw_mgr.batch_members_pending = MagicMock(return_value=True)  # straggler alive
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text):
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
        slot._orch_tracker = None
        slot._subagent_deliveries_inflight = 0
        slot._subagents_inline_collected = set()
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = TestWaveDigest()._capture_on_done(orch)
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text):
            injected.append(text)

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
        # Tombstones settle on the flushing record, after routing.
        assert sorted(flush._digest_settle_ids) == ["s0", "s1"]

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

        async def _fake_run_chat(_state, _slot, text):
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

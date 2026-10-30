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

Items 1-3 (the coalescer, batch identity with a queued run's second stop, stall
dampening) are in ``test_subagent_scale_batch.py``; item 5, the durable queue at scale,
run-id minting and the queued depth reaching zero in ``test_subagent_scale_queue.py``. This
module keeps the wave digest and its hold deadline: ``TestDigestHoldDeadline`` calls
``TestWaveDigest``'s own capture helper, so the two share one module. The mocks and the
orchestrator they share live in ``subagent_scale_helpers.py``.
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from subagent_scale_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _ANNOUNCE_WAIT_SECS,
    _close_subagent_managers,
    _drain_injections,
    _make_orchestrator,
    _mock_ctx,
    _mock_dashboard_state,
    _mock_sessions,
)

from kiro_crew.subagent import SubagentDelivery, SubagentInfo, SubagentManager
from kiro_crew.testing.wait import async_wait_until

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")

# Subagent-registry isolation is provided globally by the autouse
# ``_isolate_subagents_dir`` fixture in ``conftest.py``.


# ── 4. Wave-digest completion injection ──────────────────────────────


async def _settle(predicate, what: str, timeout: float = 5.0) -> None:
    """Poll until *predicate* is truthy, and FAIL naming *what* on the ceiling.

    create_task'd injection turns need real event-loop time on slow CI shards,
    not one sleep(0). The ceiling is a lost-run guard, never the barrier: a
    poll that returns on an unsatisfied predicate hands the assertions a state
    nobody waited for, and the run then fails as whichever of them touches it
    first — an injection turn that never ran reported as ``assert 1 == 2``, or
    as an unrelated ledger mismatch three lines on. Measured on this host,
    every call site here settles within one 0.02 s poll, so reaching the
    ceiling is a stall and not a slow runner.
    """
    await async_wait_until(predicate, timeout=timeout, describe=lambda: what)


def _wire_hold_settlement(orch, slot, mgr):
    """Wire the slot's delivery ledger and the manager's async settle seam.

    The direct-injection branch owes a flushing digest's held ids to the
    turn's CONSUMPTION through the slot's content-keyed delivery ledger and
    settles them through ``SubagentManager.settle_queued_delivery`` — the same
    machinery the queue drain uses. The MagicMock
    slot needs a real mini-ledger for that flow to be observable, and the
    mocked manager's settle must hand back a real coroutine or the settlement
    path skips it (the stubbed-manager guard in
    ``_arm_queued_delivery_settlement``). Production wires
    ``DashboardState(subagents=<the manager>)``; mirror it.

    Returns ``(ledger, settled)``: the content-keyed debts still parked, and
    the id batches the manager was asked to settle.
    """
    ledger: dict[str, list[SubagentDelivery]] = {}
    slot.note_pending_subagent_delivery = MagicMock(
        side_effect=lambda content, ids: ledger.setdefault(content, []).extend(ids)
    )
    slot.take_pending_subagent_deliveries = MagicMock(
        side_effect=lambda contents: [i for c in contents for i in ledger.pop(c, [])]
    )
    settled: list[list[str]] = []

    async def _record_settle(deliveries):
        settled.append([delivery.agent_id for delivery in deliveries])

    mgr.settle_queued_delivery = MagicMock(side_effect=_record_settle)
    orch.dashboard_state.subagents = mgr
    return ledger, settled


def _ledger_ids(ledger) -> list[list[str]]:
    return [[delivery.agent_id for delivery in debts] for debts in ledger.values()]


class TestWaveDigest:
    def _capture_on_done(self, orch):
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
                mock_sm_inst = MagicMock()
                mock_sm_inst.start_reaper = MagicMock()
                mock_sm.return_value = mock_sm_inst
                orch._init_subagents()
                orch.subagent_mgr = mock_sm_inst
                # Default: no nested spawns outstanding, so the wave-close
                # digest takes its normal "run is complete" wording. Tests that
                # exercise the nested-work path override this explicitly.
                mock_sm_inst.wave_has_live_nested_spawns = MagicMock(return_value=False)
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
        info.elapsed = 10.0 + i
        info.credits = 0.25 + i
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
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        total = 12  # chunk size 10 -> chunks of 10 + 2
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text, *, _directive_user_origin, **_kw):
            assert _directive_user_origin is False
            injected.append(text)

        with (
            patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat),
            patch("kiro_crew.subagent_persistence.mark_delivered"),
        ):
            for i in range(total):
                # running_agents_for: members still pending until the last one
                mgr.batch_members_pending = MagicMock(return_value=i != total - 1)
                err = "boom" if i == 2 else ""
                await on_done(self._member(i, total, error=err))
                await asyncio.sleep(0)
            await _settle(lambda: len(injected) >= 2, what="both digest chunks injected")

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
        assert "Usage: 2.25 credits · 12s" in first
        assert "0.25 credits · 10s" in first
        # Chunk 2 (final): summary counts + release guidance, and ONLY the
        # remaining members' lines (chunk buffers reset between flushes).
        assert "Batch results 2/2" in final
        assert "11 ✅" in final and "1 ❌" in final and "of 12 agents" in final
        assert "before spawning any follow-up" in final
        assert "/tmp/w10/result.txt" in final and "/tmp/w11/result.txt" in final
        assert "/tmp/w0/result.txt" not in final  # already delivered in chunk 1

    @pytest.mark.asyncio
    async def test_wave_digest_text_carries_member_model_provenance(self):
        """The per-member SERVED model must be visible in the
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
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        total = 2
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text, *, _directive_user_origin, **_kw):
            injected.append(text)

        with (
            patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat),
            patch("kiro_crew.subagent_persistence.mark_delivered"),
        ):
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
            await _settle(lambda: len(injected) >= 1, what="the wave digest injected")

        body = "\n".join(injected)
        # Each member shows its SERVED id inline.
        assert "model claude-opus-4.8" in body
        assert "model claude-opus-4.7" in body
        # The "(requested …)" downgrade qualifier is never printed.
        assert "requested" not in body

    @pytest.mark.asyncio
    async def test_wave_digest_flags_a_completed_partial_member(self):
        """A member kept after a generate failure is completed but partial; its ok line says so."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        slot = MagicMock()
        slot.mode = "chat"
        slot.running = False
        slot.task = None
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text, *, _directive_user_origin, **_kw):
            injected.append(text)

        with (
            patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat),
            patch("kiro_crew.subagent_persistence.mark_delivered"),
        ):
            m0, m1 = self._member(0, 2), self._member(1, 2)
            m0.partial = True
            mgr.batch_members_pending = MagicMock(return_value=True)
            await on_done(m0)
            await asyncio.sleep(0)
            mgr.batch_members_pending = MagicMock(return_value=False)
            await on_done(m1)
            await asyncio.sleep(0)
            await _settle(lambda: len(injected) >= 1, what="the wave digest injected")

        lines = "\n".join(injected).splitlines()
        tag = "✅ (partial: backend failed to generate the final response)"
        assert any(f"`{m0.id}` {tag}" in line for line in lines)
        assert not any(f"`{m1.id}`" in line and "partial" in line for line in lines)

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
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text, *, _directive_user_origin, **_kw):
            injected.append(text)

        with (
            patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat),
            patch("kiro_crew.subagent_persistence.mark_delivered"),
        ):
            m0 = self._member(0, 1)
            m0.requested_model = "auto"  # a pin, but nothing served
            m0.resolved_model = ""
            mgr.batch_members_pending = MagicMock(return_value=False)
            await on_done(m0)
            await asyncio.sleep(0)
            await _settle(lambda: len(injected) >= 1, what="the wave digest injected")

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
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text, *, _directive_user_origin, **_kw):
            injected.append(text)

        secret = "AKIAIOSFODNN7EXAMPLE"
        with (
            patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat),
            patch("kiro_crew.subagent_persistence.mark_delivered"),
        ):
            m0 = self._member(0, 1)
            m0.resolved_model = secret
            mgr.batch_members_pending = MagicMock(return_value=False)
            await on_done(m0)
            await asyncio.sleep(0)
            await _settle(lambda: len(injected) >= 1, what="the wave digest injected")

        body = "\n".join(injected)
        # The raw credential-shaped value must not appear verbatim in the
        # broadcast digest text.
        assert secret not in body

    @pytest.mark.asyncio
    async def test_wave_close_digest_does_not_claim_completion_with_live_nested_spawn(self):
        """Pins the wave-close digest wording when a wave's direct members all
        report done (so ``batch_members_pending`` is False and the digest
        fires) while a member's nested spawn is still running.

        The digest must not assert "This run is complete" / "All results
        delivered" — that nested spawn has its own uncounted ``batch_id``, so
        the wave cannot substantiate a whole-run completion. It reports the
        true direct-member tally and states that the nested work reports
        separately.
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
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        total = 2
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text, *, _directive_user_origin, **_kw):
            injected.append(text)

        with (
            patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat),
            patch("kiro_crew.subagent_persistence.mark_delivered"),
        ):
            # Both direct members complete; the count says the wave is done...
            mgr.batch_members_pending = MagicMock(return_value=False)
            # ...but a member's nested spawn is still running.
            mgr.wave_has_live_nested_spawns = MagicMock(return_value=True)
            await on_done(self._member(0, total))
            await asyncio.sleep(0)
            await on_done(self._member(1, total))
            await asyncio.sleep(0)
            await _settle(lambda: len(injected) >= 1, what="the wave-close digest injected")
            await _drain_injections(orch)

        body = "\n".join(injected)
        # The digest fired (final chunk delivered)...
        assert body, "wave-close digest was never injected"
        # ...but it must NOT overclaim completion of the whole run.
        assert "This run is complete" not in body
        assert "All results delivered" not in body
        # It must still report the true direct-member tally and flag the
        # outstanding nested work.
        assert "2 sub-agents finished" in body
        assert "nested work" in body
        # wave_has_live_nested_spawns was consulted for THIS wave's batch id.
        mgr.wave_has_live_nested_spawns.assert_called_with("bigwave")

    @pytest.mark.asyncio
    async def test_wave_close_digest_claims_completion_when_no_nested_spawn(self):
        """The honest fix must NOT degrade the common case: with no outstanding
        nested work the wave-close digest keeps its normal "run is complete /
        all results delivered" wording."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        slot = MagicMock()
        slot.mode = "chat"
        slot.running = False
        slot.task = None
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        total = 2
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text, *, _directive_user_origin, **_kw):
            injected.append(text)

        with (
            patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat),
            patch("kiro_crew.subagent_persistence.mark_delivered"),
        ):
            mgr.batch_members_pending = MagicMock(return_value=False)
            mgr.wave_has_live_nested_spawns = MagicMock(return_value=False)
            await on_done(self._member(0, total))
            await asyncio.sleep(0)
            await on_done(self._member(1, total))
            await asyncio.sleep(0)
            await _settle(lambda: len(injected) >= 1, what="the wave-close digest injected")
            await _drain_injections(orch)

        body = "\n".join(injected)
        assert "wave finished" in body
        assert "This run is complete" in body
        assert "All results delivered" in body
        assert "never started" not in body

    @pytest.mark.asyncio
    @pytest.mark.parametrize("nested_live", [False, True])
    async def test_wave_close_digest_names_members_that_never_started(self, nested_live):
        """A member ended "never started" is a failure the parent must
        resubmit, not done work. The wave-close line says how many of the
        failures a memory wait ended, after the tally sentence the card parsers
        anchor on. A member that failed after it ran is not counted, and
        neither is a refusal, which resubmitting would only repeat."""
        import re

        from kiro_crew.subagent_wait_reasons import QUEUED_WAIT_EXPIRED_TEXT

        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        slot = MagicMock()
        slot.mode = "chat"
        slot.running = False
        slot.task = None
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        total = 4
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text, *, _directive_user_origin, **_kw):
            injected.append(text)

        ran = self._member(0, total)
        failed_after_running = self._member(1, total, error="tool crashed")
        failed_after_running._exec_started = 1.0
        never_started = self._member(2, total, error=QUEUED_WAIT_EXPIRED_TEXT)
        never_started.result = ""
        never_started.result_path = ""
        refused = self._member(3, total, error="spawn refused: approval denied")
        refused.result = ""
        refused.result_path = ""
        with (
            patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat),
            patch("kiro_crew.subagent_persistence.mark_delivered"),
        ):
            # Outstanding until the last member reports, which closes the wave
            # by its own count.
            mgr.batch_members_pending = MagicMock(return_value=True)
            mgr.wave_has_live_nested_spawns = MagicMock(return_value=nested_live)
            for member in (ran, failed_after_running, never_started, refused):
                await on_done(member)
                await asyncio.sleep(0)
            await _settle(lambda: len(injected) >= 1, what="the wave-close digest injected")
            await _drain_injections(orch)

        body = "\n".join(injected)
        assert "1 ✅ · 3 ❌ · 0 ⏹" in body
        assert "1 of the ❌ never started" in body
        # A member the spent episode ended is counted too, so the advice waits
        # for memory to ease rather than resubmitting into the same episode.
        assert "resubmit them once memory eases" in body
        if not nested_live:
            # The card's own parser still reads the tally line.
            assert re.search(
                r"^Batch results \d+/\d+ — wave finished: 1 ✅ · 3 ❌ · 0 ⏹ of 4 agents\.",
                body,
                re.M,
            )

    @pytest.mark.asyncio
    async def test_digest_chunks_inject_in_fifo_order_despite_delayed_dispatch_hop(self):
        """A later digest chunk must never overtake an earlier one whose
        dispatched injection is still inside ``bounded_chat_turn``'s off-loop
        timeout resolution. The first chunk's hop is held
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
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        total = 12  # chunk size 10 -> chunk 1/2 at member 10, final 2/2 on close
        injected: list[str] = []
        injected_event = threading.Event()

        async def _fake_run_chat(_state, _slot, text, *, _directive_user_origin, **_kw):
            assert _directive_user_origin is False
            injected.append(text)
            injected_event.set()

        hop_calls: list[int] = []

        def _held_resolver() -> float:
            # Runs inside asyncio.to_thread, standing in for the config read
            # bounded_chat_turn resolves off-loop. Thread-side bounded poll
            # (not a timing guess): exits the instant an overtaking injection
            # is observed, and the deadline only pays out on the fixed path,
            # where the later chunk is parked and can never land here.
            hop_calls.append(1)
            if len(hop_calls) == 1:
                injected_event.wait(timeout=2.0)
            return 60.0

        with (
            patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat),
            patch("kiro_crew.subagent_persistence.mark_delivered"),
            patch(
                "kiro_crew.dashboard.turn_dispatch.chat_turn_timeout_secs",
                _held_resolver,
            ),
        ):
            for i in range(total):
                mgr.batch_members_pending = MagicMock(return_value=i != total - 1)
                await on_done(self._member(i, total))
                await asyncio.sleep(0)
            await _settle(lambda: len(injected) >= 2, what="both digest chunks injected")

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
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        total = 12
        with patch("kiro_crew.slack.gateway._run_chat", new_callable=AsyncMock):
            for i in range(total):
                mgr.batch_members_pending = MagicMock(return_value=i != total - 1)
                await on_done(self._member(i, total, error="boom" if i < 2 else ""))
                await asyncio.sleep(0)
            await _drain_injections(orch)
        finished = [
            c
            for c in orch.dashboard_state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "batch_finished"
        ]
        assert len(finished) == 1
        payload = finished[0].args[1]
        assert payload["total"] == 12 and payload["ok"] == 10
        assert payload["err"] == 2 and payload["stopped"] == 0

    @pytest.mark.asyncio
    async def test_held_members_marked_delivered_only_at_digest(self):
        """Restart safety: held members
        are flagged ``_digest_held`` (the run loop skips its own
        mark_delivered — the result is NOT in the parent's context yet and a
        delivered tombstone would hide it from orphan reconciliation after a
        restart). The gateway must NOT settle them at chunk COMPOSITION
        either (routing could still fail); instead it stashes each chunk's
        held OK deliveries on that chunk's FLUSHING member
        (``_digest_settle_deliveries``)
        and settlement waits for the route that owns the hand-off: the
        dashboard route below detaches the ids when the injection turn is
        launched and owes them to the turn's CONSUMPTION through the slot's
        delivery ledger; for routes whose ``_on_done`` return really
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
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        ledger, settled = _wire_hold_settlement(orch, slot, mgr)
        total = 12
        members = [self._member(i, total, error="boom" if i == 2 else "") for i in range(total)]

        async def _consuming_run_chat(_state, _slot, _text, *, _on_consumed=None, **_kw):
            # The model consumed the injected digest — the one condition that
            # settles this route's holds.
            if _on_consumed is not None:
                _on_consumed()

        marked: list[str] = []
        with (
            patch("kiro_crew.slack.gateway._run_chat", _consuming_run_chat),
            patch("kiro_crew.subagent_persistence.mark_delivered", side_effect=marked.append),
        ):
            for i, m in enumerate(members):
                mgr.batch_members_pending = MagicMock(return_value=i != total - 1)
                await on_done(m)
                await asyncio.sleep(0)
                # Let each flush's injection turn finish before the next member
                # reports, so both flushes take the direct (idle-slot) branch.
                await _settle(
                    lambda: slot.task is None, what="this member's injection turn finished"
                )
            # Both chunks' injection turns must report consumption before their
            # holds can settle — the settle is owed to the turn, not to the
            # `_on_done` return.
            await _settle(lambda: len(settled) >= 2, what="both chunks' holds settled")
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
        # hand-off, not a residue left on the member: the member is
        # left clean and the ids reach the manager exactly once, per chunk.
        assert members[9]._digest_settle_deliveries == []
        assert members[11]._digest_settle_deliveries == []
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
        info._digest_settle_deliveries = [
            SubagentDelivery("h1", 1.0, 0.1),
            SubagentDelivery("h2", 2.0, 0.2),
        ]
        with patch(
            "kiro_crew.subagent.mark_delivered",
            side_effect=lambda agent_id, **_: marked.append(agent_id),
        ):
            await mgr._settle_digest_holds(info)
        assert marked == ["h1", "h2"]
        assert info._digest_settle_deliveries == []  # idempotent re-entry safe
        # Structural guarantee: the settle call sits AFTER the awaited
        # _on_done inside the same try-block, so an _on_done exception
        # (routing failure / crash) skips it entirely. The terminal report
        # (subagent_done + _on_done + settle) lives in
        # _report_terminal_guarded (the body of _report_terminal past the
        # done-flip, so the in-flight hold's try/finally wraps it), which
        # `_run` runs on a shielded task.
        import inspect

        from kiro_crew.subagent_manager.terminal import TerminalCoordinator

        src = inspect.getsource(TerminalCoordinator._report_terminal_guarded_impl)
        on_done_pos = src.index("await asyncio.wait_for(self._manager._on_done(info)")
        settle_pos = src.index("await self._manager._settle_digest_holds(info)")
        assert settle_pos > on_done_pos

    @pytest.mark.asyncio
    async def test_holds_settle_only_after_the_injection_turn_confirms(self):
        """Ownership: the dashboard route hands off asynchronously, so a
        bare ``_on_done`` return is not proof the digest reached the parent.

        ``_report_terminal`` settles ``info._digest_settle_deliveries`` right after
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
        delivery ledger, the same debt shape the queue branch records.
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
        with (
            patch("kiro_crew.slack.gateway._run_chat", _gated_run_chat),
            patch("kiro_crew.subagent_persistence.mark_delivered", side_effect=marked.append),
        ):
            for i, m in enumerate(members[:10]):
                mgr.batch_members_pending = MagicMock(return_value=True)
                await on_done(m)
                await asyncio.sleep(0)

            # Preconditions: nine siblings are held, the tenth flushed chunk 1,
            # and its injection turn is RUNNING but has not consumed the digest.
            held_ids = [members[i].id for i in range(9)]
            owed = [members[9].id] + held_ids
            assert all(
                members[i]._digest_held for i in range(9)
            ), "precondition: the first nine members must be held for the chunk"
            await _settle(turn_started.is_set, what="the injection turn started")
            assert turn_started.is_set(), "precondition: the injection turn started"
            assert slot.task is not None and not slot.task.done(), (
                "precondition: the hand-off is still in flight — this is the "
                "window in which the current contract settles"
            )

            flusher = members[9]
            assert flusher._digest_settle_deliveries == [], (
                "the flushing member must not still be carrying the settle ids "
                "while the hand-off is unconfirmed: the run loop settles that "
                "list as soon as _on_done returns, which is now"
            )
            assert _ledger_ids(ledger) == [owed], (
                "the ids — the flusher's own tombstone included — are parked "
                "in the slot's delivery ledger, owed, not settled: a process "
                "death here leaves them tombstone-free and recoverable by "
                "orphan reconciliation"
            )
            assert (
                settled == [] and marked == []
            ), "nothing may be tombstoned before the hand-off lands"

            # The model consumes the digest — NOW the hand-off is confirmed.
            release_consume.set()
            await _settle(lambda: bool(settled), what="the confirmed hand-off settled its holds")

        # Settled through the manager by the side that owns the hand-off, once,
        # with exactly this chunk's held members. (``settle_queued_delivery`` is
        # mocked here; its real tombstone write and teardown gate are pinned by
        # test_subagent_delivery_ttl_anchor.py.)
        assert settled == [owed]
        assert marked == [], "and never through the run loop's settle, which this route detached"

    @pytest.mark.asyncio
    async def test_a_queued_hand_off_is_not_confirmed_until_the_turn_runs(self):
        """The same root cause one branch up.

        When the parent slot is busy the digest is appended to ``slot._queue``
        and ``_subagent_done`` returns — so the run loop would settle on that
        bare return, exactly as it did for the direct branch.

        ``slot._queue`` is a plain in-memory list (``state_parts/chat_slot.py``): the
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
        announce (one debt shape for both routes).

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
        with (
            patch("kiro_crew.slack.gateway._run_chat", new_callable=AsyncMock),
            patch("kiro_crew.subagent_persistence.mark_delivered", side_effect=marked.append),
        ):
            for i, m in enumerate(members[:10]):
                mgr.batch_members_pending = MagicMock(return_value=True)
                await on_done(m)
                await asyncio.sleep(0)

        assert (
            len(queued) == 1
        ), "precondition: the flushing chunk must have been QUEUED, not dispatched"
        # The queue is deliberately never drained — the process died here.
        assert settled == [] and marked == [], (
            "an announce sitting in an in-memory queue is not a hand-off: a "
            "shutdown here loses the digest, and a delivered tombstone would "
            "hide the held results from orphan reconciliation forever"
        )
        assert members[9]._digest_settle_deliveries == [], (
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
        assert [d.agent_id for d in ledger[queued[0]["content"]]] == [members[9].id] + held

    @pytest.mark.asyncio
    async def test_an_auth_required_turn_is_not_a_confirmed_hand_off(self):
        """The third state: the turn ended cleanly and delivered nothing.

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
        slot._subagent_deliveries_inflight = 0
        # Real attribute, not a MagicMock truthy stub: the stub below flips it
        # exactly as _run_chat does on a signed-out CLI.
        slot._last_turn_auth_required = False
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        ledger, settled = _wire_hold_settlement(orch, slot, mgr)
        total = 12
        members = [self._member(i, total) for i in range(total)]

        async def _auth_required_run_chat(_state, _slot, _text, *, _on_consumed=None, **_kw):
            # Exactly what _run_chat does on a signed-out CLI: record it and
            # return. No raise, no cancellation — and no consumption report,
            # because the model never saw the prompt.
            _slot._last_turn_auth_required = True

        marked: list[str] = []
        with (
            patch("kiro_crew.slack.gateway._run_chat", _auth_required_run_chat),
            patch("kiro_crew.subagent_persistence.mark_delivered", side_effect=marked.append),
        ):
            for i, m in enumerate(members[:10]):
                mgr.batch_members_pending = MagicMock(return_value=True)
                await on_done(m)
                await asyncio.sleep(0)
            await _settle(
                lambda: slot.task is None, what="the auth-required injection turn finished"
            )

        assert (
            slot._last_turn_auth_required is True
        ), "precondition: the turn must have ended in the auth-required state"
        assert settled == [] and marked == [], (
            "a signed-out CLI never received the digest — the held siblings' "
            "results are still only on disk"
        )
        assert members[9]._digest_settle_deliveries == [], (
            "and the run loop must not settle them either: the ids left the "
            "flushing member when the turn was launched"
        )
        held = [members[i].id for i in range(9)]
        assert _ledger_ids(ledger) == [[members[9].id] + held], (
            "the debt — the flusher's own tombstone included — stays owed, "
            "tombstone-free and recoverable, rather than settled on a clean "
            "return that delivered nothing"
        )

    @pytest.mark.asyncio
    async def test_a_failed_injection_turn_leaves_holds_recoverable(self):
        """The deliberate asymmetry: an unconfirmed hand-off must leave
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
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        ledger, settled = _wire_hold_settlement(orch, slot, mgr)
        total = 12
        members = [self._member(i, total) for i in range(total)]

        async def _failing_run_chat(_state, _slot, _text, *, _on_consumed=None, **_kw):
            raise RuntimeError("injection turn died")

        marked: list[str] = []
        with (
            patch("kiro_crew.slack.gateway._run_chat", _failing_run_chat),
            patch("kiro_crew.subagent_persistence.mark_delivered", side_effect=marked.append),
        ):
            for i, m in enumerate(members[:10]):
                mgr.batch_members_pending = MagicMock(return_value=True)
                await on_done(m)
                await asyncio.sleep(0)
            await _settle(lambda: slot.task is None, what="the failing injection turn finished")

        assert settled == [] and marked == [], (
            "a failed hand-off must not tombstone the held siblings — their "
            "results are still only on disk"
        )
        assert members[9]._digest_settle_deliveries == [], (
            "and the run loop must not settle them either: the ids left the "
            "flushing member when the turn was launched"
        )
        held = [members[i].id for i in range(9)]
        assert _ledger_ids(ledger) == [
            [members[9].id] + held
        ], "the debt stays parked for a recovery replay to claim"

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
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        total = 3
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text, *, _directive_user_origin, **_kw):
            assert _directive_user_origin is False
            injected.append(text)

        with (
            patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat),
            patch("kiro_crew.subagent_persistence.mark_delivered"),
        ):
            for i in range(total):
                mgr.batch_members_pending = MagicMock(return_value=i != total - 1)
                await on_done(self._member(i, total))
                await asyncio.sleep(0)
            await _settle(lambda: len(injected) >= 1, what="the single-chunk digest injected")
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
        slot._subagent_deliveries_inflight = 0
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = self._capture_on_done(orch)
        mgr.running_agents_for = MagicMock(return_value=[])
        injected: list[str] = []

        async def _fake_run_chat(_state, _slot, text, *, _directive_user_origin, **_kw):
            assert _directive_user_origin is False
            injected.append(text)

        solo = SubagentInfo(
            id="solo",
            task="one-off task",
            parent_session_key="dashboard:main",
        )
        solo.done = True
        solo.result = "solo result"
        solo.elapsed = 12.0
        solo.credits = 0.25
        with patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat):
            await on_done(solo)
            await _settle(lambda: len(injected) >= 1, what="the per-agent completion injected")
        assert len(injected) == 1
        assert injected[0].startswith("[Subagent completion event]")
        assert "Usage: 0.25 credits · 12s" in injected[0]
        assert "Batch results" not in injected[0]


# ── 4b. Hold deadline (straggler escape hatch) ──────────


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
        from kiro_crew.subagent import DIGEST_HOLD_SECS

        mgr = self._mgr()
        now = time.time()
        m = self._held_member(0)
        m._digest_held_at = now - 5.0
        mgr._agents["h0"] = m
        with patch.object(mgr, "force_digest_flush") as forced:
            mgr._sweep_digest_holds(now)
            forced.assert_not_called()
            # Control: the same hold is flushed once it is past the deadline.
            mgr._sweep_digest_holds(m._digest_held_at + DIGEST_HOLD_SECS + 1.0)
        forced.assert_called_once()

    def test_expired_hold_forces_flush(self):
        """THE BUG: two members finished, the third is still running, so
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

    def test_deadline_zero_disables_sweep(self):
        """``KIROCREW_SUBAGENT_DIGEST_HOLD_SECS=0`` is the documented opt-out
        back to count-trigger-only behavior."""
        mgr = self._mgr()
        now = time.time()
        m = self._held_member(0)
        m._digest_held_at = now - 100_000.0
        mgr._agents["h0"] = m
        with patch.object(mgr, "force_digest_flush") as forced:
            with patch("kiro_crew.subagent.DIGEST_HOLD_SECS", 0.0):
                mgr._sweep_digest_holds(now)
            forced.assert_not_called()
            # Control: with the deadline back on, the same aged hold is flushed.
            mgr._sweep_digest_holds(now)
        forced.assert_called_once()

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
            # Control: a running hold clock on the same member does trip the sweep.
            m._digest_held_at = now
            mgr._sweep_digest_holds(now + DIGEST_HOLD_SECS * 10)
        forced.assert_called_once()

    def test_force_digest_flush_builds_flush_only_record(self):
        mgr = self._mgr()
        announced: list[SubagentInfo] = []

        async def _cap(info):
            announced.append(info)

        mgr._on_done = _cap
        mgr.force_digest_flush("wv", "dashboard:main", 3, 200.0)
        assert mgr._tasks  # scheduled
        asyncio.get_event_loop().run_until_complete(asyncio.gather(*mgr._tasks.values()))
        (rec,) = announced
        assert rec._digest_flush_only is True
        assert rec.batch_id == "wv" and rec.batch_total == 3
        assert rec.done is True and rec.error == ""
        assert "200s" in rec.task

    @pytest.mark.asyncio
    async def test_force_digest_flush_announce_leaves_task_registry(self):
        """A finished flush announce leaves ``_tasks``, so a long-lived gateway
        does not keep one finished task per forced flush."""
        mgr = self._mgr()
        with patch("kiro_crew.subagent.sel"):
            for i in range(3):
                mgr.force_digest_flush(f"wv{i}", "dashboard:main", 3, 200.0)
        tasks = list(mgr._tasks.values())
        assert len(tasks) == 3  # registered while pending, so shutdown can cancel them
        await asyncio.wait_for(asyncio.gather(*tasks), _ANNOUNCE_WAIT_SECS)
        await asyncio.sleep(0)  # done callbacks run on the next loop pass
        assert mgr._on_done.await_count == 3
        assert mgr._tasks == {}

    @pytest.mark.asyncio
    async def test_cancel_all_still_cancels_a_pending_flush_announce(self):
        """The registration is what lets shutdown cancel and await a flush
        announce that is still in flight; dropping finished entries must not
        take that away from a pending one."""
        mgr = self._mgr()
        release = asyncio.Event()

        async def _blocked(info):
            await asyncio.wait_for(release.wait(), _ANNOUNCE_WAIT_SECS)

        mgr._on_done = _blocked
        with patch("kiro_crew.subagent.sel"):
            mgr.force_digest_flush("wv", "dashboard:main", 3, 200.0)
        (task,) = mgr._tasks.values()
        await asyncio.sleep(0)
        assert not task.done()  # the announce is in flight
        await asyncio.wait_for(mgr.cancel_all(), _ANNOUNCE_WAIT_SECS)
        assert task.cancelled()
        assert mgr._tasks == {}

    @pytest.mark.asyncio
    async def test_flush_only_settles_holds_only_after_on_done(self):
        """Same restart-safety contract as ``_run``: a routing failure must
        leave held members undelivered so orphan reconciliation can recover
        them. The flush-only path has no run loop, so it enforces it itself."""
        mgr = self._mgr()
        info = SubagentInfo(id="flush", task="t", batch_id="wv")
        info._digest_flush_only = True
        info._digest_settle_deliveries = [
            SubagentDelivery("h0", 1.0, 0.1),
            SubagentDelivery("h1", 2.0, 0.2),
        ]

        marked: list[str] = []
        mgr._on_done = AsyncMock(side_effect=RuntimeError("routing blew up"))
        with patch(
            "kiro_crew.subagent.mark_delivered",
            side_effect=lambda agent_id, **_: marked.append(agent_id),
        ):
            await mgr._announce_digest_flush(info)
        assert marked == []  # failure → nothing tombstoned
        assert [d.agent_id for d in info._digest_settle_deliveries] == ["h0", "h1"]

        mgr._on_done = AsyncMock()
        with patch(
            "kiro_crew.subagent.mark_delivered",
            side_effect=lambda agent_id, **_: marked.append(agent_id),
        ):
            await mgr._announce_digest_flush(info)
        assert marked == ["h0", "h1"]

    @pytest.mark.asyncio
    async def test_straggler_wave_delivers_partial_digest_end_to_end(self):
        """REPRO end to end through the real sweep.

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
                id=f"e{i}",
                task=f"task {i}",
                parent_session_key="dashboard:main",
                batch_id="e2e",
                batch_total=3,
            )
            m.done = True
            m.result = f"result {i}"
            m.result_path = f"/tmp/e{i}/result.txt"
            finished.append(m)
            real._agents[m.id] = m

        with (
            patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat),
            patch("kiro_crew.subagent_persistence.mark_delivered"),
            patch("kiro_crew.subagent.mark_delivered"),
        ):
            for m in finished:
                await on_done(m)
                await asyncio.sleep(0)
            # Held: this is the reported symptom — two complete results on disk,
            # zero signal to the parent.
            assert injected == []

            # Advance past the hold deadline and run the sweep the reaper runs.
            # getattr keeps the failure BEHAVIORAL on unfixed code (no injection)
            # instead of an AttributeError.
            hold = getattr(
                __import__("kiro_crew.subagent", fromlist=["DIGEST_HOLD_SECS"]),
                "DIGEST_HOLD_SECS",
                120.0,
            )
            sweep = getattr(real, "_sweep_digest_holds", lambda _now: None)
            sweep(time.time() + hold + 5)
            await _settle(lambda: len(injected) >= 1, what="the partial digest injected")

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
        slot._subagent_deliveries_inflight = 0
        slot._subagents_inline_collected = set()
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        mgr, on_done = TestWaveDigest()._capture_on_done(orch)
        ledger, settled = _wire_hold_settlement(orch, slot, mgr)
        injected: list[str] = []

        async def _fake_run_chat(
            _state, _slot, text, *, _directive_user_origin, _on_consumed=None, **_kw
        ):
            assert _directive_user_origin is False
            injected.append(text)
            # The model consumed the flushed digest — the condition that
            # settles the held siblings on this route.
            if _on_consumed is not None:
                _on_consumed()

        members = [
            SubagentInfo(
                id=f"s{i}",
                task=f"task {i}",
                parent_session_key="dashboard:main",
                batch_id="strag",
                batch_total=3,
            )
            for i in range(2)
        ]
        for i, m in enumerate(members):
            m.done = True
            m.result = f"result {i}"
            m.result_path = f"/tmp/s{i}/result.txt"

        with (
            patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat),
            patch("kiro_crew.subagent_persistence.mark_delivered"),
        ):
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
                id="ff",
                task="(wave digest flush — results held 200s)",
                parent_session_key="dashboard:main",
                batch_id="strag",
                batch_total=3,
            )
            flush.done = True
            flush._digest_flush_only = True
            await on_done(flush)
            await _settle(lambda: len(injected) >= 1, what="the flush-only digest injected")

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
        # slot's delivery ledger. The forced hold-deadline flush is
        # one of the settle callers, so it inherits the same ownership rule
        # without a second code path.
        assert flush._digest_settle_deliveries == []
        await _settle(lambda: bool(settled), what="the flushed holds settled")
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
            id="ff",
            task="(wave digest flush — results held 200s)",
            parent_session_key="dashboard:main",
            batch_id="gone",
            batch_total=3,
        )
        flush.done = True
        flush._digest_flush_only = True
        with patch("kiro_crew.slack.gateway._run_chat", side_effect=_fake_run_chat):
            await on_done(flush)
            await asyncio.sleep(0.05)
        assert injected == []
        # And no phantom "agent completed" notification for the synthetic record.
        orch.dashboard_state.notify.assert_not_called()

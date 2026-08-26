"""Task-runner compaction routes through the shared SessionManager path (#4686).

Pins the four contract points from the issue:

(a) ``check_context`` no longer calls ``provider.compact()`` directly — it
    delegates to :meth:`SessionManager.compact_if_needed`;
(b) a second concurrent trigger on the same key is collapsed by the
    ``_compacting`` dedup (across the awaited seam AND the fire-and-forget
    gateway trigger);
(c) the reinjection flag is set after a task-runner compaction;
(d) a busy/declined compaction neither compacts nor resets — no direct-compact
    fallback.

Plus the promoted post-compaction verification: an ineffective-and-still-
critical SETTLED verdict escalates to a reset (awaited on the seam, scheduled
on the sync turn-end path), while unmeasurable readings — unknown, or stale
showing no drop — defer instead of destroying a healthy session.

Full gate-ladder parity between the two entry points (#5132) is pinned by
``TestGateLadderParity``: both consume
``SessionManager._compaction_gate_decision`` — the single owner of the gate
order — so a gate added to one path only cannot silently diverge again.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import textwrap
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager
from kiro_crew.task_executor import check_context

KEY = "taskrunner:t1:runtime"


@pytest.fixture
def cfg():
    c = KiroCrewConfig()
    c.session.timeout_secs = 2
    return c


async def _drain_background(mgr: SessionManager) -> None:
    """Await the manager's scheduled background tasks (e.g. a critical reset)."""
    pending = [t for t in mgr._background_tasks if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


@contextlib.asynccontextmanager
async def _managed(cfg, provider_factory):
    """Manager whose teardown always runs, even on a failed assert.

    An async CONTEXT MANAGER rather than an ``@pytest_asyncio.fixture``: the
    CI-pinned ``pytest-asyncio==0.20.3`` is incompatible with pytest 8 for
    async fixtures, so the suite avoids the decorator by convention (see
    test_trusted_apps_api.py's module docstring).
    """
    mgr = SessionManager(cfg, provider_factory=provider_factory)
    try:
        yield mgr
    finally:
        await _drain_background(mgr)
        await mgr.close_all()


def _compacting_provider_factory(
    *,
    pct_before: float = 92.0,
    pct_after: float = 0.0,
    unknown_before: bool = False,
    unknown_after: bool = True,
    result: dict | None = None,
    gate: asyncio.Event | None = None,
):
    """Provider at *pct_before* whose in-place ``/compact`` completes and then
    reports *pct_after*.

    Defaults mirror kiro-cli's normal completed path: the terminal status
    arrives async via ``wait_for_compaction`` and ``reset_after_compaction``
    zeroes the stats and flags them unknown (``unknown_after=True``). *gate*,
    when given, holds the compaction in flight until the test releases it.
    """

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.is_process_alive = lambda: True
        m.has_active_turn = lambda: False
        state = {"compacted": False}

        m.context_usage_pct = lambda: pct_after if state["compacted"] else pct_before
        m.context_usage_unknown = lambda: (unknown_after if state["compacted"] else unknown_before)

        async def _stream(_cmd):
            for ev in []:
                yield ev  # pragma: no cover - empty stream, status arrives async

        m.stream_command = MagicMock(side_effect=_stream)

        async def _wait(timeout=None):
            if gate is not None:
                await gate.wait()
            state["compacted"] = True
            return dict(result or {"type": "completed"})

        m.wait_for_compaction = AsyncMock(side_effect=_wait)
        return m

    return factory


class TestCheckContextRoutesThroughManager:
    """(a) + (c): the task runner uses the shared path, not the provider."""

    @pytest.mark.asyncio
    async def test_check_context_delegates_to_compact_if_needed(self, cfg):
        """check_context awaits the public seam and never touches the provider
        pair (context_usage_pct / compact) it used to call directly."""
        async with _managed(cfg, _compacting_provider_factory()) as mgr:
            provider, _, _ = await mgr.get_or_create(KEY)
            mgr.release(KEY)

            with patch.object(mgr, "compact_if_needed", AsyncMock(return_value="ok")) as seam:
                await check_context(KEY, mgr)

            seam.assert_awaited_once_with(KEY)
            provider.compact.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_check_context_compacts_in_place_and_marks_reinjection(self, cfg):
        """End to end through the REAL seam: the shared in-place path runs
        (never a direct provider.compact()), the session survives, and the
        skills-reinjection flag is set — the guard the direct call lost."""
        async with _managed(cfg, _compacting_provider_factory()) as mgr:
            provider, _, _ = await mgr.get_or_create(KEY)
            mgr.release(KEY)

            await check_context(KEY, mgr)

            provider.compact.assert_not_awaited()
            provider.stream_command.assert_called_once_with("/compact")
            assert KEY in mgr._sessions, "in-place compaction keeps the session"
            assert mgr.consume_needs_reinjection(KEY) is True

    @pytest.mark.asyncio
    async def test_check_context_swallows_seam_errors(self, cfg):
        """The pre-turn check stays best-effort: a seam failure must not
        abort the task attempt."""
        async with _managed(cfg, _compacting_provider_factory()) as mgr:
            await mgr.get_or_create(KEY)
            mgr.release(KEY)

            with patch.object(
                mgr, "compact_if_needed", AsyncMock(side_effect=RuntimeError("boom"))
            ):
                await check_context(KEY, mgr)  # must not raise


class TestCompactIfNeededGates:
    """The awaited seam applies the same gates as the fire-and-forget path."""

    @pytest.mark.asyncio
    async def test_absent_session_is_a_noop(self, cfg):
        async with _managed(cfg, _compacting_provider_factory()) as mgr:
            assert await mgr.compact_if_needed("never-existed") == "absent"

    @pytest.mark.asyncio
    async def test_below_threshold_does_not_compact(self, cfg):
        async with _managed(cfg, _compacting_provider_factory(pct_before=10.0)) as mgr:
            provider, _, _ = await mgr.get_or_create(KEY)
            mgr.release(KEY)

            assert await mgr.compact_if_needed(KEY) == "below_threshold"
            provider.stream_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_unconfirmed_pct_does_not_compact(self, cfg):
        """Same defensive gate as check_context_usage: never compact on a
        percentage no telemetry has confirmed for the current binding."""
        async with _managed(cfg, _compacting_provider_factory(unknown_before=True)) as mgr:
            provider, _, _ = await mgr.get_or_create(KEY)
            mgr.release(KEY)

            assert await mgr.compact_if_needed(KEY) == "unconfirmed"
            provider.stream_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_cooldown_skips_compaction(self, cfg):
        async with _managed(cfg, _compacting_provider_factory()) as mgr:
            provider, _, _ = await mgr.get_or_create(KEY)
            mgr.release(KEY)
            mgr._compact_cooldown_until[KEY] = time.monotonic() + 999

            assert await mgr.compact_if_needed(KEY) == "cooldown"
            provider.stream_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_seam_settles_pending_verdict_before_deciding(self, cfg):
        """An ineffective (but not critical) prior attempt arms its cooldown
        through the seam's confirmed reading — mirroring check_context_usage —
        and the very same call then honors that cooldown."""
        async with _managed(cfg, _compacting_provider_factory(pct_before=92.0)) as mgr:
            provider, _, _ = await mgr.get_or_create(KEY)
            mgr.release(KEY)
            # Prior attempt triggered at 92%, verdict deferred; current
            # confirmed reading is still 92% → freed 0 points → ineffective.
            mgr._compact_pending_verdict[KEY] = 92.0

            assert await mgr.compact_if_needed(KEY) == "cooldown"
            assert KEY not in mgr._compact_pending_verdict
            assert mgr._compact_cooldown_until.get(KEY, 0.0) > time.monotonic()
            provider.stream_command.assert_not_called()
            assert KEY in mgr._sessions, "92% is below the critical line — no reset"

    @pytest.mark.asyncio
    async def test_busy_decline_neither_compacts_nor_resets(self, cfg, monkeypatch):
        """(d) A turn holds the semaphore: the attempt is declined, nothing is
        compacted, nothing is reset, and there is no direct-compact fallback."""
        monkeypatch.setattr("kiro_crew.session.COMPACT_WAIT_TIMEOUT_SECS", 0.05)
        async with _managed(cfg, _compacting_provider_factory()) as mgr:
            provider, _, _ = await mgr.get_or_create(KEY)
            # Deliberately NOT released: get_or_create holds the turn semaphore.

            assert await mgr.compact_if_needed(KEY) == "busy"

            provider.stream_command.assert_not_called()
            provider.compact.assert_not_awaited()
            provider.shutdown.assert_not_awaited()
            assert mgr._sessions[KEY].provider is provider, "session left alone"
            assert KEY not in mgr._compacting, "dedup entry released for the retry"
            mgr.release(KEY)


class TestConcurrentTriggerDedup:
    """(b) Concurrent triggers on one key collapse to a single attempt."""

    @pytest.mark.asyncio
    async def test_second_concurrent_trigger_is_collapsed(self, cfg):
        gate = asyncio.Event()
        async with _managed(cfg, _compacting_provider_factory(gate=gate)) as mgr:
            provider, _, _ = await mgr.get_or_create(KEY)
            mgr.release(KEY)

            first = asyncio.create_task(mgr.compact_if_needed(KEY))
            for _ in range(100):
                if KEY in mgr._compacting:
                    break
                await asyncio.sleep(0.01)
            assert KEY in mgr._compacting, "first attempt is in flight"

            # Second awaited trigger collapses...
            assert await mgr.compact_if_needed(KEY) == "in_progress"
            # ...and so does the gateway's fire-and-forget trigger.
            assert mgr._trigger_compaction(KEY, "context at 92%", 92.0, provider) == "in_progress"

            gate.set()
            assert await first == "ok"
            provider.stream_command.assert_called_once_with("/compact")


class TestPostCompactionResetPromotion:
    """The task runner's ``new_pct >= 95`` verification now lives in the
    shared path, escalating at the SETTLED verdict so every caller gets it
    without ever judging an unmeasurable reading."""

    @pytest.mark.asyncio
    async def test_deferred_confirmed_critical_damps_but_never_resets(self, cfg):
        """kiro's ordinary path: compact completes at 96% but the fresh
        reading is unknown → verdict deferred, session survives. The NEXT
        seam call sees a confirmed still-96% reading — but that reading
        includes the following turn's own growth, so it cannot prove the
        compaction failed: it arms the cooldown (damping) and must NEVER
        reset or clear the resume sid of what may be a valid conversation."""
        factory = _compacting_provider_factory(pct_before=96.0, pct_after=0.0, unknown_after=True)
        async with _managed(cfg, factory) as mgr:
            provider, _, _ = await mgr.get_or_create(KEY)
            mgr.release(KEY)

            assert await mgr.compact_if_needed(KEY) == "ok"
            assert KEY in mgr._sessions, "unknown post-reading must not reset"
            assert mgr._compact_pending_verdict[KEY] == 96.0

            # Telemetry confirms: still 96% — ambiguous (failed compaction OR
            # regrown by the next turn). Damp, never destroy.
            provider.context_usage_pct = lambda: 96.0
            provider.context_usage_unknown = lambda: False

            with patch.object(mgr._session_map, "clear_sid") as clear_sid:
                assert await mgr.compact_if_needed(KEY) == "cooldown"

            provider.shutdown.assert_not_awaited()
            assert KEY in mgr._sessions, "deferred verdict never resets"
            clear_sid.assert_not_called()
            assert mgr._compact_cooldown_until.get(KEY, 0.0) > time.monotonic()

    @pytest.mark.asyncio
    async def test_stale_confirmed_reading_defers_and_spares_healthy_session(self, cfg):
        """A backend whose stats are never reset by the compaction reads back
        the PRE-compaction value, stale but confirmed-looking. Judging it
        would reset a session whose compaction in fact succeeded — the settle
        defers instead, and the next genuinely fresh reading clears cleanly."""
        factory = _compacting_provider_factory(pct_before=96.0, pct_after=96.0, unknown_after=False)
        async with _managed(cfg, factory) as mgr:
            provider, _, _ = await mgr.get_or_create(KEY)
            mgr.release(KEY)

            assert await mgr.compact_if_needed(KEY) == "ok"

            provider.shutdown.assert_not_awaited()
            assert KEY in mgr._sessions, "stale reading must not destroy the session"
            assert mgr._compact_pending_verdict[KEY] == 96.0, "verdict deferred"

            # Next turn's telemetry shows the real drop — verdict settles clean.
            provider.context_usage_pct = lambda: 40.0
            assert await mgr.compact_if_needed(KEY) == "below_threshold"
            assert KEY not in mgr._compact_pending_verdict
            assert KEY not in mgr._compact_cooldown_until
            assert KEY in mgr._sessions

    @pytest.mark.asyncio
    async def test_measured_but_insufficient_drop_at_critical_escalates(self, cfg):
        """100% -> 96% confirmed right after the compact: a genuinely
        measurable drop that is both ineffective (< 5 points) and still
        critical — the escalation is AWAITED through the attempt itself
        (outcome "reset"), so the caller's next turn cannot race the
        recovery, and the resume sid is cleared with the pop."""
        factory = _compacting_provider_factory(
            pct_before=100.0, pct_after=96.0, unknown_after=False
        )
        async with _managed(cfg, factory) as mgr:
            provider, _, _ = await mgr.get_or_create(KEY)
            mgr.release(KEY)

            with patch.object(mgr._session_map, "clear_sid") as clear_sid:
                assert await mgr.compact_if_needed(KEY) == "reset"

            provider.shutdown.assert_awaited_once()
            assert KEY not in mgr._sessions
            clear_sid.assert_called_once_with(KEY)

    @pytest.mark.asyncio
    async def test_effective_drop_below_critical_does_not_reset(self, cfg):
        factory = _compacting_provider_factory(pct_before=92.0, pct_after=40.0, unknown_after=False)
        async with _managed(cfg, factory) as mgr:
            provider, _, _ = await mgr.get_or_create(KEY)
            mgr.release(KEY)

            assert await mgr.compact_if_needed(KEY) == "ok"
            await _drain_background(mgr)

            provider.shutdown.assert_not_awaited()
            assert KEY in mgr._sessions
            assert KEY not in mgr._compact_cooldown_until, "effective compact clears cooldown"

    @pytest.mark.asyncio
    async def test_gateway_turn_end_settle_damps_but_never_resets(self, cfg):
        """The sync turn-end settle consumes the deferred verdict for damping
        only: its reading includes the finished turn's growth (and always
        runs while the turn still holds the semaphore), so it must neither
        reset nor schedule a reset — a still-critical session recovers via
        the cooldown → re-compact → immediately-measured verdict cycle."""
        async with _managed(cfg, _compacting_provider_factory(pct_before=96.0)) as mgr:
            provider, _, _ = await mgr.get_or_create(KEY)
            mgr.release(KEY)
            mgr._compact_pending_verdict[KEY] = 96.0
            provider.context_usage_pct = lambda: 96.0
            provider.context_usage_unknown = lambda: False

            mgr.check_context_usage(KEY, provider)
            await _drain_background(mgr)

            provider.shutdown.assert_not_awaited()
            assert KEY in mgr._sessions, "turn-end settle never resets"
            assert KEY not in mgr._compact_pending_verdict, "verdict consumed"
            assert mgr._compact_cooldown_until.get(KEY, 0.0) > time.monotonic()

    @pytest.mark.asyncio
    async def test_immediate_critical_decline_reports_ok_and_keeps_damping(self, cfg):
        """A queued turn can win the semaphore in the window between the
        compact completing and the escalation's teardown: the decline maps to
        plain "ok" (the compaction DID complete), the cooldown stays armed,
        and the still-critical session re-attempts the whole cycle at its
        next threshold crossing."""
        factory = _compacting_provider_factory(
            pct_before=100.0, pct_after=96.0, unknown_after=False
        )
        async with _managed(cfg, factory) as mgr:
            provider, _, _ = await mgr.get_or_create(KEY)
            mgr.release(KEY)

            with patch.object(mgr, "_reset_still_critical", AsyncMock(return_value=False)):
                assert await mgr.compact_if_needed(KEY) == "ok"

            assert KEY in mgr._sessions
            provider.shutdown.assert_not_awaited()
            assert mgr._compact_cooldown_until.get(KEY, 0.0) > time.monotonic()

    @pytest.mark.asyncio
    async def test_critical_reset_declines_a_live_turn_without_side_effects(self, cfg):
        """never-cut-a-live-turn: with the semaphore held, the escalation
        declines and the session survives untouched — no shutdown, no sid
        clear, no stale verdict left behind (recovery rides the armed
        cooldown and the next threshold crossing instead)."""
        async with _managed(cfg, _compacting_provider_factory(pct_before=96.0)) as mgr:
            provider, _, _ = await mgr.get_or_create(KEY)
            # NOT released — a turn is in flight.
            sess = mgr._sessions[KEY]

            with patch.object(mgr._session_map, "clear_sid") as clear_sid:
                assert await mgr._reset_still_critical(KEY, 96.0, 96.0, expect=sess) is False

            provider.shutdown.assert_not_awaited()
            assert mgr._sessions[KEY].provider is provider
            clear_sid.assert_not_called()
            assert KEY not in mgr._compact_pending_verdict
            mgr.release(KEY)

    @pytest.mark.asyncio
    async def test_stale_escalation_never_resets_a_replacement_session(self, cfg):
        """Awaits sit between the verdict measurement and the teardown (the
        compaction callback, the lock): if the key was replaced by a fresh
        cold-start in that window, the stale escalation must neither destroy
        the replacement nor clear its resume sid nor re-arm the stale
        verdict onto its readings."""
        async with _managed(cfg, _compacting_provider_factory(pct_before=96.0)) as mgr:
            _, _, _ = await mgr.get_or_create(KEY)
            mgr.release(KEY)
            measured_on = mgr._sessions[KEY]
            # The measured session goes away and a fresh one takes the key.
            await mgr.reset(KEY)
            replacement_provider, _, _ = await mgr.get_or_create(KEY)
            mgr.release(KEY)
            assert mgr._sessions[KEY] is not measured_on
            replacement_provider.shutdown.reset_mock()

            with patch.object(mgr._session_map, "clear_sid") as clear_sid:
                assert await mgr._reset_still_critical(KEY, 96.0, 96.0, expect=measured_on) is False

            replacement_provider.shutdown.assert_not_awaited()
            assert KEY in mgr._sessions, "replacement survives"
            clear_sid.assert_not_called()
            assert KEY not in mgr._compact_pending_verdict, "stale verdict not re-armed"


class _FakeClaudeCode:
    """Minimal stand-in for the dormant CC seam's provider (never started).

    ``session.ClaudeCodeProvider`` is ``None`` in the public core, so the
    ``cc_managed`` rung is reachable only by restoring a class at that module
    seam and handing the ladder an instance of it.
    """

    connection_mode = "per_session"

    @staticmethod
    def context_usage_pct() -> float:
        return 92.0

    @staticmethod
    def context_usage_unknown() -> bool:
        return False


class TestGateLadderParity:
    """Full gate-order parity between the two compaction entry points (#5132).

    ``check_context_usage`` (sync, fire-and-forget via ``_trigger_compaction``)
    and ``compact_if_needed`` (awaited) must decide compaction identically:
    both consume ``SessionManager._compaction_gate_decision``, the single
    owner of the gate ladder. These tests pin that each entry point consults
    the ladder exactly once per call and reaches the same decision on every
    rung, that stacked decline states resolve in ladder order on both paths,
    and that the entry points read no gate state of their own — the
    regression this refactor exists to prevent is a future gate added to one
    path only.
    """

    @staticmethod
    def _spy_gate(mgr):
        """Patch the ladder with a recording pass-through; return (ctx, log)."""
        decisions: list[str | None] = []
        real = mgr._compaction_gate_decision

        def spy(key, provider, pct):
            decision = real(key, provider, pct)
            decisions.append(decision)
            return decision

        return patch.object(mgr, "_compaction_gate_decision", side_effect=spy), decisions

    @pytest.mark.asyncio
    async def test_every_rung_declines_identically_on_both_paths(self, cfg):
        """One scenario per decline rung: each entry point consults the shared
        ladder exactly once, reaches the same decision, and neither compacts."""

        def _mark_in_progress(mgr):
            mgr._compacting.add(KEY)

        def _arm_cooldown(mgr):
            mgr._compact_cooldown_until[KEY] = time.monotonic() + 999

        scenarios = [
            ("below_threshold", {"pct_before": 10.0}, None),
            ("unconfirmed", {"unknown_before": True}, None),
            ("in_progress", {}, _mark_in_progress),
            ("cooldown", {}, _arm_cooldown),
        ]
        for expected, factory_kwargs, mutate in scenarios:
            async with _managed(cfg, _compacting_provider_factory(**factory_kwargs)) as mgr:
                provider, _, _ = await mgr.get_or_create(KEY)
                mgr.release(KEY)
                if mutate is not None:
                    mutate(mgr)
                ctx, decisions = self._spy_gate(mgr)
                with ctx:
                    assert await mgr.compact_if_needed(KEY) == expected
                    mgr.check_context_usage(KEY, provider)
                assert decisions == [expected, expected]
                provider.stream_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_cc_managed_declines_first_on_both_paths(self, cfg):
        """The dormant CC ``per_session`` rung declines before every other
        rung on BOTH paths — even over threshold, mid-compaction, and cooling
        down, the decision is ``cc_managed``."""
        async with _managed(cfg, _compacting_provider_factory()) as mgr:
            provider, _, _ = await mgr.get_or_create(KEY)
            mgr.release(KEY)
            cc = _FakeClaudeCode()
            # The awaited seam reads the provider off the session entry.
            mgr._sessions[KEY].provider = cc
            mgr._compacting.add(KEY)
            mgr._compact_cooldown_until[KEY] = time.monotonic() + 999
            try:
                ctx, decisions = self._spy_gate(mgr)
                with ctx, patch("kiro_crew.session.ClaudeCodeProvider", _FakeClaudeCode):
                    assert await mgr.compact_if_needed(KEY) == "cc_managed"
                    assert mgr.check_context_usage(KEY, cc) == 92.0
                assert decisions == ["cc_managed", "cc_managed"]
            finally:
                mgr._sessions[KEY].provider = provider  # restore for teardown
            provider.stream_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_stacked_declines_resolve_in_ladder_order_on_both_paths(self, cfg):
        """Multiple rungs armed at once: the FIRST in ladder order wins,
        identically on both paths — pinning gate ORDER, not just presence."""
        # threshold beats unconfirmed + dedup + cooldown
        async with _managed(
            cfg, _compacting_provider_factory(pct_before=10.0, unknown_before=True)
        ) as mgr:
            provider, _, _ = await mgr.get_or_create(KEY)
            mgr.release(KEY)
            mgr._compacting.add(KEY)
            mgr._compact_cooldown_until[KEY] = time.monotonic() + 999
            ctx, decisions = self._spy_gate(mgr)
            with ctx:
                assert await mgr.compact_if_needed(KEY) == "below_threshold"
                mgr.check_context_usage(KEY, provider)
            assert decisions == ["below_threshold", "below_threshold"]
        # unconfirmed beats dedup + cooldown
        async with _managed(cfg, _compacting_provider_factory(unknown_before=True)) as mgr:
            provider, _, _ = await mgr.get_or_create(KEY)
            mgr.release(KEY)
            mgr._compacting.add(KEY)
            mgr._compact_cooldown_until[KEY] = time.monotonic() + 999
            ctx, decisions = self._spy_gate(mgr)
            with ctx:
                assert await mgr.compact_if_needed(KEY) == "unconfirmed"
                mgr.check_context_usage(KEY, provider)
            assert decisions == ["unconfirmed", "unconfirmed"]
        # dedup beats cooldown
        async with _managed(cfg, _compacting_provider_factory()) as mgr:
            provider, _, _ = await mgr.get_or_create(KEY)
            mgr.release(KEY)
            mgr._compacting.add(KEY)
            mgr._compact_cooldown_until[KEY] = time.monotonic() + 999
            ctx, decisions = self._spy_gate(mgr)
            with ctx:
                assert await mgr.compact_if_needed(KEY) == "in_progress"
                mgr.check_context_usage(KEY, provider)
            assert decisions == ["in_progress", "in_progress"]

    @pytest.mark.asyncio
    async def test_settle_arms_the_cooldown_the_same_call_then_honors(self, cfg):
        """The pending-verdict settle is the FIRST rung on both paths: an
        ineffective deferred verdict arms the cooldown, and the very same
        call's cooldown rung then declines on it."""
        async with _managed(cfg, _compacting_provider_factory(pct_before=92.0)) as mgr:
            provider, _, _ = await mgr.get_or_create(KEY)
            mgr.release(KEY)
            ctx, decisions = self._spy_gate(mgr)
            with ctx:
                mgr._compact_pending_verdict[KEY] = 92.0
                assert await mgr.compact_if_needed(KEY) == "cooldown"
                assert KEY not in mgr._compact_pending_verdict
                # Twin on the sync path: re-arm the verdict, disarm the cooldown.
                mgr._compact_pending_verdict[KEY] = 92.0
                del mgr._compact_cooldown_until[KEY]
                mgr.check_context_usage(KEY, provider)
                assert KEY not in mgr._compact_pending_verdict
            assert decisions == ["cooldown", "cooldown"]
            provider.stream_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_both_entry_points_honor_the_ladder_verbatim(self, cfg):
        """A synthetic decline injected at the shared ladder is honored by
        BOTH entry points: the decision has exactly one owner and neither
        path second-guesses it."""
        async with _managed(cfg, _compacting_provider_factory()) as mgr:
            provider, _, _ = await mgr.get_or_create(KEY)
            mgr.release(KEY)
            with patch.object(
                mgr, "_compaction_gate_decision", return_value="synthetic_decline"
            ) as gate:
                assert await mgr.compact_if_needed(KEY) == "synthetic_decline"
                assert mgr.check_context_usage(KEY, provider) == 92.0
            assert gate.call_count == 2
            provider.stream_command.assert_not_called()
            assert KEY not in mgr._compacting

    @pytest.mark.asyncio
    async def test_proceed_commits_on_both_paths(self, cfg):
        """A ``None`` decision commits on both paths: the awaited seam runs
        the attempt inline; the sync path schedules it as a background task.
        Separate managers — each path's own compaction would otherwise change
        the state the other decides on."""
        async with _managed(cfg, _compacting_provider_factory()) as mgr:
            provider, _, _ = await mgr.get_or_create(KEY)
            mgr.release(KEY)
            ctx, decisions = self._spy_gate(mgr)
            with ctx:
                assert await mgr.compact_if_needed(KEY) == "ok"
            assert decisions == [None]
            provider.stream_command.assert_called_once_with("/compact")
        async with _managed(cfg, _compacting_provider_factory()) as mgr:
            provider, _, _ = await mgr.get_or_create(KEY)
            mgr.release(KEY)
            ctx, decisions = self._spy_gate(mgr)
            with ctx:
                mgr.check_context_usage(KEY, provider)
                await _drain_background(mgr)
            assert decisions == [None]
            provider.stream_command.assert_called_once_with("/compact")

    def test_gate_state_reads_live_only_in_the_shared_ladder(self):
        """Tripwire for the regression this refactor exists to prevent (a
        future gate added inline to one entry point instead of the shared
        ladder): the entry points may COMMIT the dedup entry
        (``_compacting.add``) but must not READ gate state — every read below
        belongs to ``_compaction_gate_decision`` alone.

        Scope, stated so nobody over-trusts it: the needle set enumerates
        TODAY'S gate-state reads and the entry-point set TODAY'S consumers,
        so a future gate reading a brand-new attribute, an aliased read, or
        a fourth entry point growing its own inline ladder is out of reach —
        the per-rung behavioral parity tests above are the primary guard;
        this one only makes re-inlining a KNOWN rung loud. Docstrings and
        comments are stripped before matching, so prose that mentions a rung
        (e.g. to explain the delegation) cannot false-positive, and a
        docstring mention alone cannot satisfy the delegation check — each
        entry point must contain the actual delegating CALL.
        """

        def executable_source(fn) -> str:
            """Function source minus docstrings and comments (AST round-trip)."""
            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
            for node in list(ast.walk(tree)):
                if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef)):
                    body = node.body
                    if (
                        body
                        and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)
                    ):
                        del body[0]
            return ast.unparse(tree)

        gate_state_reads = (
            "_compact_pending_verdict",
            "_compact_cooldown_until",
            "_context_pct_is_unknown",
            "in self._compacting",
            "connection_mode",
        )
        # check_context_usage delegates via the trigger seam, which is itself
        # pinned below to delegate to the ladder — the chain has one owner.
        delegation_markers = (
            (SessionManager.check_context_usage, "self._trigger_compaction("),
            (SessionManager.compact_if_needed, "self._compaction_gate_decision("),
            (SessionManager._trigger_compaction, "self._compaction_gate_decision("),
        )
        for entry, marker in delegation_markers:
            src = executable_source(entry)
            assert marker in src, f"{entry.__qualname__} no longer delegates its decision"
            for needle in gate_state_reads:
                assert needle not in src, f"{entry.__qualname__} reads gate state: {needle}"
        ladder_src = executable_source(SessionManager._compaction_gate_decision)
        for needle in gate_state_reads:
            assert needle in ladder_src, f"ladder lost its own rung read: {needle}"

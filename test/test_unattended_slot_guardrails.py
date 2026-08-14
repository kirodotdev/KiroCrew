"""Phase 0 guardrails for unattended, app-owned chat slots.

Three defects, each of which makes a fleet of unattended worker slots unusable.
Every test here is written to go RED if its own fix is reverted.

FIX 1 — the dashboard approval path parked for two hours.
    ``chat_runner._run_chat`` waited on its OWN per-slot approval future with a
    hardcoded ``timeout=7200.0``, so it never reached the deny-fast branch that
    ``DashboardState.request_approval`` already had. An unattended worker that
    tripped one untrusted tool held its slot for 2h and then denied anyway.

FIX 2 — nothing capped concurrency.
    Chat slots and concurrent turns were both uncapped; the only real ceiling
    was a 4-wide semaphore on agent cold starts plus host memory.

FIX 3 — idle cleanup permanently destroyed an autonudge loop.
    ``api_chat_slots_cleanup`` marked an idle slot closed; the nudge fire path
    then rehydrated WITHOUT ``adopt_closed=True``, could not reach a closed
    slot, and REMOVED the loop — terminally. An unattended worker is idle by
    nature between cycles, so the 3-day heuristic shot the longest-lived loops.

FIX 4 — a scoped auto-approve grant was cached as a session approval policy.
    ``_run_chat`` wrote ``session.approval_policy = "auto"`` whenever the slot was
    trusted, including by a ``SafetyOverride`` SCOPED grant. That policy is read
    later, by the subagent spawn gate and by each subagent's own policy, where
    nothing re-checks the grant — so revoking the scope mid-turn left a live turn
    auto-approving subagent tools off the stale value.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.safety_override import safety_override

# ── FIX 1 ────────────────────────────────────────────────────────────────────


class TestUnattendedApprovalWindow:
    def test_unattended_app_slot_gets_the_deny_fast_window(self, tmp_path) -> None:
        """The named FIX 1 test. Red if either half of the fix is reverted.

        Half one is ``DashboardState.approval_timeout_for`` (the decision, using
        the same two constants ``request_approval`` uses so the windows cannot
        drift). Half two is the runner call site actually asking for it instead
        of hardcoding a literal.
        """
        state = _make_state(tmp_path)

        worker = state.get_or_create_slot("worker-1", app="issue-radar")
        human = state.get_or_create_slot("chat-1-1785")

        assert worker.unattended is True
        assert human.unattended is False

        assert state.approval_timeout_for(worker) == float(
            DashboardState._BACKGROUND_APPROVAL_TIMEOUT_SECS
        )
        # Interactive behaviour is unchanged: a human session keeps the 2h window.
        assert state.approval_timeout_for(human) == float(DashboardState._APPROVAL_TIMEOUT)
        assert state.approval_timeout_for(worker) < state.approval_timeout_for(human)

        # …and the runner must USE it. Without this assertion the fix could be
        # reverted at the call site (back to a hardcoded 7200.0) while the
        # method above still answered correctly, and nothing would fail.
        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner._run_chat)
        assert "state.approval_timeout_for(slot)" in src, (
            "the runner's approval await must take its window from DashboardState"
        )
        assert "timeout=_approval_window" in src
        assert "timeout=7200.0" not in src, "the hardcoded 2h window is back"

    def test_a_human_typing_into_an_app_tab_restores_the_full_window(self, tmp_path) -> None:
        """``_human_seen`` is the escape hatch, and only a dashboard user sets it."""
        state = _make_state(tmp_path)
        worker = state.get_or_create_slot("worker-2", app="issue-radar")
        assert state.approval_timeout_for(worker) == 180.0

        worker._human_seen = True  # what the api_chat dashboard-user branch does

        assert worker.unattended is False
        assert state.approval_timeout_for(worker) == float(DashboardState._APPROVAL_TIMEOUT)

    def test_only_the_dashboard_user_branch_marks_attendance(self) -> None:
        """An app token must not be able to forge attendance for its own worker.

        Pins the placement of the write: inside ``api_chat``'s ``else`` (empty
        ``request_app``), not in a branch an app-scoped caller can reach.
        """
        from kiro_crew.dashboard import chat_handlers

        src = inspect.getsource(chat_handlers.api_chat)
        writes = [ln for ln in src.splitlines() if "_human_seen = True" in ln]
        assert len(writes) == 1, "attendance must be recorded in exactly one place"
        gate = src.index("request_app = request.get")
        assert src.index("_human_seen = True") > gate, (
            "attendance must be recorded only after the app-ownership gate"
        )

    def test_trust_is_not_the_detector(self, tmp_path) -> None:
        """Why ``_app`` and not ``_trust``: trust is False wherever this is read.

        The runner auto-approves and ``continue``s while trust holds, so a tool
        only reaches the interactive wait once trust is absent — and trust is
        in-memory, so a restart clears it on every app worker. A trust-based
        detector reads False in exactly the case it exists to detect, which is
        why the predicate must not consult it.
        """
        state = _make_state(tmp_path)
        worker = state.get_or_create_slot("worker-3", app="issue-radar")
        assert worker._trust is False  # the state every unattended wait is in
        assert worker.unattended is True

        worker._trust = True
        assert worker.unattended is True, "trust must not change the window decision"

    def test_attendance_survives_a_gateway_restart(self, tmp_path, monkeypatch) -> None:
        """``_human_seen`` is persisted, so a restart does not silently deny fast.

        The bug this pins: ``_app`` is persisted and ``_human_seen`` was not, so
        every gateway restart — an upgrade, a crash, a config reload — dropped an
        app-owned tab a person was working in from the 2h approval window to the
        180s unattended deny-fast, with nothing on screen to say why. A restart is
        not evidence the person left: the browser tab reconnects to the same slot.
        """
        from kiro_crew.dashboard.chat_persistence import (
            _rehydrate_slot_from_history,
            _save_slot_to_history,
        )

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("worker-4", app="issue-radar")
        assert state.approval_timeout_for(slot) == float(
            DashboardState._BACKGROUND_APPROVAL_TIMEOUT_SECS
        )

        slot._human_seen = True  # what api_chat's dashboard-user branch does
        slot.append("user", "take a look at this")
        slot.drain()
        _save_slot_to_history(state, slot, closed=False)

        assert state.conversation_log._read_metadata("dashboard:worker-4").get("human_seen") is True

        # The restart: the slot is gone from memory and comes back off disk.
        del state._slots["worker-4"]
        restored = _rehydrate_slot_from_history(state, "worker-4")

        assert restored is not None
        assert restored._app == "issue-radar", "app ownership must still be restored"
        assert restored._human_seen is True, "attendance must survive the restart"
        assert restored.unattended is False
        assert state.approval_timeout_for(restored) == float(DashboardState._APPROVAL_TIMEOUT)

    def test_an_untouched_app_slot_still_denies_fast_after_a_restart(
        self, tmp_path, monkeypatch
    ) -> None:
        """The other half: persistence must not hand the 2h window to a worker.

        A crew, a cron worker and an app-spawned session are exactly the slots no
        human has ever driven, so the flag is absent from their metadata and the
        deny-fast window is what they get back. Without this, the fix above could
        be written as an unconditional restore and nothing would fail.
        """
        from kiro_crew.dashboard.chat_persistence import (
            _rehydrate_slot_from_history,
            _save_slot_to_history,
        )

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("worker-5", app="issue-radar")
        slot.append("user", "[nudge] advance your work")
        slot.drain()
        _save_slot_to_history(state, slot, closed=False)

        assert "human_seen" not in state.conversation_log._read_metadata("dashboard:worker-5")

        del state._slots["worker-5"]
        restored = _rehydrate_slot_from_history(state, "worker-5")

        assert restored is not None
        assert restored._human_seen is False
        assert restored.unattended is True
        assert state.approval_timeout_for(restored) == float(
            DashboardState._BACKGROUND_APPROVAL_TIMEOUT_SECS
        )


# ── FIX 2 ────────────────────────────────────────────────────────────────────


class TestBackgroundTurnCap:
    @pytest.mark.asyncio
    async def test_cap_queues_the_extra_unattended_turn(self, tmp_path) -> None:
        """The named FIX 2 test: at the cap a turn QUEUES and the wait is visible.

        Queue rather than reject: a rejected crew turn loses the issue it was
        working; a queued one only starts late.
        """
        state = _make_state(tmp_path)
        state.effective_max_background_turns = lambda: 1  # type: ignore[method-assign]

        w1 = state.get_or_create_slot("w1", app="issue-radar")
        w2 = state.get_or_create_slot("w2", app="issue-radar")

        started = asyncio.Event()
        release = asyncio.Event()
        order: list[str] = []

        async def _first() -> None:
            order.append("first-start")
            started.set()
            await release.wait()
            order.append("first-end")

        async def _second() -> None:
            order.append("second-start")

        t1 = asyncio.ensure_future(state.run_background_turn(w1, _first()))
        await started.wait()
        t2 = asyncio.ensure_future(state.run_background_turn(w2, _second()))
        await asyncio.sleep(0)  # let t2 reach the semaphore and block

        stats = state.background_turn_stats()
        assert stats["cap"] == 1
        assert stats["running"] == 1
        assert stats["waiting"] == 1, "a turn held at the cap must be observable"
        assert order == ["first-start"], "the second turn ran despite the cap"

        release.set()
        await asyncio.gather(t1, t2)

        assert order == ["first-start", "first-end", "second-start"]
        assert state.background_turn_stats() == {"cap": 1, "running": 0, "waiting": 0}

    @pytest.mark.asyncio
    async def test_attended_turns_bypass_the_cap_entirely(self, tmp_path) -> None:
        """Interactive turns must not queue behind a busy fleet."""
        state = _make_state(tmp_path)
        state.effective_max_background_turns = lambda: 1  # type: ignore[method-assign]

        worker = state.get_or_create_slot("w1", app="issue-radar")
        human = state.get_or_create_slot("chat-1-1785")

        started = asyncio.Event()
        release = asyncio.Event()

        async def _held() -> None:
            started.set()
            await release.wait()

        async def _human_turn() -> str:
            return "ran"

        t1 = asyncio.ensure_future(state.run_background_turn(worker, _held()))
        await started.wait()

        # Cap is full, yet the human turn completes immediately.
        assert await state.run_background_turn(human, _human_turn()) == "ran"
        assert state.background_turn_stats()["waiting"] == 0

        release.set()
        await t1

    def test_config_can_widen_the_cap_but_never_unbound_it(self, tmp_path, monkeypatch) -> None:
        """Same clamp shape as code_review_sage's MAX_CONCURRENT_CEIL.

        Patches ``state._raw_config``, not ``config.loader._raw_config``: the
        import is at module scope (AUTOSDE ``top-level-imports``), so ``state``
        binds the function object once at import time and patching the loader's
        namespace would not be seen. That mis-targeting is one of the harms the
        rule names, and it is why the patch site is pinned here.
        """
        state = _make_state(tmp_path)
        raw: dict = {}
        monkeypatch.setattr("kiro_crew.dashboard.state._raw_config", lambda: raw)

        assert state.effective_max_background_turns() == DashboardState.MAX_BACKGROUND_TURNS

        raw["dashboard"] = {"max_background_turns": 9}
        assert state.effective_max_background_turns() == 9

        raw["dashboard"] = {"max_background_turns": 10_000}
        assert (
            state.effective_max_background_turns() == DashboardState.MAX_BACKGROUND_TURNS_CEIL
        ), "the cap must never be configurable above its ceiling"

        raw["dashboard"] = {"max_background_turns": 0}
        assert state.effective_max_background_turns() == 1, "the cap cannot be disabled"

        raw["dashboard"] = {"max_background_turns": "nonsense"}
        assert state.effective_max_background_turns() == DashboardState.MAX_BACKGROUND_TURNS

    @pytest.mark.asyncio
    async def test_a_cancelled_queued_turn_does_not_leak_its_coroutine(self, tmp_path) -> None:
        """Cancelled while queued: the turn never ran, so close its coroutine."""
        state = _make_state(tmp_path)
        state.effective_max_background_turns = lambda: 1  # type: ignore[method-assign]
        w1 = state.get_or_create_slot("w1", app="issue-radar")
        w2 = state.get_or_create_slot("w2", app="issue-radar")

        started = asyncio.Event()
        release = asyncio.Event()
        ran = False

        async def _held() -> None:
            started.set()
            await release.wait()

        async def _never() -> None:
            nonlocal ran
            ran = True

        t1 = asyncio.ensure_future(state.run_background_turn(w1, _held()))
        await started.wait()
        queued = _never()
        t2 = asyncio.ensure_future(state.run_background_turn(w2, queued))
        await asyncio.sleep(0)
        t2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t2

        assert ran is False
        assert queued.cr_running is False and queued.cr_frame is None, "coroutine left open"
        assert state.background_turn_stats()["waiting"] == 0

        release.set()
        await t1

    @pytest.mark.asyncio
    async def test_a_queued_turn_that_never_gets_a_permit_fails_with_the_real_reason(
        self, tmp_path
    ) -> None:
        """The queue wait is bounded, and names itself when it expires.

        The wait happens inside the coroutine ``spawn_guarded_turn`` already
        bounds at the 7200s turn ceiling, so an unbounded wait would eventually
        be killed as "turn exceeded the ceiling" — true, but the wrong cause.
        """
        state = _make_state(tmp_path)
        state.effective_max_background_turns = lambda: 1  # type: ignore[method-assign]
        state._BACKGROUND_QUEUE_WAIT_SECS = 0.05  # type: ignore[misc]
        w1 = state.get_or_create_slot("w1", app="issue-radar")
        w2 = state.get_or_create_slot("w2", app="issue-radar")

        started = asyncio.Event()
        release = asyncio.Event()
        ran = False

        async def _held() -> None:
            started.set()
            await release.wait()

        async def _never() -> None:
            nonlocal ran
            ran = True

        t1 = asyncio.ensure_future(state.run_background_turn(w1, _held()))
        await started.wait()

        with pytest.raises(TimeoutError, match="background-turn cap"):
            await state.run_background_turn(w2, _never())

        assert ran is False
        assert state.background_turn_stats()["waiting"] == 0
        release.set()
        await t1

    def test_the_cap_is_published_in_the_status_payload(self, tmp_path) -> None:
        """The wait has to be readable from outside the process."""
        state = _make_state(tmp_path)
        stats = state.background_turn_stats()
        assert set(stats) == {"cap", "running", "waiting"}
        assert "background_turns" in inspect.getsource(DashboardState.status_snapshot)

    def test_both_unattended_dispatch_sites_go_through_the_cap(self) -> None:
        """A new dispatch site that skips the gate reintroduces the uncapped fleet."""
        from kiro_crew.dashboard import chat_handlers
        from kiro_crew.slack import gateway as gw

        assert "run_background_turn" in inspect.getsource(chat_handlers.api_chat)
        assert "run_background_turn" in inspect.getsource(
            gw.GatewayOrchestrator._fire_dashboard_nudge
        )


# ── FIX 3 ────────────────────────────────────────────────────────────────────


def _fake_autonudge(loops: list) -> MagicMock:
    svc = MagicMock()
    svc.list_all = MagicMock(return_value=loops)
    return svc


class _Loop:
    """Stand-in carrying only the fields the two call sites read."""

    def __init__(self, slot_key: str, *, active: bool = True, loop_id: str = "loop-1") -> None:
        self.id = loop_id
        self.slot_key = slot_key
        self.active = active
        self.message = "check CI"
        self.idle_secs = 300
        self.max_cycles = 24
        self.cycle_count = 3
        self.stop_sentinel_path = ""


def _bg_slot(key: str) -> MagicMock:
    slot = MagicMock()
    slot.key = key
    slot.running = False
    return slot


def _closing_spawn():
    """``spawn_guarded_turn`` stand-in that closes the coroutine it is handed."""

    def _spawn(state, slot, coro, **kwargs):
        coro.close()
        return MagicMock(name="turn-task")

    return _spawn


def _nudge_orchestrator():
    """Minimal GatewayOrchestrator for the dashboard nudge fire path."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.slack import gateway as gw

    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
        orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    orch.dashboard_state = SimpleNamespace(
        get_slot=MagicMock(return_value=None),
        push_slots_update=MagicMock(),
        _background_tasks=set(),
        run_background_turn=MagicMock(side_effect=lambda _slot, coro: coro),
    )
    orch.autonudge_svc = MagicMock()
    orch.autonudge_svc.remove = AsyncMock()
    orch._session_tasks = {}
    return orch


class TestIdleCleanupSparesArmedLoops:
    @pytest.mark.asyncio
    async def test_idle_cleanup_spares_a_slot_with_an_armed_loop(
        self, tmp_path, monkeypatch
    ) -> None:
        """The named FIX 3 test. Red if either half of the fix is reverted.

        Half one: cleanup must skip a slot owning an armed loop, so the loop's
        session is never marked closed in the first place. Half two: the fire
        path must adopt a session that IS closed, so a slot archived by any
        other automatic closer (or before this landed) is still reachable
        instead of retiring the loop terminally.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()

        babysat = state.get_or_create_slot("worker-1", app="issue-radar")
        babysat.append("user", "watching CI", ts=old_ts)
        babysat.drain()

        abandoned = state.get_or_create_slot("worker-2", app="issue-radar")
        abandoned.append("user", "nothing armed here", ts=old_ts)
        abandoned.drain()

        monkeypatch.setattr(
            "kiro_crew.autonudge.get_instance",
            lambda: _fake_autonudge([_Loop("worker-1")]),
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup", json={"max_inactive_days": 3}
            )
            data = await resp.json()

        assert data["keys"] == ["worker-2"], "cleanup archived a slot owning an armed loop"
        assert "worker-1" in state._slots
        assert "worker-2" not in state._slots

        # Half two: the fire path must actually REACH a session that was closed.
        # Asserted behaviourally, not by substring — the explanatory comment in
        # that function contains the words "adopt_closed=True" and would satisfy
        # a source scan even with the argument deleted.
        from kiro_crew.slack import gateway as gw

        orch = _nudge_orchestrator()
        rehydrate = AsyncMock(return_value=_bg_slot("worker-1"))
        with (
            patch.object(gw, "rehydrate_slot_from_history_async", new=rehydrate),
            patch.object(gw, "spawn_guarded_turn", _closing_spawn()),
            patch("kiro_crew.dashboard.chat._run_chat", new=AsyncMock()),
        ):
            assert await orch._fire_dashboard_nudge(_Loop("worker-1")) is True
        assert rehydrate.await_args.kwargs.get("adopt_closed") is True, (
            "the nudge fire path must reach a session that idle cleanup closed"
        )

    @pytest.mark.asyncio
    async def test_an_inactive_loop_does_not_pin_a_dead_slot_forever(
        self, tmp_path, monkeypatch
    ) -> None:
        """Only ARMED loops are protected — a paused one must not leak slots."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
        slot = state.get_or_create_slot("worker-1", app="issue-radar")
        slot.append("user", "paused loop", ts=old_ts)
        slot.drain()

        monkeypatch.setattr(
            "kiro_crew.autonudge.get_instance",
            lambda: _fake_autonudge([_Loop("worker-1", active=False)]),
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup", json={"max_inactive_days": 3}
            )
            data = await resp.json()

        assert data["keys"] == ["worker-1"]

    @pytest.mark.asyncio
    async def test_an_unreadable_registry_archives_nothing(self, tmp_path, monkeypatch) -> None:
        """Fail CLOSED: not knowing which slots are protected must not destroy one."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
        slot = state.get_or_create_slot("worker-1", app="issue-radar")
        slot.append("user", "stale", ts=old_ts)
        slot.drain()

        def _boom():
            raise RuntimeError("autonudge.json unreadable")

        monkeypatch.setattr("kiro_crew.autonudge.get_instance", _boom)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup", json={"max_inactive_days": 3}
            )
            data = await resp.json()

        assert data["archived"] == 0
        assert data["skipped"] == "autonudge_unknown"
        assert "worker-1" in state._slots

    @pytest.mark.asyncio
    async def test_the_users_close_still_retires_the_loop(self, tmp_path, monkeypatch) -> None:
        """"Respect the close" survives adopt_closed=True.

        The rule used to be an emergent property of the fire path's rehydrate
        miss. Now that the fire path adopts a closed session, the ✕ handler has
        to retire the loop itself — otherwise a dismissed tab would be
        resurrected by its own loop on the next cycle.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("chat-1-1785")

        removed: list[str] = []
        svc = MagicMock()
        svc.get_by_slot = MagicMock(return_value=_Loop("chat-1-1785", loop_id="loop-9"))

        async def _remove(loop_id: str) -> None:
            removed.append(loop_id)

        svc.remove = _remove
        monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: svc)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/chat/slots/chat-1-1785")
            assert resp.status == 200

        assert removed == ["loop-9"], "the user's ✕ must retire the slot's nudge loop"

    @pytest.mark.asyncio
    async def test_the_users_close_tells_the_owning_app(self, tmp_path, monkeypatch) -> None:
        """Retiring the loop is not enough for an APP-owned worker slot.

        An app driving a worker on a timer re-establishes a live worker's missing
        loop every cycle — correct after a restart, which drops the in-memory
        registry, but it also undoes the loop removal above and resurrects the tab.
        The app cannot recover the user's intent from the transcript afterwards
        either: idle archival persists the same ``closed`` + ``closed_at`` pair.

        So the ✕ notifies the slot's OWN app. Dispatch is by ``slot._app``, so core
        names no app and this stays app-agnostic.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("crew-c_1a2b3c4d", app="issue-radar")

        notified: list[tuple[str, str]] = []

        async def _hook(slot_key: str) -> None:
            notified.append(("issue-radar", slot_key))

        from kiro_crew.apps import teardown

        teardown.register_slot_close_hook("issue-radar", _hook)
        monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: None)
        try:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.delete("/api/chat/slots/crew-c_1a2b3c4d")
                assert resp.status == 200
        finally:
            teardown.unregister_slot_close_hook("issue-radar")

        assert notified == [("issue-radar", "crew-c_1a2b3c4d")]

    @pytest.mark.asyncio
    async def test_an_unowned_slot_notifies_nobody(self, tmp_path, monkeypatch) -> None:
        """An ordinary chat tab has no owning app, so there is nothing to tell."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("chat-1-1786")

        calls: list[str] = []

        async def _notify(app: str, slot_key: str) -> None:
            calls.append(app)

        monkeypatch.setattr("kiro_crew.apps.teardown.notify_slot_closed", _notify)
        monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: None)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/chat/slots/chat-1-1786")
            assert resp.status == 200

        assert calls == []

    @pytest.mark.asyncio
    async def test_a_failing_session_teardown_still_notifies_the_app(
        self, tmp_path, monkeypatch
    ) -> None:
        """The app learns of the ✕ even when tearing the ACP session down throws.

        REGRESSION: the notification used to run AFTER ``sessions.remove``. An ACP
        teardown error therefore propagated out of the handler with the app never
        told, leaving a live crew whose watchdog re-armed the very tab the user had
        just closed — the resurrection this hook exists to prevent, reachable by an
        error in an unrelated subsystem.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("crew-c_1a2b3c4d", app="issue-radar")

        notified: list[str] = []

        async def _hook(slot_key: str) -> None:
            notified.append(slot_key)

        async def _boom(key: str) -> None:
            raise RuntimeError("ACP session teardown failed")

        from kiro_crew.apps import teardown

        teardown.register_slot_close_hook("issue-radar", _hook)
        monkeypatch.setattr(state.sessions, "remove", _boom, raising=False)
        monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: None)
        try:
            async with TestClient(TestServer(_make_app(state))) as client:
                # The request may well fail — that is not what is under test. What
                # must hold is that the app was told BEFORE the failing step.
                with contextlib.suppress(Exception):
                    await client.delete("/api/chat/slots/crew-c_1a2b3c4d")
        finally:
            teardown.unregister_slot_close_hook("issue-radar")

        assert notified == ["crew-c_1a2b3c4d"], (
            "the session teardown raised and the owning app was never told the slot "
            "closed, so its watchdog will relaunch the closed tab"
        )

    @pytest.mark.asyncio
    async def test_the_loop_is_retired_before_the_slot_leaves_the_registry(
        self, tmp_path, monkeypatch
    ) -> None:
        """Order matters: kill the trigger, THEN make its absence destructive.

        REGRESSION: the slot was popped from ``_slots`` before the loop was retired.
        Retiring takes the AutoNudge lock, so it awaits — and a timer expiring inside
        that await found the slot already missing. The fire path answers a missing
        slot with ``rehydrate_slot_from_history_async(..., adopt_closed=True)``, which
        rebuilds the session and adopts it DESPITE the closed flag (deliberately, so
        idle-archived workers survive). So the pop-first order turned "the user
        dismissed this tab" into "the tab comes back".

        Asserted as an ORDER rather than by racing a real timer: the race is what the
        order exists to make unreachable, so reproducing it would be testing the
        scheduler. What must hold is that no await sits between the pop and the
        retire — and the retire is what removes the only thing that can fire.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("crew-c_1a2b3c4d", app="issue-radar")

        order: list[str] = []

        async def _retire(name: str):
            order.append("retire")
            return None

        class _WatchedSlots(dict):
            def pop(self, key, *a):
                order.append("pop")
                return super().pop(key, *a)

        state._slots = _WatchedSlots(state._slots)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._retire_slot_nudge_loop", _retire
        )
        monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: None)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/chat/slots/crew-c_1a2b3c4d")
            assert resp.status == 200

        assert order[:2] == ["retire", "pop"], (
            f"the slot left the registry before its loop was retired: {order}"
        )


# ── A SCOPED grant is never cached as a session approval policy ───────────────


_SCOPE = "issue-radar:crew:c_1a2b3c4d:autoapprove"


def _trust_slot(*, trust: bool = False, scope: str = "") -> SimpleNamespace:
    """A slot whose trust attributes are exactly the two named — nothing implicit.

    Deliberately not a ``MagicMock``: on a mock every ``getattr`` answers with a
    truthy child, so ``_trust_scope`` would read as set on a slot that is meant to
    carry no scope at all, and that is precisely the distinction under test.
    """
    return SimpleNamespace(key="crew-c_1a2b3c4d", _trust=trust, _trust_scope=scope)


def _crew_state():
    state = MagicMock()
    state.context_builder.hooks.auto_approve_subagent_tools = False
    state.is_yolo_active.return_value = False
    return state


class TestScopedGrantIsNeverPersisted:
    """``session.approval_policy`` may only cache a grant that cannot lapse.

    A crew is unattended, so its auto-approval is a ``SafetyOverride`` SCOPED
    grant re-checked on every approval — the grant lapsing is what revokes trust.
    A session-level ``"auto"`` policy is the opposite: written once at turn start
    and read later by the subagent spawn gate and by each subagent's own approval
    policy, where nothing re-checks anything. Storing one off a scoped grant
    caches a decision that is supposed to be re-decided.
    """

    def test_a_scope_trusted_slot_gets_no_persisted_policy(self) -> None:
        """The named fix. RED if the write goes back to ``_slot_is_trusted``."""
        slot = _trust_slot(scope=_SCOPE)
        with patch.object(type(safety_override()), "is_scope_active", return_value=True):
            # The grant really is live, and the per-event gate really would
            # auto-approve on it — so this is not a vacuous case.
            assert chat_runner._slot_is_trusted(slot) is True
            # …and still nothing is cached in the session store off that fact.
            assert chat_runner._persistable_session_policy(slot, False) == ""

    def test_interactive_trust_still_persists(self) -> None:
        """A human's click does not expire, so caching it changes nothing.

        Bounds the mutation of dropping the ``_trust`` read from the helper.
        """
        assert chat_runner._persistable_session_policy(_trust_slot(trust=True), False) == "auto"

    def test_yolo_still_persists(self) -> None:
        """Process-wide, and revoking it deactivates the override for everyone.

        Bounds the mutation of dropping the ``yolo_active`` read from the helper.
        """
        assert chat_runner._persistable_session_policy(_trust_slot(), True) == "auto"
        # Yolo does not need any per-slot flag to reach "auto".
        assert chat_runner._persistable_session_policy(_trust_slot(scope=_SCOPE), True) == "auto"

    def test_an_ordinary_untrusted_slot_persists_nothing(self) -> None:
        assert chat_runner._persistable_session_policy(_trust_slot(), False) == ""

    def test_revoking_the_scope_between_two_approvals_stops_the_second(self) -> None:
        """Flip ``is_scope_active`` between approvals — no sleeping, no TTL wait.

        Two properties in one test because the defect needs both: the revocation
        must be observable at the decision point (approval two denies), AND the
        policy written at turn start must not have been carrying the grant
        anyway — otherwise approval two's denial is irrelevant, since a subagent
        would still be approving off the cached ``"auto"``. The second assertion
        is the one that goes RED when the fix is reverted.
        """
        slot = _trust_slot(scope=_SCOPE)
        state = _crew_state()
        tracker = {"s1": {"done": False}}
        live = {"active": True}

        with patch.object(
            type(safety_override()),
            "is_scope_active",
            side_effect=lambda scope: live["active"],
        ):
            first = chat_runner._native_crew_should_auto_approve(tracker, state, slot)
            # The operator pauses/retires the crew, or disables the app, mid-turn.
            live["active"] = False
            second = chat_runner._native_crew_should_auto_approve(tracker, state, slot)

            assert first is True
            assert second is False
            # Nothing outlived the grant: no "auto" was ever stored, in either state.
            assert chat_runner._persistable_session_policy(slot, False) == ""

        live["active"] = True
        with patch.object(
            type(safety_override()),
            "is_scope_active",
            side_effect=lambda scope: live["active"],
        ):
            assert chat_runner._persistable_session_policy(slot, False) == ""

    def test_a_scope_trusted_crew_still_approves_its_own_tool_call(self) -> None:
        """The feature is not broken by the fix — the crew's own tools still pass.

        A crew's approvals never consult the persisted policy; they go through
        ``_slot_is_trusted`` per event. RED if the fix were mis-applied by taking
        the scope out of ``_slot_is_trusted`` (which would stall every crew) or by
        changing the gate the runner branches on.
        """
        slot = _trust_slot(scope=_SCOPE)
        state = _crew_state()
        with patch.object(type(safety_override()), "is_scope_active", return_value=True):
            assert chat_runner._slot_is_trusted(slot) is True
            assert chat_runner._native_crew_should_auto_approve({"s1": {"done": False}}, state, slot)

        src = inspect.getsource(chat_runner._run_chat)
        assert "slot_trusted = _slot_is_trusted(slot)" in src
        assert "if slot_trusted or yolo_active:" in src

    def test_the_runner_writes_the_policy_through_the_helper(self) -> None:
        """Pins the call site, not just the helper.

        Without this the helper could be correct while ``_run_chat`` went on
        deriving the stored policy from ``_slot_is_trusted`` itself, and every
        assertion above would still pass.
        """
        src = inspect.getsource(chat_runner._run_chat)
        assert "_persistable_session_policy(slot, state.is_yolo_active())" in src
        assert "_slot_is_trusted(slot) or state.is_yolo_active()" not in src
        # Assigned unconditionally, so a turn starting after the grant went away
        # clears a policy an earlier turn stored.
        assert 'set_approval_policy(session_key, "auto")' not in src

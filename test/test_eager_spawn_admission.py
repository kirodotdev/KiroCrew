"""How many speculative sessions may be live: the cap on armed prefetches, fresh eager sessions
counted against the same population cap, and the host-derived allowance checked before a
spawn rather than after it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eager_spawn_helpers import (  # noqa: F401 -- fixtures are used by name
    _bindings,
    _cfg,
    _isolate_armed_registry,
    _mock_state,
    _pin_prewarm_allowance,
    _prime,
)

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import _eager_spawn
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.testing.wait import await_until


class TestArmedPrefetchCap:
    """_cap_armed_prefetches: population cap on live-but-unclaimed prefetches.

    Design Review concern on 64a5c5f89: the spawn semaphore bounds concurrent
    spawns, not accumulated live processes — after a restart restores many
    resumable tabs, flipping through them could stack one kiro-cli process per
    tab for the whole TTL. Arming beyond the cap evicts the OLDEST unclaimed
    prefetch via the conditional remove_if_unclaimed.
    """

    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        chat_runner._armed_prefetches.clear()
        yield
        chat_runner._armed_prefetches.clear()

    @pytest.mark.asyncio
    async def test_under_cap_evicts_nothing(self):
        sessions = MagicMock()
        sessions.remove_if_unclaimed = AsyncMock(return_value=True)
        for i in range(chat_runner._RESUME_PREFETCH_MAX_LIVE):
            await chat_runner._cap_armed_prefetches(sessions, f"dashboard:k{i}")
        sessions.remove_if_unclaimed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_over_cap_evicts_the_oldest_unclaimed(self):
        sessions = MagicMock()
        sessions.remove_if_unclaimed = AsyncMock(return_value=True)
        for i in range(chat_runner._RESUME_PREFETCH_MAX_LIVE + 1):
            await chat_runner._cap_armed_prefetches(sessions, f"dashboard:k{i}")
        sessions.remove_if_unclaimed.assert_awaited_once_with("dashboard:k0")
        assert "dashboard:k0" not in chat_runner._armed_prefetches
        assert len(chat_runner._armed_prefetches) == chat_runner._RESUME_PREFETCH_MAX_LIVE

    @pytest.mark.asyncio
    async def test_rearming_a_key_moves_it_to_newest(self):
        """Re-focusing a slot must not leave its key at the eviction front."""
        sessions = MagicMock()
        sessions.remove_if_unclaimed = AsyncMock(return_value=True)
        for i in range(chat_runner._RESUME_PREFETCH_MAX_LIVE):
            await chat_runner._cap_armed_prefetches(sessions, f"dashboard:k{i}")
        await chat_runner._cap_armed_prefetches(sessions, "dashboard:k0")  # re-arm
        await chat_runner._cap_armed_prefetches(sessions, "dashboard:new")
        # k1 is now the oldest, not the re-armed k0.
        sessions.remove_if_unclaimed.assert_awaited_once_with("dashboard:k1")

    @pytest.mark.asyncio
    async def test_claimed_session_just_leaves_the_accounting(self):
        """remove_if_unclaimed returning False (claimed/gone) drops the entry
        without error — a claimed session is never touched."""
        sessions = MagicMock()
        sessions.remove_if_unclaimed = AsyncMock(return_value=False)
        for i in range(chat_runner._RESUME_PREFETCH_MAX_LIVE + 1):
            await chat_runner._cap_armed_prefetches(sessions, f"dashboard:k{i}")
        sessions.remove_if_unclaimed.assert_awaited_once_with("dashboard:k0")
        assert len(chat_runner._armed_prefetches) == chat_runner._RESUME_PREFETCH_MAX_LIVE


class TestFreshSpawnPopulationCap:
    """FRESH eager sessions count against the live-population cap.

    The cap machinery above only bounds what registers into it. Before this
    wiring, only the resumed-prefetch path registered, so sequential fresh
    signals (slot create, agent/project set) stacked one live-but-unclaimed
    agent process per slot — each with its own MCP servers — until the idle
    sweep, unbounded by the spawn semaphore (which gates concurrency, not
    population).
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this module calls monkeypatch.undo()
    def _no_debounce(self, monkeypatch):
        monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)

    @pytest.mark.asyncio
    async def test_fresh_spawn_registers_in_live_population(self):
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        bindings = _bindings()
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)),
            patch.object(chat_runner, "resolve_agent_bindings", return_value=bindings),
        ):
            await _eager_spawn(state, slot)
        key = state.sessions.get_or_create.await_args.args[0]
        assert key in chat_runner._armed_prefetches

    @pytest.mark.asyncio
    async def test_fresh_spawns_beyond_cap_evict_the_oldest_unclaimed(self):
        shared_sessions = MagicMock()
        shared_sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
        shared_sessions.release = MagicMock()
        shared_sessions.reset = AsyncMock()
        shared_sessions.remove = AsyncMock()
        shared_sessions.remove_if_unclaimed = AsyncMock(return_value=True)
        bindings = _bindings()
        keys: list[str] = []
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)),
            patch.object(chat_runner, "resolve_agent_bindings", return_value=bindings),
        ):
            for i in range(chat_runner._RESUME_PREFETCH_MAX_LIVE + 1):
                slot = _ChatSlot(f"t{i}")
                state = _mock_state(slot)
                state.sessions = shared_sessions
                await _eager_spawn(state, slot)
                keys.append(shared_sessions.get_or_create.await_args.args[0])
        shared_sessions.remove_if_unclaimed.assert_awaited_once_with(keys[0])
        assert keys[0] not in chat_runner._armed_prefetches
        assert len(chat_runner._armed_prefetches) == chat_runner._RESUME_PREFETCH_MAX_LIVE

    @pytest.mark.asyncio
    async def test_the_cap_is_the_shared_resource_status_ceiling(self):
        """One number, owned by resource_status: the ample-host allowance IS the
        fixed cap, so the two cannot drift apart."""
        from kiro_crew import resource_status

        assert chat_runner._RESUME_PREFETCH_MAX_LIVE == resource_status.PREWARM_MAX_LIVE
        assert resource_status.prewarm_allowance(64.0) == chat_runner._RESUME_PREFETCH_MAX_LIVE

    @pytest.mark.asyncio
    async def test_lost_race_does_not_enter_the_population(self):
        """is_new=False means a real creator owns that session — it must not
        enter unclaimed accounting where an eviction attempt would target it
        (the conditional remove makes that attempt a no-op, but the registry
        slot it burns would let a genuinely unclaimed session survive over
        the cap)."""
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
        bindings = _bindings()
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)),
            patch.object(chat_runner, "resolve_agent_bindings", return_value=bindings),
        ):
            await _eager_spawn(state, slot)
        assert not chat_runner._armed_prefetches


class TestPrewarmAdmission:
    """The host-derived allowance is checked BEFORE the spawn, not only after.

    The pre-fix shape spawned first and evicted after, so on a memory-starved
    host every slot signal still paid for one full kiro-cli process before the
    count-based eviction reclaimed an older one — and a constant cap of three
    never shrank with the host. ``_prewarm_allowance`` is the seam.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this module calls monkeypatch.undo()
    def _no_debounce(self, monkeypatch):
        monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)

    @staticmethod
    def _bindings():
        """The module-level resolver stub, not a local one.

        A `MagicMock` with two attributes set passes whatever the eager path
        reads today and silently starts FAILING every gate the path grows next:
        an unset attribute answers with a `MagicMock`, which is not a `str`, so a
        type-checked gate stands the spawn down and these tests then assert
        against a handshake that never happened. `_bindings()` carries the real
        field set, in one place, for exactly that reason.
        """
        return _bindings()

    @pytest.mark.asyncio
    async def test_zero_allowance_spawns_nothing(self, monkeypatch):
        """Critical host: no process is created at all — the first message
        cold-starts exactly as if eager spawn never ran."""
        monkeypatch.setattr(chat_runner, "_prewarm_allowance", lambda: 0)
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)),
            patch.object(chat_runner, "resolve_agent_bindings", return_value=self._bindings()),
        ):
            await _eager_spawn(state, slot)
        state.sessions.get_or_create.assert_not_awaited()
        assert not chat_runner._armed_prefetches

    @pytest.mark.parametrize("held", [True, False])
    @pytest.mark.asyncio
    async def test_the_subagent_pressure_hold_blocks_a_new_prewarm(self, monkeypatch, held):
        """While the subagent gate's macOS memory-pressure hold applies, a
        speculative runtime must not take the memory held starts wait for; the
        host allowance alone would admit it."""
        monkeypatch.setattr(chat_runner, "_prewarm_allowance", lambda: 3)
        chat_runner._armed_prefetches.clear()
        chat_runner._armed_prefetches["dashboard:old"] = 1.0
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        state.sessions.remove_if_unclaimed = AsyncMock(return_value=True)
        state.subagents = SimpleNamespace(memory_pressure_hold_active=lambda: held)
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)),
            patch.object(chat_runner, "resolve_agent_bindings", return_value=self._bindings()),
        ):
            await _eager_spawn(state, slot)
        try:
            if held:
                state.sessions.get_or_create.assert_not_awaited()
                # The idle pre-warm already live is left alone.
                state.sessions.remove_if_unclaimed.assert_not_awaited()
                assert "dashboard:old" in chat_runner._armed_prefetches
            else:
                state.sessions.get_or_create.assert_awaited_once()
        finally:
            chat_runner._armed_prefetches.clear()

    @pytest.mark.asyncio
    async def test_zero_allowance_evicts_the_prewarms_already_live(self, monkeypatch):
        """Critical host: refusing a new pre-warm is not enough when idle ones
        from a healthier band are still alive; the admission evicts them to zero
        before refusing."""
        sessions = MagicMock()
        sessions.remove_if_unclaimed = AsyncMock(return_value=True)
        chat_runner._armed_prefetches.clear()
        chat_runner._armed_prefetches["old-a"] = 1.0
        chat_runner._armed_prefetches["old-b"] = 2.0
        # The slot being re-armed has an earlier pre-warm of its own; in this
        # band it gets no exemption either.
        chat_runner._armed_prefetches["new"] = 3.0
        try:
            admitted = await chat_runner._admit_prefetch(sessions, "new", 0)
            assert admitted is False
            assert sessions.remove_if_unclaimed.await_count == 3
            assert not chat_runner._armed_prefetches
        finally:
            chat_runner._armed_prefetches.clear()

    @pytest.mark.asyncio
    async def test_allowance_of_one_makes_room_before_the_second_spawn(self, monkeypatch):
        """Tight host: the second slot's pre-warm evicts the first BEFORE its
        own handshake, so the live population never exceeds one — not even
        for the duration of the spawn."""
        monkeypatch.setattr(chat_runner, "_prewarm_allowance", lambda: 1)
        shared_sessions = MagicMock()
        shared_sessions.release = MagicMock()
        shared_sessions.reset = AsyncMock()
        shared_sessions.remove = AsyncMock()
        shared_sessions.remove_if_unclaimed = AsyncMock(return_value=True)
        population_at_spawn: list[int] = []

        async def _get_or_create(key, **_kwargs):
            population_at_spawn.append(len(chat_runner._armed_prefetches))
            return (MagicMock(), True, False)

        shared_sessions.get_or_create = AsyncMock(side_effect=_get_or_create)
        keys: list[str] = []
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)),
            patch.object(chat_runner, "resolve_agent_bindings", return_value=self._bindings()),
        ):
            for i in range(2):
                slot = _ChatSlot(f"t{i}")
                state = _mock_state(slot)
                state.sessions = shared_sessions
                await _eager_spawn(state, slot)
                keys.append(shared_sessions.get_or_create.await_args.args[0])
        shared_sessions.remove_if_unclaimed.assert_awaited_once_with(keys[0])
        # Room was made BEFORE the second handshake: at both spawns the
        # registry held nothing but the key being spawned -- its admission
        # reservation, which is what stops a concurrent admission passing.
        assert population_at_spawn == [1, 1]
        assert list(chat_runner._armed_prefetches) == [keys[1]]
        assert (
            chat_runner._armed_prefetches[keys[1]] is not chat_runner._RESERVED
        ), "reservation not converted"

    @pytest.mark.asyncio
    async def test_rearming_the_only_live_key_needs_no_eviction(self, monkeypatch):
        """Re-focusing the one slot that already holds the single allowed
        pre-warm must not evict it to make room for itself."""
        monkeypatch.setattr(chat_runner, "_prewarm_allowance", lambda: 1)
        sessions = MagicMock()
        sessions.remove_if_unclaimed = AsyncMock(return_value=True)
        chat_runner._armed_prefetches["dashboard:k0"] = None
        assert await chat_runner._admit_prefetch(sessions, "dashboard:k0", 1) is True
        sessions.remove_if_unclaimed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_allowance_that_shrank_mid_handshake_still_evicts_after(self):
        """The post-registration eviction takes the same allowance: three live,
        the fourth admitted against an allowance of 3 that read 1 by the time
        it registered, leaves exactly one -- the newest."""
        sessions = MagicMock()
        sessions.remove_if_unclaimed = AsyncMock(return_value=True)
        for i in range(3):
            await chat_runner._cap_armed_prefetches(sessions, f"dashboard:k{i}", cap=3)
        await chat_runner._cap_armed_prefetches(sessions, "dashboard:new", cap=1)
        assert list(chat_runner._armed_prefetches) == ["dashboard:new"]
        assert sessions.remove_if_unclaimed.await_count == 3

    @pytest.mark.asyncio
    async def test_failed_eviction_refuses_admission_and_keeps_the_entry(self):
        """Room that was not made is not room: a removal that raises leaves
        the old session live AND registered, and the new spawn is refused
        rather than admitted on top of it."""
        sessions = MagicMock()
        sessions.remove_if_unclaimed = AsyncMock(side_effect=RuntimeError("provider hung"))
        chat_runner._armed_prefetches["dashboard:old"] = None
        assert await chat_runner._admit_prefetch(sessions, "dashboard:new", 1) is False
        sessions.remove_if_unclaimed.assert_awaited_once_with("dashboard:old")
        assert list(chat_runner._armed_prefetches) == ["dashboard:old"]

    @pytest.mark.asyncio
    async def test_failed_eviction_skips_the_spawn(self, monkeypatch):
        """End to end: with one live pre-warm and an allowance of one, the
        next slot's eviction failing means no process is created for it."""
        monkeypatch.setattr(chat_runner, "_prewarm_allowance", lambda: 1)
        chat_runner._armed_prefetches["dashboard:old"] = None
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        state.sessions.remove_if_unclaimed = AsyncMock(side_effect=RuntimeError("provider hung"))
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)),
            patch.object(chat_runner, "resolve_agent_bindings", return_value=self._bindings()),
        ):
            await _eager_spawn(state, slot)
        state.sessions.get_or_create.assert_not_awaited()
        assert list(chat_runner._armed_prefetches) == ["dashboard:old"]

    @pytest.mark.asyncio
    async def test_post_registration_eviction_uses_a_fresh_allowance(self, monkeypatch):
        """The allowance is re-probed AFTER the spawn registers: admitted
        against 3 with two live, the host reads 1 by registration time, so
        both older sessions are evicted -- a cap captured before the spawn
        would have evicted nothing."""
        readings = iter([3, 1])
        monkeypatch.setattr(chat_runner, "_prewarm_allowance", lambda: next(readings))
        for k in ("dashboard:a", "dashboard:b"):
            chat_runner._armed_prefetches[k] = None
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        state.sessions.remove_if_unclaimed = AsyncMock(return_value=True)
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)),
            patch.object(chat_runner, "resolve_agent_bindings", return_value=self._bindings()),
        ):
            await _eager_spawn(state, slot)
        state.sessions.get_or_create.assert_awaited_once()
        new_key = state.sessions.get_or_create.await_args.args[0]
        evicted = [c.args[0] for c in state.sessions.remove_if_unclaimed.await_args_list]
        assert evicted == ["dashboard:a", "dashboard:b"]
        assert list(chat_runner._armed_prefetches) == [new_key]

    @pytest.mark.asyncio
    async def test_allowance_that_drops_to_zero_after_spawn_evicts_the_new_session_too(
        self, monkeypatch
    ):
        """Admitted against 3, the host reads 0 (critical band) by registration
        time: the just-spawned session is evicted along with the older one and
        the registry is left empty -- a zero allowance keeps nothing, not even
        the newest."""
        readings = iter([3, 0])
        monkeypatch.setattr(chat_runner, "_prewarm_allowance", lambda: next(readings))
        chat_runner._armed_prefetches["dashboard:old"] = None
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        state.sessions.remove_if_unclaimed = AsyncMock(return_value=True)
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)),
            patch.object(chat_runner, "resolve_agent_bindings", return_value=self._bindings()),
        ):
            await _eager_spawn(state, slot)
        state.sessions.get_or_create.assert_awaited_once()
        new_key = state.sessions.get_or_create.await_args.args[0]
        evicted = [c.args[0] for c in state.sessions.remove_if_unclaimed.await_args_list]
        assert evicted == ["dashboard:old", new_key]
        assert chat_runner._armed_prefetches == {}

    @pytest.mark.asyncio
    async def test_cap_of_zero_evicts_the_key_being_registered(self):
        """The unit shape of the same rule: ``_cap_armed_prefetches`` with a zero
        cap removes the new key itself, and a failed removal leaves it registered
        for the next attempt rather than silently dropping the accounting."""
        sessions = MagicMock()
        sessions.remove_if_unclaimed = AsyncMock(return_value=True)
        await chat_runner._cap_armed_prefetches(sessions, "dashboard:new", cap=0)
        sessions.remove_if_unclaimed.assert_awaited_once_with("dashboard:new")
        assert chat_runner._armed_prefetches == {}

        sessions.remove_if_unclaimed = AsyncMock(side_effect=RuntimeError("provider hung"))
        await chat_runner._cap_armed_prefetches(sessions, "dashboard:stuck", cap=0)
        assert list(chat_runner._armed_prefetches) == ["dashboard:stuck"]

    @pytest.mark.asyncio
    async def test_two_concurrent_admissions_against_one_allowance_admit_one(self):
        """The unit shape: admission reserves the key before returning, so the
        second admission in the same window sees the allowance spent."""
        sessions = MagicMock()
        sessions.remove_if_unclaimed = AsyncMock(return_value=True)
        results = await asyncio.gather(
            chat_runner._admit_prefetch(sessions, "dashboard:a", 1),
            chat_runner._admit_prefetch(sessions, "dashboard:b", 1),
        )
        assert results == [True, False]
        assert list(chat_runner._armed_prefetches) == ["dashboard:a"]
        assert chat_runner._armed_prefetches["dashboard:a"] is chat_runner._RESERVED
        # A reservation is not evictable: there is no process to remove yet.
        sessions.remove_if_unclaimed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_two_concurrent_slot_signals_against_one_allowance_spawn_once(self, monkeypatch):
        """End to end: two slots pre-warm at once on a host that admits one
        session. Exactly one handshake runs; the other slot is left to its
        first turn, and the registry ends holding the one that spawned."""
        monkeypatch.setattr(chat_runner, "_prewarm_allowance", lambda: 1)
        shared_sessions = MagicMock()
        shared_sessions.release = MagicMock()
        shared_sessions.reset = AsyncMock()
        shared_sessions.remove = AsyncMock()
        shared_sessions.remove_if_unclaimed = AsyncMock(return_value=True)

        async def _slow_get_or_create(key, **_kwargs):
            # flake-ok: pre-existing 10 ms handshake inside the stubbed get_or_create, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing handshake inside the stubbed get_or_create, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0.01)  # the handshake: the other signal runs meanwhile
            return (MagicMock(), True, False)

        shared_sessions.get_or_create = AsyncMock(side_effect=_slow_get_or_create)
        slots = [_ChatSlot("t0"), _ChatSlot("t1")]
        states = []
        for slot in slots:
            state = _mock_state(slot)
            state.sessions = shared_sessions
            states.append(state)
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)),
            patch.object(chat_runner, "resolve_agent_bindings", return_value=self._bindings()),
        ):
            await asyncio.gather(*(_eager_spawn(s, sl) for s, sl in zip(states, slots)))
        shared_sessions.get_or_create.assert_awaited_once()
        spawned = shared_sessions.get_or_create.await_args.args[0]
        assert list(chat_runner._armed_prefetches) == [spawned]
        assert chat_runner._armed_prefetches[spawned] is not chat_runner._RESERVED
        shared_sessions.remove_if_unclaimed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delayed_concurrent_admission_never_evicts_the_winners_registration(self):
        """The deterministic shape of the Windows flake: two signals
        arrive together, but B's admission lands only after A's whole
        handshake (the allowance probe runs in a worker thread, and Windows
        can hold it that long). B must be refused -- evicting the session A
        registered moments ago and spawning a second one is the double spawn
        the concurrent test intermittently caught."""
        sessions = MagicMock()
        sessions.remove_if_unclaimed = AsyncMock(return_value=True)
        # Both signals arrive now: they share the arm generation.
        signal_generation = chat_runner._arm_generation
        assert (
            await chat_runner._admit_prefetch(
                sessions, "dashboard:a", 1, signal_generation=signal_generation
            )
            is True
        )
        # A's handshake completes and registers, converting the reservation.
        await chat_runner._cap_armed_prefetches(sessions, "dashboard:a", cap=1)
        # B's admission was delayed past A's registration.
        assert (
            await chat_runner._admit_prefetch(
                sessions, "dashboard:b", 1, signal_generation=signal_generation
            )
            is False
        )
        sessions.remove_if_unclaimed.assert_not_awaited()
        assert list(chat_runner._armed_prefetches) == ["dashboard:a"]

    @pytest.mark.asyncio
    async def test_later_signal_still_evicts_an_older_registration(self):
        """Newest-signal-wins is intact: a signal whose generation was read
        AFTER an entry registered outranks it and takes its allowance."""
        sessions = MagicMock()
        sessions.remove_if_unclaimed = AsyncMock(return_value=True)
        await chat_runner._cap_armed_prefetches(sessions, "dashboard:a", cap=1)
        # B's signal arrives after A registered: its generation covers A.
        assert (
            await chat_runner._admit_prefetch(
                sessions, "dashboard:b", 1, signal_generation=chat_runner._arm_generation
            )
            is True
        )
        sessions.remove_if_unclaimed.assert_awaited_once_with("dashboard:a")
        assert list(chat_runner._armed_prefetches) == ["dashboard:b"]
        assert chat_runner._armed_prefetches["dashboard:b"] is chat_runner._RESERVED

    @pytest.mark.asyncio
    async def test_generation_is_snapshotted_at_schedule_time_not_first_task_step(
        self, monkeypatch
    ):
        """``create_task`` only queues ``_eager_spawn``: a registration landing
        before the queued task's first step belongs to a concurrent signal,
        and a snapshot taken inside the task would already cover it. The
        schedule call must capture the generation synchronously."""
        monkeypatch.setattr(chat_runner, "_prewarm_allowance", lambda: 1)
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        sessions = MagicMock()
        sessions.remove_if_unclaimed = AsyncMock(return_value=True)
        _prime(True)
        with patch.object(chat_runner, "resolve_agent_bindings", return_value=self._bindings()):
            task = chat_runner.schedule_eager_spawn(state, slot)
            assert task is not None
            # A concurrent signal's handshake registers BEFORE the queued
            # task's first step (no await between here and create_task).
            await chat_runner._cap_armed_prefetches(sessions, "dashboard:winner", cap=1)
            await task
        state.sessions.get_or_create.assert_not_awaited()
        assert list(chat_runner._armed_prefetches) == ["dashboard:winner"]

    @pytest.mark.asyncio
    async def test_eviction_does_not_pop_a_key_re_registered_during_the_removal(self):
        """The awaited removal is a window: a re-registration of the SAME key
        landing inside it must keep its registry entry — popping by key alone
        would erase the replacement's accounting."""
        gate = asyncio.Event()
        sessions = MagicMock()

        async def _held_remove(key):
            # flake-ok: pre-existing unbounded rendezvous inside the stubbed removal, moved verbatim; the test sets the gate before it awaits the admission
            await gate.wait()
            return True

        sessions.remove_if_unclaimed = AsyncMock(side_effect=_held_remove)
        chat_runner._armed_prefetches["dashboard:old"] = None
        admit = asyncio.create_task(
            chat_runner._admit_prefetch(
                sessions, "dashboard:b", 1, signal_generation=chat_runner._arm_generation
            )
        )
        # flake-ok: pre-existing one-turn yield that parks the admission in the held removal, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)  # admit is parked inside the awaited removal
        registrar = MagicMock()
        registrar.remove_if_unclaimed = AsyncMock(return_value=True)
        await chat_runner._cap_armed_prefetches(registrar, "dashboard:old", cap=3)
        refreshed_value = chat_runner._armed_prefetches["dashboard:old"]
        gate.set()
        # flake-ok: pre-existing task await with no timeout, moved verbatim; the gate.set() just before it releases it
        assert await admit is False
        assert chat_runner._armed_prefetches.get("dashboard:old") is refreshed_value

    @pytest.mark.asyncio
    async def test_reservation_is_released_when_the_spawn_is_refused(self, monkeypatch):
        """Every non-registering exit gives the reserved allowance back: a
        refused spawn leaves the registry empty, so the next signal is admitted."""
        monkeypatch.setattr(chat_runner, "_prewarm_allowance", lambda: 1)
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        state.sessions.get_or_create = AsyncMock(
            side_effect=chat_runner.SpeculativeResumeRefused("resumable")
        )
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)),
            patch.object(chat_runner, "resolve_agent_bindings", return_value=self._bindings()),
        ):
            await _eager_spawn(state, slot)
        state.sessions.get_or_create.assert_awaited_once()
        assert chat_runner._armed_prefetches == {}

    @pytest.mark.asyncio
    async def test_reservation_is_released_when_the_spawn_raises(self, monkeypatch):
        monkeypatch.setattr(chat_runner, "_prewarm_allowance", lambda: 1)
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        state.sessions.get_or_create = AsyncMock(side_effect=RuntimeError("spawn failed"))
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)),
            patch.object(chat_runner, "resolve_agent_bindings", return_value=self._bindings()),
        ):
            await _eager_spawn(state, slot)
        assert chat_runner._armed_prefetches == {}

    @pytest.mark.asyncio
    async def test_reservation_is_released_when_the_spawn_is_cancelled(self, monkeypatch):
        monkeypatch.setattr(chat_runner, "_prewarm_allowance", lambda: 1)
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        started = asyncio.Event()
        finish = asyncio.Event()

        async def _hang(key, **_kwargs):
            started.set()
            await await_until(finish)

        state.sessions.get_or_create = AsyncMock(side_effect=_hang)
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)),
            patch.object(chat_runner, "resolve_agent_bindings", return_value=self._bindings()),
        ):
            task = asyncio.create_task(_eager_spawn(state, slot))
            try:
                await await_until(started, what="eager spawn did not reach get_or_create")
                assert len(chat_runner._armed_prefetches) == 1, "no reservation held during spawn"
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await await_until(task)
            finally:
                task.cancel()
                await await_until(
                    asyncio.ensure_future(asyncio.gather(task, return_exceptions=True))
                )
        assert chat_runner._armed_prefetches == {}

    @pytest.mark.asyncio
    async def test_the_probe_runs_off_the_loop(self, monkeypatch):
        """``prewarm_allowance`` reads procfs; the eager task must not do that
        on the event loop."""
        import threading

        loop_thread = threading.get_ident()
        seen: list[int] = []

        def _probe():
            seen.append(threading.get_ident())
            return 3

        monkeypatch.setattr(chat_runner, "_prewarm_allowance", _probe)
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)),
            patch.object(chat_runner, "resolve_agent_bindings", return_value=self._bindings()),
        ):
            await _eager_spawn(state, slot)
        assert seen and seen[0] != loop_thread

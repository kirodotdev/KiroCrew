"""A model pick lands at the end of a channel turn, not only at a dashboard turn.

A turn a channel (Slack, Discord, Telegram, Teams) or a cron run drives passes no
dashboard turn boundary, so the picker's deferred pick waited for the next dashboard
turn while the live session kept serving the old model. ``SessionManager.release``
now tells an observer that a turn lease ended, and the dashboard lands the pick
there, LIVE ONLY: a channel turn cold-starts on the default ladder, not on
``slot.model``, so arming the reset there could not land the pick.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.acp.types import ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.llm_helpers import slot_switch_session_lock


def _live_provider(*, active_turn: bool = False):
    """An idle kiro-cli AcpProvider double whose live ``set_model`` succeeds."""
    from kiro_crew.providers.acp import AcpProvider

    provider = MagicMock(spec=AcpProvider)
    provider.capabilities = capabilities_for(ACP_BACKEND_KIRO)
    provider.has_active_turn.return_value = active_turn
    provider.available_models.return_value = [{"modelId": "auto"}]
    provider.supports_effort.return_value = False
    provider.client = MagicMock()
    provider.client.set_model = AsyncMock()
    return provider


def _slot_with_deferred_pick(state, provider, model: str = "gpt-5.6-sol"):
    state.sessions.get_provider = MagicMock(return_value=provider)
    state.get_or_create_slot("a", model=model)
    slot = state._slots["a"]
    slot._deferred_model_switch_key = effective_session_key(slot)
    state.push_slots_update = MagicMock()
    state.sessions._fold_key = lambda key: key
    state.sessions.try_acquire = AsyncMock(return_value=True)
    state.sessions.release = MagicMock()
    return slot


def _mark_running(slot) -> None:
    task = MagicMock()
    task.done.return_value = False
    slot.task = task


async def _drain_loop_callbacks() -> None:
    """Return once every callback already queued on the running loop has run.

    ``call_soon`` callbacks run in FIFO order, so a future resolved by one queued
    now resolves only after every earlier one, including the release observer's
    ``call_soon_threadsafe`` hop.
    """
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    loop.call_soon(done.set_result, None)
    await done


class TestTheReleaseObserver:
    """``SessionManager.release`` reports the end of a turn lease to its observer."""

    @staticmethod
    def _manager_holding(key: str):
        from kiro_crew.session import SessionManager

        cfg = MagicMock()
        cfg.default_agent = ""
        cfg.model = "auto"
        cfg.session.pool_size = 0
        cfg.session.pool_agent = ""
        cfg.session.pool_ttl_secs = 0
        manager = SessionManager(cfg)
        semaphore = asyncio.BoundedSemaphore(1)
        session = SimpleNamespace(
            semaphore=semaphore, turn_owner=None, last_used=0.0, adopted_parked=False
        )
        manager._sessions[key] = session
        return manager, semaphore

    @pytest.mark.asyncio
    async def test_a_released_lease_names_its_key_to_the_observer(self):
        manager, semaphore = self._manager_holding("slack:1.2")
        await semaphore.acquire()
        seen: list[str] = []
        manager.on_session_released = seen.append

        manager.release("slack:1.2")

        assert seen == ["slack:1.2"]
        assert not semaphore.locked()

    @pytest.mark.asyncio
    async def test_a_raising_observer_never_leaves_the_lease_held(self):
        manager, semaphore = self._manager_holding("slack:1.2")
        await semaphore.acquire()
        manager.on_session_released = MagicMock(side_effect=RuntimeError("boom"))

        manager.release("slack:1.2")

        assert not semaphore.locked()

    @pytest.mark.asyncio
    async def test_a_stray_release_is_not_reported(self):
        # An over-release ends no turn: the occupant was replaced under the key.
        manager, _semaphore = self._manager_holding("slack:1.2")
        seen: list[str] = []
        manager.on_session_released = seen.append

        manager.release("slack:1.2")

        assert seen == []


class TestLandingAfterAChannelTurn:
    @pytest.mark.asyncio
    async def test_an_idle_session_takes_the_pick_live(self, tmp_path):
        state = _make_state(tmp_path)
        provider = _live_provider()
        slot = _slot_with_deferred_pick(state, provider)

        await sc.land_model_pick_after_channel_turn(state, slot)

        provider.client.set_model.assert_awaited_once_with("gpt-5.6-sol")
        assert slot._deferred_model_switch_key == ""
        assert slot._pending_reset_history_key is None

    @pytest.mark.asyncio
    async def test_the_switch_runs_under_the_sessions_turn_lease(self, tmp_path):
        # MUTATION-SENSITIVE: a channel prompt starting while the switch awaits its
        # reply would read the same stdout, so the lease is held across the RPC.
        state = _make_state(tmp_path)
        provider = _live_provider()
        slot = _slot_with_deferred_pick(state, provider)
        key = slot._deferred_model_switch_key
        held: list[bool] = []

        async def _set_model(*_a, **_k):
            held.append(sc.landing_holds_lease(key))
            state.sessions.release.assert_not_called()

        provider.client.set_model = AsyncMock(side_effect=_set_model)

        await sc.land_model_pick_after_channel_turn(state, slot)

        state.sessions.try_acquire.assert_awaited_once_with(key)
        assert held == [True]
        # wake_queue: a channel message queued behind the landing is drained.
        state.sessions.release.assert_called_once_with(key, wake_queue=True)
        assert not sc.landing_holds_lease(key)

    @pytest.mark.asyncio
    async def test_a_lease_another_turn_holds_keeps_the_pick(self, tmp_path):
        state = _make_state(tmp_path)
        provider = _live_provider()
        slot = _slot_with_deferred_pick(state, provider)
        key = slot._deferred_model_switch_key
        state.sessions.try_acquire = AsyncMock(return_value=False)

        await sc.land_model_pick_after_channel_turn(state, slot)

        provider.client.set_model.assert_not_awaited()
        state.sessions.release.assert_not_called()
        assert slot._deferred_model_switch_key == key

    @pytest.mark.asyncio
    async def test_a_held_session_lock_is_left_to_its_holder(self, tmp_path):
        # A dashboard turn start holds the session lock and then waits for the
        # lease; taking the lease here first would deadlock that turn.
        state = _make_state(tmp_path)
        provider = _live_provider()
        slot = _slot_with_deferred_pick(state, provider)
        key = slot._deferred_model_switch_key

        async with slot_switch_session_lock(key):
            await asyncio.wait_for(sc.land_model_pick_after_channel_turn(state, slot), timeout=30)

        state.sessions.try_acquire.assert_not_awaited()
        assert slot._deferred_model_switch_key == key

    @pytest.mark.asyncio
    async def test_a_failed_live_switch_keeps_the_pick_and_arms_no_reset(self, tmp_path):
        # MUTATION-SENSITIVE: without live_only the helper arms the reset, and the
        # channel's next cold start would run the default ladder instead of the pick.
        state = _make_state(tmp_path)
        provider = _live_provider()
        provider.client.set_model = AsyncMock(side_effect=RuntimeError("boom"))
        slot = _slot_with_deferred_pick(state, provider)
        key = slot._deferred_model_switch_key

        await sc.land_model_pick_after_channel_turn(state, slot)

        assert slot._deferred_model_switch_key == key
        assert slot._pending_reset_history_key is None

    @pytest.mark.asyncio
    async def test_a_new_turn_already_on_the_session_keeps_the_pick(self, tmp_path):
        state = _make_state(tmp_path)
        provider = _live_provider(active_turn=True)
        slot = _slot_with_deferred_pick(state, provider)
        key = slot._deferred_model_switch_key

        await sc.land_model_pick_after_channel_turn(state, slot)

        provider.client.set_model.assert_not_awaited()
        assert slot._deferred_model_switch_key == key

    @pytest.mark.asyncio
    async def test_the_slots_own_dashboard_turn_owns_the_boundary(self, tmp_path):
        state = _make_state(tmp_path)
        provider = _live_provider()
        slot = _slot_with_deferred_pick(state, provider)
        key = slot._deferred_model_switch_key
        _mark_running(slot)

        await sc.land_model_pick_after_channel_turn(state, slot)

        provider.client.set_model.assert_not_awaited()
        assert slot._deferred_model_switch_key == key

    @pytest.mark.asyncio
    async def test_a_pick_another_boundary_already_landed_is_not_applied_twice(self, tmp_path):
        # Two boundaries can race for one pick: the one that waited for the session
        # lock finds the marker gone and does nothing.
        state = _make_state(tmp_path)
        provider = _live_provider()
        slot = _slot_with_deferred_pick(state, provider)

        async with slot_switch_session_lock(slot._deferred_model_switch_key):
            waiter = asyncio.ensure_future(sc.apply_deferred_model_switch(state, slot))
            await _drain_loop_callbacks()
            slot._deferred_model_switch_key = ""
        armed = await asyncio.wait_for(waiter, timeout=30)

        assert armed is False
        provider.client.set_model.assert_not_awaited()
        assert slot._pending_reset_history_key is None


class TestWhichSlotsWait:
    @staticmethod
    def _state(tmp_path):
        state = _make_state(tmp_path)
        state.sessions._fold_key = lambda key: key
        return state

    def test_a_deferred_pick_waits_on_its_own_session(self, tmp_path):
        state = self._state(tmp_path)
        slot = state.get_or_create_slot("a")
        slot._deferred_model_switch_key = "slack:1.2"

        assert sc.channel_pick_waits_on(state, slot, "slack:1.2") is True
        assert sc.channel_pick_waits_on(state, slot, "slack:9.9") is False

    def test_an_effort_pick_waits_for_a_dashboard_turn(self, tmp_path):
        # A live session keeps the level it started with, so only a reset lands an
        # effort, and a channel boundary never arms one.
        state = self._state(tmp_path)
        slot = state.get_or_create_slot("a")
        key = effective_session_key(slot)

        def _pick(effort):
            return sc.PendingModelPick(
                model=None,
                caller_session_key="dashboard:caller",
                caller_tab_id="tab",
                caller_fenced=False,
                pick_gen=slot._model_pick_gen,
                reasoning_effort=effort,
            )

        slot._pending_model_pick = _pick("high")
        assert sc.channel_pick_waits_on(state, slot, key) is False
        slot._pending_model_pick = _pick(None)
        assert sc.channel_pick_waits_on(state, slot, key) is True


class TestTheWiredListener:
    @pytest.mark.asyncio
    async def test_a_release_on_the_picks_session_schedules_its_landing(
        self, tmp_path, monkeypatch
    ):
        landed: list[object] = []
        monkeypatch.setattr(
            sc, "schedule_channel_pick_landing", lambda _s, slot: landed.append(slot)
        )
        state = _make_state(tmp_path)
        state.sessions._fold_key = lambda key: key
        state.wire_session_release_listener()
        slot = state.get_or_create_slot("a")
        slot._deferred_model_switch_key = "slack:1.2"

        state.sessions.on_session_released("slack:9.9")
        await _drain_loop_callbacks()
        assert landed == []

        state.sessions.on_session_released("slack:1.2")
        await _drain_loop_callbacks()
        assert landed == [slot]

    @pytest.mark.asyncio
    async def test_a_running_slot_is_left_to_its_own_turn(self, tmp_path, monkeypatch):
        landed: list[object] = []
        monkeypatch.setattr(
            sc, "schedule_channel_pick_landing", lambda _s, slot: landed.append(slot)
        )
        state = _make_state(tmp_path)
        state.sessions._fold_key = lambda key: key
        state.wire_session_release_listener()
        slot = state.get_or_create_slot("a")
        slot._deferred_model_switch_key = "slack:1.2"
        _mark_running(slot)

        state.sessions.on_session_released("slack:1.2")
        await _drain_loop_callbacks()

        assert landed == []

    @pytest.mark.asyncio
    async def test_the_landings_own_release_is_not_a_boundary(self, tmp_path, monkeypatch):
        # Otherwise a pick the live switch refused would land, release, and loop.
        landed: list[object] = []
        monkeypatch.setattr(
            sc, "schedule_channel_pick_landing", lambda _s, slot: landed.append(slot)
        )
        monkeypatch.setattr(sc, "landing_holds_lease", lambda key: key == "slack:1.2")
        state = _make_state(tmp_path)
        state.sessions._fold_key = lambda key: key
        state.wire_session_release_listener()
        slot = state.get_or_create_slot("a")
        slot._deferred_model_switch_key = "slack:1.2"

        state.sessions.on_session_released("slack:1.2")
        await _drain_loop_callbacks()

        assert landed == []

    def test_every_gateway_start_wires_the_listener(self):
        from pathlib import Path

        src = Path(__file__).parents[1] / "src" / "kiro_crew"
        for path in sorted(src.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            bind = text.count("wire_session_bind_listener()")
            release = text.count("wire_session_release_listener()")
            assert release >= bind, (
                f"{path.relative_to(src).as_posix()} starts a gateway without the "
                "release listener, so a channel turn never lands a model pick there"
            )

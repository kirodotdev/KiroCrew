"""A Default effort the slot records without the live clear must reach the kiro-cli overlay.

kiro-cli reads ``chat.modelDefaults.<model>.<key>.effort`` from the workspace
``cli.json`` at spawn. A live Default pick clears that entry (``clear_effort``). When
the pick is recorded with no live clear -- no session is running, or a turn is in
flight and the route defers -- the next spawn must not read the old level back while
every surface shows the default.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from eager_spawn_helpers import _cfg, _mock_state

from kiro_crew.acp.types import ACP_BACKEND_KIRO
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat import api_chat_slot_reasoning_effort
from kiro_crew.dashboard.chat_runner import _eager_spawn
from kiro_crew.dashboard.effort_overlay_mark import (
    effort_overlay_clear_kwargs,
    settle_effort_overlay_clear,
)
from kiro_crew.dashboard.slot_persistence import metadata_codec
from kiro_crew.dashboard.slot_persistence.metadata_values import _restore_model_fields
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history_schema import SLOT_OWNED_META_KEYS
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.acp_cli_overlay import _read_cli_overlay, _write_cli_overlay

MODEL = "claude-opus-4.7"
OTHER = "claude-sonnet-4.6"
# Through the facade, whose mirror reaches every owner module that binds the name.
_OVERLAY_CLEAR = "kiro_crew.providers.acp._clear_cli_overlay_effort"


def _app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/reasoning-effort", api_chat_slot_reasoning_effort)
    return app


def _state(slot: _ChatSlot, provider: object = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.push_slots_update = MagicMock()
    state.sessions = MagicMock()
    state.sessions.reset = AsyncMock(return_value=True)
    state.sessions.get_provider = MagicMock(return_value=provider)
    return state


def _slot(work_dir: Path, effort: str = "high") -> _ChatSlot:
    slot = _ChatSlot("test")
    slot.project = str(work_dir)
    slot.model = MODEL
    slot.reasoning_effort = effort
    return slot


def _spawn_kwargs(slot: _ChatSlot) -> dict:
    """The provider kwargs the factory builds for the slot's next spawn, as the turn calls it."""
    cfg = KiroCrewConfig()
    cfg.agent.provider = "acp"
    with patch("kiro_crew.providers.acp.AcpProvider") as built:
        built.return_value = MagicMock()
        cfg.create_provider_factory()(
            session_key=f"dashboard:{slot.key}",
            model_override=slot.model,
            cwd=slot.project,
            reasoning_effort_override=slot.reasoning_effort or None,
            **effort_overlay_clear_kwargs(slot),
        )
    return built.call_args.kwargs


def _spawned(work_dir: Path, slot: _ChatSlot) -> AcpProvider:
    """A kiro-cli provider built from what the factory hands the slot's next spawn."""
    kwargs = _spawn_kwargs(slot)
    return AcpProvider(
        acp_backend=ACP_BACKEND_KIRO,
        work_dir=work_dir,
        model=MODEL,
        effort_per_model=kwargs.get("effort_per_model"),
        clear_effort_overlay=kwargs.get("clear_effort_overlay", False),
    )


async def _spawn_overlay_steps(provider: AcpProvider) -> None:
    """What ``start()`` does to the overlay before it spawns kiro-cli."""
    await provider._clear_default_overlay_entry()
    await provider._recover_effort_overlay()
    provider._apply_effort_overlay()


async def _post(state: DashboardState, effort: str) -> tuple[int, dict]:
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/test/reasoning-effort", json={"reasoning_effort": effort}
        )
        return resp.status, await resp.json()


class TestNoLiveSession:
    @pytest.mark.asyncio
    async def test_a_default_picked_with_no_live_session_reaches_the_next_spawn(self, tmp_path):
        # The slot's last spawn wrote its level into the shared file.
        _write_cli_overlay(tmp_path, MODEL, "high")
        _write_cli_overlay(tmp_path, OTHER, "low")
        slot = _slot(tmp_path)

        status, _ = await _post(_state(slot), "")
        assert status == 200
        assert slot.reasoning_effort == ""
        assert slot._effort_overlay_clear is True

        carried = effort_overlay_clear_kwargs(slot)
        provider = _spawned(tmp_path, slot)
        await _spawn_overlay_steps(provider)

        assert provider._resolve_effort() is None
        # Only the slot's own model loses its entry.
        assert _read_cli_overlay(tmp_path) == {OTHER: "low"}
        settle_effort_overlay_clear(slot, carried, is_new=True, provider=provider)
        assert slot._effort_overlay_clear is False

    @pytest.mark.asyncio
    async def test_a_level_picked_afterwards_drops_the_mark(self, tmp_path):
        slot = _slot(tmp_path, effort="")
        slot._effort_overlay_clear = True

        status, _ = await _post(_state(slot), "max")

        assert status == 200
        assert slot._effort_overlay_clear is False
        assert effort_overlay_clear_kwargs(slot) == {}

    @pytest.mark.asyncio
    async def test_a_chat_that_never_picked_a_level_still_mirrors_the_file(self, tmp_path):
        _write_cli_overlay(tmp_path, MODEL, "high")
        slot = _slot(tmp_path, effort="")

        provider = _spawned(tmp_path, slot)
        await _spawn_overlay_steps(provider)

        assert effort_overlay_clear_kwargs(slot) == {}
        assert provider._resolve_effort() == "high"
        assert _read_cli_overlay(tmp_path) == {MODEL: "high"}


class TestResetMarkOrdering:
    @pytest.mark.asyncio
    async def test_the_default_mark_is_armed_before_reset(self, tmp_path):
        slot = _slot(tmp_path)
        state = _state(slot)
        # Recorded, not asserted, inside the reset: the route's reset helper turns a
        # raise there into a degraded-teardown warning.
        seen: list[bool] = []

        async def _observe_mark(*_args, **_kwargs):
            seen.append(slot._effort_overlay_clear)
            return True

        state.sessions.reset = AsyncMock(side_effect=_observe_mark)

        status, _ = await _post(state, "")

        assert status == 200
        assert seen == [True]
        assert slot._effort_overlay_clear is True

    @pytest.mark.asyncio
    async def test_a_pre_pop_reset_failure_restores_the_prior_mark(self, tmp_path):
        from kiro_crew.providers.base import LLMProvider

        slot = _slot(tmp_path)
        slot._effort_overlay_clear = False
        provider = MagicMock(spec=LLMProvider)
        provider.has_active_turn.return_value = False
        state = _state(slot, provider=provider)
        state.sessions.reset = AsyncMock(side_effect=RuntimeError("pre-pop boom"))

        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort", json={"reasoning_effort": ""}
            )

        assert resp.status == 500
        assert slot.reasoning_effort == "high"
        assert slot._effort_overlay_clear is False

    @pytest.mark.asyncio
    async def test_a_mark_consumed_during_reset_is_not_rearmed(self, tmp_path):
        slot = _slot(tmp_path)
        state = _state(slot)

        seen: list[bool] = []

        async def _consume_mark(*_args, **_kwargs):
            seen.append(slot._effort_overlay_clear)
            slot._effort_overlay_clear = False
            return True

        state.sessions.reset = AsyncMock(side_effect=_consume_mark)

        status, _ = await _post(state, "")

        assert status == 200
        assert seen == [True]
        assert slot._effort_overlay_clear is False


class TestTheLiveClear:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("applied_live", [True, False])
    async def test_a_live_clear_leaves_no_mark(self, tmp_path, applied_live):
        provider = MagicMock(spec=AcpProvider)
        provider.supports_effort = MagicMock(return_value=True)
        provider.has_active_turn = MagicMock(return_value=False)
        provider.clear_effort = AsyncMock(return_value=applied_live)
        slot = _slot(tmp_path)

        status, _ = await _post(_state(slot, provider=provider), "")

        assert status == 200
        provider.clear_effort.assert_awaited_once_with(every_model=True)
        assert slot._effort_overlay_clear is False

    @pytest.mark.asyncio
    async def test_a_live_clear_that_raised_marks_the_reset_successor(self, tmp_path):
        provider = MagicMock(spec=AcpProvider)
        provider.supports_effort = MagicMock(return_value=True)
        provider.has_active_turn = MagicMock(return_value=False)
        provider.switches_by_relaunch = False
        provider.clear_effort = AsyncMock(side_effect=RuntimeError("pipe closed"))
        slot = _slot(tmp_path)
        state = _state(slot, provider=provider)

        status, _ = await _post(state, "")

        assert status == 200
        state.sessions.reset.assert_awaited()
        assert slot._effort_overlay_clear is True


class TestDeferredMidTurn:
    @pytest.mark.asyncio
    async def test_a_default_deferred_mid_turn_reaches_the_next_spawn(self, tmp_path):
        _write_cli_overlay(tmp_path, MODEL, "high")
        slot = _slot(tmp_path)
        live = AcpProvider(
            acp_backend=ACP_BACKEND_KIRO,
            work_dir=tmp_path,
            model=MODEL,
            effort_per_model={MODEL: "high"},
        )
        await live._recover_effort_overlay()
        state = _state(slot, provider=live)
        send = AsyncMock()

        with (
            patch.object(AcpProvider, "has_active_turn", return_value=True),
            patch.object(live._client, "send_command", send),
        ):
            status, data = await _post(state, "")
        assert status == 200
        assert data.get("deferred") is True
        assert slot.reasoning_effort == ""
        # Nothing went to the running process, and no spawn owes a clear.
        send.assert_not_awaited()
        assert slot._effort_overlay_clear is False
        state.sessions.record_effort_override.assert_called_once_with("dashboard:test", "")

        # The same provider respawning (resume, crash restart) keeps its own map.
        live._apply_effort_overlay()
        assert live._resolve_effort() is None
        assert MODEL not in _read_cli_overlay(tmp_path)

        # A reset successor cold-starts from the slot's recorded Default.
        successor = _spawned(tmp_path, slot)
        await _spawn_overlay_steps(successor)
        assert successor._resolve_effort() is None
        assert MODEL not in _read_cli_overlay(tmp_path)

    @pytest.mark.asyncio
    async def test_a_busy_overlay_refuses_the_deferred_default_and_changes_nothing(self, tmp_path):
        _write_cli_overlay(tmp_path, MODEL, "high")
        slot = _slot(tmp_path)
        live = AcpProvider(
            acp_backend=ACP_BACKEND_KIRO,
            work_dir=tmp_path,
            model=MODEL,
            effort_per_model={MODEL: "high"},
        )
        state = _state(slot, provider=live)

        with (
            patch.object(AcpProvider, "has_active_turn", return_value=True),
            patch(_OVERLAY_CLEAR, lambda *a, **k: False),
        ):
            status, data = await _post(state, "")

        assert status == 409
        assert data["code"] == "effort_overlay_busy"
        assert slot.reasoning_effort == "high"
        assert live._effort_per_model == {MODEL: "high"}
        state.sessions.record_effort_override.assert_not_called()


class TestTheSpawnClear:
    @pytest.mark.asyncio
    async def test_a_discarded_eager_session_keeps_the_mark(self, tmp_path):
        slot = _slot(tmp_path / "old", effort="")
        slot._effort_overlay_clear = True
        state = _mock_state(slot)
        state.context_builder = None
        provider = MagicMock(effort_overlay_clear_pending=False)

        async def _create_then_switch(*_args, **_kwargs):
            slot.project = str(tmp_path / "new")
            return provider, True, False

        state.sessions.get_or_create = AsyncMock(side_effect=_create_then_switch)
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)),
            patch.object(
                chat_runner,
                "_prewarm_allowance",
                return_value=chat_runner._RESUME_PREFETCH_MAX_LIVE,
            ),
            patch.dict(chat_runner._armed_prefetches, {}, clear=True),
        ):
            await _eager_spawn(state, slot, debounce=0)

        assert state.sessions.get_or_create.await_args.kwargs["clear_effort_overlay"] is True
        state.sessions.remove.assert_awaited_once_with("dashboard:test")
        assert slot._effort_overlay_clear is True

    @pytest.mark.asyncio
    async def test_a_kept_fresh_eager_session_drops_the_mark(self, tmp_path):
        slot = _slot(tmp_path, effort="")
        slot._effort_overlay_clear = True
        state = _mock_state(slot)
        state.context_builder = None
        provider = MagicMock(effort_overlay_clear_pending=False)
        state.sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        with (
            patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)),
            patch.object(
                chat_runner,
                "_prewarm_allowance",
                return_value=chat_runner._RESUME_PREFETCH_MAX_LIVE,
            ),
            patch.dict(chat_runner._armed_prefetches, {}, clear=True),
        ):
            await _eager_spawn(state, slot, debounce=0)

        state.sessions.remove.assert_not_awaited()
        state.sessions.release.assert_called_once_with("dashboard:test")
        assert slot._effort_overlay_clear is False

    @pytest.mark.asyncio
    async def test_a_busy_overlay_keeps_the_mark_for_the_next_start(self, tmp_path):
        _write_cli_overlay(tmp_path, MODEL, "high")
        slot = _slot(tmp_path, effort="")
        slot._effort_overlay_clear = True
        carried = effort_overlay_clear_kwargs(slot)
        provider = _spawned(tmp_path, slot)

        with patch(_OVERLAY_CLEAR, lambda *a, **k: False):
            await _spawn_overlay_steps(provider)
        # The level is not adopted, so a later start of this provider cannot write it back.
        assert provider.effort_overlay_clear_pending is True
        assert provider._resolve_effort() is None
        settle_effort_overlay_clear(slot, carried, is_new=True, provider=provider)
        assert slot._effort_overlay_clear is True

        await _spawn_overlay_steps(provider)
        assert provider.effort_overlay_clear_pending is False
        assert MODEL not in _read_cli_overlay(tmp_path)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", ["", "auto"])
    async def test_an_unpinned_session_drops_the_mark_and_leaves_the_file(self, tmp_path, model):
        _write_cli_overlay(tmp_path, MODEL, "high")
        provider = AcpProvider(
            acp_backend=ACP_BACKEND_KIRO, work_dir=tmp_path, model=model, clear_effort_overlay=True
        )

        await provider._clear_default_overlay_entry()

        assert provider.effort_overlay_clear_pending is False
        assert _read_cli_overlay(tmp_path) == {MODEL: "high"}

    def test_a_claim_of_a_running_session_keeps_the_mark(self):
        slot = _ChatSlot("test")
        slot._effort_overlay_clear = True

        settle_effort_overlay_clear(
            slot, effort_overlay_clear_kwargs(slot), is_new=False, provider=object()
        )

        assert slot._effort_overlay_clear is True

    def test_the_factory_hands_the_mark_to_the_provider(self, tmp_path):
        slot = _slot(tmp_path, effort="")
        assert _spawn_kwargs(slot).get("clear_effort_overlay") is False
        slot._effort_overlay_clear = True
        assert _spawn_kwargs(slot).get("clear_effort_overlay") is True

    def test_the_mark_bypasses_the_warm_pool(self):
        """Structural pin, as for the crew effort pin: a pooled kiro-cli child already
        read the level the mark must drop, and the arm cannot be reached without a
        real pool."""
        import inspect

        from kiro_crew import session_allocation

        src = inspect.getsource(session_allocation.SessionAllocationService._get_or_create_impl)
        assert 'extra_factory_kwargs.get("clear_effort_overlay")' in src


class TestABotSaveMarksItsChat:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("held, marked", [("high", True), ("", False)])
    async def test_an_open_chat_is_marked_only_when_it_held_a_level(
        self, monkeypatch, held, marked
    ):
        from kiro_crew.dashboard import bot_chat_follow

        monkeypatch.setattr(bot_chat_follow, "save_slot_off_loop", AsyncMock(return_value=True))
        monkeypatch.setattr(
            "kiro_crew.dashboard.session_control.arm_model_switch_reset", MagicMock()
        )
        slot = _ChatSlot("member-atlas")
        slot.reasoning_effort = held
        bot = MagicMock(model="", reasoning_effort="")

        await bot_chat_follow._clear_open(MagicMock(), slot, {"reasoning_effort"}, bot)

        assert slot.reasoning_effort == ""
        assert slot._effort_overlay_clear is marked

    @pytest.mark.asyncio
    async def test_a_closed_chat_line_is_marked_only_when_it_held_a_level(self, monkeypatch):
        from kiro_crew import members as members_mod
        from kiro_crew.dashboard import bot_chat_follow

        monkeypatch.setattr(members_mod, "member_slug", lambda name, cfg: "atlas")
        monkeypatch.setattr(
            members_mod,
            "read_dm_binding",
            lambda slug: {"member": "atlas", "slot_key": "member-atlas"},
        )
        monkeypatch.setattr(
            members_mod, "member_thread_session_alias", lambda slug, store: "member:atlas"
        )
        log = MagicMock()
        state = MagicMock(_slots={}, conversation_log=log)
        cfg = MagicMock(agents={"atlas": MagicMock()})

        await bot_chat_follow.follow_bot_save(state, "atlas", cfg, ["reasoning_effort"])

        mark_call, clear_call = log.update_metadata_if.call_args_list
        key, fields, guard = mark_call.args
        assert (key, fields) == ("member:atlas", {"effort_overlay_clear": True})
        assert guard({"reasoning_effort": "high"}) is True
        assert guard({"reasoning_effort": ""}) is False
        assert clear_call.args[1] == {"reasoning_effort": ""}


class TestTheMarkIsPersisted:
    def test_a_save_writes_the_mark_and_absence_clears_it(self):
        assert "effort_overlay_clear" in SLOT_OWNED_META_KEYS
        row = next(f for f in metadata_codec.FIELDS if f.key == "effort_overlay_clear")
        slot = _ChatSlot("test")
        folds = metadata_codec.SaveFolds(memory_mode="persistent")
        assert row.line(slot, folds) is metadata_codec.OMIT
        assert row.merge(slot, folds) is False
        slot._effort_overlay_clear = True
        assert row.line(slot, folds) is True
        assert row.merge(slot, folds) is True

    @pytest.mark.parametrize(
        "meta, restored",
        [
            pytest.param({"effort_overlay_clear": True}, True, id="a_literal_true"),
            pytest.param({"effort_overlay_clear": "yes"}, False, id="only_a_literal_true"),
            pytest.param({}, False, id="absent"),
        ],
    )
    def test_a_restart_restores_only_a_literal_true_mark(self, meta, restored):
        slot = _ChatSlot("test")

        _restore_model_fields(slot, meta, cfg=None, effort_marker=False)

        assert slot._effort_overlay_clear is restored

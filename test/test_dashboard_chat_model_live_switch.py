"""Dashboard chat: in-place ``session/set_model`` switch of a live slot and its context broadcast."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard.state import DashboardState


class TestSlotModelLiveSwitch:
    """POST /api/chat/slots/{slot}/model — prefer an in-place session/set_model
    over tearing the session down.

    A reset costs a full process-tree teardown now plus a cold start and
    transcript replay on the next message. kiro-cli's ``session/set_model``
    switches a live session instead (verified against 2.15.1: acked
    synchronously, conversation carried across the switch, sticky over later
    turns), so the reset is only a fallback.
    """

    @staticmethod
    def _app(state: DashboardState) -> web.Application:
        from kiro_crew.dashboard.chat import api_chat_slot_model

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/model", api_chat_slot_model)
        return app

    @staticmethod
    def _provider(
        *,
        claude: bool = False,
        active_turn: bool = False,
        models=("auto",),
        supports_effort: bool = False,
        change_effort: bool = True,
    ):
        """A live AcpProvider double. ``spec=`` keeps isinstance() working.

        Carries the real capability record for the backend it stands in for:
        ``capabilities_of`` requires a genuine ``SessionCapabilities``, so an
        attribute-shaped flag on a spec'd mock would claim every capability at once.
        """
        from kiro_crew.providers.acp import AcpProvider

        provider = MagicMock(spec=AcpProvider)
        provider.capabilities = capabilities_for(ACP_BACKEND_CLAUDE if claude else ACP_BACKEND_KIRO)
        provider.has_active_turn.return_value = active_turn
        provider.available_models.return_value = [{"modelId": m} for m in models]
        provider.supports_effort.return_value = supports_effort
        provider.change_effort = AsyncMock(return_value=change_effort)
        provider.clear_effort = AsyncMock(return_value=False)
        provider.client = MagicMock()
        provider.client.set_model = AsyncMock()
        return provider

    @pytest.mark.asyncio
    async def test_idle_switch_goes_live_without_reset(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        assert resp.status == 200
        assert state._slots["a"].model == "gpt-5.6-sol"
        provider.client.set_model.assert_awaited_once_with("gpt-5.6-sol")
        # The whole point: the session survives.
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_canonical_key_is_translated_to_the_acp_id(self, tmp_path):
        # slot.model is a canonical/wire value; set_model only accepts kiro ids.
        from kiro_crew import model_registry

        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.6")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 200
        sent = provider.client.set_model.await_args.args[0]
        assert sent == model_registry.to_acp_id("claude-opus-4.8")
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_active_turn_defers_the_pick_to_the_turn_boundary(self, tmp_path):
        # Awaiting a response mid-turn would race the streaming prompt loop on
        # stdout for the non-multiplexed client (same hazard the effort handler
        # documents), and a reset would tear down the in-flight turn mid-stream.
        # So a turn in flight takes NEITHER: the pick is committed on the slot,
        # the live session is left alone, and the answer says the pick is
        # deferred -- the boundary of that turn lands it. This is the contract
        # every backend's own mid-turn pick has (KAS accepts the model option
        # mid-turn, keeps the running turn on its model, runs the pick next turn).
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(active_turn=True)
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()
        slot = state._slots["a"]
        gen = slot._model_pick_gen

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})
            data = await resp.json()

        assert resp.status == 200
        assert data == {"ok": True, "model": "gpt-5.6-sol", "jev_route": False, "deferred": True}
        provider.client.set_model.assert_not_awaited()
        state.sessions.reset.assert_not_awaited()
        # Committed now: the chip shows it, a restart cold-starts on it, and
        # the explicit-pick generation protects it from the fallback restore.
        assert slot.model == "gpt-5.6-sol"
        assert slot._model_pick_gen == gen + 1
        # ...and marked for the boundary, naming the session the turn runs on.
        assert slot._deferred_model_switch_key
        assert slot._pending_reset_history_key is None
        state.push_slots_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_reset_only_mid_turn_pick_is_refused_while_subagents_attached(self, tmp_path):
        # A pick that can only land via a session reset (Auto on a Claude
        # backend: _wire_model_id has no value for it) cannot be deferred while
        # a sub-agent is attached -- the boundary never arms a reset under
        # attached children, so the pick would never land and the turn would
        # keep running on the old model despite the "applies next" notice. The
        # busy branch refuses it exactly as the idle path does, committing
        # nothing, instead of reporting a deferral that cannot happen.
        from unittest.mock import patch

        from kiro_crew.dashboard import chat_handlers

        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(claude=True, active_turn=True, models=("auto", "claude-opus-4.8"))
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()
        slot = state._slots["a"]

        async def _children(*_a, **_kw):
            return chat_handlers.web.json_response(
                {"error": "sub-agents are running", "code": "slot_subagents_running"},
                status=409,
            )

        with patch.object(chat_handlers, "_subagents_attached_response", _children):
            async with TestClient(TestServer(self._app(state))) as client:
                resp = await client.post("/api/chat/slots/a/model", json={"model": ""})
                data = await resp.json()

        assert resp.status == 409
        assert data["code"] == "slot_subagents_running"
        # Nothing committed: the selection is unchanged, no deferral marked.
        assert slot.model == "claude-opus-4.8"
        assert not slot._deferred_model_switch_key
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reset_only_mid_turn_pick_defers_when_no_subagents(self, tmp_path):
        # The same reset-only Auto pick WITHOUT attached children still defers
        # (200): the boundary arms the reset once the turn ends. The refusal is
        # scoped to the attached-child case alone, never a blanket block.
        state = _make_state(tmp_path)
        provider = self._provider(claude=True, active_turn=True, models=("auto", "claude-opus-4.8"))
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()
        slot = state._slots["a"]

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": ""})
            data = await resp.json()

        assert resp.status == 200
        assert data["deferred"] is True
        assert slot._deferred_model_switch_key

    @pytest.mark.asyncio
    async def test_same_value_pick_mid_turn_is_not_deferred(self, tmp_path):
        # Re-picking what the slot already runs changes nothing at the
        # boundary, so it takes the same-value shortcut even mid-turn.
        state = _make_state(tmp_path)
        provider = self._provider(active_turn=True)
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})
            data = await resp.json()

        assert resp.status == 200
        assert "deferred" not in data
        assert not state._slots["a"]._deferred_model_switch_key

    @pytest.mark.asyncio
    async def test_live_switch_failure_falls_back_to_reset(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        provider.client.set_model = AsyncMock(side_effect=RuntimeError("boom"))
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        # The slot still lands on the new model, via the reset path.
        assert resp.status == 200
        assert state._slots["a"].model == "gpt-5.6-sol"
        state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_auto_uses_the_advertised_auto_id(self, tmp_path):
        # Back-to-default is the most common click; kiro expresses it as a real
        # model id, so it should not cost a teardown either.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(models=("auto", "claude-opus-4.8"))
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": ""})

        assert resp.status == 200
        assert state._slots["a"].model == ""
        provider.client.set_model.assert_awaited_once_with("auto")
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_without_advertised_auto_falls_back_to_reset(self, tmp_path):
        # Never invent an id the backend did not advertise.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(models=("claude-opus-4.8",))
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": ""})

        assert resp.status == 200
        provider.client.set_model.assert_not_awaited()
        state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_claude_backend_default_falls_back_to_reset(self, tmp_path):
        # The claude backend has no "let the server choose" id.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(claude=True)
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": ""})

        assert resp.status == 200
        provider.client.set_model.assert_not_awaited()
        state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_claude_backend_switch_uses_provider_id(self, tmp_path):
        from kiro_crew import model_registry

        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(claude=True)
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.6")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 200
        sent = provider.client.set_model.await_args.args[0]
        assert sent == model_registry.to_provider_id("claude-opus-4.8", "claude_code")
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_live_session_still_routes_through_reset(self, tmp_path):
        # No session to update: the reset is an O(1) no-op teardown, but it also
        # carries _reset_slot_session's pending-wait cleanup, so keep taking it.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.sessions.get_provider = MagicMock(return_value=None)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        assert resp.status == 200
        assert state._slots["a"].model == "gpt-5.6-sol"
        state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unchanged_model_touches_nothing(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="gpt-5.6-sol")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        assert resp.status == 200
        provider.client.set_model.assert_not_awaited()
        state.sessions.reset.assert_not_awaited()

    # ── reasoning effort ─────────────────────────────────────────────
    # The kiro effort overlay is written at (re)spawn, so a cold start applies
    # the slot's level for free. An in-place switch never respawns, so it must
    # push the level itself or the new model silently runs at its own default
    # while the UI still reports the slot's level.

    @pytest.mark.asyncio
    async def test_persisted_effort_is_pushed_to_the_new_model(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(supports_effort=True)
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="claude-haiku-4.5")
        slot.reasoning_effort = "high"
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 200
        provider.change_effort.assert_awaited_once_with("high")
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_effort_push_rejected_falls_back_to_reset(self, tmp_path):
        # change_effort returning False means the level never reached the
        # session — reset so the cold start applies it via the overlay.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(supports_effort=True, change_effort=False)
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="claude-haiku-4.5")
        slot.reasoning_effort = "high"
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 200
        state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_effort_push_raising_falls_back_to_reset(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(supports_effort=True)
        provider.change_effort = AsyncMock(side_effect=RuntimeError("rejected"))
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="claude-haiku-4.5")
        slot.reasoning_effort = "high"
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 200
        state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_effort_incapable_target_keeps_the_live_switch(self, tmp_path):
        # Switching to a model with no effort selector is a persisted no-op for
        # effort — it must not cost a teardown.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(supports_effort=False)
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="claude-opus-4.8")
        slot.reasoning_effort = "high"
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-haiku-4.5"})

        assert resp.status == 200
        provider.change_effort.assert_not_awaited()
        # The level stays persisted for a later switch back to a capable model.
        assert state._slots["a"].reasoning_effort == "high"
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_effort_override_re_resolves_without_resetting(self, tmp_path):
        # With no slot override, clear_effort re-resolves any workspace default.
        # Its False return is benign here (nothing to push, nothing stale for a
        # model the user never set a level on), so it must not force a reset.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(supports_effort=True)
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="claude-haiku-4.5")
        slot.reasoning_effort = ""
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 200
        provider.clear_effort.assert_awaited_once()
        provider.change_effort.assert_not_awaited()
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unavailable_model_is_4xx_and_keeps_the_session(self, tmp_path):
        """An entitlement refusal must NOT take the reset fallback.

        The generic ``except Exception`` here treats
        every set_model failure as "the call didn't land" and recovers with a
        reset. For a model the account cannot use that recovery is wrong twice
        over — it destroys the live conversation AND cold-starts on a different
        model, while the handler still answers ok:True. The slot must also keep
        its previous model, or the picker asserts a model that was refused.
        """
        from kiro_crew.acp.client import AcpModelUnavailable

        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        provider.client.set_model = AsyncMock(
            side_effect=AcpModelUnavailable(
                "claude-opus-4.8", ["gpt-5.6-sol"], backend=ACP_BACKEND_KIRO
            )
        )
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="gpt-5.6-sol")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})
            body = await resp.json()

        assert resp.status == 400
        assert "not available on your account" in body["error"]
        # Names what the account CAN use.
        assert "gpt-5.6-sol" in body["error"]
        # The conversation survives — no reset fallback.
        state.sessions.reset.assert_not_awaited()
        # And the slot still reports the model that is actually running.
        assert state._slots["a"].model == "gpt-5.6-sol"

    @pytest.mark.asyncio
    async def test_refused_pick_rolls_back_the_pick_generation(self, tmp_path):
        """A refused pick changed nothing, so the
        explicit-pick generation must roll back with the model. Leaving the
        bump in place would make the model-fallback restore probe read the
        FAILED attempt as an explicit choice and silently abandon restoring
        the primary — the session would stay on the fallback forever with no
        card and no probe."""
        from kiro_crew.acp.client import AcpModelUnavailable

        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        provider.client.set_model = AsyncMock(
            side_effect=AcpModelUnavailable(
                "claude-opus-4.8", ["gpt-5.6-sol"], backend=ACP_BACKEND_KIRO
            )
        )
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="gpt-5.6-sol")
        state.push_slots_update = MagicMock()
        gen_before = slot._model_pick_gen

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 400
        assert slot._model_pick_gen == gen_before

    @pytest.mark.asyncio
    async def test_refused_pick_rollback_spares_a_newer_concurrent_pick(self, tmp_path):
        """Two picks arriving together
        must not roll back each other's state. The pick transaction is
        serialised per slot (_model_pick_lock), so the refused pick unwinds
        cleanly before the next pick's snapshot is taken — the surviving state
        is the later, successful pick's."""
        import asyncio

        from kiro_crew.acp.client import AcpModelUnavailable

        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        slot = state.get_or_create_slot("a", model="gpt-5.6-sol")
        state.push_slots_update = MagicMock()

        release_slow = asyncio.Event()

        async def _set_model(target):
            if target == "claude-opus-4.8":
                # The SLOW, ultimately-refused pick: park inside the locked
                # transaction, then refuse.
                # flake-ok: the refused pick parks here until the test sets release_slow, before it awaits either request
                await release_slow.wait()
                raise AcpModelUnavailable(
                    "claude-opus-4.8", ["gpt-5.6-sol"], backend=ACP_BACKEND_KIRO
                )
            return None

        provider.client.set_model = AsyncMock(side_effect=_set_model)
        state.sessions.get_provider = MagicMock(return_value=provider)
        gen_before = slot._model_pick_gen

        async with TestClient(TestServer(self._app(state))) as client:
            slow = asyncio.create_task(
                client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})
            )
            # flake-ok: the pick parks on the slot's lock and sets no signal to await
            # nosemgrep: kirocrew.test-sleep-as-barrier -- the pick parks on the slot's lock and sets no signal to await
            await asyncio.sleep(0.05)  # slow pick holds the lock, parked at its await
            fast = asyncio.create_task(
                client.post("/api/chat/slots/a/model", json={"model": "deepseek-3.2"})
            )
            # flake-ok: the pick parks on the slot's lock and sets no signal to await
            # nosemgrep: kirocrew.test-sleep-as-barrier -- the pick parks on the slot's lock and sets no signal to await
            await asyncio.sleep(0.05)  # fast pick queued on the lock
            release_slow.set()
            slow_resp = await slow
            fast_resp = await fast

        assert slow_resp.status == 400
        assert fast_resp.status == 200
        # The successful pick's state survives: the refused pick's rollback
        # unwound only its own speculative writes.
        assert slot.model == "deepseek-3.2"
        assert slot._model_pick_gen == gen_before + 1

    @pytest.mark.asyncio
    async def test_two_refused_picks_leave_the_slot_untouched(self, tmp_path):
        """Two UNAVAILABLE picks overlapping must not fail out of order: without
        serialisation the later rollback would restore
        the earlier pick's already-refused model, leaving the slot advertising
        a rejected model with a phantom pick generation. Serialised, each
        transaction unwinds itself and the slot ends exactly where it began."""
        import asyncio

        from kiro_crew.acp.client import AcpModelUnavailable

        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        slot = state.get_or_create_slot("a", model="gpt-5.6-sol")
        state.push_slots_update = MagicMock()

        release_first = asyncio.Event()
        release_second = asyncio.Event()

        async def _set_model(target):
            # Park each pick independently so the FIRST refusal can complete
            # while the SECOND is still pending — the out-of-order shape that
            # made the pre-lock CAS restore an already-refused model.
            if target == "claude-opus-4.8":
                # flake-ok: the first pick parks here until the test sets release_first, before it awaits that request
                await release_first.wait()
            else:
                # flake-ok: the second pick parks here until the test sets release_second, before it awaits that request
                await release_second.wait()
            raise AcpModelUnavailable(target, ["gpt-5.6-sol"], backend=ACP_BACKEND_KIRO)

        provider.client.set_model = AsyncMock(side_effect=_set_model)
        state.sessions.get_provider = MagicMock(return_value=provider)
        gen_before = slot._model_pick_gen

        async with TestClient(TestServer(self._app(state))) as client:
            first = asyncio.create_task(
                client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})
            )
            # flake-ok: the pick parks on the slot's lock and sets no signal to await
            # nosemgrep: kirocrew.test-sleep-as-barrier -- the pick parks on the slot's lock and sets no signal to await
            await asyncio.sleep(0.05)
            second = asyncio.create_task(
                client.post("/api/chat/slots/a/model", json={"model": "deepseek-3.2"})
            )
            # flake-ok: the pick parks on the slot's lock and sets no signal to await
            # nosemgrep: kirocrew.test-sleep-as-barrier -- the pick parks on the slot's lock and sets no signal to await
            await asyncio.sleep(0.05)
            # First refusal completes while the second pick is still pending.
            release_first.set()
            first_resp = await first
            release_second.set()
            second_resp = await second

        assert first_resp.status == 400
        assert second_resp.status == 400
        # No phantom state: the slot still reports what is actually running.
        assert slot.model == "gpt-5.6-sol"
        assert slot._model_pick_gen == gen_before

    @pytest.mark.asyncio
    async def test_successful_pick_bumps_the_pick_generation(self, tmp_path):
        """The counterpart pin: a pick that LANDS must keep its bump — that is
        what stops the fallback restore probe from overriding the choice."""
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="gpt-5.6-sol")
        state.push_slots_update = MagicMock()
        gen_before = slot._model_pick_gen

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "claude-opus-4.8"})

        assert resp.status == 200
        assert slot._model_pick_gen == gen_before + 1

    @pytest.mark.asyncio
    async def test_same_value_pick_still_bumps_the_pick_generation(self, tmp_path):
        """Picking the model the slot already
        reports takes the equality early-return, but it is still an EXPLICIT
        affirmation — without the bump, a user who deliberately re-picks the
        very model the session fell back to would have that choice silently
        overridden by the next fallback restore probe."""
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="gpt-5.6-sol")
        state.push_slots_update = MagicMock()
        gen_before = slot._model_pick_gen

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        assert resp.status == 200
        assert slot._model_pick_gen == gen_before + 1
        # Same-value: no switch, no reset.
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_same_value_pick_during_fallback_takes_the_live_switch_path(self, tmp_path):
        """With a fallback actively
        serving the session, a pick of the DISPLAYED primary equals slot.model
        but the wire model is the fallback — the equality early-return would
        record the pick and switch nothing, stranding the session on the
        fallback while usage is attributed to the primary. The live-switch
        path must run instead."""
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider()
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="gpt-5.6-sol")
        slot._active_fallback_model = "fallback-model"
        state.push_slots_update = MagicMock()
        gen_before = slot._model_pick_gen

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        assert resp.status == 200
        # The pick is recorded AND the switch actually went to the wire.
        assert slot._model_pick_gen == gen_before + 1
        provider.client.set_model.assert_awaited_once_with("gpt-5.6-sol")


class TestSlotModelSwitchContextBroadcast:
    """POST /api/chat/slots/{slot}/model — one ``context_usage`` event per
    switch so the meter updates immediately.

    Regression: nothing broadcast on a model switch, so the frontend's stored
    ``slotContextTokens`` kept the OLD model's {used, window} until the next
    turn. The event carries ``reset: true`` so the reducer may replace or
    delete the stored entry (per-turn events never delete).
    """

    _app = staticmethod(TestSlotModelLiveSwitch._app)
    _provider = staticmethod(TestSlotModelLiveSwitch._provider)

    @staticmethod
    def _find_context_events(broadcast_mock):
        return [
            call.args[1]
            for call in broadcast_mock.call_args_list
            if call.args and call.args[0] == "context_usage"
        ]

    @pytest.mark.asyncio
    async def test_live_switch_broadcasts_rebased_stats(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.broadcast_ws = MagicMock()
        provider = self._provider()
        # The provider accessors report the freshly rebased stats set_model left.
        provider.context_usage_pct.return_value = 36.8
        provider.context_used_tokens.return_value = 100_000
        provider.context_window_tokens.return_value = 272_000
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        assert resp.status == 200
        events = self._find_context_events(state.broadcast_ws)
        assert events == [
            {
                "slot": state._slots["a"].key,
                "pct": 36.8,
                "used_tokens": 100_000,
                "window_tokens": 272_000,
                "reset": True,
            }
        ]

    @pytest.mark.asyncio
    async def test_live_switch_to_auto_refreshes_the_served_model_cache(self, tmp_path):
        """REGRESSION (GPT finding on 33ef1122e): ``slot.served_model`` was
        written only at spawn, so selecting Auto on a warm, preserved session
        left the concrete id the session was spawned with in the cache and the
        composer chip kept naming the OLD model as "the default". After the live
        switch lands the cache follows the provider -- ``""`` here, because
        AcpProvider collapses the ``auto`` sentinel (chip reads "auto")."""
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(models=("auto", "claude-opus-4.8"))
        provider.served_model = ""
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="claude-opus-4.8")
        slot.record_served_model("claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "auto"})

        assert resp.status == 200
        provider.client.set_model.assert_awaited_once_with("auto")
        state.sessions.reset.assert_not_awaited()
        assert slot.served_model == ""

    @pytest.mark.asyncio
    async def test_live_switch_to_a_pin_refreshes_the_served_model_cache(self, tmp_path):
        # Pin -> pin: the cache names the model the live session runs NOW.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        provider = self._provider(models=("auto", "claude-opus-4.8", "gpt-5.6-sol"))
        provider.served_model = "gpt-5.6-sol"
        state.sessions.get_provider = MagicMock(return_value=provider)
        slot = state.get_or_create_slot("a", model="claude-opus-4.8")
        slot.record_served_model("claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        assert resp.status == 200
        provider.client.set_model.assert_awaited_once_with("gpt-5.6-sol")
        assert slot.served_model == "gpt-5.6-sol"

    @pytest.mark.asyncio
    async def test_reset_fallback_broadcasts_token_clearing_event(self, tmp_path):
        # No live provider -> the reset path: no token counts, so the frontend
        # deletes its stored entry and falls back to the model-derived window.
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.broadcast_ws = MagicMock()
        state.sessions.get_provider = MagicMock(return_value=None)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        assert resp.status == 200
        state.sessions.reset.assert_awaited_once()
        events = self._find_context_events(state.broadcast_ws)
        assert events == [{"slot": state._slots["a"].key, "pct": 0.0, "reset": True}]

    @pytest.mark.asyncio
    async def test_broadcast_failure_does_not_fail_the_switch(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.broadcast_ws = MagicMock(side_effect=RuntimeError("ws down"))
        state.sessions.get_provider = MagicMock(return_value=None)
        state.get_or_create_slot("a", model="claude-opus-4.8")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "gpt-5.6-sol"})

        assert resp.status == 200
        assert state._slots["a"].model == "gpt-5.6-sol"


class TestDeferredModelSwitchAtTheBoundary:
    """``apply_deferred_model_switch``: what the turn boundary does with a pick
    the picker took mid-turn.

    Called at the end of the turn the pick arrived in and at the next turn's
    admission. It moves the session exactly as an idle pick would -- the
    harness's live switch first, the deferred reset when that is not available --
    so each backend takes the path it already takes for the picker, and it
    leaves the pick deferred while the session is still busy.
    """

    @staticmethod
    def _slot_with_deferred_pick(state, provider, model="gpt-5.6-sol"):
        from kiro_crew.dashboard.chat_utils import effective_session_key

        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model=model)
        slot = state._slots["a"]
        slot._deferred_model_switch_key = effective_session_key(slot)
        return slot

    @pytest.mark.asyncio
    async def test_nothing_deferred_is_a_no_op(self, tmp_path):
        from kiro_crew.dashboard import session_control as sc

        state = _make_state(tmp_path)
        provider = TestSlotModelLiveSwitch._provider()
        state.sessions.get_provider = MagicMock(return_value=provider)
        state.get_or_create_slot("a", model="gpt-5.6-sol")

        assert await sc.apply_deferred_model_switch(state, state._slots["a"]) is False
        provider.client.set_model.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_idle_session_is_moved_live_and_no_reset_is_armed(self, tmp_path):
        from kiro_crew.dashboard import session_control as sc

        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        provider = TestSlotModelLiveSwitch._provider()
        slot = self._slot_with_deferred_pick(state, provider)

        armed = await sc.apply_deferred_model_switch(state, slot)

        assert armed is False
        provider.client.set_model.assert_awaited_once_with("gpt-5.6-sol")
        assert slot._deferred_model_switch_key == ""
        assert slot._pending_reset_history_key is None
        # The chip's served-model refresh reaches the dashboard.
        assert state.push_slots_update.called

    @pytest.mark.asyncio
    async def test_a_rebind_during_the_rpc_keeps_the_pick_deferred(self, tmp_path):
        # The tab can relink to a live runtime WHILE the set_model RPC awaits: a
        # cron tab's ensure_cron_slot -> _bind_cron_slot sets linked_session_key
        # under none of the locks held here. The switch then reached the old
        # session, so the current binding must KEEP the pick rather than publish
        # the old provider's served model and clear the marker, which would
        # strand the new runtime on the old model with nothing armed to retry.
        from unittest.mock import patch

        import kiro_crew.dashboard.chat_handlers as ch
        from kiro_crew.dashboard import session_control as sc
        from kiro_crew.dashboard.chat_utils import effective_session_key

        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        provider = TestSlotModelLiveSwitch._provider()
        slot = self._slot_with_deferred_pick(state, provider)
        original_key = effective_session_key(slot)

        async def _rebind_mid_rpc(*_a, **_k):
            slot.linked_session_key = "cron:rebound"

        provider.client.set_model = AsyncMock(side_effect=_rebind_mid_rpc)

        with patch.object(ch, "_sync_served_model") as sync_mock:
            armed = await sc.apply_deferred_model_switch(state, slot)

        new_key = effective_session_key(slot)
        assert new_key != original_key
        # The old session was switched, but the pick is kept for the new binding
        # and no reset is armed; the old provider's served model is NOT published.
        assert armed is False
        provider.client.set_model.assert_awaited_once()
        assert slot._deferred_model_switch_key == new_key
        assert slot._pending_reset_history_key is None
        sync_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_cancel_mid_switch_keeps_the_pick_retryable(self, tmp_path):
        # The turn deadline can cancel admission while the live switch awaits,
        # before the switch lands or a reset is armed. The pick must survive so
        # a later boundary retries it; a marker cleared before the await would
        # leave the slot on the old model with nothing to recover the pick.
        import asyncio

        from kiro_crew.dashboard import session_control as sc

        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        provider = TestSlotModelLiveSwitch._provider()
        slot = self._slot_with_deferred_pick(state, provider)
        key = slot._deferred_model_switch_key
        provider.client.set_model = AsyncMock(side_effect=asyncio.CancelledError)

        with pytest.raises(asyncio.CancelledError):
            await sc.apply_deferred_model_switch(state, slot)

        # The pick is intact and retryable, and no dead reset was armed.
        assert slot._deferred_model_switch_key == key
        assert slot._pending_reset_history_key is None

    @pytest.mark.asyncio
    async def test_a_newer_pick_during_the_rpc_keeps_the_pick_deferred(self, tmp_path):
        # A newer /model pick can change slot.model WHILE the set_model RPC for
        # the earlier pick awaits. The switch reached the OLD model, so the
        # helper must keep the pick deferred for the newer model rather than
        # clear the marker and strand the session on the model it just applied.
        from unittest.mock import patch

        import kiro_crew.dashboard.chat_handlers as ch
        from kiro_crew.dashboard import session_control as sc

        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        provider = TestSlotModelLiveSwitch._provider()
        slot = self._slot_with_deferred_pick(state, provider)  # slot.model == gpt-5.6-sol
        key = slot._deferred_model_switch_key

        async def _newer_pick_mid_rpc(*_a, **_k):
            slot.model = "claude-opus-4.8"

        provider.client.set_model = AsyncMock(side_effect=_newer_pick_mid_rpc)

        with patch.object(ch, "_sync_served_model") as sync_mock:
            armed = await sc.apply_deferred_model_switch(state, slot)

        # The applied (old) model is not published; the pick stays deferred so a
        # later boundary lands the newer model. No reset was armed.
        assert armed is False
        assert slot._deferred_model_switch_key == key
        assert slot._pending_reset_history_key is None
        sync_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_it_does_not_reacquire_the_session_lock_admission_holds(self, tmp_path):
        # turn_admission calls this backstop WHILE holding
        # slot_switch_session_lock(session_key) for its cold start. That lock is
        # not reentrant, so the helper must take only _model_pick_lock; taking
        # the session lock too would hang the turn at session creation.
        import asyncio

        from kiro_crew.dashboard import session_control as sc
        from kiro_crew.llm_helpers import slot_switch_session_lock

        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        provider = TestSlotModelLiveSwitch._provider()
        slot = self._slot_with_deferred_pick(state, provider)
        key = slot._deferred_model_switch_key

        async with slot_switch_session_lock(key):
            # session_lock_held=True is the admission contract: the caller holds
            # the lock, so the helper must NOT re-take it. Would hang otherwise.
            # A ceiling, not a race: if the helper wrongly re-took the session
            # lock it holds, this await never completes, so the bound turns a
            # deadlock into a clean failure instead of a lost worker. 30 s is
            # the discipline floor for a wait_for that must stay (rule D1).
            armed = await asyncio.wait_for(
                sc.apply_deferred_model_switch(state, slot, session_lock_held=True), timeout=30
            )

        assert armed is False
        provider.client.set_model.assert_awaited_once_with("gpt-5.6-sol")
        assert slot._deferred_model_switch_key == ""

    @pytest.mark.asyncio
    async def test_a_still_busy_session_keeps_the_pick_deferred(self, tmp_path):
        # Another alias's turn (or the same session still streaming) is the
        # one case that waits: nothing is sent, nothing armed, the mark stays
        # for a later boundary.
        from kiro_crew.dashboard import session_control as sc

        state = _make_state(tmp_path)
        provider = TestSlotModelLiveSwitch._provider(active_turn=True)
        slot = self._slot_with_deferred_pick(state, provider)
        key = slot._deferred_model_switch_key

        assert await sc.apply_deferred_model_switch(state, slot) is False
        provider.client.set_model.assert_not_awaited()
        assert slot._deferred_model_switch_key == key
        assert slot._pending_reset_history_key is None

    @pytest.mark.asyncio
    async def test_a_failed_live_switch_arms_the_deferred_reset(self, tmp_path):
        from unittest.mock import patch

        from kiro_crew.dashboard import chat_handlers
        from kiro_crew.dashboard import session_control as sc

        state = _make_state(tmp_path)
        provider = TestSlotModelLiveSwitch._provider()
        provider.client.set_model = AsyncMock(side_effect=RuntimeError("boom"))
        slot = self._slot_with_deferred_pick(state, provider)

        with patch.object(chat_handlers, "_broadcast_context_reset") as bcr:
            assert await sc.apply_deferred_model_switch(state, slot) is True
        assert slot._deferred_model_switch_key == ""
        # The reset the caller's consume spends: the next turn cold-starts on
        # the committed slot.model.
        assert slot._pending_reset_history_key
        # The composer's cached context window is cleared with a None provider,
        # the same event the idle reset path emits, so the readout does not keep
        # the old model's token limit until the next turn's telemetry arrives.
        bcr.assert_called_once_with(state, slot.key, None)

    @pytest.mark.asyncio
    async def test_a_model_the_account_cannot_run_falls_to_the_reset(self, tmp_path):
        # The cold start withholds a pin the account cannot run and the slot's
        # withheld-pin card says so; the helper does not raise into the turn.
        from kiro_crew.acp.client import AcpModelUnavailable
        from kiro_crew.dashboard import session_control as sc

        state = _make_state(tmp_path)
        provider = TestSlotModelLiveSwitch._provider()
        provider.client.set_model = AsyncMock(
            side_effect=AcpModelUnavailable("gpt-5.6-sol", ["auto"], backend=ACP_BACKEND_KIRO)
        )
        slot = self._slot_with_deferred_pick(state, provider)

        assert await sc.apply_deferred_model_switch(state, slot) is True
        assert slot._pending_reset_history_key
        assert slot.model == "gpt-5.6-sol"

    @pytest.mark.asyncio
    async def test_a_non_acp_provider_takes_the_reset(self, tmp_path):
        # No live switch to try: the boundary arms the reset, the same answer
        # the idle picker gives such a provider.
        from kiro_crew.dashboard import session_control as sc
        from kiro_crew.providers.base import LLMProvider

        state = _make_state(tmp_path)
        provider = MagicMock(spec=LLMProvider)
        provider.has_active_turn.return_value = False
        slot = self._slot_with_deferred_pick(state, provider)

        assert await sc.apply_deferred_model_switch(state, slot) is True
        assert slot._pending_reset_history_key

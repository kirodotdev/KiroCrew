"""Tests for POST /api/chat/slots/{slot}/reasoning-effort endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat import api_chat_slot_reasoning_effort
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


def _make_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post(
        "/api/chat/slots/{slot}/reasoning-effort", api_chat_slot_reasoning_effort
    )
    return app


def _mock_state(slot: _ChatSlot | None = None, provider: object = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {}
    if slot:
        state._slots[slot.key] = slot
    state.push_slots_update = MagicMock()
    state.sessions = MagicMock()
    state.sessions.reset = AsyncMock()
    # No live AcpProvider by default → handler falls back to session reset
    # (matches prior behaviour for the "no session yet" path).
    state.sessions.get_provider = MagicMock(return_value=provider)
    return state


class TestChatSlotReasoningEffort:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
    async def test_set_valid_levels(self, level: str):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort",
                json={"reasoning_effort": level},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data == {"ok": True, "reasoning_effort": level}
            assert slot.reasoning_effort == level
            # No live AcpProvider → mid-session change resets the session so
            # the next cold start respawns with the new effort.
            state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_clear_to_default(self):
        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort",
                json={"reasoning_effort": ""},
            )
            assert resp.status == 200
            assert slot.reasoning_effort == ""
            state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reset_failure_keeps_committed_effort_and_reports_success(self):
        # A throwing fallback reset reports SUCCESS with a warning and the
        # new effort STAYS: the reset pops the session before shutdown can
        # fail, so the old effort's session no longer exists and every
        # replacement runs the new value. A 500 would make the acting tab
        # keep the OLD store value for a switch that actually happened.
        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"
        state = _mock_state(slot)
        state.sessions.reset = AsyncMock(side_effect=RuntimeError("shutdown blew up"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort",
                json={"reasoning_effort": "low"},
            )
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert data["reasoning_effort"] == "low"
            assert data["warning"] == "old session teardown incomplete"
            assert slot.reasoning_effort == "low"

    @pytest.mark.asyncio
    async def test_failed_reset_spares_concurrent_writes(self):
        # Commit-after-reset: the failure path touches nothing, so a value
        # written by a concurrent actor while the reset was failing survives
        # -- restoring captured priors (the old rollback shape) would
        # silently erase it.
        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"
        state = _mock_state(slot)

        async def _concurrent_lands_then_reset_fails(*args, **kwargs):
            slot.reasoning_effort = "max"
            raise RuntimeError("shutdown blew up")

        state.sessions.reset = AsyncMock(side_effect=_concurrent_lands_then_reset_fails)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort",
                json={"reasoning_effort": "low"},
            )
            assert resp.status == 200
            # The concurrent winner's value survives.
            assert slot.reasoning_effort == "max"

    @pytest.mark.asyncio
    async def test_new_effort_visible_during_reset(self):
        # A message send landing while the reset await is in flight
        # cold-starts a session from the slot's CURRENT value, so the new
        # effort must already be committed when the reset runs — otherwise
        # that session runs the old effort while the switch reports success.
        # (`reasoning_effort` has no unlocked writers, so committing before
        # the reset is safe: the failure path's rollback races nobody.)
        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"
        state = _mock_state(slot)
        seen_during_reset: list[str] = []

        async def _observe_then_succeed(*args, **kwargs):
            seen_during_reset.append(slot.reasoning_effort)
            return True

        state.sessions.reset = AsyncMock(side_effect=_observe_then_succeed)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort",
                json={"reasoning_effort": "low"},
            )
            assert resp.status == 200
            assert seen_during_reset == ["low"]
            assert slot.reasoning_effort == "low"

    @pytest.mark.asyncio
    async def test_same_target_successor_not_undone_by_failed_predecessor(self):
        # Two clients pick the SAME target; the first request's reset hangs
        # then throws while the second is already queued. Value comparison
        # alone cannot tell the successor's success from the predecessor's
        # own write, so the switch section is serialized under slot._lock:
        # the successor waits, sees the rolled-back slot, and applies the
        # switch cleanly on its own reset. Final state must be the target,
        # not snapped back to the prior value.
        import asyncio

        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"
        state = _mock_state(slot)

        first_reset_started = asyncio.Event()
        release_first_reset = asyncio.Event()
        calls = {"n": 0}

        async def _reset(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                first_reset_started.set()
                await release_first_reset.wait()
                raise RuntimeError("shutdown blew up")
            return True

        state.sessions.reset = AsyncMock(side_effect=_reset)
        async with TestClient(TestServer(_make_app(state))) as client:
            first = asyncio.create_task(client.post(
                "/api/chat/slots/test/reasoning-effort",
                json={"reasoning_effort": "low"},
            ))
            await first_reset_started.wait()
            second = asyncio.create_task(client.post(
                "/api/chat/slots/test/reasoning-effort",
                json={"reasoning_effort": "low"},
            ))
            # Let the second request reach (and block on) the slot lock, then
            # let the first request's reset fail.
            await asyncio.sleep(0.05)
            release_first_reset.set()
            resp1 = await first
            resp2 = await second
            # Both report success: the predecessor's switch committed (only
            # its old-session teardown degraded, reported via warning), and
            # the serialized successor observed the committed value and
            # correctly no-opped — one reset total, final state the target.
            assert resp1.status == 200
            assert (await resp1.json())["warning"] == "old session teardown incomplete"
            assert resp2.status == 200
            assert slot.reasoning_effort == "low"
            assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_no_op_when_unchanged_skips_session_reset(self):
        # Setting the same value twice must not reset the session
        # (avoids needless subprocess respawn on repeated UI clicks).
        slot = _ChatSlot("test")
        slot.reasoning_effort = "medium"
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort",
                json={"reasoning_effort": "medium"},
            )
            assert resp.status == 200
            state.sessions.reset.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_value",
        ["LOW", "extreme", "ultra", " low", "low ", "0", "true"],
    )
    async def test_rejects_value_outside_allowlist(self, bad_value: str):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort",
                json={"reasoning_effort": bad_value},
            )
            assert resp.status == 400
            assert slot.reasoning_effort == ""
            state.sessions.reset.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_non_string(self):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort",
                json={"reasoning_effort": 5},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unknown_slot_returns_404(self):
        state = _mock_state()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/missing/reasoning-effort",
                json={"reasoning_effort": "low"},
            )
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort",
                data="not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400


class TestChatSlotReasoningEffortLiveProvider:
    """Live-session path: effort routes through AcpProvider.change_effort
    (both backends) instead of a session reset, and is a no-op on models
    that don't support effort."""

    @pytest.mark.asyncio
    async def test_live_effort_capable_model_uses_change_effort_no_reset(self):
        from kiro_crew.providers.acp import AcpProvider

        provider = MagicMock(spec=AcpProvider)
        provider.supports_effort = MagicMock(return_value=True)
        provider.has_active_turn = MagicMock(return_value=False)
        provider.change_effort = AsyncMock(return_value=True)
        slot = _ChatSlot("test")
        state = _mock_state(slot, provider=provider)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort",
                json={"reasoning_effort": "xhigh"},
            )
            assert resp.status == 200
            assert slot.reasoning_effort == "xhigh"
            provider.change_effort.assert_awaited_once_with("xhigh")
            # Live update succeeded → no session reset.
            state.sessions.reset.assert_not_called()

    @pytest.mark.asyncio
    async def test_live_clear_applied_live_skips_reset(self):
        # clear_effort returns True only when a default was applied LIVE
        # (kiro with a workspace default) → no session reset needed.
        from kiro_crew.providers.acp import AcpProvider

        provider = MagicMock(spec=AcpProvider)
        provider.supports_effort = MagicMock(return_value=True)
        provider.has_active_turn = MagicMock(return_value=False)
        provider.clear_effort = AsyncMock(return_value=True)
        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"
        state = _mock_state(slot, provider=provider)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort",
                json={"reasoning_effort": ""},
            )
            assert resp.status == 200
            provider.clear_effort.assert_awaited_once()
            state.sessions.reset.assert_not_called()

    @pytest.mark.asyncio
    async def test_live_clear_not_applied_live_falls_back_to_reset(self):
        # clear_effort returns False (claude, or kiro with no workspace default)
        # → the running session can't be reset to default live, so the handler
        # MUST reset the session so a cold start re-resolves the true default.
        from kiro_crew.providers.acp import AcpProvider

        provider = MagicMock(spec=AcpProvider)
        provider.supports_effort = MagicMock(return_value=True)
        provider.has_active_turn = MagicMock(return_value=False)
        provider.clear_effort = AsyncMock(return_value=False)
        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"
        state = _mock_state(slot, provider=provider)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort",
                json={"reasoning_effort": ""},
            )
            assert resp.status == 200
            provider.clear_effort.assert_awaited_once()
            state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_effort_capable_model_persists_without_live_or_reset(self):
        from kiro_crew.providers.acp import AcpProvider

        provider = MagicMock(spec=AcpProvider)
        provider.supports_effort = MagicMock(return_value=False)
        provider.change_effort = AsyncMock(return_value=False)
        slot = _ChatSlot("test")
        state = _mock_state(slot, provider=provider)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort",
                json={"reasoning_effort": "high"},
            )
            assert resp.status == 200
            # Persisted on the slot for when the user switches to a capable
            # model, but neither live-applied nor session-reset.
            assert slot.reasoning_effort == "high"
            provider.change_effort.assert_not_awaited()
            state.sessions.reset.assert_not_called()

    @pytest.mark.asyncio
    async def test_live_change_failure_falls_back_to_reset(self):
        from kiro_crew.providers.acp import AcpProvider

        provider = MagicMock(spec=AcpProvider)
        provider.supports_effort = MagicMock(return_value=True)
        provider.has_active_turn = MagicMock(return_value=False)
        provider.change_effort = AsyncMock(side_effect=RuntimeError("boom"))
        slot = _ChatSlot("test")
        state = _mock_state(slot, provider=provider)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort",
                json={"reasoning_effort": "max"},
            )
            assert resp.status == 200
            state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_active_turn_defers_live_push(self):
        # A live effort change while a turn is streaming must NOT push live
        # (change_effort's response wait would race the in-flight prompt read
        # loop on the same process). The override is persisted on the slot and
        # applies on the next turn; no live push, no session reset.
        from kiro_crew.providers.acp import AcpProvider

        provider = MagicMock(spec=AcpProvider)
        provider.supports_effort = MagicMock(return_value=True)
        provider.has_active_turn = MagicMock(return_value=True)
        provider.change_effort = AsyncMock(return_value=True)
        provider.clear_effort = AsyncMock(return_value=True)
        slot = _ChatSlot("test")
        state = _mock_state(slot, provider=provider)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort",
                json={"reasoning_effort": "xhigh"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data == {"ok": True, "reasoning_effort": "xhigh", "deferred": True}
            # Persisted on the slot for the next turn.
            assert slot.reasoning_effort == "xhigh"
            # No live push and no reset while the turn is active.
            provider.change_effort.assert_not_awaited()
            provider.clear_effort.assert_not_awaited()
            state.sessions.reset.assert_not_called()

    @pytest.mark.asyncio
    async def test_active_turn_defers_clear_too(self):
        # Clearing to default while a turn is active is likewise deferred.
        from kiro_crew.providers.acp import AcpProvider

        provider = MagicMock(spec=AcpProvider)
        provider.supports_effort = MagicMock(return_value=True)
        provider.has_active_turn = MagicMock(return_value=True)
        provider.change_effort = AsyncMock(return_value=True)
        provider.clear_effort = AsyncMock(return_value=True)
        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"
        state = _mock_state(slot, provider=provider)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort",
                json={"reasoning_effort": ""},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data == {"ok": True, "reasoning_effort": "", "deferred": True}
            assert slot.reasoning_effort == ""
            provider.change_effort.assert_not_awaited()
            provider.clear_effort.assert_not_awaited()
            state.sessions.reset.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_active_turn_pushes_live(self):
        # Contrast: with no active turn the handler pushes change_effort live.
        from kiro_crew.providers.acp import AcpProvider

        provider = MagicMock(spec=AcpProvider)
        provider.supports_effort = MagicMock(return_value=True)
        provider.has_active_turn = MagicMock(return_value=False)
        provider.change_effort = AsyncMock(return_value=True)
        slot = _ChatSlot("test")
        state = _mock_state(slot, provider=provider)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort",
                json={"reasoning_effort": "high"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert "deferred" not in data
            provider.change_effort.assert_awaited_once_with("high")
            state.sessions.reset.assert_not_called()


class TestValidateReasoningEffortPersistence:
    """Persistence-layer allowlist guard prevents subprocess arg injection
    via tampered metadata (per review-bot security-controls finding)."""

    @pytest.mark.parametrize("level", ["", "low", "medium", "high", "xhigh", "max"])
    def test_passes_through_allowlisted(self, level: str):
        from kiro_crew.dashboard.chat_persistence import _validate_reasoning_effort
        assert _validate_reasoning_effort(level) == level

    @pytest.mark.parametrize(
        "tampered",
        ["LOW", "; rm -rf /", "max --evil-flag", "../../../etc", "extreme", " low"],
    )
    def test_discards_disallowed(self, tampered: str):
        from kiro_crew.dashboard.chat_persistence import _validate_reasoning_effort
        assert _validate_reasoning_effort(tampered) == ""

    def test_discards_non_string(self):
        from kiro_crew.dashboard.chat_persistence import _validate_reasoning_effort
        assert _validate_reasoning_effort(5) == ""
        assert _validate_reasoning_effort(None) == ""
        assert _validate_reasoning_effort(["max"]) == ""

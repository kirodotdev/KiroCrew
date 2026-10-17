"""Dashboard chat: approval mode and YOLO propagation across slots, bulk approve, pending approvals."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

# ── Mode/approval policy propagation (HTTP handlers) ──


class TestApiChatModePropagation:
    """api_chat_mode propagates approval policy to all session slots."""

    @pytest.mark.asyncio
    async def test_yolo_mode_propagates_auto_policy(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")
        state.get_or_create_slot("s2")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "yolo"})
            data = await resp.json()
            assert data["ok"] is True

        calls = state.sessions.set_approval_policy.call_args_list
        keys = [c.args[0] for c in calls]
        policies = [c.args[1] for c in calls]
        assert "dashboard:s1" in keys
        assert "dashboard:s2" in keys
        assert all(p == "auto" for p in policies)

    @pytest.mark.asyncio
    async def test_normal_mode_clears_policy(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.safety_override import safety_override

        safety_override().activate("test")
        state = _make_state(tmp_path)
        state.enable_yolo()
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot._trust = False

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "normal"})
            data = await resp.json()
            assert data["ok"] is True

        state.sessions.set_approval_policy.assert_called_with("dashboard:s1", "")

    @pytest.mark.asyncio
    async def test_trust_mode_scoped_to_slot_channel(self, tmp_path, monkeypatch):
        """Trust with slot_key only trusts that slot's linked channel."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot._slack_channel = "ch1"

        ch1 = MagicMock(trusted=False)
        ch2 = MagicMock(trusted=False)
        mgr = MagicMock(_channels={"ch1": ch1, "ch2": ch2})
        state.channel_manager = mgr

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
            assert (await resp.json())["ok"] is True

        assert ch1.trusted is True
        ch1._save.assert_called_once()
        assert ch2.trusted is False
        ch2._save.assert_not_called()

    @pytest.mark.asyncio
    async def test_trust_mode_all_channels_when_no_slot(self, tmp_path, monkeypatch):
        """Trust without slot_key trusts all channels."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        ch = MagicMock(trusted=False)
        mgr = MagicMock(_channels={"ch1": ch})
        state.channel_manager = mgr

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust"})
            assert (await resp.json())["ok"] is True

        assert ch.trusted is True
        ch._save.assert_called_once()

    @pytest.mark.asyncio
    async def test_normal_mode_scoped_resets_only_linked_channel(self, tmp_path, monkeypatch):
        """Normal mode with slot_key should only reset that slot's linked channel."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot._slack_channel = "ch1"
        state.get_or_create_slot("s2")

        ch1 = MagicMock(trusted=True)
        ch2 = MagicMock(trusted=True)
        mgr = MagicMock(_channels={"ch1": ch1, "ch2": ch2})
        state.channel_manager = mgr

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "normal", "slot": "s1"})
            assert (await resp.json())["ok"] is True

        assert ch1.trusted is False
        ch1._save.assert_called_once()
        assert ch2.trusted is True
        ch2._save.assert_not_called()

    @pytest.mark.asyncio
    async def test_normal_mode_resets_all_channels_when_no_slot(self, tmp_path, monkeypatch):
        """Normal mode without slot_key resets all channel trust."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        ch = MagicMock(trusted=True)
        mgr = MagicMock(_channels={"ch1": ch})
        state.channel_manager = mgr

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "normal"})
            assert (await resp.json())["ok"] is True

        assert ch.trusted is False
        ch._save.assert_called_once()

    @pytest.mark.asyncio
    async def test_trust_mode_unknown_slot_returns_400(self, tmp_path, monkeypatch):
        """Trust with unknown slot_key must return 400, not trust all."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/mode", json={"mode": "trust", "slot": "nonexistent"}
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "unknown slot"

    @pytest.mark.asyncio
    async def test_normal_mode_unknown_slot_returns_400(self, tmp_path, monkeypatch):
        """Normal with unknown slot_key must return 400, not reset all."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/mode", json={"mode": "normal", "slot": "nonexistent"}
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "unknown slot"

    @pytest.mark.asyncio
    async def test_trust_slot_preserves_other_slot_trust(self, tmp_path, monkeypatch):
        """trusting slot B must not wipe trust from slot A."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        s1 = state.get_or_create_slot("s1")
        s2 = state.get_or_create_slot("s2")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
            assert s1._trust is True

            await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s2"})
            assert s2._trust is True
            assert s1._trust is True  # must survive

    @pytest.mark.asyncio
    async def test_yolo_restores_per_slot_trust(self, tmp_path, monkeypatch):
        """YOLO does not mutate per-slot trust; disabling preserves it."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        s1 = state.get_or_create_slot("s1")
        s2 = state.get_or_create_slot("s2")

        async with TestClient(TestServer(_make_app(state))) as client:
            # Set per-slot modes: s1=trust, s2=trust_reads
            await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
            await client.post("/api/chat/mode", json={"mode": "trust_reads", "slot": "s2"})
            assert s1._trust is True
            assert s2._trust_reads is True

            # YOLO overrides everything
            await client.post("/api/chat/mode", json={"mode": "yolo"})
            assert s1._trust is True  # unchanged
            assert s2._trust_reads is True  # unchanged

            # Set s1 to normal (leaving YOLO) — s2 should be untouched
            await client.post("/api/chat/mode", json={"mode": "normal", "slot": "s1"})
            assert s1._trust is False
            assert s1._trust_reads is False
            assert s2._trust_reads is True  # preserved

    def test_yolo_auto_expires_and_clears_untrusted_policies(self, tmp_path, monkeypatch):
        """YOLO expiry clears policies for untrusted slots only."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.broadcast_ws = MagicMock()
        s1 = state.get_or_create_slot("s1")
        state.get_or_create_slot("s2")
        s1._trust = True

        from unittest.mock import patch

        from kiro_crew.safety_override import safety_override

        # Wire the on_expired callback (as server.py does at startup)
        def _on_expired(source: str) -> None:
            if state.sessions is not None:
                for slot in state._slots.values():
                    if not slot._trust and not slot._trust_reads:
                        state.sessions.set_approval_policy(f"dashboard:{slot.key}", "")

        with patch("kiro_crew.safety_override.sel"):
            safety_override().activate("dashboard")
        safety_override().on_expired = _on_expired
        safety_override()._expires_at = 0  # already expired

        assert state.is_yolo_active() is False
        assert s1._trust is True  # per-slot trust survives expiry

        cleared = [
            c[0][0] for c in state.sessions.set_approval_policy.call_args_list if c[0][1] == ""
        ]
        assert "dashboard:s2" in cleared
        assert "dashboard:s1" not in cleared

    @pytest.mark.asyncio
    async def test_trust_mode_propagates_approval_policy_to_session(self, tmp_path, monkeypatch):
        """Trust mode must set session approval_policy='auto' so subagents inherit."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.slack.handler.is_yolo_mode", lambda: False)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})

        state.sessions.set_approval_policy.assert_any_call("dashboard:s1", "auto")

    @pytest.mark.asyncio
    async def test_normal_mode_resets_approval_policy(self, tmp_path, monkeypatch):
        """Normal mode must reset session approval_policy so subagents require approval."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.slack.handler.is_yolo_mode", lambda: False)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "normal", "slot": "s1"})

        state.sessions.set_approval_policy.assert_any_call("dashboard:s1", "")

    @pytest.mark.asyncio
    async def test_trust_reads_mode_resets_approval_policy(self, tmp_path, monkeypatch):
        """trust_reads must reset approval_policy (not auto-approve writes)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.slack.handler.is_yolo_mode", lambda: False)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "trust_reads", "slot": "s1"})

        state.sessions.set_approval_policy.assert_any_call("dashboard:s1", "")

    @pytest.mark.asyncio
    async def test_trust_mode_all_slots_propagates_approval_policy(self, tmp_path, monkeypatch):
        """Trust without slot_key must set approval_policy on all slots."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.slack.handler.is_yolo_mode", lambda: False)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")
        state.get_or_create_slot("s2")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "trust"})

        state.sessions.set_approval_policy.assert_any_call("dashboard:s1", "auto")
        state.sessions.set_approval_policy.assert_any_call("dashboard:s2", "auto")


class TestApiChatModeGlobalOverrideScope:
    """A slot-scoped mode change must not revoke the process-global YOLO grant.

    The grant covers every slot, so ending it on behalf of a request that named
    ONE slot changes slots the request never mentioned — the shape that let a
    per-slot `trust` setup call drop the whole gateway out of YOLO.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["trust", "trust_reads"])
    async def test_slot_scoped_trust_preserves_global_grant(self, mode, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.safety_override import safety_override

        safety_override().activate("dashboard")
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")
        state.get_or_create_slot("s2")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": mode, "slot": "s1"})
            assert (await resp.json())["ok"] is True

        assert safety_override().is_active() is True
        # The regression itself: a slot the request never named keeps auto-approving.
        state.sessions.set_approval_policy.assert_any_call("dashboard:s2", "auto")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["trust", "trust_reads"])
    async def test_global_trust_revokes_global_grant(self, mode, tmp_path, monkeypatch):
        """Without a slot the request IS global, so it may end the grant."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.safety_override import safety_override

        safety_override().activate("dashboard")
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": mode})

        assert safety_override().is_active() is False

    @pytest.mark.asyncio
    async def test_slot_scoped_normal_revokes_global_grant(self, tmp_path, monkeypatch):
        """`normal` asks for NO auto-approval, so it revokes at any scope.

        This is the dashboard picker's YOLO off-switch: the picker always names
        the slot it is attached to.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.safety_override import safety_override

        safety_override().activate("dashboard")
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "normal", "slot": "s1"})

        assert safety_override().is_active() is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["trust", "trust_reads"])
    async def test_declared_grant_is_revoked_at_any_scope(self, mode, tmp_path, monkeypatch):
        """A declared grant has no TTL, so any mode selection is its off-switch."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.safety_override import safety_override

        safety_override().activate_declared()
        assert safety_override().is_declared is True
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": mode, "slot": "s1"})

        assert safety_override().is_active() is False

    @pytest.mark.asyncio
    async def test_until_shutdown_grant_is_ad_hoc_and_protected(self, tmp_path, monkeypatch):
        """`until_shutdown` is permanent but AD HOC, so the scope rule applies.

        Classifying it by permanence instead of source would revoke the grant of
        every operator who picked "until Kiro Crew restarts".
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.safety_override import safety_override

        so = safety_override()
        so.adhoc_until_shutdown = True
        so.activate("dashboard")
        assert so.is_permanent is True
        assert so.is_declared is False
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})

        assert so.is_active() is True

    @pytest.mark.asyncio
    async def test_non_string_mode_does_not_raise(self, tmp_path, monkeypatch):
        """The membership test must answer for an unhashable body value."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": {}, "slot": "s1"})

        assert resp.status == 200  # falls through to the normal branch, as before


class TestApproveYoloPropagation:
    """api_chat_slot_approve with yolo action propagates policy to all slots."""

    @pytest.mark.asyncio
    async def test_yolo_approve_propagates_to_all_slots(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        s1 = state.get_or_create_slot("s1")
        state.get_or_create_slot("s2")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        s1._approval_futures["test"] = fut

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/approve", json={"action": "yolo"})
            data = await resp.json()
            assert data["ok"] is True

        calls = state.sessions.set_approval_policy.call_args_list
        keys = [c.args[0] for c in calls]
        assert "dashboard:s1" in keys
        assert "dashboard:s2" in keys

    @pytest.mark.asyncio
    async def test_trust_approve_propagates_to_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        slot._approval_futures["test"] = fut
        slot.messages.append(
            {
                "role": "permission",
                "content": "Running: ls",
                "cls": json.dumps(
                    {
                        "request_id": "test",
                        "full_command": "ls",
                        "trust_grantable": "1",
                    }
                ),
            }
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/approve", json={"action": "trust"})
            data = await resp.json()
            assert data["ok"] is True

        state.sessions.set_approval_policy.assert_called_with("dashboard:s1", "auto")


# ── Coverage: bulk-approve broadcasts ──


class TestBulkApproveBroadcast:
    """Trust/YOLO mode change bulk-approve must broadcast approval_resolved."""

    @pytest.mark.asyncio
    async def test_mode_yolo_broadcasts_for_pending(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("s1")
        loop = asyncio.get_running_loop()
        f1: asyncio.Future[str] = loop.create_future()
        f2: asyncio.Future[str] = loop.create_future()
        slot._approval_futures["req-1"] = f1
        slot._approval_futures["req-2"] = f2

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "yolo"})
            assert (await resp.json())["ok"] is True

        broadcast_calls = [
            c for c in state.broadcast_ws.call_args_list if c.args[0] == "approval_resolved"
        ]
        ids = {c.args[1]["id"] for c in broadcast_calls}
        assert "req-1" in ids
        assert "req-2" in ids

    @pytest.mark.asyncio
    async def test_slot_scoped_trust_leaves_other_slots_pending(self, tmp_path, monkeypatch):
        """A slot-scoped ``trust`` must NOT sweep other slots' pending approvals.

        Regression: bulk auto-approve iterated EVERY slot regardless of scope, so
        trusting one chat resolved the pending approval card in unrelated chats
        (making them LOOK approved) while their ``_trust`` flag stayed False — so
        their next tool call prompted again. Scope the sweep to the target
        session; other slots keep their pending future AND their untrusted state.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.broadcast_ws = MagicMock()
        s1 = state.get_or_create_slot("s1")
        s2 = state.get_or_create_slot("s2")
        loop = asyncio.get_running_loop()
        f1: asyncio.Future[str] = loop.create_future()
        f2: asyncio.Future[str] = loop.create_future()
        s1._approval_futures["req-1"] = f1
        s2._approval_futures["req-2"] = f2

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
            assert (await resp.json())["ok"] is True

        # Target slot: approved + broadcast + trusted.
        assert f1.done() and f1.result() == "approved"
        assert s1._trust is True
        # Other slot: still pending, never broadcast, still untrusted.
        assert not f2.done()
        assert s2._trust is False
        resolved_ids = {
            c.args[1]["id"]
            for c in state.broadcast_ws.call_args_list
            if c.args[0] == "approval_resolved"
        }
        assert "req-1" in resolved_ids
        assert "req-2" not in resolved_ids
        # Clean up the still-pending future so the loop does not warn on GC.
        f2.set_result("rejected")

    @pytest.mark.asyncio
    async def test_unscoped_trust_still_sweeps_all_slots(self, tmp_path, monkeypatch):
        """An all-slots ``trust`` (no slot named) keeps sweeping every slot."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.broadcast_ws = MagicMock()
        s1 = state.get_or_create_slot("s1")
        s2 = state.get_or_create_slot("s2")
        loop = asyncio.get_running_loop()
        f1: asyncio.Future[str] = loop.create_future()
        f2: asyncio.Future[str] = loop.create_future()
        s1._approval_futures["req-1"] = f1
        s2._approval_futures["req-2"] = f2

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust"})
            assert (await resp.json())["ok"] is True

        assert f1.done() and f2.done()
        assert s1._trust is True and s2._trust is True
        resolved_ids = {
            c.args[1]["id"]
            for c in state.broadcast_ws.call_args_list
            if c.args[0] == "approval_resolved"
        }
        assert {"req-1", "req-2"} <= resolved_ids


# ── Coverage: multi-pending approval 400 and trust auto-approve ──


class TestMultiPendingApproval:
    """Cover the 400 response when multiple approvals are pending without request_id."""

    @pytest.mark.asyncio
    async def test_multi_pending_returns_400(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        loop = asyncio.get_running_loop()
        slot._approval_futures["a1"] = loop.create_future()
        slot._approval_futures["a2"] = loop.create_future()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/approve", json={"action": "approved"})
            assert resp.status == 400
            data = await resp.json()
            assert "pending" in data
            assert set(data["pending"]) == {"a1", "a2"}

    @pytest.mark.asyncio
    async def test_approve_with_request_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        slot._approval_futures["specific"] = fut

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/approve",
                json={"action": "approved", "request_id": "specific"},
            )
            assert resp.status == 200
            assert fut.result() == "approved"

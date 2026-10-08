"""A crewmate's notifications carry who published them (``member``).

The attribution is derived by the gateway from the publishing session's key
and the slot's live DM binding, and nothing a caller sends can set it.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.messaging import api_notification_agent_push
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.members import (
    MEMBER_NAME_MAX_CHARS,
    member_identity_for_session,
    member_slot_key,
    slug_for_name,
    write_dm_binding,
)
from kiro_crew.notifications.bus import (
    NotificationBus,
    NotificationPayload,
    NotificationValidationError,
    payload_from_legacy,
)

ADA = {"slug": "ada", "name": "Ada"}


def _bind_ada() -> str:
    """Bind crewmate Ada's DM thread and return its slot key."""
    slot = member_slot_key("ada")
    write_dm_binding("ada", member="Ada", slot_key=slot)
    return slot


def _make_state(monkeypatch, tmp_path) -> DashboardState:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


class TestMemberIdentityForSession:
    def test_every_member_key_spelling_resolves_to_the_binding_name(self):
        slot = _bind_ada()
        for key in (slot, f"dashboard_{slot}", f"dashboard:{slot}"):
            assert member_identity_for_session(key) == ADA

    def test_non_member_and_empty_keys_name_no_crewmate(self):
        _bind_ada()
        for key in ("", None, "dashboard_abc123", "dashboard:ui", "cron:job-1"):
            assert member_identity_for_session(key) is None

    def test_member_key_without_a_binding_names_no_crewmate(self):
        # The key alone is not attribution: the slug is lossy and a closed or
        # never-opened thread has no binding to name the crewmate.
        assert member_identity_for_session("dashboard_member-ghost") is None

    def test_longest_valid_crew_name_is_attributed_and_accepted(self):
        name = "A" * MEMBER_NAME_MAX_CHARS
        slug = slug_for_name(name)
        slot = member_slot_key(slug)
        write_dm_binding(slug, member=name, slot_key=slot)
        member = member_identity_for_session(slot)
        assert member == {"slug": slug, "name": name}
        NotificationPayload(
            source="system", channel="system.agent", title="t", body="", member=member
        ).validate()

    def test_a_name_past_the_cap_drops_attribution_not_the_note(self):
        name = "A" * (MEMBER_NAME_MAX_CHARS + 1)
        slug = slug_for_name(name)
        slot = member_slot_key(slug)
        write_dm_binding(slug, member=name, slot_key=slot)
        assert member_identity_for_session(slot) is None


class TestPayloadMemberField:
    def _bus(self, sink):
        return NotificationBus(sink)

    def test_member_reaches_the_note(self):
        notes: list = []
        note = self._bus(notes.append).push(
            NotificationPayload(
                source="system", channel="system.agent", title="t", body="", member=dict(ADA)
            )
        )
        assert note["member"] == ADA

    @pytest.mark.parametrize(
        "bad",
        [
            "ada",
            {"slug": "ada"},
            {"slug": "ada", "name": ""},
            {"slug": "ada", "name": 7},
            {"slug": "ada", "name": "Ada", "avatar": "x"},
            {"slug": "a" * 501, "name": "Ada"},
            {"slug": "ada", "name": "A" * 501},
        ],
    )
    def test_malformed_member_rejected(self, bad):
        with pytest.raises(NotificationValidationError):
            NotificationPayload(
                source="system", channel="system.agent", title="t", body="", member=bad
            ).validate()

    def test_meta_cannot_forge_member(self):
        # An app push forwards its body's `meta`; a crewmate's face must not be
        # attachable that way.
        notes: list = []
        note = self._bus(notes.append).push(
            NotificationPayload(
                source="app:evil",
                channel="system.agent",
                title="t",
                body="",
                meta={"member": dict(ADA)},
            )
        )
        assert "member" not in note

    def test_legacy_adapter_carries_member(self):
        payload = payload_from_legacy("agent", "t", "b", member=dict(ADA))
        assert payload.member == ADA


class TestAgentPushAttribution:
    def _app(self, state) -> web.Application:
        @web.middleware
        async def _auth_marker(request, handler):
            request["internal_auth"] = True
            return await handler(request)

        app = web.Application(middlewares=[_auth_marker])
        app["state"] = state
        app.router.add_post("/api/notifications/agent", api_notification_agent_push)
        return app

    @pytest.mark.asyncio
    async def test_crewmate_session_note_carries_member(self, monkeypatch, tmp_path):
        slot = _bind_ada()
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post(
                "/api/notifications/agent",
                json={"title": "PR ready"},
                headers={"X-Session-Key": f"dashboard_{slot}"},
            )
            assert resp.status == 200
            assert (await resp.json())["note"]["member"] == ADA
        assert state._notification_log[-1]["member"] == ADA

    @pytest.mark.asyncio
    async def test_other_sessions_carry_no_member_and_body_cannot_claim_one(
        self, monkeypatch, tmp_path
    ):
        _bind_ada()
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post(
                "/api/notifications/agent",
                json={"title": "spoof", "member": dict(ADA)},
                headers={"X-Session-Key": "dashboard_abc123"},
            )
            assert resp.status == 200
            assert "member" not in (await resp.json())["note"]


class TestSendMessageBellFallback:
    @pytest.mark.asyncio
    async def test_fallback_note_attributes_the_declared_crewmate(self, monkeypatch):
        from kiro_crew.dashboard.messaging_api import proactive_send

        slot = _bind_ada()
        state = MagicMock()
        outcome = proactive_send._SendMessageOutcome()
        await proactive_send._deliver_send_message_fallback(
            state,
            {},
            outcome,
            text="hello",
            title="t",
            blocks=None,
            options=[],
            target_channel="",
            target_user="",
            thread_ts=None,
            reply_broadcast=None,
            target_session="",
            job_name=None,
            channel_target="",
            channel_type="",
            caller_session="",
            declared_session=f"dashboard_{slot}",
            is_cron_caller=False,
            send_to_slack=False,
        )
        state.notify.assert_called_once()
        assert state.notify.call_args.kwargs["member"] == ADA

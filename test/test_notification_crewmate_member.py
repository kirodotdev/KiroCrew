"""A crewmate's notifications carry who published them (``member``).

The attribution is derived by the gateway from the publishing session's key
and the slot's live DM binding, and nothing a caller sends can set it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.messaging import (
    api_notification_agent_push,
    api_send_message,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.members import (
    MEMBER_NAME_MAX_CHARS,
    attributable_session_key,
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
from kiro_crew.session_token_sig import publish_session_token, verify_session_token

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


def _marked_app(state, marks: dict) -> web.Application:
    """Both producers behind a stand-in for the auth middleware that sets *marks*.

    ``{"peer_verified": True}`` is the kernel-attested Unix-socket caller;
    ``{"app": "evil"}`` is an app caller, which over TCP loopback carries no
    attestation for the session header it declares.
    """

    @web.middleware
    async def _auth_marker(request, handler):
        request["internal_auth"] = True
        for key, value in marks.items():
            request[key] = value
        return await handler(request)

    app = web.Application(middlewares=[_auth_marker])
    app["state"] = state
    app.router.add_post("/api/notifications/agent", api_notification_agent_push)
    app.router.add_post("/api/send-message", api_send_message)
    return app


ATTESTED = {"peer_verified": True}
UNATTESTED_CALLERS = [
    pytest.param({"app": "evil"}, id="app-caller-over-tcp"),
    pytest.param({"peer_verified": True, "app": "evil"}, id="attested-app-caller"),
    pytest.param({}, id="internal-secret-only"),
]

#: A signed per-session token, as Kiro Crew's own pooled tool backends send it
#: on a TCP loopback call: it verifies back to the session it was minted for.
ADA_TOKEN = "tok-ada-17993"
OTHER_TOKEN = "tok-other-17993"


def _publish_tokens(slot: str) -> None:
    publish_session_token(ADA_TOKEN, f"dashboard_{slot}")
    publish_session_token(OTHER_TOKEN, "dashboard_abc123")
    assert verify_session_token(ADA_TOKEN) == f"dashboard_{slot}"


@pytest.fixture(autouse=True)
def _signing_root(tmp_path):
    """An isolated token mapping directory over a valid SEL trust-root key.

    Four patches, as in ``test_cron_script_identity``: the protocol shares its
    key loader with ``session_pid_sig``, so patching one view leaves the loader
    reading the real trust root.
    """
    from kiro_crew import session_pid_sig, session_token_sig

    key_path = tmp_path / "sel_hmac.key"
    key_path.write_bytes(b"\x02" * 32)
    with (
        patch.object(session_token_sig, "config_dir", return_value=tmp_path),
        patch.object(session_pid_sig, "sel_hmac_key_path", return_value=key_path),
        patch.object(session_token_sig, "sel_hmac_key_path", return_value=key_path),
        patch.object(session_pid_sig, "_sel_hmac_key_bytes", return_value=None),
    ):
        yield


TOKEN_CASES = [
    pytest.param({}, ADA_TOKEN, True, id="valid-token-for-the-key"),
    pytest.param({}, OTHER_TOKEN, False, id="token-for-another-session"),
    pytest.param({}, "forged-token", False, id="unpublished-token"),
    pytest.param({"app": "evil"}, ADA_TOKEN, False, id="app-caller-with-valid-token"),
]


class TestAgentPushAttribution:
    def _app(self, state, marks=ATTESTED) -> web.Application:
        return _marked_app(state, marks)

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

    @pytest.mark.asyncio
    @pytest.mark.parametrize("marks", UNATTESTED_CALLERS)
    async def test_an_unattested_or_app_caller_cannot_claim_a_crewmate(
        self, monkeypatch, tmp_path, marks
    ):
        slot = _bind_ada()
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(self._app(state, marks))) as client:
            resp = await client.post(
                "/api/notifications/agent",
                json={"title": "Review this"},
                headers={"X-Session-Key": f"dashboard_{slot}"},
            )
            # This route already refuses an app caller outright; either way no
            # note may carry the crewmate.
            assert resp.status in (200, 403)
            if resp.status == 200:
                assert "member" not in (await resp.json())["note"]
        assert all("member" not in note for note in state._notification_log)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("marks,token,attributed", TOKEN_CASES)
    async def test_a_signed_session_token_attributes_only_its_own_key(
        self, monkeypatch, tmp_path, marks, token, attributed
    ):
        slot = _bind_ada()
        _publish_tokens(slot)
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(self._app(state, marks))) as client:
            resp = await client.post(
                "/api/notifications/agent",
                json={"title": "Review this"},
                headers={"X-Session-Key": f"dashboard_{slot}", "X-Session-Token": token},
            )
            if resp.status == 200:
                note = (await resp.json())["note"]
                assert (note.get("member") == ADA) is attributed
            else:
                assert resp.status == 403 and not attributed
        for note in state._notification_log:
            assert ("member" in note) is attributed


class _FakeRequest:
    def __init__(self, marks: dict, key: object, token: str = ""):
        self._marks = dict(marks)
        self.headers = {"X-Session-Key": key, "X-Session-Token": token}

    def get(self, name, default=None):
        return self._marks.get(name, default)


class TestAttributableSessionKey:
    def test_an_attested_non_app_caller_keeps_its_key(self):
        req = _FakeRequest(ATTESTED, "dashboard_member-ada")
        assert attributable_session_key(req) == "dashboard_member-ada"

    def test_a_token_for_the_declared_key_keeps_it(self):
        slot = _bind_ada()
        _publish_tokens(slot)
        req = _FakeRequest({}, f"dashboard_{slot}", ADA_TOKEN)
        assert attributable_session_key(req) == f"dashboard_{slot}"

    @pytest.mark.parametrize(
        "marks",
        [{}, {"peer_verified": "yes"}, {"app": "evil"}, {"peer_verified": True, "app": "evil"}],
    )
    def test_anything_else_names_no_session(self, marks):
        assert attributable_session_key(_FakeRequest(marks, "dashboard_member-ada")) == ""


class TestSendMessageRouteAttribution:
    """The route decides attribution from the transport, not the header alone."""

    @pytest.mark.asyncio
    async def test_attested_crewmate_caller_is_attributed(self):
        slot = _bind_ada()
        state = MagicMock(slack_client=None, owner_id="")
        async with TestClient(TestServer(_marked_app(state, ATTESTED))) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "Review this"},
                headers={"X-Session-Key": f"dashboard_{slot}"},
            )
            assert resp.status == 200
        assert state.notify.call_args.kwargs["member"] == ADA

    @pytest.mark.asyncio
    @pytest.mark.parametrize("marks", UNATTESTED_CALLERS)
    async def test_an_unattested_or_app_caller_cannot_claim_a_crewmate(self, marks):
        slot = _bind_ada()
        state = MagicMock(slack_client=None, owner_id="")
        async with TestClient(TestServer(_marked_app(state, marks))) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "Review this"},
                headers={"X-Session-Key": f"dashboard_{slot}"},
            )
            assert resp.status == 200
        state.notify.assert_called_once()
        assert state.notify.call_args.kwargs["member"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("marks,token,attributed", TOKEN_CASES)
    async def test_a_signed_session_token_attributes_only_its_own_key(
        self, marks, token, attributed
    ):
        slot = _bind_ada()
        _publish_tokens(slot)
        state = MagicMock(slack_client=None, owner_id="")
        async with TestClient(TestServer(_marked_app(state, marks))) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "Review this"},
                headers={"X-Session-Key": f"dashboard_{slot}", "X-Session-Token": token},
            )
            assert resp.status == 200
        state.notify.assert_called_once()
        assert state.notify.call_args.kwargs["member"] == (ADA if attributed else None)


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
            attribution_session=f"dashboard_{slot}",
            is_cron_caller=False,
            send_to_slack=False,
        )
        state.notify.assert_called_once()
        assert state.notify.call_args.kwargs["member"] == ADA

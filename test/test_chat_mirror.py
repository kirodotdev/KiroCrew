"""Tests for the channel-neutral mirror-link / mirror-unlink endpoints (C3)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.messaging.link import ChannelLink
from kiro_crew.messaging.transport import ConfiguredChannelTarget


def _make_mirror_app(state):
    from kiro_crew.dashboard.chat_mirror import (
        api_channel_targets,
        api_chat_slot_mirror_link,
        api_chat_slot_mirror_unlink,
    )

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{name}/mirror-link", api_chat_slot_mirror_link)
    app.router.add_post("/api/chat/slots/{name}/mirror-unlink", api_chat_slot_mirror_unlink)
    app.router.add_get("/api/chat/channel-targets", api_channel_targets)
    return app


def _fake_transport(
    channel_type="telegram", proactive=True, max_message_chars=4096, session_resume=False
):
    return SimpleNamespace(
        channel_type=channel_type,
        capabilities=SimpleNamespace(
            supports_proactive_send=proactive,
            # The real TransportCapabilities always carries this; the mirror
            # backfill chunks to it instead of truncating, so the fake needs it
            # to exercise that path rather than the getattr fallback.
            max_message_chars=max_message_chars,
            supports_session_resume=session_resume,
        ),
        send_message=AsyncMock(return_value="mid-1"),
        configured_targets=MagicMock(
            return_value=[ConfiguredChannelTarget("user:123", f"{channel_type.title()} DM · 123")]
        ),
        resolve_configured_target=AsyncMock(return_value=("123", None)),
    )


def _prep(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.sessions.get_mirror_link = MagicMock(return_value=None)
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.get_or_create_slot("s1")
    state.push_slots_update = MagicMock()
    return state


class TestMirrorLink:
    @pytest.mark.asyncio
    async def test_configured_targets_are_listed(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(_fake_transport("telegram"))
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.get("/api/chat/channel-targets")
            assert resp.status == 200
            assert await resp.json() == [
                {
                    "channel_type": "telegram",
                    "target_id": "user:123",
                    "label": "Telegram DM · 123",
                    "available": True,
                    "unavailable_reason": "",
                }
            ]

    @pytest.mark.asyncio
    async def test_configured_target_is_resolved_server_side(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        state.register_channel_transport(transport)
        state.sessions.set_mirror_link = MagicMock()
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 200
        transport.resolve_configured_target.assert_awaited_once_with("user:123")
        link = state.sessions.set_mirror_link.call_args.args[1]
        assert link == ChannelLink("telegram", channel_id="123", thread_id=None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {"channel_type": "telegram", "target_id": "user:123"},
        ],
    )
    async def test_governance_deny_blocks_target_resolution_and_send(
        self, tmp_path, monkeypatch, body
    ):
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=False),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        state.register_channel_transport(transport)

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-link", json=body)
            assert resp.status == 403
            assert (await resp.json())["error"] == "channel is not permitted"

        transport.resolve_configured_target.assert_not_awaited()
        transport.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_governance_narrowing_mid_delivery_fails_closed(self, tmp_path, monkeypatch):
        # Permit the initial link + the announcement, then deny once the
        # historical context-delivery loop starts. The endpoint must fail closed:
        # return 403 and NOT persist the mirror link (regression for a denial
        # that only broke the loop and still persisted + returned 200).
        transport = _fake_transport("telegram")

        def _permits(*args, **kwargs):
            # A PREDICATE, not a call counter. The old version denied on the
            # third governance consult, which silently depended on exactly two
            # consults happening before the loop — change how many messages the
            # backfill selects, or add a pre-loop check, and the denial lands on
            # a pre-loop gate while every assertion below still passes, so the
            # test stops guarding the path it was written for.
            #
            # Keying on "has the transport already delivered?" pins the denial
            # to the first in-loop unit regardless of selection size: the
            # announcement is the only send before the loop.
            return SimpleNamespace(
                permitted=not transport.send_message.await_args_list,
                rule="",
                layer="",
                reason="",
            )

        monkeypatch.setattr("kiro_crew.platform.governance_profiles.governance_permits", _permits)
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(transport)
        state.sessions.set_mirror_link = MagicMock()
        # Give the slot history so the context-delivery loop iterates.
        slot = state.get_or_create_slot("s1")
        slot.messages.extend(
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ]
        )

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 403
            assert (await resp.json())["error"] == "channel is not permitted"

        # Non-vacuity: the announcement is sent only AFTER its own governance
        # check passes, so having sent it proves the denial came later than that
        # check — i.e. inside the context-delivery loop, which is the path under
        # test. Without this, a denial at the very first gate would produce the
        # same 403 and the same unpersisted link.
        assert transport.send_message.await_count >= 1
        state.sessions.set_mirror_link.assert_not_called()
        state = _prep(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/nope/mirror-link",
                json={"channel_type": "telegram", "conversation_id": "1"},
            )
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_missing_channel_type(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-link", json={})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_slack_rejected(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "slack", "conversation_id": "C1"},
            )
            assert resp.status == 400
            assert "slack-link" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_missing_target_id(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(_fake_transport("telegram"))
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link", json={"channel_type": "telegram"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_channel_not_connected(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)  # no transport registered
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:1"},
            )
            assert resp.status == 503

    @pytest.mark.asyncio
    async def test_non_proactive_channel_rejected(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(_fake_transport("wecom", proactive=False))
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "wecom", "target_id": "user:u1"},
            )
            assert resp.status == 400
            assert "proactive" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_link_success(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(_fake_transport("telegram"))
        state.sessions.set_mirror_link = MagicMock()
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True and data["conversation_id"] == "123"
        state.sessions.set_mirror_link.assert_called_once()
        link = state.sessions.set_mirror_link.call_args.args[1]
        assert link == ChannelLink("telegram", channel_id="123", thread_id=None)

    @pytest.mark.asyncio
    async def test_an_explicit_link_withdraws_the_automatic_mirroring_opt_out(
        self, tmp_path, monkeypatch
    ):
        """An explicit bind is explicit intent, so it clears a standing refusal.

        A channel that re-asserts its own conversation every turn (Telegram)
        declines while the flag is set. Leaving it set would make this endpoint
        write a binding the channel then refuses to honour — the user is looking
        at a link they made, and the chat stays silent.
        """
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(_fake_transport("telegram"))
        state.sessions.set_mirror_link = MagicMock()
        state.sessions.set_mirror_opt_out = MagicMock()
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 200
        state.sessions.set_mirror_opt_out.assert_called_once()
        assert state.sessions.set_mirror_opt_out.call_args.args[1] is False

    @pytest.mark.asyncio
    async def test_link_passes_thread_id(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        # thread_id now flows from the configured-target resolution, not the body.
        transport.resolve_configured_target = AsyncMock(return_value=("C", "T"))
        state.register_channel_transport(transport)
        state.sessions.set_mirror_link = MagicMock()
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:C"},
            )
            assert resp.status == 200
        link = state.sessions.set_mirror_link.call_args.args[1]
        assert link.thread_id == "T"


class TestMirrorUnlink:
    @pytest.mark.asyncio
    async def test_slot_not_found(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/nope/mirror-unlink")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_unlink_success(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        state.sessions.clear_mirror_link = MagicMock(return_value=True)
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-unlink")
            assert resp.status == 200
            assert (await resp.json())["was_linked"] is True

    @pytest.mark.asyncio
    async def test_unlink_noop(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        state.sessions.clear_mirror_link = MagicMock(return_value=False)
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-unlink")
            assert resp.status == 200
            assert (await resp.json())["was_linked"] is False

    @pytest.mark.asyncio
    async def test_unlink_names_the_dashboard_as_the_reason(self, tmp_path, monkeypatch):
        """The audit has to say which surface cleared the binding.

        A dashboard click is invisible to the bound channel, so it is the reason
        the notice exists for — an unattributed clear would land in the trail as
        ``unspecified`` and read as a path nobody threaded.
        """
        from kiro_crew.messaging.link import UNBIND_REASON_DASHBOARD_UNLINK

        state = _prep(tmp_path, monkeypatch)
        state.sessions.clear_mirror_link = MagicMock(return_value=True)
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-unlink")
            assert resp.status == 200

        assert state.sessions.clear_mirror_link.call_args.kwargs["reason"] == (
            UNBIND_REASON_DASHBOARD_UNLINK
        )

    @pytest.mark.asyncio
    async def test_link_reports_a_conversation_claimed_mid_flight(self, tmp_path, monkeypatch):
        """The genuine race: the precheck passed, then someone else claimed it.

        Reported as the same 409 conflict rather than a 500, so the client offers
        "unlink there first" instead of inviting a retry of a request that is
        behaving correctly.
        """
        from kiro_crew.session_map import ConversationOwnershipConflict

        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        state.register_channel_transport(transport)
        state.sessions.mirror_claim_blockers = MagicMock(return_value=[])
        state.sessions.set_mirror_link = MagicMock(
            side_effect=ConversationOwnershipConflict("claimed")
        )
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "conversation_occupied"


class TestMirrorReminder:
    @pytest.mark.asyncio
    async def test_existing_live_mirror_posts_reminder(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("discord")
        state.register_channel_transport(transport)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("discord", channel_id="356163505868767244")
        )

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-link")
            assert resp.status == 200
            assert await resp.json() == {
                "ok": True,
                "already_linked": True,
                "channel_type": "discord",
            }

        transport.send_message.assert_awaited_once_with(
            "356163505868767244",
            "🔗 Session linked from dashboard — continuing here.",
            thread_id=None,
        )

    @pytest.mark.asyncio
    async def test_partial_body_validates_instead_of_posting(self, tmp_path, monkeypatch):
        """A non-empty partial payload must hit field validation, not send.

        ``{"thread_id": ...}`` carries neither channel_type nor conversation_id,
        so gating reminder mode on those two fields being absent would post an
        unsolicited message to the persisted channel instead of rejecting a
        malformed link attempt.
        """
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("discord")
        state.register_channel_transport(transport)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("discord", channel_id="356163505868767244")
        )

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link", json={"thread_id": "unexpected"}
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "channel_type required"

        transport.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_object_body_is_rejected(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("discord")
        state.register_channel_transport(transport)

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-link", json=["nope"])
            assert resp.status == 400

        transport.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_utf8_body_is_400_not_500(self, tmp_path, monkeypatch):
        """A body that cannot be decoded is a client error, not a traceback."""
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("discord")
        state.register_channel_transport(transport)

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                data=b"\xff\xfe\x00bad",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

        transport.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_charset_is_400_not_500(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("discord")
        state.register_channel_transport(transport)

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                data=b'{"channel_type":"discord"}',
                headers={"Content-Type": "application/json; charset=nosuchcharset"},
            )
            assert resp.status == 400

        transport.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_chunked_partial_body_validates_instead_of_posting(self, tmp_path, monkeypatch):
        """A CHUNKED partial payload must not read as an empty body.

        A chunked request has ``content_length is None``, so branching on
        Content-Length to decide whether to read JSON treats a real body as
        empty and falls into reminder mode — posting an unsolicited message.
        """
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("discord")
        state.register_channel_transport(transport)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("discord", channel_id="356163505868767244")
        )

        async def _chunked():
            yield b'{"thread_id": "unexpected"}'

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                data=_chunked(),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "channel_type required"

        transport.send_message.assert_not_awaited()


class TestMirrorPause:
    """Tests for the mirror-pause endpoint (api_chat_slot_mirror_pause)."""

    @pytest.fixture
    def mirror_pause_app(self):
        from kiro_crew.dashboard.chat_mirror import api_chat_slot_mirror_pause

        def _build(state):
            app = web.Application()
            app["state"] = state
            app.router.add_post(
                "/api/chat/slots/{name}/mirror-pause", api_chat_slot_mirror_pause
            )
            return app

        return _build

    @pytest.mark.asyncio
    async def test_slot_not_found_returns_404(self, tmp_path, monkeypatch, mirror_pause_app):
        state = _prep(tmp_path, monkeypatch)
        async with TestClient(TestServer(mirror_pause_app(state))) as client:
            resp = await client.post("/api/chat/slots/ghost/mirror-pause", json={"paused": True})
            assert resp.status == 404
            assert (await resp.json())["code"] == "slot_not_found"

    @pytest.mark.asyncio
    async def test_pause_explicit_mirror_link(self, tmp_path, monkeypatch, mirror_pause_app):
        """Pausing an explicit mirror on a linked session returns ok."""
        state = _prep(tmp_path, monkeypatch)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123")
        )
        state.sessions.set_mirror_paused = MagicMock(return_value=False)
        async with TestClient(TestServer(mirror_pause_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-pause", json={"paused": True}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["paused"] is True
            assert data["was_paused"] is False

    @pytest.mark.asyncio
    async def test_resume_explicit_mirror_link(self, tmp_path, monkeypatch, mirror_pause_app):
        """Resuming (paused=false) an explicit mirror."""
        state = _prep(tmp_path, monkeypatch)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123")
        )
        state.sessions.set_mirror_paused = MagicMock(return_value=True)
        async with TestClient(TestServer(mirror_pause_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-pause", json={"paused": False}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["paused"] is False
            assert data["was_paused"] is True

    @pytest.mark.asyncio
    async def test_not_linked_returns_409(self, tmp_path, monkeypatch, mirror_pause_app):
        """Pausing a session with no explicit mirror returns 409."""
        state = _prep(tmp_path, monkeypatch)
        # get_mirror_link returns None → no explicit mirror, and session key is
        # not a channel key → not origin-connected either.
        async with TestClient(TestServer(mirror_pause_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-pause", json={"paused": True}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "mirror_not_linked"

    @pytest.mark.asyncio
    async def test_pause_sends_disconnect_note_when_governance_permits(
        self, tmp_path, monkeypatch, mirror_pause_app
    ):
        """The disconnect note is sent when governance allows it."""
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        state.register_channel_transport(transport)
        link = ChannelLink("telegram", channel_id="456", thread_id="t1")
        state.sessions.get_mirror_link = MagicMock(return_value=link)
        state.sessions.set_mirror_paused = MagicMock(return_value=False)

        async with TestClient(TestServer(mirror_pause_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-pause", json={"paused": True}
            )
            assert resp.status == 200

        # The disconnect note was delivered to the mirror channel.
        transport.send_message.assert_awaited_once()
        call_args = transport.send_message.await_args
        assert call_args.args[0] == "456"
        assert "Disconnected" in call_args.args[1]
        assert call_args.kwargs["thread_id"] == "t1"

    @pytest.mark.asyncio
    async def test_pause_skips_note_when_governance_denies(
        self, tmp_path, monkeypatch, mirror_pause_app
    ):
        """No disconnect note when governance denies the send."""
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=False),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        state.register_channel_transport(transport)
        link = ChannelLink("telegram", channel_id="456")
        state.sessions.get_mirror_link = MagicMock(return_value=link)
        state.sessions.set_mirror_paused = MagicMock(return_value=False)

        async with TestClient(TestServer(mirror_pause_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-pause", json={"paused": True}
            )
            assert resp.status == 200

        transport.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pause_skips_note_for_origin_disconnect(
        self, tmp_path, monkeypatch, mirror_pause_app
    ):
        """Origin disconnect must not send a note to the explicit mirror."""
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        state.register_channel_transport(transport)
        link = ChannelLink("telegram", channel_id="456")
        state.sessions.get_mirror_link = MagicMock(return_value=link)
        state.sessions.set_mirror_paused = MagicMock(return_value=False)
        # Make this an origin slot by giving it a channel session key.
        slot = state.get_or_create_slot("s1")
        slot.linked_session_key = "telegram:conv123"

        async with TestClient(TestServer(mirror_pause_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-pause", json={"paused": True, "origin": True}
            )
            assert resp.status == 200

        transport.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pause_noop_when_already_paused(
        self, tmp_path, monkeypatch, mirror_pause_app
    ):
        """No disconnect note when already paused (not a transition)."""
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        state.register_channel_transport(transport)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="456")
        )
        state.sessions.set_mirror_paused = MagicMock(return_value=True)

        async with TestClient(TestServer(mirror_pause_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-pause", json={"paused": True}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["was_paused"] is True

        # No disconnect note because it was already paused (not a transition).
        transport.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disconnect_note_delivery_failure_is_silent(
        self, tmp_path, monkeypatch, mirror_pause_app
    ):
        """Disconnect note delivery failure does not affect the response."""
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        transport.send_message = AsyncMock(side_effect=RuntimeError("network"))
        state.register_channel_transport(transport)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="456")
        )
        state.sessions.set_mirror_paused = MagicMock(return_value=False)

        async with TestClient(TestServer(mirror_pause_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-pause", json={"paused": True}
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_invalid_body_defaults_to_pause(
        self, tmp_path, monkeypatch, mirror_pause_app
    ):
        """Non-JSON or non-dict body defaults to paused=True."""
        state = _prep(tmp_path, monkeypatch)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123")
        )
        state.sessions.set_mirror_paused = MagicMock(return_value=False)
        async with TestClient(TestServer(mirror_pause_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-pause",
                data=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 200
            assert (await resp.json())["paused"] is True


class TestChannelTargetsSlackEnumeration:
    """Cover the Slack channel enumeration in api_channel_targets."""

    @pytest.mark.asyncio
    async def test_slack_channels_are_listed(self, tmp_path, monkeypatch):
        """When slack_client and owner_id are present, Slack channels appear."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_mirror.list_slack_channels",
            AsyncMock(
                return_value=[
                    {"id": "C001", "name": "general"},
                    {"id": "C002", "name": "random"},
                ]
            ),
        )
        state = _prep(tmp_path, monkeypatch)
        state.slack_client = MagicMock()
        state.owner_id = "U123"
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.get("/api/chat/channel-targets")
            assert resp.status == 200
            data = await resp.json()
            slack_targets = [t for t in data if t["channel_type"] == "slack"]
            assert len(slack_targets) == 2
            assert slack_targets[0]["target_id"] == "C001"
            assert slack_targets[0]["label"] == "Slack · general"

    @pytest.mark.asyncio
    async def test_slack_enumeration_failure_is_silent(self, tmp_path, monkeypatch):
        """Slack failure does not prevent other targets from listing."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_mirror.list_slack_channels",
            AsyncMock(side_effect=RuntimeError("slack down")),
        )
        state = _prep(tmp_path, monkeypatch)
        state.slack_client = MagicMock()
        state.owner_id = "U123"
        state.register_channel_transport(_fake_transport("telegram"))
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.get("/api/chat/channel-targets")
            assert resp.status == 200
            data = await resp.json()
            # Slack targets are absent, but telegram is listed
            assert all(t["channel_type"] != "slack" for t in data)
            assert any(t["channel_type"] == "telegram" for t in data)

    @pytest.mark.asyncio
    async def test_transport_enumeration_failure_is_silent(self, tmp_path, monkeypatch):
        """A transport that throws on configured_targets is skipped."""
        state = _prep(tmp_path, monkeypatch)
        broken_transport = _fake_transport("broken_channel")
        broken_transport.configured_targets = MagicMock(side_effect=RuntimeError("boom"))
        state.register_channel_transport(broken_transport)
        state.register_channel_transport(_fake_transport("telegram"))
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.get("/api/chat/channel-targets")
            assert resp.status == 200
            data = await resp.json()
            # The broken transport is skipped but telegram still appears
            assert any(t["channel_type"] == "telegram" for t in data)
            assert all(t["channel_type"] != "broken_channel" for t in data)

    @pytest.mark.asyncio
    async def test_slack_channel_with_empty_id_is_skipped(self, tmp_path, monkeypatch):
        """Slack channels with no id are excluded."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_mirror.list_slack_channels",
            AsyncMock(
                return_value=[
                    {"id": "", "name": "phantom"},
                    {"id": "C003", "name": "real"},
                ]
            ),
        )
        state = _prep(tmp_path, monkeypatch)
        state.slack_client = MagicMock()
        state.owner_id = "U123"
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.get("/api/chat/channel-targets")
            assert resp.status == 200
            data = await resp.json()
            slack_targets = [t for t in data if t["channel_type"] == "slack"]
            assert len(slack_targets) == 1
            assert slack_targets[0]["target_id"] == "C003"


class TestMirrorLinkEdgeCases:
    """Cover remaining edge cases in mirror-link: invalid JSON, reminder failure,
    target resolution failure, initial delivery failure, ownership conflict race."""

    @pytest.mark.asyncio
    async def test_invalid_json_body_returns_400(self, tmp_path, monkeypatch):
        """Non-JSON text body returns 400."""
        state = _prep(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                data=b"{invalid json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert "valid JSON" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_reminder_delivery_failure_returns_502(self, tmp_path, monkeypatch):
        """When reminder send fails, 502 is returned."""
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("discord")
        transport.send_message = AsyncMock(side_effect=RuntimeError("network down"))
        state.register_channel_transport(transport)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("discord", channel_id="chan1")
        )
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-link")
            assert resp.status == 502
            assert "failed to post reminder" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_reminder_mirror_not_live_returns_503(self, tmp_path, monkeypatch):
        """When mirror target cannot be resolved but link exists, 503."""
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=False),
        )
        state = _prep(tmp_path, monkeypatch)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="789")
        )
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-link")
            assert resp.status == 503
            assert "not live" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_configured_target_unavailable_returns_409(self, tmp_path, monkeypatch):
        """When resolve_configured_target returns None, 409 is returned."""
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        transport.resolve_configured_target = AsyncMock(return_value=None)
        state.register_channel_transport(transport)
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:bad"},
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "configured_target_unavailable"

    @pytest.mark.asyncio
    async def test_initial_delivery_failure_returns_502(self, tmp_path, monkeypatch):
        """When the initial link message send fails, 502 with channel_link_failed."""
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        transport.resolve_configured_target = AsyncMock(return_value=("conv1", None))
        transport.send_message = AsyncMock(side_effect=RuntimeError("timeout"))
        state.register_channel_transport(transport)
        state.sessions.mirror_claim_blockers = MagicMock(return_value=[])
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 502
            assert (await resp.json())["code"] == "channel_link_failed"

    @pytest.mark.asyncio
    async def test_ownership_conflict_race_returns_409(self, tmp_path, monkeypatch):
        """ConversationOwnershipConflict during set_mirror_link returns 409."""
        from kiro_crew.session_map import ConversationOwnershipConflict

        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        state.register_channel_transport(transport)
        state.sessions.mirror_claim_blockers = MagicMock(return_value=[])
        state.sessions.set_mirror_opt_out = MagicMock()
        state.sessions.set_mirror_link = MagicMock(
            side_effect=ConversationOwnershipConflict("race")
        )

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "conversation_occupied"

    @pytest.mark.asyncio
    async def test_occupancy_precheck_exception_degrades_open(self, tmp_path, monkeypatch):
        """An exception in mirror_claim_blockers degrades open (allows link)."""
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        state.register_channel_transport(transport)
        state.sessions.mirror_claim_blockers = MagicMock(
            side_effect=RuntimeError("accessor unavailable")
        )
        state.sessions.set_mirror_link = MagicMock()
        state.sessions.set_mirror_opt_out = MagicMock()

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 200
            assert (await resp.json())["ok"] is True

        # Link was still persisted despite the precheck exception.
        state.sessions.set_mirror_link.assert_called_once()

    @pytest.mark.asyncio
    async def test_governance_narrows_between_resolution_and_initial_send(
        self, tmp_path, monkeypatch
    ):
        """Governance denying at the send-boundary recheck returns 403.

        This is distinct from the pre-resolution denial: target resolution
        succeeds, but the recheck at the actual send boundary (line 313-316)
        finds that governance narrowed while the resolution yielded.
        """
        call_count = {"n": 0}

        def _permits(*args, **kwargs):
            call_count["n"] += 1
            # First call passes (the pre-resolution governance), second denies
            # (the send-boundary recheck).
            return SimpleNamespace(permitted=call_count["n"] <= 1)

        monkeypatch.setattr("kiro_crew.platform.governance_profiles.governance_permits", _permits)
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        state.register_channel_transport(transport)
        state.sessions.mirror_claim_blockers = MagicMock(return_value=[])
        state.sessions.set_mirror_link = MagicMock()

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "channel_not_permitted"

        # The link must NOT be persisted.
        state.sessions.set_mirror_link.assert_not_called()
        # The initial announcement must NOT have been sent either.
        transport.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_context_delivery_failure_is_silent(self, tmp_path, monkeypatch):
        """A failure during backfill delivery does not break the link."""
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        call_count = 0

        async def _flaky_send(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Succeed on the initial announcement, fail on backfill delivery.
            if call_count > 1:
                raise RuntimeError("flaky network")
            return "mid-1"

        transport.send_message = _flaky_send
        state.register_channel_transport(transport)
        state.sessions.mirror_claim_blockers = MagicMock(return_value=[])
        state.sessions.set_mirror_link = MagicMock()
        state.sessions.set_mirror_opt_out = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot.messages.extend(
            [
                {"role": "user", "content": "test msg"},
                {"role": "assistant", "content": "reply msg"},
            ]
        )

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            # Link still succeeds despite context delivery failure
            assert resp.status == 200

        state.sessions.set_mirror_link.assert_called_once()


class TestMirrorBackfillFidelity:
    """The non-Slack mirror seeds the same turn-aware history, chunked not cut.

    Deliberately asymmetric with the Slack path: this delivery stays INLINE
    because its per-message governance re-check has to be able to fail the
    request closed with 403, which a backgrounded drain could not do after the
    handler had already returned 200 and persisted the link.
    """

    # A LITERAL ceiling, deliberately NOT chat_mirror._MAX_INLINE_BACKFILL_UNITS:
    # asserting against the module constant would move with it, so raising the
    # cap — or deleting it — would still pass. This leaves headroom for a
    # deliberate tuning change while still failing an unbounded loop.
    _BOUND_CEILING = 16

    def _linked(self, tmp_path, monkeypatch, max_message_chars=4096):
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram", max_message_chars=max_message_chars)
        state.register_channel_transport(transport)
        state.sessions.set_mirror_link = MagicMock()
        return state, transport

    async def _link(self, state):
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 200

    def _sent(self, transport):
        """Delivered bodies, excluding the link announcement."""
        texts = [call.args[1] for call in transport.send_message.await_args_list]
        return [t for t in texts if "Session linked from dashboard" not in t]

    @pytest.mark.asyncio
    async def test_filter_runs_before_slice(self, tmp_path, monkeypatch):
        state, transport = self._linked(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        for role, content in [
            ("user", "why is the build red"),
            ("assistant", "a lint rule changed"),
            ("tool", "grep ..."),
            ("tool", "cat ..."),
            ("tool", "pytest ..."),
        ]:
            slot.append(role, content)
        slot.drain()

        await self._link(state)
        body = "\n".join(self._sent(transport))
        assert "why is the build red" in body
        assert "a lint rule changed" in body
        assert "grep" not in body and "pytest" not in body

    @pytest.mark.asyncio
    async def test_long_message_is_chunked_not_truncated(self, tmp_path, monkeypatch):
        state, transport = self._linked(tmp_path, monkeypatch, max_message_chars=500)
        slot = state.get_or_create_slot("s1")
        long_answer = "".join(f"[{i:04d}]" for i in range(600))  # 3600 chars
        slot.append("user", "explain")
        slot.append("assistant", long_answer)
        slot.drain()

        await self._link(state)
        sent = self._sent(transport)
        assert all(len(text) <= 500 for text in sent), "a chunk exceeded the transport limit"
        body = "".join(sent)
        for i in (0, 300, 599):
            assert f"[{i:04d}]" in body, f"marker {i} lost — content was truncated"

    @pytest.mark.asyncio
    async def test_first_turn_and_gap_marker(self, tmp_path, monkeypatch):
        state, transport = self._linked(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        for i in range(1, 11):
            slot.append("user", f"question {i}")
            slot.append("assistant", f"answer {i}")
        slot.drain()

        await self._link(state)
        sent = self._sent(transport)
        body = "\n".join(sent)
        assert "question 1" in body
        assert "question 10" in body
        assert "question 3" not in body
        markers = [t for t in sent if "earlier turn" in t]
        assert len(markers) == 1
        # Slack would report 4 skipped (10 turns, 5 recent). The inline path is
        # additionally under a delivery budget, so the oldest recent turn is
        # folded into the marker instead of being sent -- 5, not 4. That fold is
        # the point of the budget: the marker absorbs the overflow.
        assert "5 earlier turns" in markers[0]

    @pytest.mark.asyncio
    async def test_history_that_fits_exactly_is_not_trimmed(self, tmp_path, monkeypatch):
        """No gap marker when there is no gap.

        Six two-message turns is exactly the 12-unit budget. An earlier version
        reserved the marker's slot unconditionally, so the reservation pushed the
        oldest turn out and then spent that slot announcing the omission it had
        itself caused — a false gap on history that fit.
        """
        state, transport = self._linked(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        for i in range(1, 7):
            slot.append("user", f"q{i}")
            slot.append("assistant", f"a{i}")
        slot.drain()

        await self._link(state)
        sent = self._sent(transport)
        body = "\n".join(sent)
        assert not any("earlier turn" in t for t in sent), f"false gap marker: {sent}"
        for i in range(1, 7):
            assert f"q{i}" in body, f"turn {i} was trimmed even though it fit"
        assert len(sent) == 12, f"expected all 12 units, got {len(sent)}"

    @pytest.mark.asyncio
    async def test_inline_delivery_is_bounded(self, tmp_path, monkeypatch):
        """The request cannot grow without limit just because history did.

        This path is inline (its governance re-check must be able to 403), so
        every extra unit is another governance hop plus a send on a channel that
        may accept ~1 msg/s. Long history must not push the request past a
        browser fetch timeout.
        """

        state, transport = self._linked(tmp_path, monkeypatch, max_message_chars=200)
        slot = state.get_or_create_slot("s1")
        for i in range(1, 9):
            slot.append("user", f"question {i}")
            slot.append("assistant", f"answer {i} " + "y" * 900)  # ~5 units each
        slot.drain()

        await self._link(state)
        sent = self._sent(transport)
        assert len(sent) <= self._BOUND_CEILING, (
            f"inline delivery sent {len(sent)} units, over the budget"
        )
        # Priority order: the newest turn is irreducible, then the marker, then
        # the opening turn, then older turns. Here each turn costs ~6 units, so
        # the opening turn cannot be afforded and is folded into the count.
        body = "\n".join(sent)
        assert "question 8" in body, "newest turn was trimmed away"
        assert any("earlier turn" in t for t in sent), "trim happened with no marker"

    @pytest.mark.asyncio
    async def test_delivery_scales_with_the_budget_not_with_history(
        self, tmp_path, monkeypatch
    ):
        """Ten times the history must not mean ten times the request duration."""

        counts = []
        for turn_count in (8, 80):
            state, transport = self._linked(tmp_path, monkeypatch)
            slot = state.get_or_create_slot("s1")
            for i in range(1, turn_count + 1):
                slot.append("user", f"q{i}")
                slot.append("assistant", f"a{i}")
            slot.drain()
            await self._link(state)
            counts.append(len(self._sent(transport)))

        assert all(c <= self._BOUND_CEILING for c in counts), counts
        assert counts[0] == counts[1], (
            f"unit count tracked history length ({counts}) instead of the budget"
        )

    @pytest.mark.asyncio
    async def test_no_slack_mrkdwn_conversion_on_a_non_slack_channel(self, tmp_path, monkeypatch):
        """Telegram is not Slack: markdown must pass through unconverted."""
        state, transport = self._linked(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "doc it")
        slot.append("assistant", "## Heading\n\n**bold** text")
        slot.drain()

        await self._link(state)
        body = "\n".join(self._sent(transport))
        assert "## Heading" in body
        assert "**bold**" in body

    @pytest.mark.asyncio
    async def test_credentials_are_redacted(self, tmp_path, monkeypatch):
        state, transport = self._linked(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        secret = "AKIAIOSFODNN7EXAMPLE"
        slot.append("user", "creds")
        slot.append("assistant", f"key is {secret}")
        slot.drain()

        await self._link(state)
        body = "\n".join(self._sent(transport))
        assert secret not in body

    @pytest.mark.asyncio
    async def test_compaction_rows_are_excluded(self, tmp_path, monkeypatch):
        state, transport = self._linked(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "real question")
        slot.append("assistant", "real answer")
        slot.append("assistant", "context compacted", meta={"kind": "compaction"})
        slot.drain()

        await self._link(state)
        body = "\n".join(self._sent(transport))
        assert "real question" in body and "real answer" in body
        assert "context compacted" not in body

    @pytest.mark.asyncio
    async def test_delivery_stays_inline_so_the_link_persists_after_seeding(
        self, tmp_path, monkeypatch
    ):
        """The 200 must not be returned before the seeding is delivered.

        This is the property that forbids backgrounding this path: the mirror
        link is persisted only after every unit has cleared governance.
        """
        state, transport = self._linked(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "one")
        slot.append("assistant", "two")
        slot.drain()

        order: list[str] = []
        original_send = transport.send_message

        async def _tracked_send(*args, **kwargs):
            order.append("send")
            return await original_send(*args, **kwargs)

        transport.send_message = _tracked_send
        state.sessions.set_mirror_link = MagicMock(side_effect=lambda *a, **k: order.append("persist"))

        await self._link(state)
        assert "persist" in order, "link was never persisted"
        assert order.index("persist") == len(order) - 1, (
            "the link was persisted before delivery finished"
        )
        assert order.count("send") >= 3, "announcement + both messages should have been sent"


class TestInboundClaimFollowsTheCapability:
    """Connecting claims INBOUND only where the transport's inbound path honours it.

    This is the fix for the reported bug. Without the claim the connect writes an
    outbound-only binding, the channel's inbound resolver skips it, and the user's
    reply starts a brand-new session with none of this transcript.
    """

    @pytest.mark.asyncio
    async def test_a_resume_capable_transport_gets_an_inbound_binding(
        self, tmp_path, monkeypatch
    ):
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(_fake_transport("discord", session_resume=True))
        state.sessions.set_mirror_link = MagicMock()
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "discord", "target_id": "user:123"},
            )
            assert resp.status == 200
        assert state.sessions.set_mirror_link.call_args.kwargs["accepts_inbound"] is True

    @pytest.mark.asyncio
    async def test_a_transport_that_cannot_resume_stays_outbound_only(
        self, tmp_path, monkeypatch
    ):
        """Degrade, never over-promise.

        Telegram builds its session key from the route and never consults the
        binding, so claiming inbound would not make replies come back — it would
        only make the slot row say they do.
        """
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(_fake_transport("telegram", session_resume=False))
        state.sessions.set_mirror_link = MagicMock()
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 200
        assert state.sessions.set_mirror_link.call_args.kwargs["accepts_inbound"] is False

    @pytest.mark.asyncio
    async def test_an_occupied_conversation_is_refused_before_anything_is_posted(
        self, tmp_path, monkeypatch
    ):
        """A binding can be unwound; posted messages cannot.

        The authoritative check is atomic inside ``set_mirror_link``, but that
        fires only after the link notice and the whole catch-up transcript have
        been delivered. So the same question is asked at the first point the real
        location is known, and nothing is sent into a conversation this session
        does not get to own.
        """
        transport = _fake_transport("discord", session_resume=True)
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(transport)
        state.sessions.mirror_claim_blockers = MagicMock(return_value=["dashboard:someone-else"])
        state.sessions.set_mirror_link = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot.messages.extend(
            [
                {"role": "user", "content": "private"},
                {"role": "assistant", "content": "transcript"},
            ]
        )

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "discord", "target_id": "user:123"},
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "conversation_occupied"

        assert transport.send_message.await_count == 0, (
            "the transcript was delivered into a conversation another session owns"
        )
        state.sessions.set_mirror_link.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_stubbed_session_manager_does_not_read_as_occupied(
        self, tmp_path, monkeypatch
    ):
        """A Mock is truthy, and truthy must not mean "taken".

        Read as a rival list, a stubbed accessor's Mock would refuse every connect
        and the refusal would look like a real conflict. Only an actual list is an
        answer; anything else means "no precheck", and the writer still enforces.
        """
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(_fake_transport("discord", session_resume=True))
        state.sessions.mirror_claim_blockers = MagicMock(return_value=MagicMock())
        state.sessions.set_mirror_link = MagicMock()
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "discord", "target_id": "user:123"},
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_a_governance_denial_persists_no_inbound_binding(self, tmp_path, monkeypatch):
        """The write that grants inbound capability must stay behind the gate.

        Persisting is the side effect that matters: a binding written for a
        message governance went on to deny would leave the conversation connected
        AND inbound-capable, so the channel would keep resuming a session policy
        had just refused. The endpoint's existing fail-closed path is what
        prevents it — this pins that the strengthened write inherits it.
        """
        transport = _fake_transport("discord", session_resume=True)

        def _permits(*args, **kwargs):
            # Keyed on "has anything been delivered yet?", not a call count, so
            # the denial lands on the first in-loop unit regardless of how many
            # messages the backfill selects.
            return SimpleNamespace(
                permitted=not transport.send_message.await_args_list,
                rule="",
                layer="",
                reason="",
            )

        monkeypatch.setattr("kiro_crew.platform.governance_profiles.governance_permits", _permits)
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(transport)
        state.sessions.set_mirror_link = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot.messages.extend(
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ]
        )

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "discord", "target_id": "user:123"},
            )
            assert resp.status == 403

        state.sessions.set_mirror_link.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_precheck_asks_the_writers_exact_question(self, tmp_path, monkeypatch):
        """The precheck must pass the claim's inbound intent, not just the location.

        Drop the argument and the precheck answers a different question from the
        writer it backs -- refusing where the writer allows, which would newly
        reject a second outbound-only mirror on transports that cannot resume at
        all. The sentinel default is what makes omission detectable: a plain
        ``False`` default would make "passed False" and "not passed" identical, so
        the test would pass against the very divergence it exists to catch.
        """
        sentinel = object()
        seen: list[object] = []

        def _blockers(key, link, *, accepts_inbound=sentinel):
            seen.append(accepts_inbound)
            return []

        state = _prep(tmp_path, monkeypatch)
        # Discord declares session resume, so a threaded argument is True here and
        # an omitted one would fall to the default -- two distinguishable states.
        state.register_channel_transport(_fake_transport("discord", session_resume=True))
        state.sessions.mirror_claim_blockers = _blockers
        state.sessions.set_mirror_link = MagicMock()
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "discord", "target_id": "user:123"},
            )
            assert resp.status == 200
        assert seen == [True], (
            f"the precheck did not ask the writer's question with this claim's "
            f"inbound intent: {seen}"
        )

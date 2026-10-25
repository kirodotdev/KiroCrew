"""Per-part channel authorization on the ``channel_type`` leg of send_message.

The route, state and transport doubles come from ``test_send_message_targeted.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_send_message_targeted import (
    _TG_KEY,
    _TG_LINK,
    _channel_state,
    _channel_transport,
    _governance,
    _make_app,
    _permitted,
)

from kiro_crew.messaging.link import ChannelLink
from kiro_crew.telegram.client import TELEGRAM_MAX_TEXT


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.sel.sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


#: Small enough that the bodies below chunk into several parts, so a revocation
#: has a LATER part to stop; the real cap would need a 4 KB fixture to do it.
_PARTED_CAP = 12
_PARTED_TEXT = "aaaaaaaaaa bbbbbbbbbb cccccccccc dddddddddd"


def _parted_transport():
    """A transport whose cap forces *several* parts, flipping a flag once it sends.

    The flag is what the revocation tests key on: each asserts that something
    withdrawn AFTER the first part lands stops the remainder, and reading "has a
    part been delivered yet" off the transport is the one signal that does not
    depend on counting how many times the governance gate happens to be asked.
    """
    transport = _channel_transport()
    transport.capabilities.max_message_chars = _PARTED_CAP
    delivered: dict[str, bool] = {"any": False}

    async def _send(conversation_id, text, thread_id=None):
        delivered["any"] = True
        return "tg-1"

    transport.send_message = AsyncMock(side_effect=_send)
    return transport, delivered


async def _post_parted(state):
    app = _make_app(state)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/send-message",
            json={"text": _PARTED_TEXT, "channel_type": "telegram"},
            headers={"X-Session-Key": _TG_KEY},
        )
        return resp.status, await resp.json()


class TestPerPartRevalidation:
    @pytest.mark.asyncio
    async def test_a_multi_part_send_still_delivers_every_part(self, mock_sel):
        """The control for the whole class: nothing is revoked, so nothing stops.

        Without this, a bug that refused the second part unconditionally would
        satisfy every revocation test below while breaking all long sends.
        """
        transport, _ = _parted_transport()
        state = _channel_state(transport=transport)

        with _governance(True):
            status, data = await _post_parted(state)

        assert status == 200
        assert data["ok"] is True
        assert data["delivered_to"] == "telegram"
        assert transport.send_message.await_count >= 3

    @pytest.mark.asyncio
    async def test_a_governance_ceiling_narrowed_mid_send_stops_the_remainder(self, mock_sel):
        """A ceiling published after the first part refuses the rest, as 403.

        403 rather than the generic 502 because a revocation is a decision about
        the caller's permission, and retrying it cannot succeed.
        """
        transport, delivered = _parted_transport()
        state = _channel_state(transport=transport)

        with patch(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            side_effect=lambda *a, **k: _permitted(not delivered["any"]),
        ):
            status, data = await _post_parted(state)

        assert transport.send_message.await_count == 1
        assert status == 403
        assert data["code"] == "channel_not_permitted"
        assert data["error"].startswith("telegram delivery failed: ")
        assert "aborted before part 2" in data["error"]
        assert "(1 already delivered)" in data["error"]

    @pytest.mark.asyncio
    async def test_a_recipient_dropped_mid_send_stops_the_remainder(self, mock_sel):
        """The allow-list, not governance, withdraws the destination mid-send."""
        transport, delivered = _parted_transport()
        transport.may_send_to = lambda *a, **k: not delivered["any"]
        state = _channel_state(transport=transport)

        with _governance(True):
            status, data = await _post_parted(state)

        assert transport.send_message.await_count == 1
        assert status == 403
        assert data["code"] == "channel_not_permitted"

    @pytest.mark.asyncio
    async def test_a_session_unlinked_mid_send_stops_the_remainder(self, mock_sel):
        """An UNLINK leaves the resolved conversation id identical.

        So the id cannot show it and only re-walking the binding can: this is the
        case a check that compared ids alone would pass straight through.
        """
        transport, delivered = _parted_transport()
        state = _channel_state(transport=transport)
        state.sessions.get_origin_link.side_effect = lambda *a, **k: (
            None if delivered["any"] else _TG_LINK
        )

        with _governance(True):
            status, data = await _post_parted(state)

        assert transport.send_message.await_count == 1
        assert status == 403
        assert data["code"] == "channel_not_permitted"

    @pytest.mark.asyncio
    async def test_a_same_location_row_with_a_new_admission_stops_the_remainder(self, mock_sel):
        """A rollback can put a different row back at the same conversation.

        ``==`` sees the same location; only the whole row shows the change.
        """
        import dataclasses

        transport, delivered = _parted_transport()
        state = _channel_state(transport=transport)
        restored = dataclasses.replace(_TG_LINK, admission="restored-unsigned")
        state.sessions.get_origin_link.side_effect = lambda *a, **k: (
            restored if delivered["any"] else _TG_LINK
        )

        with _governance(True):
            status, data = await _post_parted(state)

        assert restored == _TG_LINK
        assert transport.send_message.await_count == 1
        assert status == 403
        assert data["code"] == "channel_not_permitted"

    @pytest.mark.asyncio
    async def test_a_transport_replaced_mid_send_aborts_as_retryable(self, mock_sel):
        """A replacement is a FAULT, so it answers 502 and not the 403 above.

        The tail was sanitised and chunked for the transport that was captured, so
        a replacement is refused rather than handed a tail it was not shaped for.
        """
        transport, delivered = _parted_transport()
        replacement, _ = _parted_transport()
        state = _channel_state(transport=transport)
        state.get_channel_transport.side_effect = lambda *a, **k: (
            replacement if delivered["any"] else transport
        )

        with _governance(True):
            status, data = await _post_parted(state)

        assert transport.send_message.await_count == 1
        replacement.send_message.assert_not_called()
        assert status == 502
        assert data["code"] == "channel_delivery_failed"

    @pytest.mark.asyncio
    async def test_a_generation_that_settles_on_the_second_look_still_delivers(self, mock_sel):
        """The profile layer bumps the generation INSIDE the authorization read.

        A send must not be refused for that alone, so the question is re-asked
        once; here the second look agrees and the send proceeds.
        """
        transport, _ = _parted_transport()
        transport.capabilities.max_message_chars = TELEGRAM_MAX_TEXT
        state = _channel_state(transport=transport)
        readings = [1, 1, 1, 2, 2, 3, 3, 3]
        cursor = {"i": 0}

        def _gen():
            i = cursor["i"]
            cursor["i"] = i + 1
            return readings[i] if i < len(readings) else readings[-1]

        with (
            _governance(True),
            patch(
                "kiro_crew.platform.governance_profiles.governance_answer_generation",
                side_effect=_gen,
            ),
        ):
            status, data = await _post_parted(state)

        assert status == 200
        assert data["ok"] is True
        assert transport.send_message.await_count == 1

    @pytest.mark.asyncio
    async def test_a_generation_that_never_settles_refuses_as_retryable(self, mock_sel):
        """Both attempts authorized, so nothing was narrowed and a retry can work.

        That makes it a 502 rather than the 403 a real denial answers.
        """
        transport, _ = _parted_transport()
        transport.capabilities.max_message_chars = TELEGRAM_MAX_TEXT
        state = _channel_state(transport=transport)
        moving = {"n": 0}

        def _ever_moving():
            moving["n"] += 1
            return moving["n"]

        with (
            _governance(True),
            patch(
                "kiro_crew.platform.governance_profiles.governance_answer_generation",
                side_effect=_ever_moving,
            ),
        ):
            status, data = await _post_parted(state)

        transport.send_message.assert_not_called()
        assert status == 502
        assert data["code"] == "channel_delivery_failed"

    @pytest.mark.asyncio
    async def test_a_dashboard_session_mirrored_to_a_discord_dm_delivers_every_part(self, mock_sel):
        """The per-part recipient check hands the transport the ladder's principal.

        A dashboard key names nobody, so the only peer for a Discord DM mirror is
        the gateway-admitted one on the link. The per-part check must read that
        same peer: the key alone gives ``""``, which Discord refuses for every DM.
        """
        import dataclasses

        from kiro_crew.mirror_admission import sign_mirror_admission

        key = "dashboard:chat-1-1700000000"
        link = ChannelLink("discord", channel_id="dm-chan-1", principal="42")
        link = dataclasses.replace(link, admission=sign_mirror_admission(key, link))
        transport, _ = _parted_transport()
        transport.direct_peer_of = lambda _conv: ""

        def _admits_only_the_peer(_conv, _thread=None, *, principal=""):
            return principal == "42"

        transport.may_send_to = _admits_only_the_peer
        state = _channel_state(link=None, transport=transport)
        state.sessions.get_mirror_link.return_value = link

        with _governance(True):
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={"text": _PARTED_TEXT, "channel_type": "discord"},
                    headers={"X-Session-Key": key},
                )
                status, data = resp.status, await resp.json()

        assert status == 200, data
        assert data["ok"] is True
        assert transport.send_message.await_count >= 3

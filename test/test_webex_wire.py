"""Wire-level tests for the Webex channel.

Runs the REAL WebexClient against a fake aiohttp session, so the request the
channel would actually make -- verb, URL, payload, auth header -- is asserted
instead of being replaced by a FakeClient in the dispatcher tests.

Webex drives every verb through ``_api`` -> ``session.request(...)``, which is
why the harness needs a generic ``request()`` surface; a fake with only
post/get would leave this client untestable at this layer.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from kiro_crew.testing.channel_fixtures import load_fixture
from kiro_crew.testing.fake_channel_wire import FakeWireSession, WireResponse
from kiro_crew.webex.client import WebexClient

CHANNEL_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "channels"

_MESSAGE_CREATED = load_fixture("webex", "message_created", root=CHANNEL_FIXTURES).payload


def _client(wire: FakeWireSession) -> WebexClient:
    client = WebexClient(token="webex-tok")
    client._session = wire
    return client


class TestSendMessageAddressing:
    """Webex takes EITHER a roomId or a toPersonEmail -- never both."""

    def test_a_room_id_is_sent_as_room_id(self) -> None:
        wire = FakeWireSession().route("POST", "/messages", _MESSAGE_CREATED)
        client = _client(wire)

        msg_id = asyncio.run(client.send_message("ROOM-123", "hello"))

        body = wire.requests[0].json_body
        assert body["roomId"] == "ROOM-123"
        assert "toPersonEmail" not in body
        assert msg_id == _MESSAGE_CREATED["id"]

    def test_an_email_is_sent_as_to_person_email(self) -> None:
        # The '@' heuristic is how resolve_conversation hands proactive sends a
        # person instead of a room; sending it as roomId would 400.
        wire = FakeWireSession().route("POST", "/messages", _MESSAGE_CREATED)
        client = _client(wire)

        asyncio.run(client.send_message("someone@example.com", "hello"))

        body = wire.requests[0].json_body
        assert body["toPersonEmail"] == "someone@example.com"
        assert "roomId" not in body

    def test_a_parent_id_threads_the_reply(self) -> None:
        wire = FakeWireSession().route("POST", "/messages", _MESSAGE_CREATED)
        client = _client(wire)

        asyncio.run(client.send_message("ROOM-1", "reply", parent_id="MSG-9"))

        assert wire.requests[0].json_body["parentId"] == "MSG-9"

    def test_the_bearer_credential_is_attached(self) -> None:
        wire = FakeWireSession().route("POST", "/messages", _MESSAGE_CREATED)
        client = _client(wire)

        asyncio.run(client.send_message("ROOM-1", "hi"))

        assert wire.requests[0].headers.get("Authorization") == "Bearer webex-tok"


class TestVerbsAndPaths:
    def test_edit_uses_put_on_the_message_path(self) -> None:
        wire = FakeWireSession().route("PUT", "/messages/", _MESSAGE_CREATED)
        client = _client(wire)

        ok = asyncio.run(client.edit_message("MSG-9", "ROOM-1", "edited"))

        req = wire.requests[0]
        assert (req.method, ok) == ("PUT", True)
        assert req.path.endswith("/messages/MSG-9")
        assert req.json_body["roomId"] == "ROOM-1"

    def test_delete_uses_delete_and_sends_no_body(self) -> None:
        wire = FakeWireSession().route("DELETE", "/messages/", WireResponse(body={}))
        client = _client(wire)

        asyncio.run(client.delete_message("MSG-9"))

        req = wire.requests[0]
        assert req.method == "DELETE"
        assert req.body is None

    def test_fetch_uses_get(self) -> None:
        wire = FakeWireSession().route("GET", "/messages/", _MESSAGE_CREATED)
        client = _client(wire)

        out = asyncio.run(client.fetch_message("MSG-9"))

        assert wire.requests[0].method == "GET"
        assert out == _MESSAGE_CREATED


class TestRateLimitBackoff:
    def test_a_429_is_retried_once_honouring_retry_after(self, monkeypatch) -> None:
        """A dropped rate-limited edit would freeze the placeholder forever.

        The client waits out one Retry-After and retries; this pins that the
        retry actually happens (two requests) rather than the call giving up.
        """
        slept: list[float] = []

        async def _no_sleep(secs: float) -> None:
            slept.append(secs)

        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        wire = FakeWireSession().route(
            "POST",
            "/messages",
            [
                WireResponse(body={}, status=429, headers={"Retry-After": "2"}),
                _MESSAGE_CREATED,
            ],
        )
        client = _client(wire)

        msg_id = asyncio.run(client.send_message("ROOM-1", "hi"))

        assert len(wire.requests) == 2, "the 429 must be retried, not dropped"
        assert slept == [2.0], "Retry-After must be honoured, not a fixed guess"
        assert msg_id == _MESSAGE_CREATED["id"]

    def test_a_second_429_gives_up_instead_of_looping(self, monkeypatch) -> None:
        async def _no_sleep(_secs: float) -> None:
            return None

        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        wire = FakeWireSession().route(
            "POST", "/messages", WireResponse(body={}, status=429, headers={"Retry-After": "1"})
        )
        client = _client(wire)

        msg_id = asyncio.run(client.send_message("ROOM-1", "hi"))

        assert msg_id is None
        assert len(wire.requests) == 2, "bounded at one retry -- never an unbounded loop"

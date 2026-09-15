"""Wire-level tests for the WeCom channel.

Runs the REAL WeComClient against fakes for both of its wire surfaces:

* the one-shot reply, an HTTP POST to the inbound message's ``response_url``
  (whose embedded response_code is the ONLY credential -- single-use, 1h TTL);
* the streaming reply, a WS frame (``aibot_respond_msg``) whose routing depends
  entirely on echoing back the server-assigned ``req_id``.

Both are pure request construction, so the dispatcher tests -- which replace the
client wholesale -- never exercise either.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from kiro_crew.testing.fake_channel_wire import (
    FakeWireSession,
    FakeWireWebSocket,
    WireResponse,
)
from kiro_crew.wecom.client import WECOM_MAX_REPLY_BYTES, WeComClient

CHANNEL_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "channels"

_RESPONSE_URL = "https://qyapi.weixin.qq.com/cgi-bin/aibot/reply?code=abc123"


def _client(wire: FakeWireSession | None = None) -> WeComClient:
    client = WeComClient(bot_id="bot-1", secret="s", ws_url="wss://example.invalid/ws")
    if wire is not None:
        client._session = wire
    return client


class TestOneShotReply:
    def test_the_reply_posts_markdown_to_the_response_url(self) -> None:
        wire = FakeWireSession().route("POST", "/cgi-bin/aibot/reply", {"errcode": 0})
        client = _client(wire)

        asyncio.run(client.send_reply(_RESPONSE_URL, "**hi**"))

        req = wire.requests[0]
        assert req.method == "POST"
        assert req.url == _RESPONSE_URL, "the response_url must be used verbatim"
        assert req.json_body == {"msgtype": "markdown", "markdown": {"content": "**hi**"}}

    def test_empty_content_is_replaced_so_the_api_never_400s(self) -> None:
        wire = FakeWireSession().route("POST", "/cgi-bin/aibot/reply", {"errcode": 0})
        client = _client(wire)

        asyncio.run(client.send_reply(_RESPONSE_URL, ""))

        assert wire.requests[0].json_body["markdown"]["content"]

    def test_overlong_content_is_capped_in_bytes_before_sending(self) -> None:
        wire = FakeWireSession().route("POST", "/cgi-bin/aibot/reply", {"errcode": 0})
        client = _client(wire)

        asyncio.run(client.send_reply(_RESPONSE_URL, "x" * (WECOM_MAX_REPLY_BYTES + 500)))

        sent = wire.requests[0].json_body["markdown"]["content"]
        assert len(sent.encode("utf-8")) == WECOM_MAX_REPLY_BYTES, (
            "the client must cap at the documented limit, not let the API reject it"
        )

    def test_the_cap_is_bytes_not_characters(self) -> None:
        # WeCom's limit is 20480 UTF-8 BYTES. Capping by CHARACTERS let a Chinese
        # reply through at ~3x the byte limit, and WeCom rejects the whole frame --
        # so the user got nothing at all, which is how this went unnoticed.
        wire = FakeWireSession().route("POST", "/cgi-bin/aibot/reply", {"errcode": 0})
        client = _client(wire)

        asyncio.run(client.send_reply(_RESPONSE_URL, "\u6c49" * WECOM_MAX_REPLY_BYTES))

        sent = wire.requests[0].json_body["markdown"]["content"]
        assert len(sent.encode("utf-8")) <= WECOM_MAX_REPLY_BYTES
        assert len(sent) < WECOM_MAX_REPLY_BYTES, "a 3-byte character must cost 3 bytes"

    def test_truncation_never_splits_a_code_point(self) -> None:
        wire = FakeWireSession().route("POST", "/cgi-bin/aibot/reply", {"errcode": 0})
        client = _client(wire)

        # A 3-byte character straddling the boundary must be dropped whole, not
        # cut into a replacement character.
        asyncio.run(client.send_reply(_RESPONSE_URL, "\u6c49" * (WECOM_MAX_REPLY_BYTES // 2)))

        sent = wire.requests[0].json_body["markdown"]["content"]
        assert "\ufffd" not in sent
        sent.encode("utf-8").decode("utf-8")  # must round-trip

    def test_a_missing_response_url_sends_nothing(self) -> None:
        wire = FakeWireSession()
        client = _client(wire)

        asyncio.run(client.send_reply("", "hi"))

        assert wire.requests == []

    def test_an_http_error_does_not_raise_out_of_the_reply_path(self) -> None:
        # A raising ack would abort the turn after the model already answered.
        wire = FakeWireSession().route(
            "POST", "/cgi-bin/aibot/reply", WireResponse(body="nope", status=500)
        )
        client = _client(wire)

        asyncio.run(client.send_reply(_RESPONSE_URL, "hi"))  # must not raise


class TestStreamFrames:
    """The WS frame shape -- routing lives entirely in the echoed req_id."""

    def test_the_frame_carries_the_command_req_id_and_stream_body(self) -> None:
        ws = FakeWireWebSocket()
        client = _client()
        client._ws = ws  # type: ignore[assignment]

        ok = asyncio.run(
            client.send_stream("REQ-1", "STREAM-1", "partial", finish=False)
        )

        assert ok is True
        frame = json.loads(ws.sent[0])
        assert frame["cmd"] == "aibot_respond_msg"
        assert frame["headers"]["req_id"] == "REQ-1", (
            "req_id is the ONLY routing key -- a wrong one silently delivers nowhere"
        )
        assert frame["body"]["msgtype"] == "stream"
        assert frame["body"]["stream"] == {
            "id": "STREAM-1",
            "finish": False,
            "content": "partial",
        }

    def test_finish_locks_the_bubble(self) -> None:
        ws = FakeWireWebSocket()
        client = _client()
        client._ws = ws  # type: ignore[assignment]

        asyncio.run(client.send_stream("REQ-1", "STREAM-1", "done", finish=True))

        frame = json.loads(ws.sent[0])
        assert frame["body"]["stream"]["finish"] is True

    def test_frames_carry_the_full_accumulated_text_not_a_delta(self) -> None:
        ws = FakeWireWebSocket()
        client = _client()
        client._ws = ws  # type: ignore[assignment]

        asyncio.run(client.send_stream("REQ-1", "S", "Hel", finish=False))
        asyncio.run(client.send_stream("REQ-1", "S", "Hello", finish=True))

        contents = [json.loads(f)["body"]["stream"]["content"] for f in ws.sent]
        assert contents == ["Hel", "Hello"], (
            "each frame REPLACES the bubble, so a delta would render truncated text"
        )

    def test_no_live_socket_reports_failure_instead_of_raising(self) -> None:
        client = _client()
        client._ws = None

        assert asyncio.run(client.send_stream("REQ-1", "S", "hi", finish=True)) is False

    def test_a_missing_req_id_reports_failure(self) -> None:
        ws = FakeWireWebSocket()
        client = _client()
        client._ws = ws  # type: ignore[assignment]

        assert asyncio.run(client.send_stream("", "S", "hi", finish=True)) is False
        assert ws.sent == [], "an unroutable frame must not be put on the wire"

    def test_a_closed_socket_reports_failure(self) -> None:
        ws = FakeWireWebSocket()
        ws.closed = True
        client = _client()
        client._ws = ws  # type: ignore[assignment]

        assert asyncio.run(client.send_stream("REQ-1", "S", "hi", finish=True)) is False

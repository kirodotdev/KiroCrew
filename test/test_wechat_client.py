"""Tests for kiro_crew.wechat.client."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from kiro_crew.wechat.client import (
    WeComClient,
    WeComInbound,
    _build_subscribe_frame,
    _redact_frame,
)

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


@dataclass
class FakeWSMessage:
    type: Any
    data: str = ""


class FakeWS:
    """Fake aiohttp WebSocket that records sent frames."""

    def __init__(self, inbound_frames: list[str] | None = None) -> None:
        self.sent: list[dict] = []
        self.closed = False
        self._inbound = list(inbound_frames or [])
        self._index = 0

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self) -> FakeWSMessage:
        if self._index < len(self._inbound):
            raw = self._inbound[self._index]
            self._index += 1
            return FakeWSMessage(type=aiohttp.WSMsgType.TEXT, data=raw)
        raise StopAsyncIteration


# ------------------------------------------------------------------
# Tests: send_stream builds correct aibot_respond_msg frame
# ------------------------------------------------------------------


class TestSendStream:
    """Verify send_stream produces correct aibot_respond_msg frames."""

    @pytest.mark.asyncio
    async def test_intermediate_frame_structure(self) -> None:
        fake_ws = FakeWS()
        client = WeComClient(
            bot_id="bot1",
            secret="sec1",
            ws_url="wss://fake",
            on_message=AsyncMock(),
        )
        client._ws = fake_ws  # type: ignore[assignment]

        result = await client.send_stream("req-abc", "stream-123", "Hello", finish=False)

        assert result is True
        assert len(fake_ws.sent) == 1
        frame = fake_ws.sent[0]
        assert frame["cmd"] == "aibot_respond_msg"
        assert frame["headers"]["req_id"] == "req-abc"
        body = frame["body"]
        assert body["msgtype"] == "stream"
        stream = body["stream"]
        assert stream["id"] == "stream-123"
        assert stream["finish"] is False
        assert stream["content"] == "Hello"

    @pytest.mark.asyncio
    async def test_final_frame_structure(self) -> None:
        fake_ws = FakeWS()
        client = WeComClient(
            bot_id="bot1",
            secret="sec1",
            ws_url="wss://fake",
            on_message=AsyncMock(),
        )
        client._ws = fake_ws  # type: ignore[assignment]

        result = await client.send_stream("req-def", "stream-456", "Done", finish=True)

        assert result is True
        frame = fake_ws.sent[0]
        assert frame["cmd"] == "aibot_respond_msg"
        assert frame["headers"]["req_id"] == "req-def"
        assert frame["body"]["stream"]["id"] == "stream-456"
        assert frame["body"]["stream"]["finish"] is True
        assert frame["body"]["stream"]["content"] == "Done"

    @pytest.mark.asyncio
    async def test_returns_false_when_no_ws(self) -> None:
        client = WeComClient(
            bot_id="bot1",
            secret="sec1",
            ws_url="wss://fake",
            on_message=AsyncMock(),
        )
        client._ws = None
        result = await client.send_stream("req-1", "s1", "text", finish=False)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_req_id(self) -> None:
        fake_ws = FakeWS()
        client = WeComClient(
            bot_id="bot1",
            secret="sec1",
            ws_url="wss://fake",
            on_message=AsyncMock(),
        )
        client._ws = fake_ws  # type: ignore[assignment]
        result = await client.send_stream("", "s1", "text", finish=False)
        assert result is False
        assert len(fake_ws.sent) == 0

    @pytest.mark.asyncio
    async def test_multiple_frames_correlate_by_req_id(self) -> None:
        fake_ws = FakeWS()
        client = WeComClient(
            bot_id="bot1",
            secret="sec1",
            ws_url="wss://fake",
            on_message=AsyncMock(),
        )
        client._ws = fake_ws  # type: ignore[assignment]

        await client.send_stream("req-x", "s1", "chunk1", finish=False)
        await client.send_stream("req-x", "s1", "chunk1 chunk2", finish=True)

        assert len(fake_ws.sent) == 2
        assert fake_ws.sent[0]["headers"]["req_id"] == "req-x"
        assert fake_ws.sent[1]["headers"]["req_id"] == "req-x"
        assert fake_ws.sent[0]["body"]["stream"]["finish"] is False
        assert fake_ws.sent[1]["body"]["stream"]["finish"] is True


# ------------------------------------------------------------------
# Tests: send_reply POSTs to response_url
# ------------------------------------------------------------------


class TestSendReply:
    """Verify send_reply POSTs the correct payload."""

    @pytest.mark.asyncio
    async def test_posts_markdown_payload(self) -> None:
        client = WeComClient(
            bot_id="bot1",
            secret="sec1",
            ws_url="wss://fake",
            on_message=AsyncMock(),
        )

        posted: list[dict] = []

        class FakeResp:
            status = 200

            async def json(self, content_type=None):
                return {"errcode": 0}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

        class FakeSession:
            def post(self, url, json=None, proxy=None, timeout=None):
                posted.append({"url": url, "json": json})
                return FakeResp()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def close(self):
                pass

        with patch("aiohttp.ClientSession", return_value=FakeSession()):
            await client.send_reply("https://example.com/resp", "Hello!")

        assert len(posted) == 1
        assert posted[0]["url"] == "https://example.com/resp"
        assert posted[0]["json"] == {
            "msgtype": "markdown",
            "markdown": {"content": "Hello!"},
        }

    @pytest.mark.asyncio
    async def test_no_op_without_response_url(self) -> None:
        client = WeComClient(
            bot_id="bot1",
            secret="sec1",
            ws_url="wss://fake",
            on_message=AsyncMock(),
        )
        # Should not raise
        await client.send_reply("", "Hello!")

    @pytest.mark.asyncio
    async def test_reuses_live_ws_session_without_closing_it(self) -> None:
        """send_reply reuses the live WS ClientSession and must NOT close it."""
        posted: list[str] = []
        closed = {"n": 0}

        class FakeResp:
            status = 200

            async def json(self, content_type=None):
                return {"errcode": 0}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

        class LiveSession:
            closed = False

            def post(self, url, json=None, proxy=None, timeout=None):
                posted.append(url)
                return FakeResp()

            async def close(self):
                closed["n"] += 1

        client = WeComClient(bot_id="b", secret="s", ws_url="wss://fake")
        client._session = LiveSession()  # type: ignore[assignment]

        # aiohttp.ClientSession is NOT patched: opening a new one would error out,
        # proving the live session is reused.
        await client.send_reply("https://example.com/resp", "Hi")

        assert posted == ["https://example.com/resp"]
        assert closed["n"] == 0  # a reused (not owned) session must not be closed


# ------------------------------------------------------------------
# Tests: inbound aibot_msg_callback dispatches to on_message
# ------------------------------------------------------------------


class TestInboundDispatch:
    """Verify aibot_msg_callback frames are parsed into WeComInbound."""

    @pytest.mark.asyncio
    async def test_callback_dispatches_correct_fields(self) -> None:
        received: list[WeComInbound] = []

        async def handler(msg: WeComInbound) -> None:
            received.append(msg)

        client = WeComClient(
            bot_id="bot1",
            secret="sec1",
            ws_url="wss://fake",
            on_message=handler,
        )

        callback_frame = json.dumps(
            {
                "cmd": "aibot_msg_callback",
                "headers": {"req_id": "abc123"},
                "body": {
                    "from": {"userid": "user-77"},
                    "response_url": "https://example.com/resp",
                    "chatid": "chat-42",
                    "msgtype": "text",
                    "text": {"content": "Hello bot!"},
                },
            }
        )

        await client._handle_message(callback_frame)
        # Background task dispatched; give it a tick
        await asyncio.sleep(0.01)

        assert len(received) == 1
        msg = received[0]
        assert msg.userid == "user-77"
        assert msg.text == "Hello bot!"
        assert msg.req_id == "abc123"
        assert msg.response_url == "https://example.com/resp"
        assert msg.chatid == "chat-42"
        assert msg.msgtype == "text"

    @pytest.mark.asyncio
    async def test_callback_missing_optional_fields(self) -> None:
        """Minimal callback without chatid still works."""
        received: list[WeComInbound] = []

        async def handler(msg: WeComInbound) -> None:
            received.append(msg)

        client = WeComClient(
            bot_id="bot1",
            secret="sec1",
            ws_url="wss://fake",
            on_message=handler,
        )

        callback_frame = json.dumps(
            {
                "cmd": "aibot_msg_callback",
                "headers": {"req_id": "def456"},
                "body": {
                    "from": {"userid": "user-1"},
                    "msgtype": "text",
                    "text": {"content": "Hi"},
                },
            }
        )

        await client._handle_message(callback_frame)
        await asyncio.sleep(0.01)

        assert len(received) == 1
        msg = received[0]
        assert msg.userid == "user-1"
        assert msg.text == "Hi"
        assert msg.req_id == "def456"
        assert msg.chatid == ""
        assert msg.response_url == ""

    @pytest.mark.asyncio
    async def test_dispatch_runs_as_background_task(self) -> None:
        """on_message is dispatched via create_task, not awaited inline."""
        started = asyncio.Event()
        finish = asyncio.Event()

        async def slow_handler(msg: WeComInbound) -> None:
            started.set()
            await finish.wait()

        client = WeComClient(
            bot_id="bot1",
            secret="sec1",
            ws_url="wss://fake",
            on_message=slow_handler,
        )

        callback_frame = json.dumps(
            {
                "cmd": "aibot_msg_callback",
                "headers": {"req_id": "r1"},
                "body": {"from": {"userid": "u1"}, "text": {"content": "x"}},
            }
        )

        # _handle_message should return immediately (not block on handler)
        await client._handle_message(callback_frame)
        # Handler task is running in background
        await asyncio.sleep(0.01)
        assert started.is_set()
        finish.set()
        await asyncio.sleep(0.01)


# ------------------------------------------------------------------
# Tests: ACK/pong handling
# ------------------------------------------------------------------


class TestAckPongHandling:
    """Verify cmd-less frames are routed correctly."""

    @pytest.mark.asyncio
    async def test_pong_decrements_counter(self) -> None:
        client = WeComClient(
            bot_id="bot1",
            secret="sec1",
            ws_url="wss://fake",
            on_message=AsyncMock(),
        )
        client._pending_pongs = 2
        client._ping_reqs.add("ping-1")

        pong_frame = json.dumps(
            {
                "headers": {"req_id": "ping-1"},
                "errcode": 0,
            }
        )
        await client._handle_message(pong_frame)

        assert client._pending_pongs == 1
        assert "ping-1" not in client._ping_reqs

    @pytest.mark.asyncio
    async def test_stream_ack_non_zero_errcode_logged(self) -> None:
        """Non-zero errcode on a non-ping ACK is a stream error."""
        client = WeComClient(
            bot_id="bot1",
            secret="sec1",
            ws_url="wss://fake",
            on_message=AsyncMock(),
        )
        client._pending_pongs = 0

        ack_frame = json.dumps(
            {
                "headers": {"req_id": "not-a-ping"},
                "errcode": 846605,
                "errmsg": "invalid req_id",
            }
        )
        # Should not raise
        await client._handle_message(ack_frame)
        # pongs unchanged
        assert client._pending_pongs == 0

    @pytest.mark.asyncio
    async def test_stream_ack_zero_errcode_silent(self) -> None:
        """errcode=0 stream ACK is silently accepted."""
        client = WeComClient(
            bot_id="bot1",
            secret="sec1",
            ws_url="wss://fake",
            on_message=AsyncMock(),
        )
        ack_frame = json.dumps(
            {
                "headers": {"req_id": "some-req"},
                "errcode": 0,
            }
        )
        await client._handle_message(ack_frame)


# ------------------------------------------------------------------
# Tests: disconnected_event stops reconnection
# ------------------------------------------------------------------


class TestDisconnectedEvent:
    """Verify disconnected_event sets _kicked flag."""

    @pytest.mark.asyncio
    async def test_kicked_flag_set(self) -> None:
        client = WeComClient(
            bot_id="bot1",
            secret="sec1",
            ws_url="wss://fake",
            on_message=AsyncMock(),
        )
        fake_ws = FakeWS()
        client._ws = fake_ws  # type: ignore[assignment]

        frame = json.dumps(
            {
                "cmd": "disconnected_event",
                "headers": {"req_id": "kick1"},
                "body": {},
            }
        )
        await client._handle_message(frame)

        assert client._kicked is True
        assert fake_ws.closed is True


# ------------------------------------------------------------------
# Tests: reconnect backoff timing
# ------------------------------------------------------------------


class TestReconnectBackoff:
    """Verify exponential backoff with cap at 30s."""

    @pytest.mark.asyncio
    async def test_backoff_timing(self) -> None:
        sleep_calls: list[float] = []

        async def mock_sleep(delay: float) -> None:
            sleep_calls.append(delay)
            if len(sleep_calls) >= 4:
                client._kicked = True

        client = WeComClient(
            bot_id="bot1",
            secret="sec1",
            ws_url="wss://fake",
            on_message=AsyncMock(),
        )

        async def failing_connect() -> None:
            raise aiohttp.ClientError("connection refused")

        client._connect_and_serve = failing_connect  # type: ignore

        with patch("asyncio.sleep", side_effect=mock_sleep):
            await client._run_loop()

        assert len(sleep_calls) >= 3
        assert sleep_calls[0] == pytest.approx(1.0)
        assert sleep_calls[1] == pytest.approx(2.0)
        assert sleep_calls[2] == pytest.approx(4.0)


# ------------------------------------------------------------------
# Tests: subscribe frame structure
# ------------------------------------------------------------------


class TestSubscribeFrame:
    """Verify the subscribe handshake frame."""

    def test_subscribe_frame_keys(self) -> None:
        frame = _build_subscribe_frame("my-bot", "my-secret")
        assert frame["cmd"] == "aibot_subscribe"
        assert "req_id" in frame["headers"]
        assert frame["body"]["bot_id"] == "my-bot"
        assert frame["body"]["secret"] == "my-secret"


class TestRedactFrame:
    def test_masks_response_url_code(self) -> None:
        raw = (
            '{"cmd":"aibot_msg_callback","body":{"userid":"Wei",'
            '"response_url":"https://qyapi.weixin.qq.com/cgi-bin/x?code=SECRET123"}}'
        )
        out = _redact_frame(raw)
        assert "SECRET123" not in out
        assert '"response_url":"<redacted>"' in out
        # Non-sensitive fields survive.
        assert '"userid":"Wei"' in out

    def test_noop_without_response_url(self) -> None:
        raw = '{"cmd":"ping","body":{"userid":"Wei"}}'
        assert _redact_frame(raw) == raw


class TestConcurrentSends:
    """Verify all WS sends are serialized (no interleaved send_json)."""

    @pytest.mark.asyncio
    async def test_concurrent_send_stream_is_serialized(self) -> None:
        depth = {"cur": 0, "max": 0}

        class SlowWS:
            closed = False

            async def send_json(self, frame: dict) -> None:
                depth["cur"] += 1
                depth["max"] = max(depth["max"], depth["cur"])
                await asyncio.sleep(0.01)
                depth["cur"] -= 1

        client = WeComClient(bot_id="b", secret="s", ws_url="wss://fake")
        client._ws = SlowWS()  # type: ignore[assignment]

        await asyncio.gather(
            *[client.send_stream("rq", "s", f"c{i}", finish=False) for i in range(5)]
        )
        # The send lock guarantees at most one send_json in flight at a time.
        assert depth["max"] == 1


class TestThresholdClamp:
    """WeComConfig.__post_init__ clamps to [0,100] and enforces soft <= hard."""

    def test_soft_above_hard_is_lowered_to_hard(self) -> None:
        from kiro_crew.config.loader import WeComConfig

        c = WeComConfig(soft_threshold_pct=95, hard_threshold_pct=50)
        assert c.soft_threshold_pct == 50
        assert c.hard_threshold_pct == 50

    def test_out_of_range_values_clamped(self) -> None:
        from kiro_crew.config.loader import WeComConfig

        c = WeComConfig(soft_threshold_pct=-10, hard_threshold_pct=200)
        assert c.soft_threshold_pct == 0
        assert c.hard_threshold_pct == 100

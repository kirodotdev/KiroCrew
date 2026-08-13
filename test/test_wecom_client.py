"""Tests for kiro_crew.wecom.client."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from kiro_crew.wecom.client import (
    WeComClient,
    WeComInbound,
    _build_subscribe_frame,
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
    async def test_stream_ack_non_zero_errcode_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Non-zero errcode ACK logs a generic event, never errcode/errmsg."""
        client = WeComClient(
            bot_id="bot1",
            secret="sec1",
            ws_url="wss://fake",
            on_message=AsyncMock(),
        )
        client._pending_pongs = 0

        errmsg = "invalid req_id sk-live-ACKSECRET-token"
        ack_frame = json.dumps(
            {
                "headers": {"req_id": "not-a-ping"},
                "errcode": 846605,
                "errmsg": errmsg,
            }
        )
        with caplog.at_level(logging.WARNING):
            # Should not raise
            await client._handle_message(ack_frame)
        # pongs unchanged
        assert client._pending_pongs == 0
        # A generic stream-ACK-error warning is emitted, but the externally
        # derived errcode/errmsg values never reach the log.
        assert "stream ACK error" in caplog.text
        assert "846605" not in caplog.text
        assert errmsg not in caplog.text
        assert "ACKSECRET" not in caplog.text

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
    """WeComConfig.__post_init__ clamps to [1,100] and enforces soft <= hard."""

    def test_soft_above_hard_is_lowered_to_hard(self) -> None:
        from kiro_crew.config.loader import WeComConfig

        c = WeComConfig(soft_threshold_pct=95, hard_threshold_pct=50)
        assert c.soft_threshold_pct == 50
        assert c.hard_threshold_pct == 50

    def test_out_of_range_values_clamped(self) -> None:
        from kiro_crew.config.loader import WeComConfig

        c = WeComConfig(soft_threshold_pct=-10, hard_threshold_pct=200)
        assert c.soft_threshold_pct == 1
        assert c.hard_threshold_pct == 100


# ------------------------------------------------------------------
# Tests: malformed frames are isolated from the receive loop
# ------------------------------------------------------------------


class TestMalformedFrames:
    """Malformed external frames must not terminate the WeCom client."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [None, [], "text", 5, True])
    async def test_non_object_json_is_ignored(self, payload: object) -> None:
        handler = AsyncMock()
        client = WeComClient(
            bot_id="bot1",
            secret="sec1",
            ws_url="wss://fake",
            on_message=handler,
        )

        await client._handle_message(json.dumps(payload))

        handler.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "frame",
        [
            {"cmd": "aibot_msg_callback", "headers": None, "body": {}},
            {"cmd": "aibot_msg_callback", "headers": {}, "body": []},
            {
                "cmd": "aibot_msg_callback",
                "headers": {},
                "body": {"from": "user", "text": {}},
            },
            {
                "cmd": "aibot_msg_callback",
                "headers": {},
                "body": {"from": {}, "text": "message"},
            },
        ],
    )
    async def test_malformed_nested_objects_are_ignored(self, frame: dict) -> None:
        handler = AsyncMock()
        client = WeComClient(
            bot_id="bot1",
            secret="sec1",
            ws_url="wss://fake",
            on_message=handler,
        )

        await client._handle_message(json.dumps(frame))

        handler.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handler_error_does_not_abort_receive_loop(self) -> None:
        fake_ws = FakeWS(["first", "second"])

        class FakeConnection:
            async def __aenter__(self):
                return fake_ws

            async def __aexit__(self, *args):
                return None

        class FakeSession:
            closed = False

            def ws_connect(self, url, proxy=None):
                return FakeConnection()

        client = WeComClient(bot_id="bot1", secret="sec1", ws_url="wss://fake")
        client._session = FakeSession()  # type: ignore[assignment]
        client._handle_message = AsyncMock(  # type: ignore[method-assign]
            side_effect=[ValueError("malformed"), None]
        )
        client._ping_loop = AsyncMock()  # type: ignore[method-assign]

        await client._connect_and_serve()

        assert client._handle_message.await_count == 2

    @pytest.mark.asyncio
    async def test_non_object_frame_warning_omits_payload(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A dropped non-dict frame logs only its type, never the payload content."""
        handler = AsyncMock()
        client = WeComClient(
            bot_id="bot1",
            secret="sec1",
            ws_url="wss://fake",
            on_message=handler,
        )

        secret = "sk-live-DEADBEEF-super-secret-scalar-payload"
        with caplog.at_level(logging.WARNING):
            await client._handle_message(json.dumps(secret))

        handler.assert_not_awaited()
        # The scalar payload (potentially a secret) must never reach the
        # warning log; only safe metadata (the Python type) is recorded.
        assert secret not in caplog.text
        assert "type=str" in caplog.text


# ------------------------------------------------------------------
# Tests: log sites never emit externally-derived frame content
# ------------------------------------------------------------------


class TestFrameLoggingOmitsContent:
    """Every WeCom log site records only safe metadata (length / type / generic
    event), never externally-derived frame content -- guards the five
    py/clear-text-logging-sensitive-data alerts."""

    @staticmethod
    def _client() -> WeComClient:
        return WeComClient(
            bot_id="bot1",
            secret="sec1",
            ws_url="wss://fake",
            on_message=AsyncMock(),
        )

    @pytest.mark.asyncio
    async def test_inbound_debug_logs_length_not_content(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The inbound-frame debug line records the byte length, not the frame."""
        client = self._client()
        secret = "sk-live-INBOUND-response-url-credential-SECRET"
        # Unhandled cmd path exercises both the inbound-frame debug line and the
        # unhandled-cmd debug line for a single frame.
        raw = json.dumps({"cmd": "totally-unknown-cmd", "response_url": secret})
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.wecom.client"):
            await client._handle_message(raw)
        # The raw frame (incl. the response_url credential) never reaches logs.
        assert secret not in caplog.text
        # Safe metadata remains: the inbound-frame byte length is recorded.
        assert "inbound frame" in caplog.text
        assert "bytes" in caplog.text

    @pytest.mark.asyncio
    async def test_unparseable_frame_warning_omits_content(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An unparseable frame logs only its byte length, never its content."""
        client = self._client()
        secret = "sk-live-UNPARSEABLE-SECRET-blob"
        raw = "<<<not-json>>> " + secret  # invalid JSON carrying a secret token
        with caplog.at_level(logging.WARNING):
            await client._handle_message(raw)
        assert secret not in caplog.text
        assert "unparseable frame" in caplog.text
        assert "bytes" in caplog.text

    @pytest.mark.asyncio
    async def test_unhandled_cmd_debug_omits_command_name(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An unhandled command logs a generic event, never the command name."""
        client = self._client()
        cmd = "aibot_secret_command_name"
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.wecom.client"):
            await client._handle_message(json.dumps({"cmd": cmd}))
        # The externally-derived command name must not appear...
        assert cmd not in caplog.text
        # ...but a generic unhandled-cmd event is recorded.
        assert "unhandled cmd" in caplog.text


class TestStatusCallback:
    """Live connection-status reporting for the settings badge."""

    def _client(self) -> WeComClient:
        return WeComClient(bot_id="b", secret="s", ws_url="wss://fake")

    def test_transitions_are_deduped(self) -> None:
        """Only state TRANSITIONS reach the callback, not every report."""
        client = self._client()
        seen: list[tuple[bool, str]] = []
        client.on_status = lambda healthy, reason: seen.append((healthy, reason))

        client._notify_status(False, "connect failed")
        client._notify_status(False, "connect failed")  # duplicate, swallowed
        client._notify_status(True, "")
        client._notify_status(True, "")  # duplicate, swallowed
        client._notify_status(False, "server closed connection immediately")

        assert seen == [
            (False, "connect failed"),
            (True, ""),
            (False, "server closed connection immediately"),
        ]

    def test_callback_exception_is_swallowed(self) -> None:
        """A broken status observer must never break the WS loop."""
        client = self._client()

        def _boom(healthy: bool, reason: str) -> None:
            raise RuntimeError("observer bug")

        client.on_status = _boom
        client._notify_status(False, "connect failed")  # must not raise
        # The transition was still recorded, so dedupe keeps working.
        assert client._last_status is False

    def test_no_callback_still_records_state(self) -> None:
        """Without an observer the transition is recorded (late wiring safe-ish),
        and a later opposite transition still fires a freshly-set callback."""
        client = self._client()
        client._notify_status(True, "")
        assert client._last_status is True
        seen: list[bool] = []
        client.on_status = lambda healthy, reason: seen.append(healthy)
        client._notify_status(False, "dropped")
        assert seen == [False]

    @pytest.mark.asyncio
    async def test_run_loop_reports_connect_failure_reason(self) -> None:
        """A failing connect attempt surfaces not-healthy with a reason."""
        client = self._client()
        seen: list[tuple[bool, str]] = []
        client.on_status = lambda healthy, reason: seen.append((healthy, reason))

        calls = 0

        async def _fail_then_stop() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise aiohttp.ClientError("dns boom")
            client._closed = True  # stop the loop on the second pass

        client._connect_and_serve = _fail_then_stop  # type: ignore[method-assign]

        async def _no_sleep(_delay: float) -> None:
            return None

        with patch("kiro_crew.wecom.client.asyncio.sleep", _no_sleep):
            await client._run_loop()

        assert seen and seen[0][0] is False
        assert "dns boom" in seen[0][1]

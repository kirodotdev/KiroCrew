"""Agent-role ACP server: protocol conformance and permission gating."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from kiro_crew.acp.types import (
    CONFIG_OPTION_MODEL,
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_PARSE_ERROR,
    METHOD_SESSION_UPDATE,
    OPTION_ALLOW_ONCE,
    OPTION_REJECT_ONCE,
    OUTCOME_CANCELLED,
    OUTCOME_SELECTED,
    SESSION_MODE_DEFAULT_ID,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
    UPDATE_AVAILABLE_COMMANDS,
    UPDATE_CONFIG_OPTION,
    UPDATE_CURRENT_MODE,
)
from kiro_crew.acp_server import transport as transport_mod
from kiro_crew.acp_server.http_backend import build_mode_state, build_model_config_option
from kiro_crew.acp_server.server import (
    DEFAULT_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSION,
    AcpAgentServer,
    PromptRequest,
    SelectorBusyError,
    SelectorState,
    SessionSink,
    extract_prompt_text,
)
from kiro_crew.acp_server.transport import AcpServerError, AgentTransport


class _CapturingWriter:
    """Collects newline-delimited JSON frames the agent writes."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self._buf = b""

    def write(self, data: bytes) -> None:
        self._buf += data
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if line.strip():
                self.frames.append(json.loads(line))

    async def drain(self) -> None:
        return None

    def find(self, *, req_id: Any = None, method: str | None = None) -> dict[str, Any] | None:
        for f in self.frames:
            if req_id is not None and f.get("id") == req_id and f.get("method") is None:
                return f
            if method is not None and f.get("method") == method:
                return f
        return None


class _Harness:
    """Drives an AcpAgentServer over in-memory pipes."""

    def __init__(self, handler: Any, backend: Any = None) -> None:
        self.reader = asyncio.StreamReader()
        self.writer = _CapturingWriter()
        self.transport = AgentTransport(self.reader, self.writer)
        self.server = AcpAgentServer(self.transport, handler, session_backend=backend)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self.server.serve())

    def send(self, payload: dict[str, Any]) -> None:
        self.reader.feed_data((json.dumps(payload) + "\n").encode("utf-8"))

    def send_raw(self, text: str) -> None:
        self.reader.feed_data((text + "\n").encode("utf-8"))

    async def stop(self) -> None:
        self.reader.feed_eof()
        if self._task is not None:
            await asyncio.wait_for(self._task, timeout=5)

    async def wait_for(self, predicate: Any, timeout: float = 2.0) -> dict[str, Any]:
        """Poll captured frames until predicate matches one."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            for f in self.writer.frames:
                if predicate(f):
                    return f
            await asyncio.sleep(0.01)
        raise AssertionError(f"no frame matched within {timeout}s: {self.writer.frames}")


async def _noop_handler(_req: PromptRequest, _sink: SessionSink) -> str:
    return STOP_REASON_END_TURN


async def _new_session(h: _Harness) -> str:
    h.send({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": "/tmp"}})
    frame = await h.wait_for(lambda f: f.get("id") == 1 and "result" in f)
    return str(frame["result"]["sessionId"])


class TestInitialize:
    @pytest.mark.asyncio
    async def test_negotiates_integer_v1(self) -> None:
        h = _Harness(_noop_handler)
        await h.start()
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "initialize",
                "params": {"protocolVersion": 1},
            }
        )
        frame = await h.wait_for(lambda f: f.get("id") == 7 and "result" in f)
        # Strict ACP v1: an offered integer 1 is answered with integer 1.
        assert frame["result"]["protocolVersion"] == SUPPORTED_PROTOCOL_VERSION == 1
        assert frame["result"]["agentCapabilities"]["loadSession"] is False
        await h.stop()

    @pytest.mark.asyncio
    async def test_missing_version_negotiates_to_v1(self) -> None:
        h = _Harness(_noop_handler)
        await h.start()
        h.send({"jsonrpc": "2.0", "id": 8, "method": "initialize", "params": {}})
        frame = await h.wait_for(lambda f: f.get("id") == 8 and "result" in f)
        assert frame["result"]["protocolVersion"] == DEFAULT_PROTOCOL_VERSION == 1
        await h.stop()

    @pytest.mark.asyncio
    async def test_unsupported_version_is_not_echoed(self) -> None:
        # A peer offering an unsupported spelling (e.g. kiro's date string) must
        # NOT get it echoed back — we answer with the version we actually speak.
        h = _Harness(_noop_handler)
        await h.start()
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "initialize",
                "params": {"protocolVersion": "2025-08-22"},
            }
        )
        frame = await h.wait_for(lambda f: f.get("id") == 9 and "result" in f)
        assert frame["result"]["protocolVersion"] == SUPPORTED_PROTOCOL_VERSION
        await h.stop()


class TestSessionLifecycle:
    @pytest.mark.asyncio
    async def test_session_new_mints_id(self) -> None:
        h = _Harness(_noop_handler)
        await h.start()
        sid = await _new_session(h)
        assert sid.startswith("kirocrew-")
        await h.stop()

    @pytest.mark.asyncio
    async def test_prompt_on_unknown_session_errors(self) -> None:
        h = _Harness(_noop_handler)
        await h.start()
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {"sessionId": "nope", "prompt": []},
            }
        )
        frame = await h.wait_for(lambda f: f.get("id") == 3 and "error" in f)
        # session/prompt DOES exist; only the sessionId is invalid -> -32602
        # Invalid params, NOT -32601 Method not found (a strict client must not
        # read a bad session id as "this agent lacks session/prompt").
        assert frame["error"]["code"] == JSONRPC_INVALID_PARAMS
        await h.stop()

    @pytest.mark.asyncio
    async def test_unknown_method_is_answered_not_dropped(self) -> None:
        # An unanswered JSON-RPC request blocks the peer forever.
        h = _Harness(_noop_handler)
        await h.start()
        h.send({"jsonrpc": "2.0", "id": 4, "method": "totally/unknown", "params": {}})
        frame = await h.wait_for(lambda f: f.get("id") == 4 and "error" in f)
        assert frame["error"]["code"] == JSONRPC_METHOD_NOT_FOUND
        await h.stop()

    @pytest.mark.asyncio
    async def test_config_methods_are_method_not_found(self) -> None:
        # set_mode/set_model/set_config_option are NOT implemented; no-op success
        # would make an editor believe a switch took effect. They must 404.
        h = _Harness(_noop_handler)
        await h.start()
        for req_id, method in (
            (5, "session/set_model"),
            (6, "session/set_mode"),
            (11, "session/set_config_option"),
        ):
            h.send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": {}})
            frame = await h.wait_for(lambda f, rid=req_id: f.get("id") == rid and "error" in f)
            assert frame["error"]["code"] == JSONRPC_METHOD_NOT_FOUND
        await h.stop()


class TestPromptTurn:
    @pytest.mark.asyncio
    async def test_streams_text_then_completes(self) -> None:
        async def handler(req: PromptRequest, sink: SessionSink) -> str:
            assert req.text == "hello there"
            await sink.send_text("hi back")
            return STOP_REASON_END_TURN

        h = _Harness(handler)
        await h.start()
        sid = await _new_session(h)
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {
                    "sessionId": sid,
                    "prompt": [
                        {"type": "text", "text": "hello "},
                        {"type": "text", "text": "there"},
                    ],
                },
            }
        )
        update = await h.wait_for(lambda f: f.get("method") == "session/update")
        assert update["params"]["update"]["content"]["text"] == "hi back"
        done = await h.wait_for(lambda f: f.get("id") == 2 and "result" in f)
        assert done["result"]["stopReason"] == STOP_REASON_END_TURN
        await h.stop()

    @pytest.mark.asyncio
    async def test_handler_exception_answers_internal_error(self) -> None:
        # A handler fault is an internal error, not a turn end. It must NOT be an
        # out-of-schema stopReason="error" — it is a JSON-RPC internal error.
        async def handler(_req: PromptRequest, _sink: SessionSink) -> str:
            raise RuntimeError("boom")

        h = _Harness(handler)
        await h.start()
        sid = await _new_session(h)
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": []},
            }
        )
        done = await h.wait_for(lambda f: f.get("id") == 2 and "error" in f)
        assert done["error"]["code"] == JSONRPC_INTERNAL_ERROR
        await h.stop()

    @pytest.mark.asyncio
    async def test_non_conformant_stop_reason_is_internal_error(self) -> None:
        # A handler returning a non-ACP sentinel (the HTTP backend returns the
        # bare "error" when the gateway is unreachable) must not surface as an
        # out-of-schema stopReason; it maps to a JSON-RPC internal error.
        async def handler(_req: PromptRequest, _sink: SessionSink) -> str:
            return "error"

        h = _Harness(handler)
        await h.start()
        sid = await _new_session(h)
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": []},
            }
        )
        done = await h.wait_for(lambda f: f.get("id") == 2 and "error" in f)
        assert done["error"]["code"] == JSONRPC_INTERNAL_ERROR
        await h.stop()

    @pytest.mark.asyncio
    async def test_refusal_stop_reason_is_passed_through(self) -> None:
        # "refusal" IS a valid ACP stop reason and must survive as a result.
        async def handler(_req: PromptRequest, _sink: SessionSink) -> str:
            return "refusal"

        h = _Harness(handler)
        await h.start()
        sid = await _new_session(h)
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": []},
            }
        )
        done = await h.wait_for(lambda f: f.get("id") == 2 and "result" in f)
        assert done["result"]["stopReason"] == "refusal"
        await h.stop()

    @pytest.mark.asyncio
    async def test_cancel_notification_overrides_stop_reason(self) -> None:
        started = asyncio.Event()

        async def handler(_req: PromptRequest, sink: SessionSink) -> str:
            started.set()
            for _ in range(200):
                if sink.cancelled:
                    break
                await asyncio.sleep(0.01)
            return STOP_REASON_END_TURN

        h = _Harness(handler)
        await h.start()
        sid = await _new_session(h)
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": []},
            }
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        h.send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}})
        done = await h.wait_for(lambda f: f.get("id") == 2 and "result" in f)
        assert done["result"]["stopReason"] == STOP_REASON_CANCELLED
        await h.stop()


class TestPermissionGate:
    """``request_permission`` is the inline-diff review path — fail-closed."""

    async def _run_permission(self, answer: dict[str, Any] | None) -> bool:
        outcome: dict[str, bool] = {}

        async def handler(_req: PromptRequest, sink: SessionSink) -> str:
            outcome["allowed"] = await sink.request_permission(
                {
                    "toolCallId": "t1",
                    "title": "edit file",
                    "kind": "edit",
                    "content": [
                        {
                            "type": "diff",
                            "path": "/tmp/a.py",
                            "oldText": "a",
                            "newText": "b",
                        }
                    ],
                }
            )
            return STOP_REASON_END_TURN

        h = _Harness(handler)
        await h.start()
        sid = await _new_session(h)
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": []},
            }
        )
        req = await h.wait_for(lambda f: f.get("method") == "session/request_permission")
        # The diff must ride inside the permission request itself.
        assert req["params"]["toolCall"]["content"][0]["type"] == "diff"
        ids = [o["optionId"] for o in req["params"]["options"]]
        assert ids == [OPTION_ALLOW_ONCE, OPTION_REJECT_ONCE]
        # Public-ACP shape: optionId/name, not kiro's id/label.
        assert all("name" in o for o in req["params"]["options"])
        if answer is not None:
            h.send({"jsonrpc": "2.0", "id": req["id"], "result": answer})
        else:
            h.send({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -1, "message": "x"}})
        await h.wait_for(lambda f: f.get("id") == 2 and "result" in f)
        await h.stop()
        return bool(outcome.get("allowed"))

    @pytest.mark.asyncio
    async def test_allow_once_grants(self) -> None:
        granted = await self._run_permission(
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": OPTION_ALLOW_ONCE}}
        )
        assert granted is True

    @pytest.mark.asyncio
    async def test_cancelled_outcome_denies(self) -> None:
        granted = await self._run_permission({"outcome": {"outcome": OUTCOME_CANCELLED}})
        assert granted is False

    @pytest.mark.asyncio
    async def test_reject_option_denies(self) -> None:
        granted = await self._run_permission(
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": OPTION_REJECT_ONCE}}
        )
        assert granted is False

    @pytest.mark.asyncio
    async def test_error_response_denies(self) -> None:
        assert await self._run_permission(None) is False

    @pytest.mark.asyncio
    async def test_malformed_result_denies(self) -> None:
        assert await self._run_permission({"nonsense": True}) is False


class TestFramingRobustness:
    @pytest.mark.asyncio
    async def test_malformed_frame_does_not_kill_session(self) -> None:
        h = _Harness(_noop_handler)
        await h.start()
        h.send_raw("{not json")
        h.send_raw("[1,2,3]")  # valid JSON, wrong shape
        # The strict transport answers rather than dropping — with id=null, since
        # no id can be recovered — and keeps the session alive.
        pe = await h.wait_for(lambda f: f.get("error", {}).get("code") == JSONRPC_PARSE_ERROR)
        assert pe["id"] is None
        ir = await h.wait_for(lambda f: f.get("error", {}).get("code") == JSONRPC_INVALID_REQUEST)
        assert ir["id"] is None
        sid = await _new_session(h)
        assert sid
        await h.stop()

    @pytest.mark.asyncio
    async def test_inbound_request_id_collision_is_not_a_response(self) -> None:
        """An inbound request whose id equals our in-flight request's id.

        Response correlation requires ``method is None``; without that guard the
        colliding request is misread as the permission answer, resolving it with
        garbage and stranding the real request.
        """
        seen: dict[str, Any] = {}

        async def handler(_req: PromptRequest, sink: SessionSink) -> str:
            seen["allowed"] = await sink.request_permission({"toolCallId": "t1"})
            return STOP_REASON_END_TURN

        h = _Harness(handler)
        await h.start()
        sid = await _new_session(h)
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": []},
            }
        )
        req = await h.wait_for(lambda f: f.get("method") == "session/request_permission")
        collide = req["id"]
        # Same id, but carries a method => it is a REQUEST, not our response.
        h.send({"jsonrpc": "2.0", "id": collide, "method": "totally/unknown", "params": {}})
        # It must be answered as a request...
        err = await h.wait_for(
            lambda f: f.get("id") == collide and "error" in f and f.get("method") is None
        )
        assert err["error"]["code"] == JSONRPC_METHOD_NOT_FOUND
        # ...and the permission must still be pending, resolvable afterwards.
        assert "allowed" not in seen
        h.send(
            {
                "jsonrpc": "2.0",
                "id": collide,
                "result": {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": OPTION_ALLOW_ONCE}},
            }
        )
        await h.wait_for(lambda f: f.get("id") == 2 and "result" in f)
        assert seen["allowed"] is True
        await h.stop()


class TestPromptTextExtraction:
    def test_concatenates_text_blocks_only(self) -> None:
        params = {
            "prompt": [
                {"type": "text", "text": "a"},
                {"type": "image", "data": "ignored"},
                {"type": "text", "text": "b"},
            ]
        }
        assert extract_prompt_text(params) == "ab"

    def test_missing_prompt_is_empty(self) -> None:
        assert extract_prompt_text({}) == ""


class _Recorder:
    """Collects inbound requests/notifications for bare-transport tests."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any], Any]] = []
        self.notifications: list[tuple[str, dict[str, Any]]] = []

    async def on_request(self, method: str, params: dict[str, Any], req_id: Any) -> None:
        self.requests.append((method, params, req_id))

    async def on_notification(self, method: str, params: dict[str, Any]) -> None:
        self.notifications.append((method, params))


def _bare_transport() -> tuple[asyncio.StreamReader, _CapturingWriter, AgentTransport]:
    reader = asyncio.StreamReader()
    writer = _CapturingWriter()
    return reader, writer, AgentTransport(reader, writer)


class TestTransportRequests:
    @pytest.mark.asyncio
    async def test_timeout_when_peer_never_answers(self) -> None:
        _reader, _writer, transport = _bare_transport()
        with pytest.raises(asyncio.TimeoutError):
            await transport.send_request("session/request_permission", {}, timeout=0.05)

    @pytest.mark.asyncio
    async def test_peer_error_raises_acp_server_error(self) -> None:
        reader, writer, transport = _bare_transport()
        rec = _Recorder()
        task = asyncio.create_task(transport.run(rec.on_request, rec.on_notification))
        send = asyncio.create_task(transport.send_request("x/y", {}, timeout=2))
        await asyncio.sleep(0.02)
        req_id = writer.frames[0]["id"]
        reader.feed_data(
            (
                json.dumps(
                    {"jsonrpc": "2.0", "id": req_id, "error": {"code": -1, "message": "nope"}}
                )
                + "\n"
            ).encode("utf-8")
        )
        with pytest.raises(AcpServerError):
            await send
        reader.feed_eof()
        await asyncio.wait_for(task, timeout=5)

    @pytest.mark.asyncio
    async def test_pending_request_fails_on_eof(self) -> None:
        # Otherwise a handler awaiting the editor hangs until its full timeout
        # after the editor has already gone away.
        reader, _writer, transport = _bare_transport()
        rec = _Recorder()
        task = asyncio.create_task(transport.run(rec.on_request, rec.on_notification))
        send = asyncio.create_task(transport.send_request("x/y", {}, timeout=10))
        await asyncio.sleep(0.02)
        reader.feed_eof()
        with pytest.raises(ConnectionError):
            await send
        await asyncio.wait_for(task, timeout=5)

    @pytest.mark.asyncio
    async def test_close_fails_pending(self) -> None:
        _reader, _writer, transport = _bare_transport()
        send = asyncio.create_task(transport.send_request("x/y", {}, timeout=10))
        await asyncio.sleep(0.02)
        await transport.close()
        with pytest.raises(ConnectionError):
            await send


class TestTransportFraming:
    @pytest.mark.asyncio
    async def test_oversized_frame_is_drained_without_killing_session(self) -> None:
        reader = asyncio.StreamReader(limit=64)
        writer = _CapturingWriter()
        transport = AgentTransport(reader, writer)
        rec = _Recorder()
        task = asyncio.create_task(transport.run(rec.on_request, rec.on_notification))
        reader.feed_data((b"{" + b"x" * 200 + b"}\n"))
        reader.feed_data(b'{"jsonrpc":"2.0","method":"still/alive","params":{}}\n')
        await asyncio.sleep(0.05)
        reader.feed_eof()
        await asyncio.wait_for(task, timeout=5)
        assert [m for m, _p in rec.notifications] == ["still/alive"]
        errs = [f for f in writer.frames if "error" in f]
        assert errs and errs[0]["error"]["message"] == "Frame too large"

    @pytest.mark.asyncio
    async def test_two_frames_in_one_chunk_both_dispatch(self) -> None:
        reader, _writer, transport = _bare_transport()
        rec = _Recorder()
        task = asyncio.create_task(transport.run(rec.on_request, rec.on_notification))
        blob = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "a/b", "params": {}})
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "method": "c/d", "params": {}})
            + "\n"
        )
        reader.feed_data(blob.encode("utf-8"))
        await asyncio.sleep(0.05)
        reader.feed_eof()
        await asyncio.wait_for(task, timeout=5)
        assert [m for m, _p, _i in rec.requests] == ["a/b"]
        assert [m for m, _p in rec.notifications] == ["c/d"]

    @pytest.mark.asyncio
    async def test_frame_without_method_or_id_gets_invalid_request(self) -> None:
        reader, writer, transport = _bare_transport()
        rec = _Recorder()
        task = asyncio.create_task(transport.run(rec.on_request, rec.on_notification))
        reader.feed_data(b'{"jsonrpc":"2.0"}\n')  # neither method nor id
        reader.feed_data(b'{"jsonrpc":"2.0","method":"ok/one","params":{}}\n')
        await asyncio.sleep(0.05)
        reader.feed_eof()
        await asyncio.wait_for(task, timeout=5)
        # The following notification must still be processed (loop survives)...
        assert [m for m, _p in rec.notifications] == ["ok/one"]
        # ...and the invalid frame is answered, not silently dropped.
        errs = [f for f in writer.frames if "error" in f]
        assert errs and errs[0]["error"]["code"] == JSONRPC_INVALID_REQUEST
        assert errs[0]["id"] is None

    @pytest.mark.asyncio
    async def test_response_for_unknown_id_is_dropped(self) -> None:
        reader, _writer, transport = _bare_transport()
        rec = _Recorder()
        task = asyncio.create_task(transport.run(rec.on_request, rec.on_notification))
        reader.feed_data(b'{"jsonrpc":"2.0","id":4242,"result":{}}\n')
        reader.feed_data(b'{"jsonrpc":"2.0","method":"still/alive","params":{}}\n')
        await asyncio.sleep(0.05)
        reader.feed_eof()
        await asyncio.wait_for(task, timeout=5)
        assert [m for m, _p in rec.notifications] == ["still/alive"]

    @pytest.mark.asyncio
    async def test_concurrent_sends_do_not_interleave(self) -> None:
        # The write lock exists so two senders cannot emit partial frames.
        _reader, writer, transport = _bare_transport()
        await asyncio.gather(*(transport.send_notification("n/i", {"i": i}) for i in range(25)))
        assert len(writer.frames) == 25
        assert sorted(f["params"]["i"] for f in writer.frames) == list(range(25))


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_two_sessions_prompt_in_parallel(self) -> None:
        """Turns on distinct sessions must overlap.

        Regression guard for awaiting the handler inline in the read loop, which
        serialised every request behind the current turn.
        """
        active = 0
        peak = 0
        release = asyncio.Event()

        async def handler(_req: PromptRequest, _sink: SessionSink) -> str:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await release.wait()
            active -= 1
            return STOP_REASON_END_TURN

        h = _Harness(handler)
        await h.start()
        h.send({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": "/tmp"}})
        f1 = await h.wait_for(lambda f: f.get("id") == 1 and "result" in f)
        h.send({"jsonrpc": "2.0", "id": 9, "method": "session/new", "params": {"cwd": "/tmp"}})
        f2 = await h.wait_for(lambda f: f.get("id") == 9 and "result" in f)
        for req_id, sid in ((20, f1["result"]["sessionId"]), (21, f2["result"]["sessionId"])):
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "session/prompt",
                    "params": {"sessionId": sid, "prompt": []},
                }
            )
        await asyncio.sleep(0.1)
        assert peak == 2, f"turns serialised (peak={peak})"
        release.set()
        await h.wait_for(lambda f: f.get("id") == 20 and "result" in f)
        await h.wait_for(lambda f: f.get("id") == 21 and "result" in f)
        await h.stop()

    @pytest.mark.asyncio
    async def test_stale_cancel_does_not_abort_next_turn(self) -> None:
        """A cancel from turn N must not kill turn N+1 on the same session."""
        h = _Harness(_noop_handler)
        await h.start()
        sid = await _new_session(h)
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": []},
            }
        )
        h.send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}})
        await h.wait_for(lambda f: f.get("id") == 2 and "result" in f)
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": []},
            }
        )
        done = await h.wait_for(lambda f: f.get("id") == 3 and "result" in f)
        assert done["result"]["stopReason"] == STOP_REASON_END_TURN
        await h.stop()

    @pytest.mark.asyncio
    async def test_cancel_for_unknown_session_is_ignored(self) -> None:
        h = _Harness(_noop_handler)
        await h.start()
        h.send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "ghost"}})
        sid = await _new_session(h)
        assert sid
        await h.stop()


class TestPermissionOptionLabels:
    @pytest.mark.asyncio
    async def test_custom_labels_are_sent(self) -> None:
        async def handler(_req: PromptRequest, sink: SessionSink) -> str:
            await sink.request_permission(
                {"toolCallId": "t1"}, allow_label="Apply patch", reject_label="Discard"
            )
            return STOP_REASON_END_TURN

        h = _Harness(handler)
        await h.start()
        sid = await _new_session(h)
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": []},
            }
        )
        req = await h.wait_for(lambda f: f.get("method") == "session/request_permission")
        names = [o["name"] for o in req["params"]["options"]]
        assert names == ["Apply patch", "Discard"]
        h.send(
            {
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": {"outcome": {"outcome": OUTCOME_CANCELLED}},
            }
        )
        await h.wait_for(lambda f: f.get("id") == 2 and "result" in f)
        await h.stop()


class TestDrainTasks:
    """EOF must not strand or leak an in-flight handler."""

    @pytest.mark.asyncio
    async def test_hung_handler_is_cancelled_after_grace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(transport_mod, "DRAIN_TIMEOUT", 0.05)
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def on_request(_m: str, _p: dict[str, Any], _i: Any) -> None:
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        async def on_notification(_m: str, _p: dict[str, Any]) -> None:
            return None

        reader, _writer, tp = _bare_transport()
        task = asyncio.create_task(tp.run(on_request, on_notification))
        reader.feed_data(b'{"jsonrpc":"2.0","id":1,"method":"slow/op","params":{}}\n')
        await asyncio.wait_for(started.wait(), timeout=2)
        reader.feed_eof()
        # run() must return rather than block on the hung handler.
        await asyncio.wait_for(task, timeout=3)
        await asyncio.wait_for(cancelled.wait(), timeout=2)

    @pytest.mark.asyncio
    async def test_fast_handler_completes_before_drain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(transport_mod, "DRAIN_TIMEOUT", 2.0)
        finished = asyncio.Event()

        async def on_request(_m: str, _p: dict[str, Any], req_id: Any) -> None:
            await asyncio.sleep(0.02)
            finished.set()

        async def on_notification(_m: str, _p: dict[str, Any]) -> None:
            return None

        reader, _writer, tp = _bare_transport()
        task = asyncio.create_task(tp.run(on_request, on_notification))
        reader.feed_data(b'{"jsonrpc":"2.0","id":1,"method":"quick/op","params":{}}\n')
        await asyncio.sleep(0.01)
        reader.feed_eof()
        await asyncio.wait_for(task, timeout=3)
        assert finished.is_set(), "handler was cancelled instead of allowed to finish"


class TestFramingStrictness:
    """Strict JSON-RPC 2.0: bad frames are answered with the right error, not dropped."""

    @pytest.mark.asyncio
    async def test_parse_error_answered_with_null_id(self) -> None:
        reader, writer, transport = _bare_transport()
        rec = _Recorder()
        task = asyncio.create_task(transport.run(rec.on_request, rec.on_notification))
        reader.feed_data(b"{not json\n")
        reader.feed_data(b'{"jsonrpc":"2.0","method":"still/alive","params":{}}\n')
        await asyncio.sleep(0.05)
        reader.feed_eof()
        await asyncio.wait_for(task, timeout=5)
        errs = [f for f in writer.frames if "error" in f]
        assert errs and errs[0]["error"]["code"] == JSONRPC_PARSE_ERROR
        assert errs[0]["id"] is None
        # The loop survives one bad line.
        assert [m for m, _p in rec.notifications] == ["still/alive"]

    @pytest.mark.asyncio
    async def test_non_object_frame_is_invalid_request(self) -> None:
        reader, writer, transport = _bare_transport()
        rec = _Recorder()
        task = asyncio.create_task(transport.run(rec.on_request, rec.on_notification))
        reader.feed_data(b"[1,2,3]\n")
        await asyncio.sleep(0.05)
        reader.feed_eof()
        await asyncio.wait_for(task, timeout=5)
        errs = [f for f in writer.frames if "error" in f]
        assert errs and errs[0]["error"]["code"] == JSONRPC_INVALID_REQUEST
        assert errs[0]["id"] is None

    @pytest.mark.asyncio
    async def test_bad_jsonrpc_version_is_invalid_request(self) -> None:
        reader, writer, transport = _bare_transport()
        rec = _Recorder()
        task = asyncio.create_task(transport.run(rec.on_request, rec.on_notification))
        reader.feed_data(b'{"jsonrpc":"1.0","id":5,"method":"a/b","params":{}}\n')
        await asyncio.sleep(0.05)
        reader.feed_eof()
        await asyncio.wait_for(task, timeout=5)
        errs = [f for f in writer.frames if "error" in f]
        assert errs and errs[0]["error"]["code"] == JSONRPC_INVALID_REQUEST
        # A determinable id is echoed on the error.
        assert errs[0]["id"] == 5
        # The malformed frame is NOT dispatched as a request.
        assert rec.requests == []

    @pytest.mark.asyncio
    async def test_non_string_method_is_invalid_request(self) -> None:
        reader, writer, transport = _bare_transport()
        rec = _Recorder()
        task = asyncio.create_task(transport.run(rec.on_request, rec.on_notification))
        reader.feed_data(b'{"jsonrpc":"2.0","id":6,"method":123,"params":{}}\n')
        await asyncio.sleep(0.05)
        reader.feed_eof()
        await asyncio.wait_for(task, timeout=5)
        errs = [f for f in writer.frames if "error" in f]
        assert errs and errs[0]["error"]["code"] == JSONRPC_INVALID_REQUEST
        assert errs[0]["id"] == 6
        assert rec.requests == []


class TestParamValidation:
    """ACP request parameter validation → -32602 Invalid params."""

    @pytest.mark.asyncio
    async def test_session_new_requires_absolute_cwd(self) -> None:
        h = _Harness(_noop_handler)
        await h.start()
        for rid, params in ((1, {}), (2, {"cwd": ""}), (3, {"cwd": "relative/dir"})):
            h.send({"jsonrpc": "2.0", "id": rid, "method": "session/new", "params": params})
            frame = await h.wait_for(lambda f, r=rid: f.get("id") == r and "error" in f)
            assert frame["error"]["code"] == JSONRPC_INVALID_PARAMS
        await h.stop()

    @pytest.mark.asyncio
    async def test_prompt_requires_session_id(self) -> None:
        h = _Harness(_noop_handler)
        await h.start()
        h.send({"jsonrpc": "2.0", "id": 1, "method": "session/prompt", "params": {"prompt": []}})
        frame = await h.wait_for(lambda f: f.get("id") == 1 and "error" in f)
        assert frame["error"]["code"] == JSONRPC_INVALID_PARAMS
        await h.stop()

    @pytest.mark.asyncio
    async def test_prompt_rejects_non_list_prompt(self) -> None:
        h = _Harness(_noop_handler)
        await h.start()
        sid = await _new_session(h)
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": "hi"},
            }
        )
        frame = await h.wait_for(lambda f: f.get("id") == 2 and "error" in f)
        assert frame["error"]["code"] == JSONRPC_INVALID_PARAMS
        await h.stop()

    @pytest.mark.asyncio
    async def test_prompt_rejects_block_without_type(self) -> None:
        h = _Harness(_noop_handler)
        await h.start()
        sid = await _new_session(h)
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"no": "type"}]},
            }
        )
        frame = await h.wait_for(lambda f: f.get("id") == 2 and "error" in f)
        assert frame["error"]["code"] == JSONRPC_INVALID_PARAMS
        await h.stop()

    @pytest.mark.asyncio
    async def test_empty_prompt_list_is_a_valid_turn(self) -> None:
        # An empty content array is a valid (contentless) prompt, not an error.
        h = _Harness(_noop_handler)
        await h.start()
        sid = await _new_session(h)
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": []},
            }
        )
        done = await h.wait_for(lambda f: f.get("id") == 2 and "result" in f)
        assert done["result"]["stopReason"] == STOP_REASON_END_TURN
        await h.stop()


class TestCapabilityDiscipline:
    """Unadvertised optional methods fail explicitly rather than hanging or no-oping."""

    @pytest.mark.asyncio
    async def test_initialize_advertises_no_optional_caps_without_backend(self) -> None:
        h = _Harness(_noop_handler)
        await h.start()
        h.send(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}}
        )
        frame = await h.wait_for(lambda f: f.get("id") == 1 and "result" in f)
        caps = frame["result"]["agentCapabilities"]
        assert caps["loadSession"] is False
        assert "sessionCapabilities" not in caps
        await h.stop()

    @pytest.mark.asyncio
    async def test_unadvertised_optional_methods_are_method_not_found(self) -> None:
        h = _Harness(_noop_handler)
        await h.start()
        cases = (
            (1, "session/load", {"sessionId": "x", "cwd": "/tmp"}),
            (2, "session/list", {}),
            (3, "session/resume", {"sessionId": "x", "cwd": "/tmp"}),
        )
        for rid, method, params in cases:
            h.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            frame = await h.wait_for(lambda f, r=rid: f.get("id") == r and "error" in f)
            assert frame["error"]["code"] == JSONRPC_METHOD_NOT_FOUND, method
        await h.stop()


class _StubMcpBackend:
    """A SessionBackend that advertises nothing but records MCP configuration."""

    supports_load = False
    supports_list = False
    supports_resume = False

    def __init__(self) -> None:
        self.configured: list[tuple[str, list[Any]]] = []

    async def create_session(self, cwd: str) -> str:
        return "s-1"

    async def load_session(self, session_id: str, cwd: str) -> list[dict[str, str]]:
        return []

    async def list_sessions(self, *, cwd: Any = None, cursor: Any = None) -> dict[str, Any]:
        return {"sessions": []}

    async def resume_session(self, session_id: str, cwd: str) -> None:
        return None

    async def cancel(self, session_id: str) -> None:
        return None

    async def configure_session_mcp(self, session_id: str, servers: list[Any]) -> None:
        self.configured.append((session_id, list(servers)))


class _ClientSafeError(RuntimeError):
    """Mimics McpSpawnError's client-safe marker without the spawn machinery."""

    acp_client_safe = True


class _CommandBackend(_StubMcpBackend):
    supports_load = True
    supports_resume = True

    def __init__(self) -> None:
        super().__init__()
        self.command_calls: list[str] = []

    async def get_available_commands(self, session_id: str) -> list[Any]:
        self.command_calls.append(session_id)
        return [
            {
                "name": "/help",
                "description": "Show available commands",
                "input": {"hint": "topic"},
            },
            {"name": "help", "description": "duplicate"},
            {"name": "bad command", "description": "invalid"},
            {"description": "missing name"},
        ]


class _SafeFailMcpBackend(_StubMcpBackend):
    """configure_session_mcp fails with an actionable, client-safe message."""

    async def configure_session_mcp(self, session_id: str, servers: list[Any]) -> None:
        raise _ClientSafeError("MCP server 'echo' failed to start: command not found")


class _CrashMcpBackend(_StubMcpBackend):
    """configure_session_mcp raises an unexpected error carrying internal detail."""

    async def configure_session_mcp(self, session_id: str, servers: list[Any]) -> None:
        raise RuntimeError("secret internal path /home/x/.aws/creds")


class _NoHookMcpBackend:
    """A SessionBackend that cannot host MCP servers (no configure hook)."""

    supports_load = False
    supports_list = False
    supports_resume = False

    async def create_session(self, cwd: str) -> str:
        return "s-1"

    async def load_session(self, session_id: str, cwd: str) -> list[dict[str, str]]:
        return []

    async def list_sessions(self, *, cwd: Any = None, cursor: Any = None) -> dict[str, Any]:
        return {"sessions": []}

    async def resume_session(self, session_id: str, cwd: str) -> None:
        return None

    async def cancel(self, session_id: str) -> None:
        return None


class TestAvailableCommandAdvertisement:
    @pytest.mark.asyncio
    async def test_new_load_and_resume_emit_normalized_commands(self) -> None:
        backend = _CommandBackend()
        writer = _CapturingWriter()
        transport = AgentTransport(asyncio.StreamReader(), writer)
        server = AcpAgentServer(transport, _noop_handler, session_backend=backend)

        await server._handle_session_new({"cwd": "/repo"}, 1)
        await server._handle_session_load({"sessionId": "loaded", "cwd": "/repo"}, 2)
        await server._handle_session_resume({"sessionId": "resumed", "cwd": "/repo"}, 3)

        updates = [
            frame["params"]
            for frame in writer.frames
            if frame.get("method") == METHOD_SESSION_UPDATE
            and frame["params"]["update"].get("sessionUpdate") == UPDATE_AVAILABLE_COMMANDS
        ]
        assert [params["sessionId"] for params in updates] == ["s-1", "loaded", "resumed"]
        assert all(
            params["update"]["availableCommands"]
            == [
                {
                    "name": "help",
                    "description": "Show available commands",
                    "input": {"hint": "topic"},
                }
            ]
            for params in updates
        )
        assert backend.command_calls == ["s-1", "loaded", "resumed"]


class _BrokenCommandBackend(_StubMcpBackend):
    async def get_available_commands(self, session_id: str) -> list[Any]:
        raise RuntimeError("discovery unavailable")


class TestAvailableCommandFailureIsolation:
    @pytest.mark.asyncio
    async def test_discovery_failure_does_not_fail_session_new(self) -> None:
        backend = _BrokenCommandBackend()
        writer = _CapturingWriter()
        server = AcpAgentServer(
            AgentTransport(asyncio.StreamReader(), writer),
            _noop_handler,
            session_backend=backend,
        )

        await server._handle_session_new({"cwd": "/repo"}, 4)

        frame = writer.find(req_id=4)
        assert frame is not None and frame["result"]["sessionId"] == "s-1"
        assert not any(item.get("method") == METHOD_SESSION_UPDATE for item in writer.frames)


class TestMcpServers:
    """session/new mcpServers: stdio parsed & handed off; other transports rejected."""

    @pytest.mark.asyncio
    async def test_unsupported_transport_rejected(self) -> None:
        h = _Harness(_noop_handler)
        await h.start()
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session/new",
                "params": {
                    "cwd": "/repo",
                    "mcpServers": [{"name": "remote", "url": "https://mcp.example"}],
                },
            }
        )
        frame = await h.wait_for(lambda f: f.get("id") == 1 and "error" in f)
        assert frame["error"]["code"] == JSONRPC_INVALID_PARAMS
        await h.stop()

    @pytest.mark.asyncio
    async def test_stdio_server_parsed_and_passed_to_backend(self) -> None:
        backend = _StubMcpBackend()
        reader = asyncio.StreamReader()
        writer = _CapturingWriter()
        transport = AgentTransport(reader, writer)
        srv = AcpAgentServer(transport, _noop_handler, session_backend=backend)
        await srv._handle_session_new(
            {
                "cwd": "/repo",
                "mcpServers": [
                    {
                        "name": "echo",
                        "command": "/bin/echo",
                        "args": ["hi"],
                        "env": [{"name": "K", "value": "V"}],
                    }
                ],
            },
            1,
        )
        result = writer.find(req_id=1)
        assert result is not None and result["result"]["sessionId"] == "s-1"
        stored = srv._sessions["s-1"].mcp_servers
        assert len(stored) == 1
        assert stored[0].name == "echo"
        assert stored[0].command == "/bin/echo"
        assert stored[0].args == ["hi"]
        assert stored[0].env == {"K": "V"}
        # The validated config is handed to the backend's optional hook.
        assert backend.configured == [("s-1", stored)]

    @pytest.mark.asyncio
    async def test_spawn_failure_surfaced_as_error_and_session_dropped(self) -> None:
        # A hosting failure with a client-safe message is forwarded verbatim, and
        # the half-created session is removed so a later prompt can't target it.
        backend = _SafeFailMcpBackend()
        writer = _CapturingWriter()
        transport = AgentTransport(asyncio.StreamReader(), writer)
        srv = AcpAgentServer(transport, _noop_handler, session_backend=backend)
        await srv._handle_session_new(
            {"cwd": "/repo", "mcpServers": [{"name": "echo", "command": "/bin/echo"}]},
            7,
        )
        frame = writer.find(req_id=7)
        assert frame is not None and "error" in frame
        assert frame["error"]["code"] == JSONRPC_INTERNAL_ERROR
        assert "command not found" in frame["error"]["message"]
        assert "s-1" not in srv._sessions  # not left half-hosted

    @pytest.mark.asyncio
    async def test_unexpected_hook_error_is_generic_not_leaked(self) -> None:
        # An unmarked exception must not leak internal detail to the editor.
        backend = _CrashMcpBackend()
        writer = _CapturingWriter()
        transport = AgentTransport(asyncio.StreamReader(), writer)
        srv = AcpAgentServer(transport, _noop_handler, session_backend=backend)
        await srv._handle_session_new(
            {"cwd": "/repo", "mcpServers": [{"name": "echo", "command": "/bin/echo"}]},
            8,
        )
        frame = writer.find(req_id=8)
        assert frame is not None and "error" in frame
        assert frame["error"]["code"] == JSONRPC_INTERNAL_ERROR
        assert frame["error"]["message"] == "Failed to start the requested MCP servers"
        assert "/home/x/.aws" not in frame["error"]["message"]
        assert "s-1" not in srv._sessions

    @pytest.mark.asyncio
    async def test_backend_without_hook_refuses_to_accept_mcp(self) -> None:
        # A backend that cannot host client MCP servers must fail the request
        # rather than accept a config that would never run.
        backend = _NoHookMcpBackend()
        writer = _CapturingWriter()
        transport = AgentTransport(asyncio.StreamReader(), writer)
        srv = AcpAgentServer(transport, _noop_handler, session_backend=backend)
        await srv._handle_session_new(
            {"cwd": "/repo", "mcpServers": [{"name": "echo", "command": "/bin/echo"}]},
            9,
        )
        frame = writer.find(req_id=9)
        assert frame is not None and "error" in frame
        assert frame["error"]["code"] == JSONRPC_INTERNAL_ERROR
        assert "cannot host" in frame["error"]["message"]
        assert "s-1" not in srv._sessions


class _ListBackend(_StubMcpBackend):
    """A backend that advertises session/list and records the cwd filter."""

    supports_list = True

    def __init__(self) -> None:
        super().__init__()
        self.list_calls: list[Any] = []

    async def list_sessions(self, *, cwd: Any = None, cursor: Any = None) -> dict[str, Any]:
        self.list_calls.append(cwd)
        return {"sessions": []}


class TestRuntimeFixPass:
    """Focused coverage for the runtime-hardening pass (protocol + concurrency)."""

    @pytest.mark.asyncio
    async def test_boolean_response_id_does_not_resolve_request_one(self) -> None:
        # hash(True) == hash(1) and True == 1: a malicious {"id": true} response
        # must NOT resolve our in-flight outbound request whose id is 1.
        reader, writer, transport = _bare_transport()
        rec = _Recorder()
        task = asyncio.create_task(transport.run(rec.on_request, rec.on_notification))
        send = asyncio.create_task(
            transport.send_request("session/request_permission", {}, timeout=1.0)
        )
        await asyncio.sleep(0.02)
        assert writer.frames[0]["id"] == 1  # first outbound id
        reader.feed_data(
            (json.dumps({"jsonrpc": "2.0", "id": True, "result": {"spoofed": True}}) + "\n").encode(
                "utf-8"
            )
        )
        await asyncio.sleep(0.05)
        assert not send.done()  # bool id was dropped, request still pending
        reader.feed_data(
            (json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}) + "\n").encode("utf-8")
        )
        result = await asyncio.wait_for(send, timeout=1.0)
        assert result == {"ok": True}
        reader.feed_eof()
        await asyncio.wait_for(task, timeout=5)

    @pytest.mark.asyncio
    async def test_concurrent_prompt_is_rejected(self) -> None:
        release = asyncio.Event()
        started = asyncio.Event()

        async def handler(_req: PromptRequest, _sink: SessionSink) -> str:
            started.set()
            await release.wait()
            return STOP_REASON_END_TURN

        h = _Harness(handler)
        await h.start()
        sid = await _new_session(h)
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": []},
            }
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        # A second prompt for the SAME session while the first is in flight.
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": []},
            }
        )
        err = await h.wait_for(lambda f: f.get("id") == 11 and "error" in f)
        assert err["error"]["code"] == JSONRPC_INVALID_PARAMS
        release.set()
        done = await h.wait_for(lambda f: f.get("id") == 10 and "result" in f)
        assert done["result"]["stopReason"] == STOP_REASON_END_TURN
        await h.stop()

    @pytest.mark.asyncio
    async def test_cancel_while_permission_pending_denies_fast(self) -> None:
        outcome: dict[str, bool] = {}
        started = asyncio.Event()

        async def handler(_req: PromptRequest, sink: SessionSink) -> str:
            started.set()
            outcome["allowed"] = await sink.request_permission(
                {"toolCallId": "t", "title": "x", "kind": "other"}
            )
            return STOP_REASON_END_TURN

        h = _Harness(handler)
        await h.start()
        sid = await _new_session(h)
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": []},
            }
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        # Wait until the agent has actually emitted the permission request, then
        # cancel WITHOUT ever answering it.
        await h.wait_for(lambda f: f.get("method") == "session/request_permission")
        h.send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}})
        done = await h.wait_for(lambda f: f.get("id") == 2 and "result" in f)
        assert done["result"]["stopReason"] == STOP_REASON_CANCELLED
        assert outcome["allowed"] is False  # fail-closed: cancel denies the pending permission
        await h.stop()

    @pytest.mark.asyncio
    async def test_session_list_cwd_must_be_absolute(self) -> None:
        backend = _ListBackend()
        reader = asyncio.StreamReader()
        writer = _CapturingWriter()
        transport = AgentTransport(reader, writer)
        server = AcpAgentServer(transport, _noop_handler, session_backend=backend)
        task = asyncio.create_task(server.serve())
        try:
            reader.feed_data(
                (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "session/list",
                            "params": {"cwd": "rel/dir"},
                        }
                    )
                    + "\n"
                ).encode("utf-8")
            )
            await asyncio.sleep(0.05)
            err = writer.find(req_id=1)
            assert err is not None and err["error"]["code"] == JSONRPC_INVALID_PARAMS
            assert backend.list_calls == []  # never queried on a bad cwd
            reader.feed_data(
                (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "session/list",
                            "params": {"cwd": "/abs/dir"},
                        }
                    )
                    + "\n"
                ).encode("utf-8")
            )
            await asyncio.sleep(0.05)
            ok = writer.find(req_id=2)
            assert ok is not None and "result" in ok
            assert backend.list_calls == ["/abs/dir"]
        finally:
            reader.feed_eof()
            await asyncio.wait_for(task, timeout=5)


# ─────────────────────────── selector backends ───────────────────────────


class _SelectorBackend:
    """In-memory SessionBackend with selector hooks for the protocol tests.

    Reuses the production wire-shape builders so the advertised selectors match
    what HttpGatewayBackend emits. State is deliberately simple (one model + one
    effort) — enough to exercise advertise / apply / persist / rollback / serialize.
    """

    supports_load = True
    supports_list = True
    supports_resume = True

    def __init__(self) -> None:
        self.model = ""  # "" = provider default
        self.effort = ""  # "" = default mode
        self.models: list[dict[str, Any]] = [
            {"model_name": "sonnet-4.6-1m", "display_name": "Sonnet 4.6", "description": "default"},
            {"model_name": "opus-4.8", "display_name": "Opus 4.8", "description": "capable"},
        ]
        self.levels: list[str] = ["low", "medium", "high"]
        self.fail_mode = False
        self.fail_config = False
        self.busy_mode = False
        self.busy_config = False
        self.degraded_refresh = False
        self.set_mode_calls: list[str] = []
        self.set_config_calls: list[tuple[str, str]] = []
        self.config_gate: asyncio.Event | None = None
        self._created = 0
        self.history: list[dict[str, str]] = []

    async def create_session(self, cwd: str) -> str:
        self._created += 1
        return f"sel-{self._created}"

    async def load_session(self, session_id: str, cwd: str) -> list[dict[str, str]]:
        return list(self.history)

    async def list_sessions(
        self, *, cwd: str | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        return {"sessions": []}

    async def resume_session(self, session_id: str, cwd: str) -> None:
        return None

    async def delete_session(self, session_id: str) -> None:
        return None

    async def cancel(self, session_id: str) -> None:
        return None

    def _snapshot(self) -> SelectorState:
        return SelectorState(
            modes=build_mode_state(self.effort, self.levels),
            config_options=[build_model_config_option(self.model, self.models)],
        )

    async def get_session_selectors(self, session_id: str) -> SelectorState:
        return self._snapshot()

    async def set_session_mode(self, session_id: str, mode_id: str) -> SelectorState:
        self.set_mode_calls.append(mode_id)
        if self.busy_mode:
            raise SelectorBusyError("slot prompt is in progress")
        if self.fail_mode:
            raise RuntimeError("mode apply failed")
        self.effort = "" if mode_id == SESSION_MODE_DEFAULT_ID else mode_id
        return SelectorState() if self.degraded_refresh else self._snapshot()

    async def set_session_config_option(
        self, session_id: str, config_id: str, value: str
    ) -> SelectorState:
        self.set_config_calls.append((config_id, value))
        if self.busy_config:
            raise SelectorBusyError("slot prompt is in progress")
        if self.config_gate is not None:
            await self.config_gate.wait()
        if self.fail_config:
            raise RuntimeError("config apply failed")
        if config_id == CONFIG_OPTION_MODEL:
            self.model = value
        return SelectorState() if self.degraded_refresh else self._snapshot()


class _NoSelectorBackend:
    """A backend WITHOUT get_session_selectors — selectors stay unimplemented."""

    supports_load = False
    supports_list = False
    supports_resume = False

    async def create_session(self, cwd: str) -> str:
        return "plain-1"

    async def load_session(self, session_id: str, cwd: str) -> list[dict[str, str]]:
        return []

    async def list_sessions(
        self, *, cwd: str | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        return {"sessions": []}

    async def resume_session(self, session_id: str, cwd: str) -> None:
        return None

    async def delete_session(self, session_id: str) -> None:
        return None

    async def cancel(self, session_id: str) -> None:
        return None


async def _new_selector_session(h: _Harness, req_id: int = 1) -> dict[str, Any]:
    """session/new against a selector backend; return the full result object."""
    h.send({"jsonrpc": "2.0", "id": req_id, "method": "session/new", "params": {"cwd": "/tmp"}})
    frame = await h.wait_for(lambda f, rid=req_id: f.get("id") == rid and "result" in f)
    return dict(frame["result"])


def _has_update(h: _Harness, kind: str) -> bool:
    return any(
        f.get("method") == "session/update" and f["params"]["update"].get("sessionUpdate") == kind
        for f in h.writer.frames
    )


class TestSelectors:
    @pytest.mark.asyncio
    async def test_new_advertises_modes_and_config_options(self) -> None:
        h = _Harness(_noop_handler, _SelectorBackend())
        await h.start()
        result = await _new_selector_session(h)
        modes = result["modes"]
        assert modes["currentModeId"] == SESSION_MODE_DEFAULT_ID
        mode_ids = {m["id"] for m in modes["availableModes"]}
        assert SESSION_MODE_DEFAULT_ID in mode_ids
        assert {"low", "medium", "high"} <= mode_ids
        options = result["configOptions"]
        assert len(options) == 1
        model_opt = options[0]
        assert model_opt["id"] == CONFIG_OPTION_MODEL
        assert model_opt["type"] == "select"
        assert model_opt["category"] == "model"
        # "" slot model resolves to the default-first option.
        assert model_opt["currentValue"] == "sonnet-4.6-1m"
        assert {o["value"] for o in model_opt["options"]} == {"sonnet-4.6-1m", "opus-4.8"}
        await h.stop()

    @pytest.mark.asyncio
    async def test_set_mode_applies_and_notifies(self) -> None:
        backend = _SelectorBackend()
        h = _Harness(_noop_handler, backend)
        await h.start()
        sid = (await _new_selector_session(h))["sessionId"]
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/set_mode",
                "params": {"sessionId": sid, "modeId": "high"},
            }
        )
        frame = await h.wait_for(lambda f: f.get("id") == 2 and "result" in f)
        assert frame["result"] == {}  # SetSessionModeResponse is empty
        update = await h.wait_for(
            lambda f: f.get("method") == "session/update"
            and f["params"]["update"].get("sessionUpdate") == UPDATE_CURRENT_MODE
        )
        assert update["params"]["update"]["currentModeId"] == "high"
        assert update["params"]["sessionId"] == sid
        assert backend.effort == "high"
        assert backend.set_mode_calls == ["high"]
        await h.stop()

    @pytest.mark.asyncio
    async def test_set_mode_retains_choices_when_refresh_degrades(self) -> None:
        backend = _SelectorBackend()
        backend.degraded_refresh = True
        h = _Harness(_noop_handler, backend)
        await h.start()
        sid = (await _new_selector_session(h))["sessionId"]
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/set_mode",
                "params": {"sessionId": sid, "modeId": "high"},
            }
        )
        await h.wait_for(lambda f: f.get("id") == 2 and "result" in f)
        update = await h.wait_for(
            lambda f: f.get("method") == "session/update"
            and f["params"]["update"].get("sessionUpdate") == UPDATE_CURRENT_MODE
        )
        assert update["params"]["update"]["currentModeId"] == "high"
        modes = h.server._sessions[sid].selectors.modes
        assert modes is not None
        assert "high" in {mode["id"] for mode in modes["availableModes"]}
        await h.stop()

    @pytest.mark.asyncio
    async def test_set_mode_default_maps_to_empty_effort(self) -> None:
        backend = _SelectorBackend()
        backend.effort = "high"
        h = _Harness(_noop_handler, backend)
        await h.start()
        sid = (await _new_selector_session(h))["sessionId"]
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/set_mode",
                "params": {"sessionId": sid, "modeId": SESSION_MODE_DEFAULT_ID},
            }
        )
        await h.wait_for(lambda f: f.get("id") == 2 and "result" in f)
        assert backend.effort == ""  # default id -> provider default
        await h.stop()

    @pytest.mark.asyncio
    async def test_set_config_option_model_applies_and_notifies(self) -> None:
        backend = _SelectorBackend()
        h = _Harness(_noop_handler, backend)
        await h.start()
        sid = (await _new_selector_session(h))["sessionId"]
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/set_config_option",
                "params": {"sessionId": sid, "configId": "model", "value": "opus-4.8"},
            }
        )
        frame = await h.wait_for(lambda f: f.get("id") == 2 and "result" in f)
        options = frame["result"]["configOptions"]
        assert options[0]["currentValue"] == "opus-4.8"
        update = await h.wait_for(
            lambda f: f.get("method") == "session/update"
            and f["params"]["update"].get("sessionUpdate") == UPDATE_CONFIG_OPTION
        )
        assert update["params"]["update"]["configOptions"][0]["currentValue"] == "opus-4.8"
        assert backend.model == "opus-4.8"
        assert backend.set_config_calls == [("model", "opus-4.8")]
        await h.stop()

    @pytest.mark.asyncio
    async def test_set_model_retains_choices_when_refresh_degrades(self) -> None:
        backend = _SelectorBackend()
        backend.degraded_refresh = True
        h = _Harness(_noop_handler, backend)
        await h.start()
        sid = (await _new_selector_session(h))["sessionId"]
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/set_config_option",
                "params": {
                    "sessionId": sid,
                    "configId": "model",
                    "value": "opus-4.8",
                },
            }
        )
        frame = await h.wait_for(lambda f: f.get("id") == 2 and "result" in f)
        options = frame["result"]["configOptions"]
        assert options[0]["currentValue"] == "opus-4.8"
        assert {option["value"] for option in options[0]["options"]} == {
            "sonnet-4.6-1m",
            "opus-4.8",
        }
        await h.stop()

    @pytest.mark.asyncio
    async def test_unknown_mode_id_is_invalid_params(self) -> None:
        h = _Harness(_noop_handler, _SelectorBackend())
        await h.start()
        sid = (await _new_selector_session(h))["sessionId"]
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/set_mode",
                "params": {"sessionId": sid, "modeId": "ultra"},
            }
        )
        frame = await h.wait_for(lambda f: f.get("id") == 2 and "error" in f)
        assert frame["error"]["code"] == JSONRPC_INVALID_PARAMS
        await h.stop()

    @pytest.mark.asyncio
    async def test_unknown_config_id_is_invalid_params(self) -> None:
        h = _Harness(_noop_handler, _SelectorBackend())
        await h.start()
        sid = (await _new_selector_session(h))["sessionId"]
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/set_config_option",
                "params": {"sessionId": sid, "configId": "temperature", "value": "hot"},
            }
        )
        frame = await h.wait_for(lambda f: f.get("id") == 2 and "error" in f)
        assert frame["error"]["code"] == JSONRPC_INVALID_PARAMS
        await h.stop()

    @pytest.mark.asyncio
    async def test_unadvertised_model_value_is_invalid_params(self) -> None:
        h = _Harness(_noop_handler, _SelectorBackend())
        await h.start()
        sid = (await _new_selector_session(h))["sessionId"]
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/set_config_option",
                "params": {"sessionId": sid, "configId": "model", "value": "gpt-9"},
            }
        )
        frame = await h.wait_for(lambda f: f.get("id") == 2 and "error" in f)
        assert frame["error"]["code"] == JSONRPC_INVALID_PARAMS
        await h.stop()

    @pytest.mark.asyncio
    async def test_boolean_value_rejected_for_select(self) -> None:
        h = _Harness(_noop_handler, _SelectorBackend())
        await h.start()
        sid = (await _new_selector_session(h))["sessionId"]
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/set_config_option",
                "params": {
                    "sessionId": sid,
                    "configId": "model",
                    "type": "boolean",
                    "value": True,
                },
            }
        )
        frame = await h.wait_for(lambda f: f.get("id") == 2 and "error" in f)
        assert frame["error"]["code"] == JSONRPC_INVALID_PARAMS
        await h.stop()

    @pytest.mark.asyncio
    async def test_missing_mode_id_is_invalid_params(self) -> None:
        h = _Harness(_noop_handler, _SelectorBackend())
        await h.start()
        sid = (await _new_selector_session(h))["sessionId"]
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/set_mode",
                "params": {"sessionId": sid},
            }
        )
        frame = await h.wait_for(lambda f: f.get("id") == 2 and "error" in f)
        assert frame["error"]["code"] == JSONRPC_INVALID_PARAMS
        await h.stop()

    @pytest.mark.asyncio
    async def test_missing_value_is_invalid_params(self) -> None:
        h = _Harness(_noop_handler, _SelectorBackend())
        await h.start()
        sid = (await _new_selector_session(h))["sessionId"]
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/set_config_option",
                "params": {"sessionId": sid, "configId": "model"},
            }
        )
        frame = await h.wait_for(lambda f: f.get("id") == 2 and "error" in f)
        assert frame["error"]["code"] == JSONRPC_INVALID_PARAMS
        await h.stop()

    @pytest.mark.asyncio
    async def test_stale_session_is_invalid_params(self) -> None:
        h = _Harness(_noop_handler, _SelectorBackend())
        await h.start()
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/set_mode",
                "params": {"sessionId": "ghost", "modeId": "high"},
            }
        )
        frame = await h.wait_for(lambda f: f.get("id") == 2 and "error" in f)
        assert frame["error"]["code"] == JSONRPC_INVALID_PARAMS
        await h.stop()

    @pytest.mark.asyncio
    async def test_set_mode_backend_failure_is_internal_error(self) -> None:
        backend = _SelectorBackend()
        backend.fail_mode = True
        h = _Harness(_noop_handler, backend)
        await h.start()
        sid = (await _new_selector_session(h))["sessionId"]
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/set_mode",
                "params": {"sessionId": sid, "modeId": "high"},
            }
        )
        frame = await h.wait_for(lambda f: f.get("id") == 2 and "error" in f)
        assert frame["error"]["code"] == JSONRPC_INTERNAL_ERROR
        # Rollback: no current_mode_update announced, effort unchanged.
        await asyncio.sleep(0.05)
        assert not _has_update(h, UPDATE_CURRENT_MODE)
        assert backend.effort == ""
        await h.stop()

    @pytest.mark.asyncio
    async def test_set_config_backend_failure_rolls_back(self) -> None:
        backend = _SelectorBackend()
        backend.fail_config = True
        h = _Harness(_noop_handler, backend)
        await h.start()
        sid = (await _new_selector_session(h))["sessionId"]
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/set_config_option",
                "params": {"sessionId": sid, "configId": "model", "value": "opus-4.8"},
            }
        )
        frame = await h.wait_for(lambda f: f.get("id") == 2 and "error" in f)
        assert frame["error"]["code"] == JSONRPC_INTERNAL_ERROR
        await asyncio.sleep(0.05)
        assert not _has_update(h, UPDATE_CONFIG_OPTION)
        assert backend.model == ""  # unchanged
        await h.stop()

    @pytest.mark.asyncio
    async def test_set_mode_rejected_while_prompt_in_flight(self) -> None:
        backend = _SelectorBackend()
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocking_handler(_req: PromptRequest, _sink: SessionSink) -> str:
            started.set()
            await release.wait()
            return STOP_REASON_END_TURN

        h = _Harness(blocking_handler, backend)
        await h.start()
        sid = (await _new_selector_session(h))["sessionId"]
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]},
            }
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/set_mode",
                "params": {"sessionId": sid, "modeId": "high"},
            }
        )
        frame = await h.wait_for(lambda f: f.get("id") == 3 and "error" in f)
        assert frame["error"]["code"] == JSONRPC_INVALID_PARAMS
        assert backend.set_mode_calls == []  # never reached the backend
        release.set()
        await h.wait_for(lambda f: f.get("id") == 2 and "result" in f)
        await h.stop()

    @pytest.mark.asyncio
    async def test_prompt_rejected_while_selector_in_flight(self) -> None:
        backend = _SelectorBackend()
        backend.config_gate = asyncio.Event()  # block the mutation mid-apply
        h = _Harness(_noop_handler, backend)
        await h.start()
        sid = (await _new_selector_session(h))["sessionId"]
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/set_config_option",
                "params": {"sessionId": sid, "configId": "model", "value": "opus-4.8"},
            }
        )
        # Wait until the backend method is entered (the flag is set just before).
        for _ in range(200):
            if backend.set_config_calls:
                break
            await asyncio.sleep(0.01)
        assert backend.set_config_calls, "selector mutation never started"
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]},
            }
        )
        frame = await h.wait_for(lambda f: f.get("id") == 3 and "error" in f)
        assert frame["error"]["code"] == JSONRPC_INVALID_PARAMS
        backend.config_gate.set()  # release the mutation
        await h.wait_for(lambda f: f.get("id") == 2 and "result" in f)
        await h.stop()

    @pytest.mark.asyncio
    async def test_cross_surface_busy_is_invalid_params(self) -> None:
        backend = _SelectorBackend()
        backend.busy_config = True
        h = _Harness(_noop_handler, backend)
        await h.start()
        sid = (await _new_selector_session(h))["sessionId"]
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/set_config_option",
                "params": {
                    "sessionId": sid,
                    "configId": "model",
                    "value": "opus-4.8",
                },
            }
        )
        frame = await h.wait_for(lambda f: f.get("id") == 2 and "error" in f)
        assert frame["error"]["code"] == JSONRPC_INVALID_PARAMS
        assert not _has_update(h, UPDATE_CONFIG_OPTION)
        await h.stop()

    @pytest.mark.asyncio
    async def test_load_and_resume_advertise_persisted_selection(self) -> None:
        backend = _SelectorBackend()
        h = _Harness(_noop_handler, backend)
        await h.start()
        sid = (await _new_selector_session(h))["sessionId"]
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/set_config_option",
                "params": {"sessionId": sid, "configId": "model", "value": "opus-4.8"},
            }
        )
        await h.wait_for(lambda f: f.get("id") == 2 and "result" in f)
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/load",
                "params": {"sessionId": sid, "cwd": "/tmp"},
            }
        )
        loaded = await h.wait_for(lambda f: f.get("id") == 3 and "result" in f)
        assert loaded["result"]["configOptions"][0]["currentValue"] == "opus-4.8"
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "session/resume",
                "params": {"sessionId": sid, "cwd": "/tmp"},
            }
        )
        resumed = await h.wait_for(lambda f: f.get("id") == 4 and "result" in f)
        assert resumed["result"]["configOptions"][0]["currentValue"] == "opus-4.8"
        await h.stop()

    @pytest.mark.asyncio
    async def test_backend_without_selectors_is_method_not_found(self) -> None:
        # A backend lacking get_session_selectors keeps set_mode /
        # set_config_option unimplemented (old-client / pre-selector behaviour),
        # advertises no modes/configOptions, and set_model is always 404.
        h = _Harness(_noop_handler, _NoSelectorBackend())
        await h.start()
        h.send({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": "/tmp"}})
        result = await h.wait_for(lambda f: f.get("id") == 1 and "result" in f)
        assert "modes" not in result["result"]
        assert "configOptions" not in result["result"]
        sid = result["result"]["sessionId"]
        for rid, method, params in (
            (2, "session/set_mode", {"sessionId": sid, "modeId": "high"}),
            (3, "session/set_config_option", {"sessionId": sid, "configId": "model", "value": "x"}),
            (4, "session/set_model", {"sessionId": sid}),
        ):
            h.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            frame = await h.wait_for(lambda f, r=rid: f.get("id") == r and "error" in f)
            assert frame["error"]["code"] == JSONRPC_METHOD_NOT_FOUND
        await h.stop()

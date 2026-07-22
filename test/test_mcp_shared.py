"""Tests for mcp_shared: _read_message framing detection and respond output."""

from __future__ import annotations

import io
import json
from unittest.mock import patch

import kiro_crew.mcp_shared as mcp_shared
from kiro_crew.mcp_shared import _read_message, respond


def _make_stdin(data: bytes):
    """Create a fake stdin with a binary .buffer attribute."""
    buf = io.BytesIO(data)
    fake = type("FakeStdin", (), {"buffer": buf})()
    return fake


def _content_length_frame(obj: dict) -> bytes:
    body = json.dumps(obj).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8") + body


class _ShortReadBuffer:
    """A binary buffer whose .read(n) returns at most `chunk` bytes per call.

    Models the RawIOBase / pipe / socket contract where read(n) may return fewer
    than n bytes even when more data is available. readline() is exact (used for
    headers, which are line-oriented).
    """

    def __init__(self, data: bytes, chunk: int):
        self._data = data
        self._pos = 0
        self._chunk = chunk

    def readline(self) -> bytes:
        nl = self._data.find(b"\n", self._pos)
        end = len(self._data) if nl == -1 else nl + 1
        line = self._data[self._pos : end]
        self._pos = end
        return line

    def read(self, n: int) -> bytes:
        end = min(self._pos + min(n, self._chunk), len(self._data))
        out = self._data[self._pos : end]
        self._pos = end
        return out


class _ShortReadStdin:
    def __init__(self, data: bytes, chunk: int):
        self.buffer = _ShortReadBuffer(data, chunk)


class TestReadMessageContentLength:
    def setup_method(self):
        mcp_shared._use_content_length = False

    def test_reads_content_length_message(self):
        msg = {"jsonrpc": "2.0", "method": "initialize", "id": 1}
        stdin = _make_stdin(_content_length_frame(msg))
        result = _read_message(stdin)
        assert result == msg
        assert mcp_shared._use_content_length is True

    def test_reads_multibyte_utf8(self):
        msg = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 1,
            "params": {"name": "tëst_émoji_🎉"},
        }
        stdin = _make_stdin(_content_length_frame(msg))
        result = _read_message(stdin)
        assert result == msg

    def test_reads_two_sequential_messages(self):
        """Two Content-Length messages from the same stream are read correctly."""
        msg1 = {"jsonrpc": "2.0", "method": "initialize", "id": 1}
        msg2 = {"jsonrpc": "2.0", "method": "tools/list", "id": 2}
        stdin = _make_stdin(_content_length_frame(msg1) + _content_length_frame(msg2))
        assert _read_message(stdin) == msg1
        assert _read_message(stdin) == msg2

    def test_malformed_length_continues(self):
        """Malformed Content-Length skips to next message, flag stays False."""
        bad = b"Content-Length: abc\r\n\r\n"
        good_msg = {"jsonrpc": "2.0", "id": 2}
        data = bad + json.dumps(good_msg).encode("utf-8") + b"\n"
        stdin = _make_stdin(data)
        result = _read_message(stdin)
        assert result == good_msg
        assert mcp_shared._use_content_length is False

    def test_invalid_json_in_content_length_frame_continues(self):
        """Invalid JSON body with correct Content-Length skips to next message."""
        bad = b"Content-Length: 5\r\n\r\n{bad}"
        good_msg = {"jsonrpc": "2.0", "id": 3}
        good = json.dumps(good_msg).encode("utf-8") + b"\n"
        stdin = _make_stdin(bad + good)
        result = _read_message(stdin)
        assert result == good_msg

    def test_true_truncation_continues(self):
        """Content-Length larger than available body consumes remaining bytes, skips to next."""
        # Claim 100 bytes but only provide 5 — read(100) returns short, json.loads fails
        bad = b"Content-Length: 100\r\n\r\n{bad}"
        good_msg = {"jsonrpc": "2.0", "id": 4}
        good = json.dumps(good_msg).encode("utf-8") + b"\n"
        stdin = _make_stdin(bad + good)
        # The truncated read consumes into the next message's bytes, so we get None (EOF)
        result = _read_message(stdin)
        assert result is None

    def test_short_reads_are_reassembled(self):
        """Regression: a stream whose read(n) returns FEWER than n bytes (the
        RawIOBase / socket contract permits this) must not truncate the body.

        Before the fix, a single ``raw.read(length)`` took only the first chunk, so
        ``json.loads`` failed on the partial body and the message was silently dropped
        (and the leftover bytes desynced every subsequent message). The read-loop must
        reassemble the full body across multiple short reads.
        """
        msg = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 7,
            "params": {"name": "x" * 300},
        }  # body >> chunk size
        stdin = _ShortReadStdin(_content_length_frame(msg), chunk=8)
        result = _read_message(stdin)
        assert result == msg

    def test_incomplete_body_after_eof_is_discarded(self):
        """If EOF arrives before the full declared body, the incomplete message MUST
        be discarded (return None) — even when the truncated body is itself valid JSON.

        The body below is well-formed JSON, but Content-Length declares far more bytes
        than are delivered. Returning the parsed prefix would surface a message the
        sender never finished; the loop must reject it rather than rely on json.loads
        happening to fail.
        """
        body = b'{"jsonrpc":"2.0","id":1}'  # valid JSON on its own
        framed = b"Content-Length: 999\r\n\r\n" + body  # declares more than provided
        stdin = _ShortReadStdin(framed, chunk=4)
        result = _read_message(stdin)
        assert result is None  # incomplete body discarded, never partially parsed


class TestReadMessageBareJson:
    def setup_method(self):
        mcp_shared._use_content_length = False

    def test_reads_bare_json(self):
        msg = {"jsonrpc": "2.0", "method": "initialize", "id": 1}
        stdin = _make_stdin(json.dumps(msg).encode("utf-8") + b"\n")
        result = _read_message(stdin)
        assert result == msg
        assert mcp_shared._use_content_length is False

    def test_skips_invalid_json(self):
        good_msg = {"jsonrpc": "2.0", "id": 1}
        data = b"not json\n" + json.dumps(good_msg).encode("utf-8") + b"\n"
        stdin = _make_stdin(data)
        result = _read_message(stdin)
        assert result == good_msg

    def test_eof_returns_none(self):
        stdin = _make_stdin(b"")
        assert _read_message(stdin) is None

    def test_skips_blank_lines(self):
        msg = {"jsonrpc": "2.0", "id": 1}
        data = b"\n\n" + json.dumps(msg).encode("utf-8") + b"\n"
        stdin = _make_stdin(data)
        assert _read_message(stdin) == msg


class TestRespondFraming:
    def setup_method(self):
        mcp_shared._use_content_length = False

    def test_respond_bare_json(self):
        out = io.StringIO()
        with patch("sys.stdout", out):
            respond(1, {"ok": True})
        output = out.getvalue()
        assert output.endswith("\n")
        assert "Content-Length" not in output
        parsed = json.loads(output.strip())
        assert parsed["id"] == 1
        assert parsed["result"] == {"ok": True}

    def test_respond_content_length(self):
        mcp_shared._use_content_length = True
        out = io.BytesIO()
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.buffer = out
            respond(1, {"ok": True})
        output = out.getvalue()
        assert output.startswith(b"Content-Length:")
        header, body = output.split(b"\r\n\r\n", 1)
        length = int(header.split(b":")[1].strip())
        assert length == len(body)
        parsed = json.loads(body.decode("utf-8"))
        assert parsed["id"] == 1

    def test_respond_none_id_is_noop(self):
        out = io.StringIO()
        with patch("sys.stdout", out):
            respond(None, {"ok": True})
        assert out.getvalue() == ""


class TestCallToolWithLoggingRedaction:
    """The SEL audit ``resources`` (serialized tool args) must be redacted, so a
    credential passed in a free-text arg (e.g. artifact_post_comment ``text``,
    artifact_delete_comment ``reason``) can't be persisted verbatim in the audit
    log even when the per-tool handler only scrubbed its own egress copy."""

    def test_args_redacted_before_sel_log(self):
        from kiro_crew.mcp_shared import call_tool_with_logging

        captured = {}

        class _FakeSel:
            def log_tool_invocation(self, **kw):
                captured.update(kw)

        secret = "AKIAIOSFODNN7EXAMPLE"

        def _validate(_name, raw):
            return raw

        def _inner(_name, _args):
            return "ok"

        with patch("kiro_crew.mcp_shared.sel", return_value=_FakeSel()):
            call_tool_with_logging(
                "artifact_post_comment",
                {"slug": "doc", "text": f"leak {secret} here"},
                _validate,
                _inner,
                session_key="mcp_core",
                downstream_service="kirocrew-core",
            )
        # The raw AKIA credential must NOT appear in the logged resources.
        assert secret not in captured.get("resources", "")
        # The non-sensitive fields still make it into the audit trail.
        assert "slug" in captured.get("resources", "")


# --- run_mcp_stdio_loop busy-queue behavior (Mesh-3020) ----------------------
#
# A tools/call arriving while a worker is busy used to be silently dropped:
# no response was ever written, so the client waited forever. These tests
# drive the real loop over a pipe-backed stdin (select() needs a real fd)
# and assert queued calls are answered FIFO once the worker frees. The
# worker-thread + select() interleave is POSIX-only (the Windows loop
# dispatches synchronously), so gate the class accordingly.

import pytest  # noqa: E402

from kiro_crew import platform_compat  # noqa: E402


class _LoopHarness:
    """Run run_mcp_stdio_loop in a thread against a pipe-backed stdin.

    Responses are captured by patching mcp_shared.respond; SEL and tool-policy
    resolution are stubbed out so the loop needs no gateway environment.
    """

    def __init__(self, monkeypatch, call_tool_fn):
        import os
        import sys
        import threading
        from unittest.mock import MagicMock

        self.responses: list = []  # (req_id, result, error)
        rfd, self._wfd = os.pipe()
        self._stdin = io.TextIOWrapper(io.open(rfd, "rb"))
        monkeypatch.setattr(sys, "stdin", self._stdin)
        monkeypatch.setattr(mcp_shared, "respond", self._record)
        monkeypatch.setattr(mcp_shared, "_resolve_excluded_tools", lambda: set())
        self.sel_mock = MagicMock()
        monkeypatch.setattr(mcp_shared, "sel", lambda: self.sel_mock)
        self._os = os
        self._thread = threading.Thread(
            target=mcp_shared.run_mcp_stdio_loop,
            args=("test-server", "0.0.0", lambda: [], call_tool_fn),
            daemon=True,
        )
        self._thread.start()

    def _record(self, req_id, result, error=None) -> None:
        self.responses.append((req_id, result, error))

    def send(self, msg: dict) -> None:
        self._os.write(self._wfd, (json.dumps(msg) + "\n").encode("utf-8"))

    def wait_for(self, predicate, timeout: float = 5.0) -> bool:
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()

    def close(self) -> None:
        self._os.close(self._wfd)
        self._thread.join(timeout=5.0)


def _tools_call(req_id, tool_name: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": {}},
    }


def _slow_then_echo():
    """Return (call_tool_fn, started_event, release_event) for a blockable tool."""
    import threading

    started = threading.Event()
    release = threading.Event()

    def call_tool(name, args):
        if name == "slow":
            started.set()
            release.wait(timeout=10.0)
        return f"done:{name}"

    return call_tool, started, release


@pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="worker-thread + select() interleave is POSIX-only",
)
class TestStdioLoopBusyQueue:
    def setup_method(self):
        mcp_shared._use_content_length = False

    def test_tools_call_while_busy_is_queued_and_answered_fifo(self, monkeypatch):
        import time

        call_tool, started, release = _slow_then_echo()
        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call(201, "slow"))
            assert started.wait(timeout=5.0)
            harness.send(_tools_call(202, "fast"))
            harness.send(_tools_call(203, "fast"))
            # Give the busy read loop a beat to buffer both calls
            time.sleep(0.3)
            assert harness.responses == []  # nothing answered while busy
            release.set()
            assert harness.wait_for(lambda: len(harness.responses) >= 3)
            assert [r[0] for r in harness.responses] == [201, 202, 203]
            assert all(r[2] is None for r in harness.responses)
        finally:
            release.set()
            harness.close()

    def test_cancelled_queued_call_gets_no_response_and_loop_continues(self, monkeypatch):
        import time

        call_tool, started, release = _slow_then_echo()
        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call(301, "slow"))
            assert started.wait(timeout=5.0)
            harness.send(_tools_call(302, "fast"))
            # Let the read loop consume 302 before the cancel arrives: two
            # back-to-back pipe writes can coalesce into one buffered read,
            # in which case cancel-of-queued is best-effort (same as the
            # pre-existing in-flight cancel race).
            time.sleep(0.3)
            harness.send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": 302},
                }
            )
            time.sleep(0.3)
            release.set()
            assert harness.wait_for(lambda: any(r[0] == 301 for r in harness.responses))
            # Loop must still serve new calls after skipping the cancelled one
            harness.send(_tools_call(303, "fast"))
            assert harness.wait_for(lambda: any(r[0] == 303 for r in harness.responses))
            assert not any(r[0] == 302 for r in harness.responses)
        finally:
            release.set()
            harness.close()

    def test_queue_overflow_returns_busy_error(self, monkeypatch):
        import time

        monkeypatch.setattr(mcp_shared, "PENDING_CALLS_MAX", 1)
        call_tool, started, release = _slow_then_echo()
        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call(401, "slow"))
            assert started.wait(timeout=5.0)
            harness.send(_tools_call(402, "fast"))  # fills the queue
            time.sleep(0.2)
            harness.send(_tools_call(403, "fast"))  # overflow
            assert harness.wait_for(lambda: any(r[0] == 403 for r in harness.responses))
            overflow = next(r for r in harness.responses if r[0] == 403)
            assert overflow[2] is not None and overflow[2]["code"] == -32000
            # The rejection is a tool-invocation decision and must be SEL-audited
            assert any(
                call.kwargs.get("outcome") == "rejected_busy"
                for call in harness.sel_mock.log_tool_invocation.call_args_list
            )
            release.set()
            assert harness.wait_for(
                lambda: {401, 402} <= {r[0] for r in harness.responses}
            )
        finally:
            release.set()
            harness.close()

    def test_ping_still_answered_while_busy(self, monkeypatch):
        call_tool, started, release = _slow_then_echo()
        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call(501, "slow"))
            assert started.wait(timeout=5.0)
            harness.send({"jsonrpc": "2.0", "id": 599, "method": "ping"})
            assert harness.wait_for(lambda: any(r[0] == 599 for r in harness.responses))
            release.set()
            assert harness.wait_for(lambda: any(r[0] == 501 for r in harness.responses))
        finally:
            release.set()
            harness.close()

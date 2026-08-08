"""Tests for mcp_shared: _read_message framing detection and respond output."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from unittest.mock import patch

import pytest

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


class TestRespondStdoutFdSnapshot:
    """``respond()`` must survive a process-wide ``dup2(devnull, 1)``.

    The vendored llama-cpp runtime wraps its multi-second GGUF load in
    ``suppress_stdout_stderr``, which dup2's fd 1 to /dev/null process-wide AND
    rebinds the ``sys.stdout`` object. The first ``local_knowledge_search``
    kicks that load on a background thread and answers in milliseconds, so its
    JSON-RPC response raced the window and was silently destroyed -- no
    exception, SEL still logged success, and the client hung until the 600s
    ACP tool-stall watchdog killed the turn.
    """

    def setup_method(self):
        mcp_shared._use_content_length = False
        mcp_shared.release_stdout_fd()

    def teardown_method(self):
        mcp_shared._use_content_length = False
        mcp_shared.release_stdout_fd()

    @staticmethod
    @contextlib.contextmanager
    def _stdout_on_fd1():
        """Make ``sys.stdout`` an fd-1-backed stream, as in a real server.

        Under pytest's fd-capture ``sys.stdout`` is a capture object whose
        ``fileno()`` is NOT 1, so without this the snapshot would dup pytest's
        capture file and the test would assert nothing about the real bug.
        Writes go straight to fd 1 (so they follow a later ``dup2``), and
        ``fileno()`` reports 1 (so the snapshot dups the right descriptor).
        """

        class _Buffer:
            @staticmethod
            def write(data: bytes) -> int:
                return os.write(1, data)

            @staticmethod
            def flush() -> None:
                pass

        class _Fd1Stdout:
            buffer = _Buffer()

            @staticmethod
            def fileno() -> int:
                return 1

            @staticmethod
            def write(text: str) -> int:
                return os.write(1, text.encode("utf-8"))

            @staticmethod
            def flush() -> None:
                pass

        with patch("sys.stdout", _Fd1Stdout()):
            yield

    @staticmethod
    def _redirect_stdout_to_pipe():
        """Point fd 1 at a pipe; returns (read_fd, restore_callable)."""
        read_fd, write_fd = os.pipe()
        saved = os.dup(1)
        os.dup2(write_fd, 1)
        os.close(write_fd)

        def _restore():
            os.dup2(saved, 1)
            os.close(saved)

        return read_fd, _restore

    @staticmethod
    def _drain(read_fd) -> bytes:
        os.set_blocking(read_fd, False)
        try:
            return os.read(read_fd, 65536)
        except BlockingIOError:
            return b""
        finally:
            os.close(read_fd)

    @contextlib.contextmanager
    def _devnull_over_fd1(self):
        """Mimic ``suppress_stdout_stderr``: swap fd 1 AND the sys.stdout object."""
        devnull = open(os.devnull, "w")
        saved_fd = os.dup(1)
        saved_obj = sys.stdout
        os.dup2(devnull.fileno(), 1)
        sys.stdout = devnull
        try:
            yield
        finally:
            os.dup2(saved_fd, 1)
            os.close(saved_fd)
            sys.stdout = saved_obj
            devnull.close()

    def test_response_survives_dup2_devnull_bare_json(self):
        read_fd, restore = self._redirect_stdout_to_pipe()
        try:
            with self._stdout_on_fd1():
                mcp_shared.snapshot_stdout_fd()
                with self._devnull_over_fd1():
                    respond(1, {"ok": True})
        finally:
            restore()
        got = self._drain(read_fd)
        assert got, "response was destroyed by the dup2 window (the reported bug)"
        parsed = json.loads(got.decode("utf-8").strip())
        assert parsed["id"] == 1
        assert parsed["result"] == {"ok": True}

    def test_response_survives_dup2_devnull_content_length(self):
        mcp_shared._use_content_length = True
        read_fd, restore = self._redirect_stdout_to_pipe()
        try:
            with self._stdout_on_fd1():
                mcp_shared.snapshot_stdout_fd()
                with self._devnull_over_fd1():
                    respond(2, {"ok": True})
        finally:
            restore()
        got = self._drain(read_fd)
        assert got.startswith(b"Content-Length:"), got
        header, body = got.split(b"\r\n\r\n", 1)
        assert int(header.split(b":")[1].strip()) == len(body)
        assert json.loads(body.decode("utf-8"))["id"] == 2

    def test_without_snapshot_the_response_is_lost(self):
        """Negative control: this is exactly the pre-fix failure mode.

        Locks in that the snapshot -- not some incidental buffering -- is what
        saves the response, so a future refactor that drops it fails here.
        """
        read_fd, restore = self._redirect_stdout_to_pipe()
        try:
            with self._stdout_on_fd1():
                assert mcp_shared._stdout_fd is None
                with self._devnull_over_fd1():
                    respond(3, {"ok": True})
        finally:
            restore()
        assert self._drain(read_fd) == b""

    def test_snapshot_is_idempotent_and_released(self):
        read_fd, restore = self._redirect_stdout_to_pipe()
        try:
            with self._stdout_on_fd1():
                first = mcp_shared.snapshot_stdout_fd()
                assert first is not None
                assert mcp_shared.snapshot_stdout_fd() == first
        finally:
            restore()
        os.close(read_fd)
        mcp_shared.release_stdout_fd()
        assert mcp_shared._stdout_fd is None
        # Idempotent: a second release must not raise (nor close a reused fd).
        mcp_shared.release_stdout_fd()

    def test_falls_back_when_stdout_has_no_fileno(self):
        """Captured/StringIO stdout has no usable fileno — keep the old path."""
        out = io.StringIO()
        with patch("sys.stdout", out):
            assert mcp_shared.snapshot_stdout_fd() is None
            respond(4, {"ok": True})
        assert json.loads(out.getvalue().strip())["id"] == 4

    def test_falls_back_when_snapshot_fd_is_broken(self):
        """A closed/unusable snapshot fd must fall back, not lose the response."""
        out = io.StringIO()
        read_fd, restore = self._redirect_stdout_to_pipe()
        try:
            with self._stdout_on_fd1():
                mcp_shared.snapshot_stdout_fd()
        finally:
            restore()
        os.close(read_fd)
        # Close the snapshot behind respond()'s back so os.write raises EBADF.
        os.close(mcp_shared._stdout_fd)
        try:
            with patch("sys.stdout", out):
                respond(5, {"ok": True})
            assert json.loads(out.getvalue().strip())["id"] == 5
        finally:
            mcp_shared._stdout_fd = None

    def test_write_all_loops_on_short_writes(self):
        """A short ``os.write`` must not truncate the frame."""
        chunks = []

        def _short_write(_fd, buf):
            take = min(4, len(buf))
            chunks.append(bytes(buf[:take]))
            return take

        with patch.object(mcp_shared.os, "write", _short_write):
            written = mcp_shared._write_all(99, b"0123456789abcdef")
        assert b"".join(chunks) == b"0123456789abcdef"
        assert written == 16

    def test_write_all_reports_bytes_written_on_failure(self):
        """A mid-frame failure must expose how much already went out."""

        def _fail_after_one(_fd, buf):
            if chunks:
                raise BrokenPipeError("gone")
            chunks.append(bytes(buf[:4]))
            return 4

        chunks: list = []
        with patch.object(mcp_shared.os, "write", _fail_after_one):
            with pytest.raises(OSError) as excinfo:
                mcp_shared._write_all(99, b"0123456789")
        assert excinfo.value.bytes_written == 4

    def test_partial_write_is_not_duplicated_on_fallback(self):
        """A torn frame must be dropped, never re-sent whole via sys.stdout.

        Falling back after a PARTIAL os.write would put the frame's prefix on
        the wire twice and desync the JSON-RPC stream for every later message.
        """
        out = io.StringIO()
        sent: list = []

        def _partial_then_fail(_fd, buf):
            if sent:
                raise BrokenPipeError("gone")
            sent.append(bytes(buf[:5]))
            return 5

        read_fd, restore = self._redirect_stdout_to_pipe()
        try:
            with self._stdout_on_fd1():
                mcp_shared.snapshot_stdout_fd()
        finally:
            restore()
        os.close(read_fd)
        try:
            with patch.object(mcp_shared.os, "write", _partial_then_fail):
                with patch("sys.stdout", out):
                    respond(9, {"ok": True})
            assert out.getvalue() == "", "torn frame was duplicated onto sys.stdout"
        finally:
            mcp_shared.release_stdout_fd()

    def test_clean_failure_still_falls_back(self):
        """Zero bytes written → safe to retry on sys.stdout (no duplication)."""
        out = io.StringIO()

        def _fail_immediately(_fd, _buf):
            raise BrokenPipeError("gone")

        read_fd, restore = self._redirect_stdout_to_pipe()
        try:
            with self._stdout_on_fd1():
                mcp_shared.snapshot_stdout_fd()
        finally:
            restore()
        os.close(read_fd)
        try:
            with patch.object(mcp_shared.os, "write", _fail_immediately):
                with patch("sys.stdout", out):
                    respond(10, {"ok": True})
            assert json.loads(out.getvalue().strip())["id"] == 10
        finally:
            mcp_shared.release_stdout_fd()

    def test_run_loop_releases_fd_on_exit(self):
        """``run_mcp_stdio_loop`` must not leak the dup across invocations."""
        with patch.object(mcp_shared, "_read_message", lambda _stdin: None):
            mcp_shared.run_mcp_stdio_loop(
                "test-server", "0.1.0", lambda: [], lambda _n, _a: "ok"
            )
        assert mcp_shared._stdout_fd is None


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

from kiro_crew import platform_compat  # noqa: E402


class _LoopHarness:
    """Run run_mcp_stdio_loop in a thread against a pipe-backed stdin.

    Responses are captured by patching mcp_shared.respond; SEL and tool-policy
    resolution are stubbed out so the loop needs no gateway environment.
    """

    def __init__(self, monkeypatch, call_tool_fn, loop_kwargs: dict | None = None):
        import os
        import sys
        import threading
        from unittest.mock import MagicMock

        self.responses: list = []  # (req_id, result, error)
        rfd, self._wfd = os.pipe()
        self._stdin = io.TextIOWrapper(io.open(rfd, "rb"))
        monkeypatch.setattr(sys, "stdin", self._stdin)
        monkeypatch.setattr(mcp_shared, "respond", self._record)
        monkeypatch.setattr(mcp_shared, "_resolve_excluded_tools", lambda *a: set())
        self.sel_mock = MagicMock()
        monkeypatch.setattr(mcp_shared, "sel", lambda: self.sel_mock)
        self._os = os
        self._thread = threading.Thread(
            target=mcp_shared.run_mcp_stdio_loop,
            args=("test-server", "0.0.0", lambda: [], call_tool_fn),
            kwargs=loop_kwargs or {},
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


# --- Caller-identity extension through the stdio loop (PR #422 round 18) -----


def _initialize(req_id) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "method": "initialize", "params": {}}


def _tools_call_with_caller(req_id, tool_name: str, session_key: str) -> dict:
    from kiro_crew.mcp_caller import CallerContext, build_caller_meta

    msg = _tools_call(req_id, tool_name)
    msg["params"]["_meta"] = build_caller_meta(
        CallerContext(session_key=session_key, from_gateway=True)
    )
    return msg


class TestStdioLoopCallerIdentity:
    def setup_method(self):
        mcp_shared._use_content_length = False

    def test_initialize_advertises_capability_when_opted_in(self, monkeypatch):
        # GPT 5.6 round 18 HIGH: without the advertisement gatewayd treats
        # the backend as single-session and never injects the caller block,
        # so the whole per-call identity path would be dead code.
        harness = _LoopHarness(
            monkeypatch, lambda n, a: "ok", {"advertise_caller_identity": True}
        )
        try:
            harness.send(_initialize(1))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            caps = harness.responses[0][1]["capabilities"]
            assert caps["experimental"] == {
                "kirocrew.caller-identity": {"schemaVersion": 1}
            }
        finally:
            harness.close()

    def test_initialize_omits_capability_by_default(self, monkeypatch):
        # kirocrew-cron does NOT consume per-call identity — it must stay
        # single-session (gatewayd refuses to pool non-advertising backends).
        harness = _LoopHarness(monkeypatch, lambda n, a: "ok")
        try:
            harness.send(_initialize(1))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert "experimental" not in harness.responses[0][1]["capabilities"]
        finally:
            harness.close()

    def test_tool_sees_current_caller_from_meta(self, monkeypatch):
        # The dispatch loop must install the gateway-injected caller for the
        # duration of the call and clear it afterwards.
        from kiro_crew import mcp_caller

        seen: list = []

        def call_tool(name, args):
            ctx = mcp_caller.current_caller()
            seen.append(ctx.session_key if ctx else None)
            return "ok"

        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call_with_caller(11, "echo", "dashboard:chat-3"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert seen == ["dashboard:chat-3"]
            assert mcp_caller.current_caller() is None  # cleared after dispatch
        finally:
            harness.close()

    def test_excluded_tool_audit_attributes_caller_session(self, monkeypatch):
        # GPT 5.6 round 18 MEDIUM: in a shared backend the env var attributes
        # rejection audits to "mcp" or the wrong session — the parsed caller
        # identity must win when present.
        harness = _LoopHarness(monkeypatch, lambda n, a: "ok")
        monkeypatch.setattr(
            mcp_shared, "_resolve_excluded_tools", lambda *a: {"blocked"}
        )
        try:
            harness.send(_tools_call_with_caller(21, "blocked", "dashboard:chat-9"))
            assert harness.wait_for(
                lambda: harness.sel_mock.log_tool_invocation.call_count >= 1
            )
            kw = harness.sel_mock.log_tool_invocation.call_args.kwargs
            assert kw["outcome"] == "rejected_excluded"
            assert kw["session_key"] == "dashboard:chat-9"
        finally:
            harness.close()

    def test_failed_tool_audit_attributes_caller_session(self, monkeypatch):
        def boom(name, args):
            raise RuntimeError("kaput")

        harness = _LoopHarness(monkeypatch, boom)
        try:
            harness.send(_tools_call_with_caller(31, "echo", "dashboard:chat-5"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert harness.wait_for(
                lambda: harness.sel_mock.log_tool_invocation.call_count >= 1
            )
            kw = harness.sel_mock.log_tool_invocation.call_args.kwargs
            assert kw["outcome"] == "failed"
            assert kw["session_key"] == "dashboard:chat-5"
        finally:
            harness.close()


class TestPerSessionToolPolicy:
    """Pooled backends must not bleed one session's policy into another
    (GPT 5.6 PR #422 round 20)."""

    def _reset(self):
        # Full module-state reset: the negative-cache timestamps are shared
        # process globals — a failure-path test from another file in the
        # same shard leaves them set, short-circuiting this test to
        # fail-open set() (exactly what happened on CI shard 3).
        mcp_shared._excluded_tools_by_session.clear()
        mcp_shared._last_failure_time = 0.0
        mcp_shared._last_startup_race_time = 0.0
        mcp_shared._failure_count = 0

    def setup_method(self):
        self._reset()

    def teardown_method(self):
        self._reset()

    def test_cache_is_keyed_per_session(self, monkeypatch):
        calls: list = []

        def fake_urlopen(req, timeout=0):
            import io

            calls.append(req.headers.get("X-session-key"))
            body = (
                b'{"exclude": ["tool_a"]}'
                if req.headers.get("X-session-key") == "dashboard:chat-1"
                else b'{"exclude": ["tool_b"]}'
            )

            class _Resp(io.BytesIO):
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            return _Resp(body)

        monkeypatch.setattr(mcp_shared, "loopback_urlopen", fake_urlopen)
        monkeypatch.setattr(
            mcp_shared.KiroCrewConfig,
            "load",
            classmethod(lambda cls: type("C", (), {"dashboard": type("D", (), {"url": "http://localhost:5476"})()})()),
        )
        monkeypatch.setattr(mcp_shared, "_read_internal_secret", lambda: "s3cr3t", raising=False)

        a = mcp_shared._resolve_excluded_tools("dashboard:chat-1")
        b = mcp_shared._resolve_excluded_tools("dashboard:chat-2")
        assert a == {"tool_a"}
        assert b == {"tool_b"}  # NOT session-1's cached policy
        # Cache hit path: no third HTTP call for a repeat lookup.
        n = len(calls)
        assert mcp_shared._resolve_excluded_tools("dashboard:chat-1") == {"tool_a"}
        assert len(calls) == n

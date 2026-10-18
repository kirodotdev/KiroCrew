"""Tests for mcp_shared: _read_message framing detection and respond output.

This module also pins the SEL audit ``call_tool_with_logging`` writes: its redacted
resources and its ``tool_kind``. ``run_mcp_stdio_loop``'s busy queue, the frames it
cannot use and its exit once its install is pruned live in
``test_mcp_shared_stdio_loop.py``, and the caller identity and per-session tool policy it
applies in ``test_mcp_shared_caller_identity.py``; the shared loop harness, request
builders and framing reset in ``mcp_shared_helpers.py``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from unittest.mock import patch

import pytest
from mcp_shared_helpers import (  # noqa: F401 -- the autouse fixture, and re-exports for importers
    _framing_undetected,
    _initialize,
    _LoopHarness,
    _slow_then_echo,
    _tools_call,
    _tools_call_with_caller,
)
from stray_line_helpers import STRAY_LINES, too_deep_to_decode

import kiro_crew.mcp_shared as mcp_shared
from kiro_crew.mcp_shared import SKIP, _read_message, respond


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

    def readline(self, size: int = -1) -> bytes:
        nl = self._data.find(b"\n", self._pos)
        end = len(self._data) if nl == -1 else nl + 1
        if size >= 0:
            end = min(end, self._pos + size)
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
    def test_reads_content_length_message(self):
        msg = {"jsonrpc": "2.0", "method": "initialize", "id": 1}
        stdin = _make_stdin(_content_length_frame(msg))
        result = _read_message(stdin)
        assert result == msg
        assert mcp_shared._framing == "content-length"

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

    def test_malformed_length_ends_the_stream_and_picks_no_framing(self):
        """The body behind a length nobody can read cannot be delimited, so the
        stream ends rather than read later frames joined to it."""
        bad = b"Content-Length: abc\r\n\r\n"
        data = bad + json.dumps({"jsonrpc": "2.0", "id": 2}).encode("utf-8") + b"\n"
        stdin = _make_stdin(data)
        assert _read_message(stdin) is None
        assert mcp_shared._framing != "content-length"

    def test_invalid_json_in_content_length_frame_continues(self):
        """Invalid JSON body with correct Content-Length skips to next message."""
        bad = b"Content-Length: 5\r\n\r\n{bad}"
        good_msg = {"jsonrpc": "2.0", "id": 3}
        good = json.dumps(good_msg).encode("utf-8") + b"\n"
        stdin = _make_stdin(bad + good)
        assert _read_message(stdin) is SKIP
        assert _read_message(stdin) == good_msg

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

    @pytest.mark.parametrize("stray", sorted(STRAY_LINES))
    def test_a_body_that_is_not_an_object_is_one_skipped_frame(self, stray):
        body = STRAY_LINES[stray]().rstrip(b"\n")
        framed = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        good = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        stdin = _make_stdin(framed + _content_length_frame(good))
        assert _read_message(stdin) is SKIP
        assert _read_message(stdin) == good

    def test_an_unparseable_body_is_answered_under_its_recovered_id(self, monkeypatch):
        answered: list = []
        monkeypatch.setattr(mcp_shared, "respond", lambda *a, **k: answered.append((a, k)))
        body = b'{"jsonrpc":"2.0","id":7,"method":"tools/list","params":{"s":"\xff"}}'
        framed = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        good = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        stdin = _make_stdin(framed + _content_length_frame(good))
        assert _read_message(stdin) is SKIP
        assert _read_message(stdin) == good
        [(args, kwargs)] = answered
        assert args[0] == 7 and kwargs["error"]["code"] == mcp_shared.JSONRPC_PARSE_ERROR

    @pytest.mark.parametrize(
        "big",
        [
            {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"pad": "x" * 64}},
            # The MCP TypeScript SDK writes the id last.
            {
                "method": "ping",
                "params": {"pad": "x" * 64, "id": "nested"},
                "jsonrpc": "2.0",
                "id": 1,
            },
        ],
        ids=["id-first", "id-last"],
    )
    def test_a_body_over_the_cap_is_drained_and_answered(self, monkeypatch, big):
        """Drained in bounded reads, never held whole, and answered with the id it
        carries at its top level: dropped silently, its caller waited forever."""
        answered: list = []
        monkeypatch.setattr(mcp_shared, "respond", lambda *a, **k: answered.append((a, k)))
        monkeypatch.setattr(mcp_shared, "MAX_CONTENT_LENGTH_BYTES", 60)
        monkeypatch.setattr(mcp_shared, "_DRAIN_CHUNK_BYTES", 5)
        # Long enough for the top-level ``method`` member, which marks a request.
        monkeypatch.setattr(mcp_shared, "ID_PROBE_BYTES", 48)
        good = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        stdin = _make_stdin(_content_length_frame(big) + _content_length_frame(good))
        assert _read_message(stdin) is SKIP
        assert _read_message(stdin) == good
        [(args, kwargs)] = answered
        assert args[0] == 1
        assert kwargs["error"]["code"] == mcp_shared.JSONRPC_INVALID_REQUEST

    @pytest.mark.parametrize(
        "big",
        [
            {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"pad": "x" * 64}},
            {
                "method": "ping",
                "params": {"pad": "x" * 64, "id": "nested"},
                "jsonrpc": "2.0",
                "id": 1,
            },
        ],
        ids=["id-first", "id-last"],
    )
    def test_a_bare_line_over_the_cap_is_drained_and_answered(self, monkeypatch, big):
        """A bare-JSON line is held to the same ceiling as a Content-Length
        body: drained in bounded reads, never parsed or returned, and answered
        with its top-level id. The next line still reads whole."""
        answered: list = []
        monkeypatch.setattr(mcp_shared, "respond", lambda *a, **k: answered.append((a, k)))
        monkeypatch.setattr(mcp_shared, "MAX_CONTENT_LENGTH_BYTES", 60)
        monkeypatch.setattr(mcp_shared, "_DRAIN_CHUNK_BYTES", 5)
        monkeypatch.setattr(mcp_shared, "ID_PROBE_BYTES", 48)
        good = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        stdin = _make_stdin((json.dumps(big) + "\n" + json.dumps(good) + "\n").encode("utf-8"))
        assert _read_message(stdin) is SKIP
        assert _read_message(stdin) == good
        [(args, kwargs)] = answered
        assert args[0] == 1
        assert kwargs["error"]["code"] == mcp_shared.JSONRPC_INVALID_REQUEST

    @pytest.mark.parametrize("end", [b"\n", b"\r\n", b""], ids=["lf", "crlf", "eof"])
    def test_a_bare_line_at_the_cap_is_read_whole(self, monkeypatch, end):
        """The ceiling counts the body, not its terminator."""
        monkeypatch.setattr(mcp_shared, "respond", lambda *a, **k: None)
        msg = {"jsonrpc": "2.0", "id": 3, "method": "ping"}
        body = json.dumps(msg).encode("utf-8")
        monkeypatch.setattr(mcp_shared, "MAX_CONTENT_LENGTH_BYTES", len(body))
        assert _read_message(_make_stdin(body + end)) == msg

    @pytest.mark.parametrize("end", [b"\n", b"\r\n"], ids=["lf", "crlf"])
    def test_a_bare_line_one_past_the_cap_is_refused(self, monkeypatch, end):
        answered: list = []
        monkeypatch.setattr(mcp_shared, "respond", lambda *a, **k: answered.append((a, k)))
        body = json.dumps({"jsonrpc": "2.0", "id": 4, "method": "ping"}).encode("utf-8")
        monkeypatch.setattr(mcp_shared, "MAX_CONTENT_LENGTH_BYTES", len(body) - 1)
        good = {"id": 5, "method": "x"}  # shorter than the cap
        stdin = _make_stdin(body + end + json.dumps(good).encode("utf-8") + b"\n")
        assert _read_message(stdin) is SKIP
        assert _read_message(stdin) == good
        assert [a[0] for a, _ in answered] == [4]

    def test_the_body_cap_is_the_gateways_read_limit(self):
        """The constant's comment says so; a copy could drift without a test."""
        from kiro_crew.mcp_gateway import pool

        assert mcp_shared.MAX_CONTENT_LENGTH_BYTES == pool._DEFAULT_READ_BUFFER_LIMIT

    @pytest.mark.parametrize("length", [10**20, 2**62], ids=["over-ssize", "2**62"])
    def test_a_length_past_any_drainable_size_ends_the_stream_loudly(self, length, caplog):
        """Draining to it would swallow every later request, and one
        ``raw.read`` of it raises OverflowError or MemoryError."""
        later = _content_length_frame({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        stdin = _make_stdin(f"Content-Length: {length}\r\n\r\n".encode("ascii") + later)
        with caplog.at_level("ERROR", logger=mcp_shared.__name__):
            assert _read_message(stdin) is None
        assert any("framing is lost" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize(
        "header",
        [
            b"Content-Length: abc",
            b"Content-Length: -5",
            b"Content-Length: \xff",
            b"Content-Length: " + b"9" * 5000,
        ],
        ids=["word", "negative", "undecodable", "over-digit-limit"],
    )
    def test_an_unreadable_length_on_a_framed_stream_ends_it_loudly(
        self, header, monkeypatch, caplog
    ):
        """A body follows the header, and with no length it cannot be delimited:
        every later frame would be read joined to it and answered as unparseable."""
        answered: list = []
        monkeypatch.setattr(mcp_shared, "respond", lambda *a, **k: answered.append((a, k)))
        first = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}).encode("utf-8")
        later = _content_length_frame({"jsonrpc": "2.0", "id": 3, "method": "ping"})
        stdin = _make_stdin(_content_length_frame(first) + header + b"\r\n\r\n" + body + later)
        assert _read_message(stdin) == first
        with caplog.at_level("ERROR", logger=mcp_shared.__name__):
            assert _read_message(stdin) is None
        assert any("framing is lost" in r.getMessage() for r in caplog.records)
        assert answered == []


class TestReadMessageBareJson:
    def test_reads_bare_json(self):
        msg = {"jsonrpc": "2.0", "method": "initialize", "id": 1}
        stdin = _make_stdin(json.dumps(msg).encode("utf-8") + b"\n")
        result = _read_message(stdin)
        assert result == msg
        assert mcp_shared._framing != "content-length"

    def test_skips_invalid_json(self):
        good_msg = {"jsonrpc": "2.0", "id": 1}
        data = b"not json\n" + json.dumps(good_msg).encode("utf-8") + b"\n"
        stdin = _make_stdin(data)
        assert _read_message(stdin) is SKIP
        assert _read_message(stdin) == good_msg

    def test_eof_returns_none(self):
        stdin = _make_stdin(b"")
        assert _read_message(stdin) is None

    def test_skips_blank_lines(self):
        msg = {"jsonrpc": "2.0", "id": 1}
        data = b"\n\n" + json.dumps(msg).encode("utf-8") + b"\n"
        stdin = _make_stdin(data)
        assert _read_message(stdin) is SKIP
        assert _read_message(stdin) is SKIP
        assert _read_message(stdin) == msg

    @pytest.mark.parametrize("stray", sorted(STRAY_LINES))
    def test_a_stray_line_is_one_skipped_frame_not_eof(self, stray):
        """``None`` is EOF to both loop call sites, so a ``null`` line returned
        as ``None`` would end the server, as would any raise here."""
        msg = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        stdin = _make_stdin(STRAY_LINES[stray]() + json.dumps(msg).encode("utf-8") + b"\n")
        assert _read_message(stdin) is SKIP
        assert _read_message(stdin) == msg

    @pytest.mark.parametrize(
        "line",
        [
            b'{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"n":' + b"9" * 5000 + b"}}\n",
            b'{"jsonrpc":"2.0","id":7,"method":"tools/list","params":{"s":"\xff"}}\n',
            b'{"method":"tools/list","params":{"s":"\xff"},"jsonrpc":"2.0","id":7}\n',
        ],
        ids=["over-digit-limit", "undecodable", "undecodable-id-last"],
    )
    def test_an_unparseable_request_is_answered_under_its_recovered_id(self, monkeypatch, line):
        """Answered, so its caller does not wait for its own timeout while pings
        are still answered and nothing sees the server as wedged."""
        answered: list = []
        monkeypatch.setattr(mcp_shared, "respond", lambda *a, **k: answered.append((a, k)))
        stdin = _make_stdin(line + b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n')
        assert _read_message(stdin) is SKIP
        assert _read_message(stdin) == {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        [(args, kwargs)] = answered
        assert args[0] == 7
        assert kwargs["error"]["code"] == mcp_shared.JSONRPC_PARSE_ERROR

    def test_an_unparseable_request_too_deep_to_decode_is_answered(self, monkeypatch):
        answered: list = []
        monkeypatch.setattr(mcp_shared, "respond", lambda *a, **k: answered.append((a, k)))
        line = (
            '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":' + too_deep_to_decode() + "}\n"
        ).encode()
        assert _read_message(_make_stdin(line)) is SKIP
        assert [a[0] for a, _ in answered] == [7]

    @pytest.mark.parametrize(
        "line",
        [
            b"not json\n",
            b'{"method":"log","params":{"id":99,"s":"\xff"}}\n',
            # A response: its id is the client's own, never this server's to answer.
            b'{"jsonrpc":"2.0","id":3,"result":{"s":"\xff"}}\n',
            b'{"jsonrpc":"2.0","id":3,"error":{"code":1,"message":"\xff"}}\n',
        ],
        ids=["log-line", "nested-id-only", "response", "error-response"],
    )
    def test_a_line_that_is_not_a_request_with_an_id_is_dropped_unanswered(self, monkeypatch, line):
        answered: list = []
        monkeypatch.setattr(mcp_shared, "respond", lambda *a, **k: answered.append((a, k)))
        assert _read_message(_make_stdin(line)) is SKIP
        assert answered == []

    def test_a_header_shaped_line_on_a_bare_stream_is_noise(self):
        """Honouring it would read the requests behind it as a body."""
        first = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        second = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        data = b"".join(
            [
                json.dumps(first).encode("utf-8") + b"\n",
                b"Content-Length: 40\r\n",
                b"\r\n",
                json.dumps(second).encode("utf-8") + b"\n",
            ]
        )
        stdin = _make_stdin(data)
        assert _read_message(stdin) == first
        assert _read_message(stdin) is SKIP
        assert _read_message(stdin) is SKIP  # the blank separator line
        assert _read_message(stdin) == second
        assert mcp_shared._framing != "content-length"


class TestRespondFraming:
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

    def test_respond_content_length(self, monkeypatch):
        monkeypatch.setattr(mcp_shared, "_framing", "content-length")
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
        mcp_shared.release_stdout_fd()

    def teardown_method(self):
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

    def test_response_survives_dup2_devnull_content_length(self, monkeypatch):
        monkeypatch.setattr(mcp_shared, "_framing", "content-length")
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
        """An unusable snapshot fd must fall back, not lose the response.

        The unusable descriptor is a real, open, read-only one: ``os.write``
        gives the same kernel EBADF a closed fd would, so this stays a genuine
        syscall failure rather than a patched raise (that variant is
        ``test_clean_failure_still_falls_back``) -- but the fd table is left
        untouched, which the closed-fd spelling could not promise.
        """
        out = io.StringIO()
        read_fd, restore = self._redirect_stdout_to_pipe()
        try:
            with self._stdout_on_fd1():
                mcp_shared.snapshot_stdout_fd()
        finally:
            restore()
        os.close(read_fd)
        # Do NOT close the snapshot: freeing the NUMBER while _stdout_fd still
        # holds it lets another thread's open() in this worker be handed it, and
        # respond() would then write the JSON-RPC frame into that unrelated
        # stream -- the exact hazard mcp_shared.respond()'s own comment names.
        # release_stdout_fd() does the owner-correct close and clears the global
        # in one step, so no stale number is ever visible to respond().
        mcp_shared.release_stdout_fd()
        mcp_shared._stdout_fd = os.open(os.devnull, os.O_RDONLY)
        try:
            with patch("sys.stdout", out):
                respond(5, {"ok": True})
            assert json.loads(out.getvalue().strip())["id"] == 5
        finally:
            os.close(mcp_shared._stdout_fd)
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
            mcp_shared.run_mcp_stdio_loop("test-server", "0.1.0", lambda: [], lambda _n, _a: "ok")
        assert mcp_shared._stdout_fd is None


class TestCallToolWithLoggingRedaction:
    """The SEL audit ``resources`` (serialized tool args) must be redacted, so a
    credential passed in a free-text arg (e.g. artifact_post_comment ``text``,
    artifact_delete_comment ``reason``) can't be persisted verbatim in the audit
    log even when the per-tool handler only scrubbed its own egress copy."""

    @pytest.mark.parametrize("failure", ["", "validation", "execution"])
    @pytest.mark.parametrize(
        "tool",
        [
            "memory_recall",
            "search_chat_history",
            "learn_add",
            "spawn_run",
            "spawn_continue",
            "spawn_steer",
            "spawn_sub_agents",
            "session_send",
            "register_hook",
            "task_run",
            "workflow_author",
            "workflow_run",
            "workflow_rerun_subtree",
            "cron_add",
            "cron_update",
        ],
    )
    def test_session_body_tools_audit_outcome_without_retaining_payload(self, failure, tool):
        from kiro_crew.validation import ValidationError

        captured = {}

        class Audit:
            def log_tool_invocation(self, **kwargs):
                captured.update(kwargs)

        query = "PERSONAL_QUERY_CANARY"

        def validate(_name, raw):
            if failure == "validation":
                raise ValidationError(query, "invalid query field")
            return raw

        def recall(_name, _args):
            return f"Error: {query}" if failure else "recalled context"

        with patch.object(mcp_shared, "sel", return_value=Audit()):
            result = mcp_shared.call_tool_with_logging(
                tool,
                {"query": query, "context_summary": query},
                validate,
                recall,
                session_key="dashboard:alice",
                downstream_service="kirocrew-core",
            )

        assert captured["tool_name"] == tool
        assert captured["session_key"] == "dashboard:alice"
        assert captured["outcome"] == ("failed" if failure else "completed")
        assert query not in json.dumps(captured)
        assert result.startswith("Error:") if failure else result == "recalled context"

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


class TestCallToolWithLoggingKind:
    """``tool_kind`` classifies the invocation; it must not hold the caller.

    The wrapper passed ``session_key`` as ``tool_kind``, so every row it wrote --
    the bulk of the agent tool surface, across all five MCP servers -- carried a
    high-cardinality session key where a kind belongs, sitting in the same file as
    correctly-kinded rows written directly by callers. The field looked populated
    and trustworthy while carrying no kind at all.
    """

    _CALLER = "dashboard:chat-7"

    def _capture(self, *, raises: bool = False) -> dict:
        from kiro_crew.mcp_shared import call_tool_with_logging
        from kiro_crew.validation import ValidationError

        captured: dict = {}

        class _FakeSel:
            def log_tool_invocation(self, **kw):
                captured.update(kw)

        def _validate(_name, raw):
            if raises:
                raise ValidationError("slug", "bad arg")
            return raw

        def _inner(_name, _args):
            return "ok"

        with patch("kiro_crew.mcp_shared.sel", return_value=_FakeSel()):
            call_tool_with_logging(
                "artifact_list",
                {"slug": "doc"},
                _validate,
                _inner,
                session_key=self._CALLER,
                downstream_service="kirocrew-core",
            )
        assert captured, "the wrapper wrote no audit row at all"
        return captured

    def test_the_success_row_does_not_put_the_caller_in_tool_kind(self):
        captured = self._capture()
        assert captured.get("tool_kind", "") != self._CALLER

    def test_the_validation_failure_row_does_not_either(self):
        """The other call site. It was the same copy of the same wrong variable,
        so fixing only the success path would leave every rejected call mislabeled.
        """
        captured = self._capture(raises=True)
        assert captured["outcome"] == "failed"
        assert captured.get("tool_kind", "") != self._CALLER

    @pytest.mark.parametrize("raises", [False, True])
    def test_the_caller_is_still_recorded_on_both_paths(self, raises):
        """Guards the obvious wrong fix: dropping the caller instead of the kind.

        ``session_key`` is what SEL stores as ``caller_identity``, and it is the
        field the ownership and attribution questions are answered from.
        """
        assert self._capture(raises=raises)["session_key"] == self._CALLER

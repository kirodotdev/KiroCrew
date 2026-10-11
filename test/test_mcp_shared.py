"""Tests for mcp_shared: _read_message framing detection and respond output."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from unittest.mock import patch

import pytest
from stray_line_helpers import STRAY_LINES, too_deep_to_decode

import kiro_crew.mcp_shared as mcp_shared
from kiro_crew.mcp_shared import SKIP, _read_message, respond


@pytest.fixture(autouse=True)
def _framing_undetected(monkeypatch):
    """Every test starts on a stream whose framing is not yet detected.

    Through ``monkeypatch`` so a test that fails mid-way, or a loop thread that
    detected a framing, cannot leak it into the next test.
    """
    monkeypatch.setattr(mcp_shared, "_framing", None)


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


# --- run_mcp_stdio_loop busy-queue behavior ----------------------------------
#
# A tools/call arriving while a worker is busy must not be silently dropped:
# with no response ever written, the client waits forever. These tests
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

    def __init__(
        self, monkeypatch, call_tool_fn, loop_kwargs: dict | None = None, list_tools_fn=None
    ):
        import os
        import sys
        import threading
        from unittest.mock import MagicMock

        self.responses: list = []  # (req_id, result, error)
        rfd, self._wfd = os.pipe()
        self._stdin = io.TextIOWrapper(io.open(rfd, "rb"))
        monkeypatch.setattr(sys, "stdin", self._stdin)
        monkeypatch.setattr(mcp_shared, "respond", self._record)
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), ""),
        )
        self.sel_mock = MagicMock()
        monkeypatch.setattr(mcp_shared, "sel", lambda: self.sel_mock)
        self._os = os
        self._thread = threading.Thread(
            target=mcp_shared.run_mcp_stdio_loop,
            args=("test-server", "0.0.0", list_tools_fn or (lambda: []), call_tool_fn),
            kwargs=loop_kwargs or {},
            daemon=True,
        )
        self._thread.start()

    def _record(self, req_id, result, error=None) -> bool:
        self.responses.append((req_id, result, error))
        return True  # the whole frame reached the client

    def send(self, msg: dict) -> None:
        self.send_raw((json.dumps(msg) + "\n").encode("utf-8"))

    def send_raw(self, data: bytes, timeout: float = 5.0) -> None:
        """Write *data* from a helper thread, joined with a bound.

        A write larger than the pipe buffer (about 4 KiB on Windows) blocks
        until the loop reads it, so a loop that died would hang the test here;
        the bound turns that into a failure that names the cause.
        """
        import threading

        def _write_all() -> None:
            view = memoryview(data)
            while view:
                view = view[self._os.write(self._wfd, view) :]

        writer = threading.Thread(target=_write_all, daemon=True)
        writer.start()
        writer.join(timeout)
        assert not writer.is_alive(), "the loop stopped reading stdin"

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
            assert harness.wait_for(lambda: {401, 402} <= {r[0] for r in harness.responses})
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


class _ReadSpy:
    """Wraps ``_read_message`` and records what each call returned.

    The interleaving handshake for the busy-loop tests: a test waits until the
    loop has CONSUMED the frame it sent before it lets the tool finish, so the
    frame is read on the busy path rather than wherever timing puts it.
    """

    def __init__(self, monkeypatch) -> None:
        import threading

        self.returned: list = []
        self._lock = threading.Lock()
        real = mcp_shared._read_message

        def _spy(stdin):
            value = real(stdin)
            with self._lock:
                self.returned.append(value)
            return value

        monkeypatch.setattr(mcp_shared, "_read_message", _spy)

    def saw(self, predicate) -> bool:
        with self._lock:
            return any(predicate(v) for v in self.returned)


_PING = {"jsonrpc": "2.0", "id": 7, "method": "ping"}


def _answered(harness, req_id):
    return lambda: any(r[0] == req_id for r in harness.responses)


def _assert_one_refusal_audited(harness) -> None:
    refusals = [
        c
        for c in harness.sel_mock.log_api_access.mock_calls
        if c.kwargs.get("operation") == "tool_call.invalid_params"
    ]
    assert len(refusals) == 1
    assert refusals[0].kwargs["outcome"] == "rejected"


class TestStdioLoopStrayFrames:
    """One frame the loop cannot use costs that frame, never the server process."""

    @pytest.mark.parametrize("stray", sorted(STRAY_LINES))
    def test_the_idle_loop_answers_the_next_request(self, monkeypatch, stray):
        harness = _LoopHarness(monkeypatch, lambda name, args: "ok")
        try:
            harness.send_raw(STRAY_LINES[stray]())
            harness.send(_PING)
            assert harness.wait_for(_answered(harness, 7))
        finally:
            harness.close()

    def test_a_cancel_with_non_object_params_is_ignored(self, monkeypatch):
        harness = _LoopHarness(monkeypatch, lambda name, args: "ok")
        try:
            harness.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": [1]})
            harness.send(_PING)
            assert harness.wait_for(_answered(harness, 7))
        finally:
            harness.close()

    @pytest.mark.parametrize(
        "policy",
        [
            mcp_shared.ToolPolicy(frozenset(), ""),
            mcp_shared.ToolPolicy(frozenset({"echo"}), ""),
            mcp_shared.ToolPolicy(frozenset(), "resolution_failed"),
        ],
        ids=["empty", "excluded", "unresolved"],
    )
    @pytest.mark.parametrize(
        "params",
        [[1], "x", None, {}, {"name": ""}, {"name": ["echo"]}, {"name": {"n": 1}}, {"name": 5}],
        ids=[
            "list-params",
            "string-params",
            "null-params",
            "no-name",
            "empty-name",
            "list-name",
            "dict-name",
            "int-name",
        ],
    )
    def test_a_tools_call_without_an_object_params_and_string_name_is_invalid_params(
        self, monkeypatch, policy, params
    ):
        """Answered ``-32602`` before the policy check: never dispatched, never
        audited as a call to a tool with no usable name, and audited as the
        refusal it is."""
        ran: list = []
        harness = _LoopHarness(monkeypatch, lambda name, args: ran.append(name) or "ok")
        monkeypatch.setattr(mcp_shared, "_resolve_tool_policy", lambda *a, **k: policy)
        try:
            harness.send({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": params})
            harness.send(_PING)
            assert harness.wait_for(_answered(harness, 7))
            reply = next(r for r in harness.responses if r[0] == 9)
            assert reply[2] is not None and reply[2]["code"] == mcp_shared.JSONRPC_INVALID_PARAMS
            assert ran == []
            assert not harness.sel_mock.log_tool_invocation.called
            _assert_one_refusal_audited(harness)
        finally:
            harness.close()

    def test_an_envelope_error_on_a_request_is_answered(self, monkeypatch):
        """Answered, so the caller (the gateway's pending table) does not wait
        for the stub to detach or the hard ceiling."""
        harness = _LoopHarness(monkeypatch, lambda name, args: "ok")
        try:
            harness.send({"jsonrpc": "2.0", "id": 5, "method": 7})
            harness.send({"jsonrpc": "1.0", "id": 6, "method": "ping"})
            harness.send(_PING)
            assert harness.wait_for(_answered(harness, 7))
            codes = {r[0]: r[2] and r[2]["code"] for r in harness.responses}
            assert codes[5] == codes[6] == mcp_shared.JSONRPC_INVALID_REQUEST
        finally:
            harness.close()

    @pytest.mark.skipif(
        not platform_compat.IS_POSIX,
        reason="worker-thread + select() interleave is POSIX-only",
    )
    def test_a_worker_that_cannot_start_costs_that_call(self, monkeypatch):
        """``Thread.start`` raises at the scope's task ceiling. Nothing holds the
        unstarted thread afterwards: joining it raises, and would end the server
        for every session sharing it."""
        import threading

        harness = _LoopHarness(monkeypatch, lambda name, args: "ok")
        real = threading.Thread

        class _NoWorker(real):  # type: ignore[misc, valid-type]
            def start(self) -> None:
                if getattr(self, "_target", None).__name__ == "_run_tool":
                    raise RuntimeError("can't start new thread")
                super().start()

        monkeypatch.setattr(threading, "Thread", _NoWorker)
        try:
            harness.send(_tools_call(301, "echo"))
            harness.send(_PING)
            assert harness.wait_for(_answered(harness, 7))
            reply = next(r for r in harness.responses if r[0] == 301)
            assert reply[2]["code"] == mcp_shared.JSONRPC_INTERNAL_ERROR
            # The refused start is still an audited invocation decision.
            assert [
                (c.kwargs["request_id"], c.kwargs["tool_name"])
                for c in harness.sel_mock.log_tool_invocation.call_args_list
                if c.kwargs.get("outcome") == "failed"
            ] == [("301", "echo")]
        finally:
            monkeypatch.setattr(threading, "Thread", real)
            harness.close()

    def test_a_dispatch_that_raises_is_answered_internal_error(self, monkeypatch):
        def _boom():
            raise RuntimeError("listing failed")

        harness = _LoopHarness(monkeypatch, lambda name, args: "ok", list_tools_fn=_boom)
        try:
            harness.send({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
            harness.send(_PING)
            assert harness.wait_for(_answered(harness, 7))
            reply = next(r for r in harness.responses if r[0] == 3)
            assert reply[2] == {
                "code": mcp_shared.JSONRPC_INTERNAL_ERROR,
                "message": "Internal error",
            }
        finally:
            harness.close()

    @pytest.mark.skipif(
        not platform_compat.IS_POSIX,
        reason="worker-thread + select() interleave is POSIX-only",
    )
    @pytest.mark.parametrize("stray", ["undecodable", "null", "list"])
    def test_a_finished_tool_is_answered_after_a_stray_frame_with_nothing_after_it(
        self, monkeypatch, stray
    ):
        """The busy loop gets back to the worker after each dropped frame. Had it
        skipped internally, it would sit in ``readline`` until the client wrote
        again -- with no heartbeat, forever -- holding the result back."""
        spy = _ReadSpy(monkeypatch)
        call_tool, started, release = _slow_then_echo()
        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call(301, "slow"))
            assert started.wait(timeout=5.0)
            harness.send_raw(STRAY_LINES[stray]())
            assert harness.wait_for(lambda: spy.saw(lambda v: v is SKIP))
            release.set()
            assert harness.wait_for(_answered(harness, 301))
            harness.send(_PING)
            assert harness.wait_for(_answered(harness, 7))
        finally:
            release.set()
            harness.close()

    @pytest.mark.skipif(
        not platform_compat.IS_POSIX,
        reason="worker-thread + select() interleave is POSIX-only",
    )
    @pytest.mark.parametrize(
        "before", ["stray", "request"], ids=["after-a-stray-line", "after-a-request"]
    )
    def test_a_busy_loop_reads_a_frame_already_buffered(self, monkeypatch, before):
        """``select`` polls the descriptor and cannot see what the reader already
        buffered: a ping that comes in the same write as the line before it is
        answered without waiting for the client's next write or the tool."""
        call_tool, started, release = _slow_then_echo()
        harness = _LoopHarness(monkeypatch, call_tool)
        first = b"null\n" if before == "stray" else (json.dumps(_PING) + "\n").encode()
        second = {"jsonrpc": "2.0", "id": 8, "method": "ping"}
        try:
            harness.send(_tools_call(301, "slow"))
            assert started.wait(timeout=5.0)
            harness.send_raw(first + (json.dumps(second) + "\n").encode())
            assert harness.wait_for(_answered(harness, 8)), "the buffered ping was stranded"
            assert not _answered(harness, 301)(), "answered only because the tool finished"
        finally:
            release.set()
            harness.close()

    @pytest.mark.skipif(
        not platform_compat.IS_POSIX,
        reason="worker-thread + select() interleave is POSIX-only",
    )
    def test_a_busy_tools_call_without_a_usable_name_is_refused_not_queued(self, monkeypatch):
        """The busy path refuses it exactly as the idle one, so it is never
        queued, busy-rejected, or audited as a call to a tool named by whatever
        it sent."""
        call_tool, started, release = _slow_then_echo()
        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call(301, "slow"))
            assert started.wait(timeout=5.0)
            harness.send(
                {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": [1]}}
            )
            assert harness.wait_for(_answered(harness, 9))
            reply = next(r for r in harness.responses if r[0] == 9)
            assert reply[2]["code"] == mcp_shared.JSONRPC_INVALID_PARAMS
            assert not _answered(harness, 301)()
            _assert_one_refusal_audited(harness)
            release.set()
            assert harness.wait_for(_answered(harness, 301))
            names = [c.kwargs["tool_name"] for c in harness.sel_mock.log_tool_invocation.mock_calls]
            assert [1] not in names
        finally:
            release.set()
            harness.close()

    @pytest.mark.skipif(
        not platform_compat.IS_POSIX,
        reason="worker-thread + select() interleave is POSIX-only",
    )
    def test_a_busy_cancel_with_non_object_params_cancels_nothing(self, monkeypatch):
        spy = _ReadSpy(monkeypatch)
        call_tool, started, release = _slow_then_echo()
        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call(301, "slow"))
            assert started.wait(timeout=5.0)
            harness.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": [301]})
            assert harness.wait_for(
                lambda: spy.saw(
                    lambda v: isinstance(v, dict) and v.get("method") == "notifications/cancelled"
                )
            )
            release.set()
            # Delivered: a list ``params`` names no request, so 301 is not cancelled.
            assert harness.wait_for(_answered(harness, 301))
            harness.send(_PING)
            assert harness.wait_for(_answered(harness, 7))
        finally:
            release.set()
            harness.close()


# --- Caller-identity extension through the stdio loop -----------------------


def _initialize(req_id) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "method": "initialize", "params": {}}


def _tools_call_with_caller(req_id, tool_name: str, session_key: str) -> dict:
    from kiro_crew.mcp_caller import CallerContext, build_caller_meta

    msg = _tools_call(req_id, tool_name)
    msg["params"]["_meta"] = build_caller_meta(
        CallerContext(session_key=session_key, from_gateway=True)
    )
    return msg


def _tools_call_with_tenant(req_id, tool_name: str, nonce: str) -> dict:
    """A forwarded call as an UNNAMED co-tenant receives it: nonce, no identity."""
    from kiro_crew.mcp_caller import build_tenant_meta

    msg = _tools_call(req_id, tool_name)
    msg["params"]["_meta"] = build_tenant_meta(nonce)
    return msg


class TestStdioLoopCallerIdentity:
    def test_tool_policy_uses_only_the_current_request_identity(self, monkeypatch):
        from kiro_crew.mcp_caller import CallerContext, build_caller_meta

        seen = []
        harness = _LoopHarness(monkeypatch, lambda _name, _args: "ok")

        def policy(session="", **_kwargs):
            seen.append(session)
            return mcp_shared.ToolPolicy(frozenset(), "")

        monkeypatch.setattr(mcp_shared, "_resolve_tool_policy", policy)
        try:
            for req_id, method, session in (
                (1, "tools/list", "dashboard:alice"),
                (2, "tools/call", "dashboard:bob"),
                (3, "tools/list", "dashboard:global"),
            ):
                msg = _tools_call(req_id, "echo")
                msg["method"] = method
                msg["params"]["_meta"] = build_caller_meta(
                    CallerContext(session_key=session, from_gateway=True)
                )
                harness.send(msg)
                assert harness.wait_for(lambda: len(harness.responses) >= req_id)
            assert seen == [
                "dashboard:alice",
                "dashboard:bob",
                "dashboard:global",
            ]
        finally:
            harness.close()

    def test_tools_call_hands_the_caller_block_token_to_the_policy_read(self, monkeypatch):
        """The policy read runs on the read loop, before the worker installs the
        caller ContextVar, so ``current_caller()`` is None there. A pooled
        control-plane backend's only copy of the per-session token is the caller
        block gatewayd injected; the dispatch must hand it to the resolver, or the
        gateway answers the read as an unattested caller and refuses every call."""
        from kiro_crew.mcp_caller import CallerContext, build_caller_meta, current_caller

        seen: list[tuple[str, str, object]] = []
        harness = _LoopHarness(monkeypatch, lambda _name, _args: "ok")

        def policy(session="", *, caller_token="", **_kwargs):
            seen.append((session, caller_token, current_caller()))
            return mcp_shared.ToolPolicy(frozenset(), "")

        monkeypatch.setattr(mcp_shared, "_resolve_tool_policy", policy)
        try:
            msg = _tools_call(1, "echo")
            msg["params"]["_meta"] = build_caller_meta(
                CallerContext(
                    session_key="dashboard:carol",
                    from_gateway=True,
                    session_token="tok-frame-carol",
                )
            )
            harness.send(msg)
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert seen == [("dashboard:carol", "tok-frame-carol", None)]
            # And a caller block without a token hands an empty one, never a
            # stale one from a previous frame.
            msg = _tools_call(2, "echo")
            msg["params"]["_meta"] = build_caller_meta(
                CallerContext(session_key="dashboard:dave", from_gateway=True)
            )
            harness.send(msg)
            assert harness.wait_for(lambda: len(harness.responses) >= 2)
            assert seen[-1] == ("dashboard:dave", "", None)
        finally:
            harness.close()

    def test_identity_unattested_refuses_with_the_identity_message(self, monkeypatch):
        """The attestation refusal is fail-closed like the unreadable-spec one,
        but its text names the missing token rather than the agents directory."""
        ran = []
        harness = _LoopHarness(monkeypatch, lambda n, a: ran.append(n) or "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "identity_unattested"),
        )
        try:
            harness.send(_tools_call_with_caller(51, "echo", "dashboard:chat-11"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert ran == []
            body = json.dumps(harness.responses[0][1])
            assert "identity_unattested" in body
            assert "could not prove which session it acts for" in body
            assert "carried no session token" in body
            assert "agents directory" not in body
            assert "Retry shortly" not in body
            assert harness.wait_for(lambda: harness.sel_mock.log_tool_invocation.call_count >= 1)
            kw = harness.sel_mock.log_tool_invocation.call_args.kwargs
            assert kw["outcome"] == "rejected_policy_unresolved"
            assert kw["session_key"] == "dashboard:chat-11"
            assert kw["error"] == "managedToolPolicy.unresolved:identity_unattested"
            # A gateway-stamped caller is a server the gateway spawned; the
            # external-client explanation is not for it, so its text is unchanged.
            assert mcp_shared.external_client_identity_note() not in body
        finally:
            harness.close()

    def test_identity_unattestable_refuses_without_claiming_a_gateway_read(self, monkeypatch):
        """A key resolved from a lenient source with no attestation fails closed.

        The resolver skips the futile dial and returns ``identity_unattestable``, so
        ``tools/call`` refuses (a key resolved, so an operator exclusion may exist)
        but the text does not claim a gateway read that never happened.
        """
        ran = []
        harness = _LoopHarness(monkeypatch, lambda n, a: ran.append(n) or "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "identity_unattestable"),
        )
        try:
            harness.send(_tools_call_with_caller(53, "echo", "dashboard:chat-13"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert ran == []
            body = json.dumps(harness.responses[0][1])
            assert "identity_unattestable" in body
            assert "could not prove which session it acts for" in body
            assert "the gateway refused the tool-policy read" not in body
            assert harness.wait_for(lambda: harness.sel_mock.log_tool_invocation.call_count >= 1)
            kw = harness.sel_mock.log_tool_invocation.call_args.kwargs
            assert kw["outcome"] == "rejected_policy_unresolved"
            assert kw["session_key"] == "dashboard:chat-13"
            assert kw["error"] == "managedToolPolicy.unresolved:identity_unattestable"
        finally:
            harness.close()

    def test_identity_unattested_explains_an_externally_spawned_server(self, monkeypatch):
        """The editor-config report: ``KIROCREW_SESSION_KEY`` copied into an
        editor's own MCP config, no token, no launcher pid, no gateway caller. The
        decision is the same refusal; the text gains the one explanation the
        reader can act on, shared verbatim with the strict-identity refusals."""
        ran = []
        harness = _LoopHarness(monkeypatch, lambda n, a: ran.append(n) or "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "identity_unattested"),
        )
        monkeypatch.delenv("KIROCREW_STUB_SESSION_TOKEN", raising=False)
        monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:chat-copied-by-hand")
        try:
            harness.send(_tools_call(54, "echo"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert ran == []
            body = harness.responses[0][1]["content"][0]["text"]
            assert "identity_unattested" in body
            assert "could not prove which session it acts for" in body
            assert body.endswith(mcp_shared.external_client_identity_note("test-server"))
            assert harness.wait_for(lambda: harness.sel_mock.log_tool_invocation.call_count >= 1)
            kw = harness.sel_mock.log_tool_invocation.call_args.kwargs
            assert kw["outcome"] == "rejected_policy_unresolved"
            assert kw["session_key"] == "dashboard:chat-copied-by-hand"
        finally:
            harness.close()

    def test_identity_unattested_without_a_caller_keeps_its_text_for_a_spawned_server(
        self, monkeypatch
    ):
        """No gateway caller but a token on the element: the non-pooled stdio
        topology the gateway itself spawns. Its failure is the trust root, not an
        editor config, so the note stays off and the wording is what it was."""
        harness = _LoopHarness(monkeypatch, lambda n, a: "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "identity_unattested"),
        )
        monkeypatch.setenv("KIROCREW_STUB_SESSION_TOKEN", "tok")
        monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:chat-12")
        try:
            harness.send(_tools_call(55, "echo"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            body = harness.responses[0][1]["content"][0]["text"]
            assert "identity_unattested" in body
            assert body.endswith("operator's exclusion list.")
            assert mcp_shared.external_client_identity_note("test-server") not in body
        finally:
            harness.close()

    def test_identity_unattested_quotes_the_daemons_denial_when_the_frame_carries_it(
        self, monkeypatch
    ):
        """The Toolbox-shim report: the one accurate diagnosis ("spawned X is not the spec's Y")
        lived only in gatewayd's stdout, so the refusal steered operators at the
        token and the spec instead. When the caller block carries the daemon's
        ``identityDenial``, the refusal says it; without it, the text is unchanged
        (the previous test)."""
        from kiro_crew.mcp_caller import CallerContext, build_caller_meta

        harness = _LoopHarness(monkeypatch, lambda n, a: "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "identity_unattested"),
        )
        reason = "spawned '/opt/local/bin/kirocrew' is not the spec's '/tb/0.7.0.8/bin/kirocrew'"
        try:
            msg = _tools_call(52, "echo")
            msg["params"]["_meta"] = build_caller_meta(
                CallerContext(
                    session_key="dashboard:chat-69", from_gateway=True, identity_denial=reason
                )
            )
            harness.send(msg)
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            body = json.dumps(harness.responses[0][1])
            assert "identity_unattested" in body
            assert "spawned this server without a token because" in body
            assert "/opt/local/bin/kirocrew" in body
            assert "/tb/0.7.0.8/bin/kirocrew" in body
        finally:
            harness.close()

    def test_the_quoted_denial_is_defanged_like_every_other_echoed_error(self, monkeypatch):
        """The reason quotes the spec's ``command``/``args`` verbatim, and this early
        refusal answers through ``_tool_response`` without the tool path's scrubber,
        so a directive sentinel smuggled into a spec's ``args`` must not reach the
        consumer intact."""
        from kiro_crew.mcp_caller import CallerContext, build_caller_meta
        from kiro_crew.session_directive import SENTINEL

        harness = _LoopHarness(monkeypatch, lambda n, a: "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "identity_unattested"),
        )
        reason = f"spawned '/opt/x' is not the spec's '{SENTINEL}{{\"k\":1}}'"
        try:
            msg = _tools_call(53, "echo")
            msg["params"]["_meta"] = build_caller_meta(
                CallerContext(
                    session_key="dashboard:chat-69", from_gateway=True, identity_denial=reason
                )
            )
            harness.send(msg)
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            body = json.dumps(harness.responses[0][1])
            assert "spawned this server without a token because" in body
            assert SENTINEL not in body, "the sentinel must be defanged, not forwarded"
        finally:
            harness.close()

    def test_the_quoted_denial_is_credential_redacted(self, monkeypatch):
        """A denial reason that carries a token (a hand-authored reserved entry
        whose argv the gate quoted) must not hand that token to the session."""
        from kiro_crew.mcp_caller import CallerContext, build_caller_meta

        harness = _LoopHarness(monkeypatch, lambda n, a: "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "identity_unattested"),
        )
        token = "ghp_1234567890abcdefghijklmnopqrstuvwxyzAB"
        reason = f"args ['mcp-core', '--token', '{token}'] differ from spec ['mcp-core']"
        try:
            msg = _tools_call(54, "echo")
            msg["params"]["_meta"] = build_caller_meta(
                CallerContext(
                    session_key="dashboard:chat-69", from_gateway=True, identity_denial=reason
                )
            )
            harness.send(msg)
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            body = json.dumps(harness.responses[0][1])
            assert "spawned this server without a token because" in body
            assert token not in body, "the credential must be redacted, not forwarded"
        finally:
            harness.close()

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="select interleave uses a POSIX pipe")
    def test_listing_while_busy_does_not_borrow_the_running_members_identity(self, monkeypatch):
        from kiro_crew.mcp_caller import CallerContext, build_caller_meta

        call, started, release = _slow_then_echo()
        seen = []
        harness = _LoopHarness(monkeypatch, call)

        def policy(session="", **_kwargs):
            seen.append(session)
            return mcp_shared.ToolPolicy(frozenset(), "")

        monkeypatch.setattr(mcp_shared, "_resolve_tool_policy", policy)
        try:
            for req_id, method, session in (
                (1, "tools/call", "dashboard:alice"),
                (2, "tools/list", "dashboard:bob"),
            ):
                msg = _tools_call(req_id, "slow")
                msg["method"] = method
                msg["params"]["_meta"] = build_caller_meta(
                    CallerContext(session_key=session, from_gateway=True)
                )
                harness.send(msg)
                if req_id == 1:
                    assert started.wait(timeout=5)
            assert harness.wait_for(lambda: any(row[0] == 2 for row in harness.responses))
            assert seen == [
                "dashboard:alice",
                "dashboard:bob",
            ]
        finally:
            release.set()
            harness.close()

    def test_initialize_advertises_capability_when_opted_in(self, monkeypatch):
        # Without the advertisement gatewayd treats the backend as
        # single-session and never injects the caller block, which would make
        # the whole per-call identity path dead code.
        harness = _LoopHarness(monkeypatch, lambda n, a: "ok", {"advertise_caller_identity": True})
        try:
            harness.send(_initialize(1))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            caps = harness.responses[0][1]["capabilities"]
            assert caps["experimental"] == {"kirocrew.caller-identity": {"schemaVersion": 1}}
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

    def test_tool_sees_the_tenant_nonce_WITHOUT_an_identity(self, monkeypatch):
        """The separator arrives even when the identity does not.

        This is the frame an unnamed co-tenant of a pooled backend receives. The
        nonce must reach the tool (it is what per-tenant state is keyed on when
        there is nothing else), the caller must stay None (a connection name is not
        an identity), and both must be cleared afterwards so the next dispatch on
        this thread cannot inherit them.
        """
        from kiro_crew import mcp_caller

        seen: list = []

        def call_tool(name, args):
            seen.append((mcp_caller.current_caller(), mcp_caller.current_tenant_nonce()))
            return "ok"

        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call_with_tenant(12, "echo", "n0nce-a"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert seen == [(None, "n0nce-a")]
            assert mcp_caller.current_tenant_nonce() == ""  # cleared after dispatch
        finally:
            harness.close()

    def test_a_call_with_no_tenant_block_sees_an_empty_nonce(self, monkeypatch):
        """The 1:1 topology, where no gateway injects anything.

        An empty nonce is the signal to keep using the backend's own per-process
        fallback, so it must not be a stale value from a previous call.
        """
        from kiro_crew import mcp_caller

        seen: list = []

        def call_tool(name, args):
            seen.append(mcp_caller.current_tenant_nonce())
            return "ok"

        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call_with_tenant(13, "echo", "n0nce-a"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            harness.send(_tools_call(14, "echo"))
            assert harness.wait_for(lambda: len(harness.responses) >= 2)
            assert seen == ["n0nce-a", ""]
        finally:
            harness.close()

    def test_excluded_tool_audit_attributes_caller_session(self, monkeypatch):
        # In a shared backend the env var attributes rejection audits to "mcp"
        # or the wrong session, so the parsed caller identity must win when
        # present.
        harness = _LoopHarness(monkeypatch, lambda n, a: "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset({"blocked"}), ""),
        )
        try:
            harness.send(_tools_call_with_caller(21, "blocked", "dashboard:chat-9"))
            assert harness.wait_for(lambda: harness.sel_mock.log_tool_invocation.call_count >= 1)
            kw = harness.sel_mock.log_tool_invocation.call_args.kwargs
            assert kw["outcome"] == "rejected_excluded"
            assert kw["session_key"] == "dashboard:chat-9"
        finally:
            harness.close()

    def test_unresolved_policy_refuses_the_call(self, monkeypatch):
        # The one reason that refuses. It means ONE thing -- the gateway read a
        # spec for this session and could not determine its policy -- so an
        # operator's exclusion may exist and is being withheld, and running the
        # tool would ignore it.
        ran = []
        harness = _LoopHarness(monkeypatch, lambda n, a: ran.append(n) or "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "policy_unreadable"),
        )
        try:
            harness.send(_tools_call_with_caller(41, "echo", "dashboard:chat-7"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            # The tool never ran.
            assert ran == []
            # The refusal is loud and says why, rather than looking like a
            # missing tool.
            body = json.dumps(harness.responses[0][1])
            assert "tool policy could not be read" in body
            assert "policy_unreadable" in body
            # And it is attributable: the audit names the caller and the path.
            assert harness.wait_for(lambda: harness.sel_mock.log_tool_invocation.call_count >= 1)
            kw = harness.sel_mock.log_tool_invocation.call_args.kwargs
            assert kw["outcome"] == "rejected_policy_unresolved"
            assert kw["session_key"] == "dashboard:chat-7"
            assert kw["error"] == "managedToolPolicy.unresolved:policy_unreadable"
            # Regression pin: with no reason from the gateway the text is the
            # historical wording, so a client on an older gateway reads exactly
            # what it read before.
            assert "fix or remove the unreadable spec in the agents directory" in body
            assert "Gateway reason:" not in body
        finally:
            harness.close()

    def test_the_unreadable_policy_refusal_rules_out_a_connection_problem(self, monkeypatch):
        """``policy_unreadable`` is an answer from the gateway, so its text says so.

        It shares its symptom -- every call refused -- with an unreachable
        gateway, and an operator who reads it as an outage restarts healthy
        processes while the spec stays broken. The text therefore rules out
        reachability, and names every condition the endpoint answers this code
        for (an unreadable or unparseable spec, a duplicate name, a wrong-shape
        ``managedToolPolicy``) rather than only the parse.
        """
        harness = _LoopHarness(monkeypatch, lambda n, a: "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "policy_unreadable"),
        )
        try:
            harness.send(_tools_call_with_caller(45, "echo", "dashboard:chat-7"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            body = json.dumps(harness.responses[0][1])
            assert "the gateway answered" in body
            assert "This is not a connection problem" in body
            assert "a restart will not clear it" in body
            assert "two specs declare the same name" in body
            assert "could not be read or parsed" in body
            assert "managedToolPolicy has the wrong shape" in body
        finally:
            harness.close()

    def test_unresolved_policy_refusal_names_the_file_the_gateway_named(self, monkeypatch):
        """The gateway's ``reason`` reaches the caller, defanged and redacted.

        The 409 body names the unreadable file and what to do; that is the one
        piece of information the operator needs, and a refusal without it
        sends them to validate every file by hand. The reason interpolates a
        FILENAME from a user-writable
        directory, so it goes through the same two scrubbers the
        ``identity_unattested`` arm applies to its denial text -- the directive
        defang and the credential redaction -- and is bounded, because this
        early refusal does not pass through the tool path's scrubbers.
        """
        ran = []
        harness = _LoopHarness(monkeypatch, lambda n, a: ran.append(n) or "ok")
        reason = (
            "agent spec 'broken.json' in the agents directory could not be read "
            "(not valid JSON), so the policy for 'default' is unknown. "
            "Move or fix 'broken.json' in the agents directory; no restart needed."
        )
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "policy_unreadable", reason),
        )
        try:
            harness.send(_tools_call_with_caller(42, "echo", "dashboard:chat-7"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert ran == []
            body = json.dumps(harness.responses[0][1])
            assert "policy_unreadable" in body
            assert "'broken.json'" in body
            assert "not valid JSON" in body
            assert "no restart needed" in body
        finally:
            harness.close()

    def test_the_gateway_reason_is_defanged_and_bounded(self, monkeypatch):
        """A filename can carry the directive sentinel or a token; neither survives."""
        from kiro_crew import session_directive

        harness = _LoopHarness(monkeypatch, lambda n, a: "ok")
        sentinel = session_directive.SENTINEL
        secret = "ghp_" + "A" * 36
        reason = f"agent spec {sentinel + 'x.json'!r} could not be read {secret} " + "y" * 5000
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "policy_unreadable", reason),
        )
        try:
            harness.send(_tools_call_with_caller(43, "echo", "dashboard:chat-7"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            body = json.dumps(harness.responses[0][1])
            assert sentinel not in body
            assert secret not in body
            assert len(body) < 2000, "the gateway reason was not bounded"
        finally:
            harness.close()

    def test_a_secret_straddling_the_detail_cap_leaves_no_fragment(self, monkeypatch):
        """Redaction runs over the WHOLE reason; the cap trims what it returns.

        Cut first and a token that straddles the cap loses its tail, the
        fragment fails to match the redactor's pattern, and the head of the
        secret is echoed. So the cap is applied to the redacted text, and the
        prefix of a token that would have been cut is never in the response.
        """
        harness = _LoopHarness(monkeypatch, lambda n, a: "ok")
        cap = mcp_shared._POLICY_DETAIL_MAX_CHARS
        secret = "ghp_" + "B" * 36
        # The token begins 10 characters before the cap, so a cut-first
        # implementation keeps ``ghp_BBBBBB`` and drops the rest.
        reason = "x" * (cap - 10) + secret + " tail"
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "policy_unreadable", reason),
        )
        try:
            harness.send(_tools_call_with_caller(44, "echo", "dashboard:chat-7"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            body = json.dumps(harness.responses[0][1])
            assert "ghp_B" not in body, "a fragment of the secret survived the cut"
        finally:
            harness.close()

    def test_no_usable_answer_refuses_the_call(self, monkeypatch):
        """``resolution_failed`` refuses, because the exclusion set is unknown.

        Nothing came back, or a ``5xx`` said the gateway is broken, or the resolve
        raised. Every ``4xx`` returns before that arm, so this reason means the
        policy could not be READ -- an operator exclusion may exist while the
        process holding it cannot answer for it. Serving the empty set as a
        permission is what would let an excluded tool run, so the call is refused
        and the refusal names the same condition the audit trail does.
        """
        ran = []
        harness = _LoopHarness(monkeypatch, lambda n, a: ran.append(n) or "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "resolution_failed"),
        )
        try:
            harness.send(_tools_call_with_caller(53, "echo", "dashboard:chat-13"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert ran == []
            _body = json.dumps(harness.responses[0][1])
            assert "is unavailable" in _body
            assert "resolution_failed" in _body
            ops = [
                c.kwargs.get("operation") for c in harness.sel_mock.log_api_access.call_args_list
            ]
            assert "tool_policy.unenforced_call" not in ops
        finally:
            harness.close()

    def test_the_unreachable_gateway_refusal_names_the_exception_class(self, monkeypatch):
        """The agent sees WHICH failure blocked it, not just that one happened."""
        harness = _LoopHarness(monkeypatch, lambda n, a: "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(
                frozenset(), "resolution_failed", "ConnectionRefusedError"
            ),
        )
        try:
            harness.send(_tools_call_with_caller(57, "echo", "dashboard:chat-17"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            body = json.dumps(harness.responses[0][1])
            assert "(cause: ConnectionRefusedError)" in body
        finally:
            harness.close()

    def test_the_unreachable_gateway_refusal_names_a_retry_not_a_spec_edit(self, monkeypatch):
        """The refusal has to diagnose the condition it actually hit.

        ``policy_unreadable`` means the gateway read a spec and could not use it,
        so its text sends the caller to the agents directory. ``resolution_failed``
        means the gateway was never reached: no spec is implicated, and that same
        text would have the caller edit healthy files to fix an outage, leaving the
        edit as the real defect. The condition clears on its own, so the remedy is
        a retry.
        """
        harness = _LoopHarness(monkeypatch, lambda n, a: "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "resolution_failed"),
        )
        try:
            harness.send(_tools_call_with_caller(56, "echo", "dashboard:chat-16"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            body = json.dumps(harness.responses[0][1])
            assert "could not reach the gateway" in body
            assert "retry" in body
            assert "agents directory" not in body
            assert "could not parse" not in body
        finally:
            harness.close()

    def test_an_excluded_tool_stays_excluded_when_the_gateway_cannot_answer(self, monkeypatch):
        """The defect this guards: a real exclusion going unenforced.

        The named tool is genuinely excluded for this session, and the resolver
        cannot reach the gateway to say so. The exclusion set therefore arrives
        empty with ``resolution_failed``, which is indistinguishable by value
        alone from an operator who excluded nothing. The reason is what keeps them
        apart, so the excluded tool must not execute.
        """
        ran = []
        harness = _LoopHarness(monkeypatch, lambda n, a: ran.append(n) or "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "resolution_failed"),
        )
        try:
            harness.send(_tools_call_with_caller(55, "blocked", "dashboard:chat-15"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert ran == []
        finally:
            harness.close()

    def test_a_gateway_that_declines_this_caller_does_not_refuse(self, monkeypatch):
        """``policy_forbidden`` is a boundary the gateway HOLDS, not one it lost.

        The 403 is ``member_session_unverified``: a session claiming a member
        store without a scope the gateway can verify. That is the steady
        state for a whole class of callers rather than a window that closes, so
        refusing would deny them tools permanently. The call proceeds and the
        window is audited.
        """
        ran = []
        harness = _LoopHarness(monkeypatch, lambda n, a: ran.append(n) or "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "policy_forbidden"),
        )
        try:
            harness.send(_tools_call_with_caller(54, "echo", "dashboard:chat-14"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert ran == ["echo"]
            ops = [
                c.kwargs.get("operation") for c in harness.sel_mock.log_api_access.call_args_list
            ]
            assert "tool_policy.unenforced_call" in ops
        finally:
            harness.close()

    def test_an_identity_reason_lets_the_call_run_and_audits_it(self, monkeypatch):
        # no_session_key / agent_not_resolved mean no AGENT was named, and a
        # managedToolPolicy is a property of an agent -- so there is no operator
        # exclusion for this call to bypass. The gateway returns the same 404
        # both for a session still registering and for a caller it can never
        # name, so refusing here denies the second class forever. The call runs,
        # and the window is audited rather than silent.
        ran = []
        harness = _LoopHarness(monkeypatch, lambda n, a: ran.append(n) or "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "agent_not_resolved"),
        )
        try:
            harness.send(_tools_call_with_caller(51, "echo", "dashboard:chat-11"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert ran == ["echo"]
            ops = [
                c.kwargs.get("operation") for c in harness.sel_mock.log_api_access.call_args_list
            ]
            assert "tool_policy.unenforced_call" in ops
        finally:
            harness.close()

    def test_an_unreadable_policy_refuses_the_call(self, monkeypatch):
        # The other half: the gateway NAMED an agent and could not read its
        # policy, so an operator exclusion may exist and is being withheld.
        ran = []
        harness = _LoopHarness(monkeypatch, lambda n, a: ran.append(n) or "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "policy_unreadable"),
        )
        try:
            harness.send(_tools_call_with_caller(52, "echo", "dashboard:chat-12"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert ran == []
            assert "tool policy could not be read" in json.dumps(harness.responses[0][1])
        finally:
            harness.close()

    def test_unresolved_policy_still_lists_every_tool(self, monkeypatch):
        # The anti-brick half. kiro-cli calls tools/list ONCE per
        # session and caches the answer, so hiding tools on a transient policy
        # failure would hide them for the session's whole life. Listing is not
        # the enforcement point; the call path above is.
        tools = [{"name": "echo"}, {"name": "blocked"}]
        harness = _LoopHarness(monkeypatch, lambda n, a: "ok", list_tools_fn=lambda: list(tools))
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "no_session_key"),
        )
        try:
            harness.send({"jsonrpc": "2.0", "id": 42, "method": "tools/list", "params": {}})
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            listed = [t["name"] for t in harness.responses[0][1]["tools"]]
            assert listed == ["echo", "blocked"]
            # The widened listing window is recorded, so it is attributable.
            ops = [
                c.kwargs.get("operation") for c in harness.sel_mock.log_api_access.call_args_list
            ]
            assert "tool_policy.unfiltered_listing" in ops
        finally:
            harness.close()

    def test_resolved_empty_policy_runs_the_call(self, monkeypatch):
        # The control for the test above it: a session whose policy WAS read
        # and excludes nothing must still be able to call tools. A fix that
        # refused here would brick every session that never had an exclusion.
        ran = []
        harness = _LoopHarness(monkeypatch, lambda n, a: ran.append(n) or "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), ""),
        )
        try:
            harness.send(_tools_call_with_caller(43, "echo", "dashboard:chat-8"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert ran == ["echo"]
            assert "could not be read" not in json.dumps(harness.responses[0][1])
        finally:
            harness.close()

    def test_failed_tool_audit_attributes_caller_session(self, monkeypatch):
        def boom(name, args):
            raise RuntimeError("kaput")

        harness = _LoopHarness(monkeypatch, boom)
        try:
            harness.send(_tools_call_with_caller(31, "echo", "dashboard:chat-5"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert harness.wait_for(lambda: harness.sel_mock.log_tool_invocation.call_count >= 1)
            kw = harness.sel_mock.log_tool_invocation.call_args.kwargs
            assert kw["outcome"] == "failed"
            assert kw["session_key"] == "dashboard:chat-5"
        finally:
            harness.close()


class TestPerSessionToolPolicy:
    """Pooled backends must not bleed one session's policy into another."""

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

    def test_policy_http_forwards_each_session_without_a_memory_capability(self, monkeypatch):
        requests = []

        def policy(req, timeout=0):
            requests.append(dict(req.header_items()))
            return io.BytesIO(b'{"exclude":["blocked"]}')

        monkeypatch.setattr(mcp_shared, "loopback_urlopen", policy)
        monkeypatch.setattr(mcp_shared, "read_local_secret", lambda _port, **_kw: "internal")
        monkeypatch.setattr(mcp_shared, "resolve_client_port_src", lambda port: (5476, "config"))
        assert mcp_shared._resolve_tool_policy("dashboard:alice").excluded == {"blocked"}
        assert mcp_shared._resolve_tool_policy("dashboard:global").excluded == {"blocked"}
        assert requests[0]["X-session-key"] == "dashboard:alice"
        assert "X-member-session-proof" not in requests[0]
        assert "X-member-session-proof" not in requests[1]
        assert mcp_shared._excluded_tools_by_session == {
            "dashboard:alice": {"blocked"},
            "dashboard:global": {"blocked"},
        }

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
        monkeypatch.setattr(mcp_shared, "resolve_client_port_src", lambda port: (5476, "config"))
        monkeypatch.setattr(mcp_shared, "_read_internal_secret", lambda: "s3cr3t", raising=False)

        a = mcp_shared._resolve_tool_policy("dashboard:chat-1").excluded
        b = mcp_shared._resolve_tool_policy("dashboard:chat-2").excluded
        assert a == {"tool_a"}
        assert b == {"tool_b"}  # NOT session-1's cached policy
        # Cache hit path: no third HTTP call for a repeat lookup.
        n = len(calls)
        assert mcp_shared._resolve_tool_policy("dashboard:chat-1").excluded == {"tool_a"}
        assert len(calls) == n


@pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="worker-thread + select() interleave is POSIX-only",
)
class TestStdioLoopExitsWhenItsInstallIsPruned:
    """A backend whose install an update removed must be replaced, not limp on.

    Lazy imports fail there with ``No module named 'kiro_crew.<x>'`` (seen live
    on ``kirocrew-core``: every ``monitor_*`` and ``session_ledger_record`` call)
    and keep failing until the process is gone. The loop answers the call with a
    retryable refusal, answers anything queued behind it, never runs the tool,
    and exits non-zero so the pool respawns it from the current install.
    """

    def setup_method(self):
        mcp_shared._use_content_length = False

    def test_a_call_after_the_prune_is_refused_and_the_process_exits(
        self, monkeypatch, tmp_path
    ) -> None:
        import shutil as _shutil

        from kiro_crew import install_liveness

        package = tmp_path / "0.8.0.4" / "kiro_crew"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        monkeypatch.setattr(install_liveness, "_PACKAGE_ROOT", package)
        monkeypatch.setenv(
            install_liveness.POOLED_BACKEND_ENV, install_liveness.POOLED_BACKEND_VALUE
        )
        monkeypatch.setenv(install_liveness.POOLED_RESPAWN_COMMAND_ENV, sys.executable)
        exits: list = []
        monkeypatch.setattr(mcp_shared.sys, "exit", exits.append)

        calls: list[str] = []

        def call_tool(name, args):
            calls.append(name)
            return f"done:{name}"

        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call(1, "before"))
            assert harness.wait_for(lambda: len(harness.responses) == 1)
            _shutil.rmtree(package.parent)  # the update's prune
            harness.send(_tools_call(2, "monitor_start"))
            assert harness.wait_for(lambda: len(harness.responses) == 2)
            harness._thread.join(timeout=5.0)
            assert not harness._thread.is_alive(), "the loop kept serving a pruned install"
        finally:
            harness.close()

        assert calls == ["before"], "a tool ran from the pruned install"
        req_id, result, error = harness.responses[1]
        assert req_id == 2 and result is None
        assert error["code"] == -32000 and "retry" in error["message"]
        assert exits == [install_liveness.INSTALL_PRUNED_EXIT_CODE]

    def test_calls_queued_behind_the_prune_are_answered_too(self, monkeypatch, tmp_path) -> None:
        """The loop exits on the first refused call; anything already queued
        behind it must get the same retryable answer, not silence."""
        import shutil as _shutil
        import time

        from kiro_crew import install_liveness

        package = tmp_path / "0.8.0.4" / "kiro_crew"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        monkeypatch.setattr(install_liveness, "_PACKAGE_ROOT", package)
        monkeypatch.setenv(
            install_liveness.POOLED_BACKEND_ENV, install_liveness.POOLED_BACKEND_VALUE
        )
        monkeypatch.setenv(install_liveness.POOLED_RESPAWN_COMMAND_ENV, sys.executable)
        monkeypatch.setattr(mcp_shared.sys, "exit", lambda _code: None)
        call_tool, started, release = _slow_then_echo()
        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call(1, "slow"))
            assert started.wait(timeout=5.0)
            # One frame per read so the busy loop's select() sees each and
            # moves it into the pending queue (two frames in one write land in
            # the text buffer together, where select() cannot see the second).
            harness.send(_tools_call(2, "queued-a"))
            time.sleep(0.3)
            harness.send(_tools_call(3, "queued-b"))
            time.sleep(0.3)
            harness.send(_tools_call(4, "queued-c"))
            time.sleep(0.3)
            # Cancelled while it waited: must get no response, as on the
            # ordinary dispatch path.
            harness.send(
                {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 4}}
            )
            time.sleep(0.3)
            _shutil.rmtree(package.parent)
            release.set()
            assert harness.wait_for(lambda: len(harness.responses) == 3), harness.responses
            harness._thread.join(timeout=5.0)
        finally:
            harness.close()
        by_id = {r[0]: r for r in harness.responses}
        assert sorted(by_id) == [1, 2, 3], "a cancelled queued call was answered"
        assert by_id[1][2] is None  # in flight before the prune: delivered
        assert by_id[2][2]["code"] == -32000
        assert by_id[3][2]["code"] == -32000

        def _audited(outcome: str) -> list:
            return sorted(
                (c.kwargs["request_id"], c.kwargs["tool_name"])
                for c in harness.sel_mock.log_tool_invocation.call_args_list
                if c.kwargs.get("outcome") == outcome
            )

        # Every refusal is its own audited invocation decision, queued or not.
        assert _audited("rejected_install_pruned") == [("2", "queued-a"), ("3", "queued-b")]
        assert _audited("cancelled") == [("4", "queued-c")]

    def test_an_intact_install_keeps_serving(self, monkeypatch, tmp_path) -> None:
        from kiro_crew import install_liveness

        package = tmp_path / "kiro_crew"
        package.mkdir()
        (package / "__init__.py").write_text("")
        monkeypatch.setattr(install_liveness, "_PACKAGE_ROOT", package)
        monkeypatch.setenv(
            install_liveness.POOLED_BACKEND_ENV, install_liveness.POOLED_BACKEND_VALUE
        )
        monkeypatch.setenv(install_liveness.POOLED_RESPAWN_COMMAND_ENV, sys.executable)
        exits: list = []
        monkeypatch.setattr(mcp_shared.sys, "exit", exits.append)
        harness = _LoopHarness(monkeypatch, lambda n, a: f"done:{n}")
        try:
            harness.send(_tools_call(1, "a"))
            harness.send(_tools_call(2, "b"))
            assert harness.wait_for(lambda: len(harness.responses) == 2)
        finally:
            harness.close()
        assert [r[2] for r in harness.responses] == [None, None]
        assert exits == []

    def test_a_directly_launched_server_refuses_but_keeps_its_transport(
        self, monkeypatch, tmp_path
    ) -> None:
        """Without the pool there is no respawner: exiting would leave the
        session with no kirocrew-core/kirocrew-cron tools at all. The server
        stays up and refuses every call with the restart that recovers it."""
        import shutil as _shutil

        from kiro_crew import install_liveness

        package = tmp_path / "0.8.0.4" / "kiro_crew"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        monkeypatch.setattr(install_liveness, "_PACKAGE_ROOT", package)
        monkeypatch.delenv(install_liveness.POOLED_BACKEND_ENV, raising=False)
        exits: list = []
        monkeypatch.setattr(mcp_shared.sys, "exit", exits.append)
        calls: list[str] = []

        def call_tool(name, args):
            calls.append(name)
            return f"done:{name}"

        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call(1, "before"))
            assert harness.wait_for(lambda: len(harness.responses) == 1)
            _shutil.rmtree(package.parent)
            harness.send(_tools_call(2, "monitor_start"))
            harness.send(_tools_call(3, "cron_list"))
            assert harness.wait_for(lambda: len(harness.responses) == 3), harness.responses
            assert harness._thread.is_alive(), "a directly launched server exited"
        finally:
            harness.close()

        assert calls == ["before"], "a tool ran from the pruned install"
        for req_id, result, error in harness.responses[1:]:
            assert result is None
            assert error["code"] == -32000 and "restart" in error["message"]
        assert exits == []
        audited = sorted(
            c.kwargs["request_id"]
            for c in harness.sel_mock.log_tool_invocation.call_args_list
            if c.kwargs.get("outcome") == "rejected_install_pruned"
        )
        assert audited == ["2", "3"]

    def test_a_pooled_server_whose_respawn_launcher_was_pruned_keeps_its_transport(
        self, monkeypatch, tmp_path
    ) -> None:
        """The pool respawns from the command it spawned this backend with, and
        that launcher can sit inside the pruned tree. Exiting then gets no
        replacement, so the server must stay up and refuse like a direct one."""
        import shutil as _shutil

        from kiro_crew import install_liveness

        package = tmp_path / "0.8.0.4" / "kiro_crew"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        launcher = tmp_path / "0.8.0.4" / "bin" / "kirocrew"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\n")
        launcher.chmod(0o755)
        monkeypatch.setattr(install_liveness, "_PACKAGE_ROOT", package)
        monkeypatch.setenv(
            install_liveness.POOLED_BACKEND_ENV, install_liveness.POOLED_BACKEND_VALUE
        )
        monkeypatch.setenv(install_liveness.POOLED_RESPAWN_COMMAND_ENV, str(launcher))
        exits: list = []
        monkeypatch.setattr(mcp_shared.sys, "exit", exits.append)
        calls: list[str] = []

        def call_tool(name, args):
            calls.append(name)
            return f"done:{name}"

        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call(1, "before"))
            assert harness.wait_for(lambda: len(harness.responses) == 1)
            _shutil.rmtree(package.parent)  # takes the launcher with it
            harness.send(_tools_call(2, "monitor_start"))
            assert harness.wait_for(lambda: len(harness.responses) == 2), harness.responses
            assert harness._thread.is_alive(), "exited with no working respawn"
        finally:
            harness.close()

        assert calls == ["before"], "a tool ran from the pruned install"
        _req_id, result, error = harness.responses[1]
        assert result is None
        assert error["code"] == -32000 and "restart" in error["message"]
        assert exits == []


@pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="worker-thread + select() interleave is POSIX-only",
)
class TestResponseOutcomeHook:
    """A tool learns whether its response was written, only after the dispatcher decided."""

    @staticmethod
    def _tool(harness_ref: list, outcomes: list, done, *, block=None, cancel=False):
        def call_tool(name, args):
            def _hook(delivered: bool) -> None:
                answered = [r for r in harness_ref[0].responses if r[0] == 1]
                outcomes.append((delivered, bool(answered)))
                done.set()

            assert mcp_shared.on_response_outcome(_hook) is None
            if block is not None:
                block[0].set()
                block[1].wait(timeout=10.0)
            if cancel:
                raise mcp_shared.ToolCancelled("stopped")
            return "ok"

        return call_tool

    def test_a_written_response_reports_delivered_after_it_is_written(self, monkeypatch):
        import threading

        outcomes: list = []
        done = threading.Event()
        ref: list = []
        harness = _LoopHarness(monkeypatch, self._tool(ref, outcomes, done))
        ref.append(harness)
        try:
            harness.send(_tools_call(1, "spawn_sub_agents"))
            assert done.wait(timeout=10.0)
            assert outcomes == [(True, True)]
        finally:
            harness.close()

    def test_a_response_cancelled_after_the_call_returned_reports_dropped(self, monkeypatch):
        import threading

        outcomes: list = []
        done = threading.Event()
        started, release = threading.Event(), threading.Event()
        ref: list = []
        harness = _LoopHarness(
            monkeypatch, self._tool(ref, outcomes, done, block=(started, release))
        )
        ref.append(harness)
        try:
            harness.send(_tools_call(1, "spawn_sub_agents"))
            assert started.wait(timeout=5.0)
            cancel = {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 1},
            }
            harness.send(cancel)
            assert harness.wait_for(lambda: "1" in _cancelled_now(harness))
            release.set()
            assert done.wait(timeout=10.0)
            assert outcomes == [(False, False)]
        finally:
            release.set()
            harness.close()

    def test_a_torn_response_frame_reports_dropped(self, monkeypatch):
        import threading

        outcomes: list = []
        done = threading.Event()
        ref: list = []
        harness = _LoopHarness(monkeypatch, self._tool(ref, outcomes, done))
        ref.append(harness)
        record = harness._record

        def _torn(req_id, result, error=None):
            record(req_id, result, error)
            return False  # the frame tore before the client had all of it

        monkeypatch.setattr(mcp_shared, "respond", _torn)
        try:
            harness.send(_tools_call(1, "spawn_sub_agents"))
            assert done.wait(timeout=10.0)
            assert outcomes == [(False, True)]
        finally:
            harness.close()

    def test_a_cooperatively_cancelled_call_reports_dropped(self, monkeypatch):
        import threading

        outcomes: list = []
        done = threading.Event()
        ref: list = []
        harness = _LoopHarness(monkeypatch, self._tool(ref, outcomes, done, cancel=True))
        ref.append(harness)
        try:
            harness.send(_tools_call(1, "spawn_sub_agents"))
            assert done.wait(timeout=10.0)
            assert outcomes == [(False, False)]
        finally:
            harness.close()

    def test_a_hook_registered_after_an_eof_hears_the_drop(self, monkeypatch):
        """The dispatcher settles at EOF before the worker registers: the late
        registration gets the drop, never the direct-call commit."""
        import threading

        registered, release = threading.Event(), threading.Event()
        seen: list = []

        def call_tool(name, args):
            release.wait(timeout=10.0)
            seen.append(mcp_shared.on_response_outcome(lambda delivered: None))
            registered.set()
            return "ok"

        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call(1, "spawn_sub_agents"))
            assert harness.wait_for(lambda: "1" in mcp_shared._response_outcome_arms)
            mcp_shared._settle_response_outcome(1, False)  # the EOF path's settle
            release.set()
            assert registered.wait(timeout=10.0)
            assert seen == [False]
        finally:
            release.set()
            harness.close()

    def test_outside_a_dispatched_call_nothing_is_registered(self):
        assert mcp_shared.on_response_outcome(lambda d: None) is True


def _cancelled_now(harness) -> set:
    """The ids the loop has marked cancelled: its cancel event is the source."""
    evt = mcp_shared._thread_cancel_event
    return {"1"} if evt is not None and evt.is_set() else set()


def test_a_thread_no_dispatched_call_runs_on_hears_a_drop_once_calls_are_dispatched():
    import threading

    mcp_shared._arm_response_outcome(1)
    try:
        seen: list = []
        other = threading.Thread(
            target=lambda: seen.append(mcp_shared.on_response_outcome(lambda d: None))
        )
        other.start()
        other.join(timeout=10)
        # It cannot learn the outcome, so it is never told to commit.
        assert seen == [False]
        assert mcp_shared.on_response_outcome(lambda d: None) is None
    finally:
        mcp_shared._settle_response_outcome(1, False)


def test_a_settled_calls_late_registration_gets_its_outcome_and_registers_nothing():
    for delivered, expected in ((True, True), (False, False)):
        mcp_shared._arm_response_outcome(1)
        arm = mcp_shared._response_outcome_arms["1"]
        mcp_shared._settle_response_outcome(1, delivered)
        assert mcp_shared.on_response_outcome(lambda d: None) is expected
        assert arm.hook is None  # nothing waits on an answer already given


def test_a_second_registration_by_the_same_call_registers_nothing_and_never_commits():
    mcp_shared._arm_response_outcome(1)
    try:
        first = mcp_shared.on_response_outcome(lambda d: None)
        second = mcp_shared.on_response_outcome(lambda d: None)
        assert (first, second) == (None, False)
    finally:
        mcp_shared._settle_response_outcome(1, False)


@pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="worker-thread + select() interleave is POSIX-only",
)
def test_the_loop_settles_every_call_before_it_arms_the_next(monkeypatch):
    """The loop dispatches one call at a time: a second call is never armed
    while the first is unsettled."""
    import threading

    unsettled_at_arm: list = []
    real_arm = mcp_shared._arm_response_outcome

    def _arm(req_id):
        for previous in mcp_shared._response_outcome_arms.values():
            if previous.outcome is None:
                unsettled_at_arm.append((previous.req, str(req_id)))
        real_arm(req_id)

    monkeypatch.setattr(mcp_shared, "_arm_response_outcome", _arm)
    answered = threading.Event()
    calls: list = []

    def call_tool(name, args):
        calls.append(name)
        if len(calls) == 3:
            answered.set()
        return "ok"

    harness = _LoopHarness(monkeypatch, call_tool)
    try:
        for rid in (1, 2, 3):
            harness.send(_tools_call(rid, "spawn_sub_agents"))
        assert answered.wait(timeout=10.0)
        assert harness.wait_for(lambda: len([r for r in harness.responses if r[0] == 3]) == 1)
        assert unsettled_at_arm == []
    finally:
        harness.close()


def _hookless_call_observations(monkeypatch) -> dict:
    """Dispatch one call of a tool other than ``spawn_sub_agents``, and
    record what the arming left behind while it ran and after it was answered."""
    import threading

    joins: list = []
    monkeypatch.setattr(mcp_shared, "_register_exit_join", lambda *a: joins.append(a))
    seen: dict = {}
    answered = threading.Event()

    def call_tool(name, args):
        seen["arms"] = dict(mcp_shared._response_outcome_arms)
        seen["thread_arm"] = getattr(mcp_shared._response_outcome_local, "arm", None)
        answered.set()
        return "ok"

    harness = _LoopHarness(monkeypatch, call_tool)
    try:
        harness.send(_tools_call(1, "plain"))
        assert answered.wait(timeout=10.0)
        assert harness.wait_for(lambda: len([r for r in harness.responses if r[0] == 1]) == 1)
    finally:
        harness.close()
    seen["joins"] = joins
    seen["join_registered"] = mcp_shared._outcome_exit_join_registered
    return seen


_NOTHING_ARMED = {"arms": {}, "thread_arm": None, "joins": [], "join_registered": False}


@pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="worker-thread + select() interleave is POSIX-only",
)
def test_a_hookless_tools_call_arms_nothing_and_registers_no_exit_join(monkeypatch):
    """Only ``spawn_sub_agents`` is armed for its response's outcome. Any other
    call, on any MCP server, leaves no arm and no exit join, so its exit never
    waits on outcome reports."""
    assert _hookless_call_observations(monkeypatch) == _NOTHING_ARMED


@pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="worker-thread + select() interleave is POSIX-only",
)
def test_mutation_arming_every_call_is_caught(monkeypatch):
    """Mutation pin: a dispatcher that arms every call, hook or not, arms the
    hookless call and registers the exit join, so the check above goes red."""
    real_arm = mcp_shared._arm_response_outcome
    monkeypatch.setattr(mcp_shared, "_arm_for_dispatch", lambda req_id, _name: real_arm(req_id))
    seen = _hookless_call_observations(monkeypatch)
    assert seen != _NOTHING_ARMED
    assert seen["thread_arm"] is not None and seen["joins"]


def test_another_tool_that_asks_for_its_outcome_is_told_it_was_dropped():
    mcp_shared._arm_for_dispatch(1, "plain")
    assert mcp_shared._response_outcome_arms == {}
    # It can never commit: the dispatcher answers for no call it did not arm.
    assert mcp_shared.on_response_outcome(lambda d: None) is False


def test_a_non_string_tool_name_arms_nothing():
    mcp_shared._arm_for_dispatch(1, ["spawn_sub_agents"])  # unhashable: never armed
    assert mcp_shared._response_outcome_arms == {}


def test_another_tools_call_clears_the_threads_arm_from_an_earlier_call():
    mcp_shared._arm_for_dispatch(1, "spawn_sub_agents")
    mcp_shared._settle_response_outcome(1, True)
    # The same thread then runs a hookless call: the earlier "written" outcome
    # is not its outcome.
    mcp_shared._arm_for_dispatch(2, "plain")
    assert mcp_shared.on_response_outcome(lambda d: None) is False


def test_the_exit_join_is_registered_once_by_the_first_armed_call(monkeypatch):
    joins: list = []
    monkeypatch.setattr(mcp_shared, "_register_exit_join", lambda *a: joins.append(a))
    for rid in (1, 2):
        mcp_shared._arm_for_dispatch(rid, "spawn_sub_agents")
        assert str(rid) in mcp_shared._response_outcome_arms
        mcp_shared._settle_response_outcome(rid, False)
    assert joins == [(mcp_shared._join_outcome_hooks, mcp_shared.OUTCOME_HOOK_EXIT_WAIT_SECS)]


def test_an_exit_join_with_no_running_report_waits_on_nothing():
    assert not mcp_shared._outcome_hook_threads
    assert mcp_shared._join_outcome_hooks(mcp_shared.OUTCOME_HOOK_EXIT_WAIT_SECS) == 0


def test_spawn_sub_agents_is_the_one_tool_armed_for_outcomes():
    mcp_shared._arm_for_dispatch(1, "spawn_sub_agents")
    assert set(mcp_shared._response_outcome_arms) == {"1"}
    for rid, other in ((2, "spawn_run"), (3, "Spawn_Sub_Agents"), (4, "")):
        mcp_shared._arm_for_dispatch(rid, other)
    assert set(mcp_shared._response_outcome_arms) == {"1"}


def test_calls_armed_at_once_each_hear_only_their_own_outcome():
    """Arms are per request: arming a second call leaves the first pending, and
    each settle fires its own hook with its own outcome."""
    import threading

    heard: dict = {}
    done = {1: threading.Event(), 2: threading.Event()}

    def _call(rid: int) -> None:
        mcp_shared._arm_response_outcome(rid)
        hook = lambda d, rid=rid: (heard.__setitem__(rid, d), done[rid].set())  # noqa: E731
        assert mcp_shared.on_response_outcome(hook) is None

    for rid in (1, 2):
        worker = threading.Thread(target=_call, args=(rid,))
        worker.start()
        worker.join(timeout=10)
    assert heard == {}  # arming the second did not settle the first
    mcp_shared._settle_response_outcome(2, False)
    mcp_shared._settle_response_outcome(1, True)
    assert done[1].wait(timeout=10) and done[2].wait(timeout=10)
    assert heard == {1: True, 2: False}
    assert mcp_shared._response_outcome_arms == {}


def test_the_stdio_loop_drops_every_unanswered_arm_when_it_ends(monkeypatch):
    """However a call came to be left armed, the loop's exit settles it as
    dropped, so no hook waits on an answer nothing will send."""
    import threading

    heard: list = []
    done = threading.Event()
    mcp_shared._arm_response_outcome("orphan")
    assert mcp_shared.on_response_outcome(lambda d: (heard.append(d), done.set())) is None
    harness = _LoopHarness(monkeypatch, lambda name, args: "ok")
    harness.close()  # EOF: the loop returns
    assert done.wait(timeout=10)
    assert heard == [False]
    assert mcp_shared._response_outcome_arms == {}


def test_an_arm_the_loop_never_answered_is_dropped_when_the_loop_ends():
    import threading

    heard: list = []
    done = threading.Event()
    mcp_shared._arm_response_outcome(1)
    mcp_shared.on_response_outcome(lambda d: (heard.append(d), done.set()))
    mcp_shared._drop_unsettled_arms()
    assert done.wait(timeout=10)
    assert heard == [False]
    assert mcp_shared._response_outcome_arms == {}
    mcp_shared._settle_response_outcome(1, True)  # too late: nothing changes
    assert heard == [False]


def test_another_requests_settle_never_fires_this_requests_hook():
    import threading

    fired = threading.Event()
    mcp_shared._arm_response_outcome(7)
    try:
        assert mcp_shared.on_response_outcome(lambda d: fired.set()) is None
        mcp_shared._settle_response_outcome(8, True)
        arm = mcp_shared._response_outcome_arms["7"]
        assert (arm.req, arm.outcome, arm.hook is not None) == ("7", None, True)
    finally:
        mcp_shared._settle_response_outcome(7, False)
    assert fired.wait(timeout=10)


def test_respond_reports_a_torn_frame_as_not_written(monkeypatch):
    class _Torn(BrokenPipeError):
        bytes_written = 3

    def _write_all(fd, frame):
        raise _Torn()

    monkeypatch.setattr(mcp_shared, "_stdout_fd", 99)
    monkeypatch.setattr(mcp_shared, "_write_all", _write_all)
    assert mcp_shared.respond(1, {"ok": True}) is False
    monkeypatch.setattr(mcp_shared, "_write_all", lambda fd, frame: None)
    assert mcp_shared.respond(1, {"ok": True}) is True
    assert mcp_shared.respond(None, {"ok": True}) is False

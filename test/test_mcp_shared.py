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
        msg = {"jsonrpc": "2.0", "method": "tools/call", "id": 1, "params": {"name": "tëst_émoji_🎉"}}
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

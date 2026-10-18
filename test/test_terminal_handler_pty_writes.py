"""Tests for writes to the PTY.

Covers ``_write_all``'s short-write loop and the input-ownership lock that
serializes frames across reconnects and takeovers.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from terminal_handler_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _clear_enabled_cache,
    _make_session,
)

from kiro_crew.dashboard.handlers import terminal
from kiro_crew.testing.wait import await_until


class TestWriteAll:
    """The POSIX PTY write path must deliver every byte despite short writes.

    ``os.write`` on a blocking PTY controller fd can accept fewer bytes than requested
    when the tty input buffer is full. ``terminal._write_all`` loops over the
    remaining bytes until the buffer is consumed; the previous single-write shape
    (one ``os.write`` whose return was discarded) truncated the tail. The loop
    writes through a private ``os.dup`` so a concurrent teardown closing (and the
    kernel reusing) the original descriptor number cannot redirect the tail.
    """

    DUP_OFFSET = 1000

    def _stub_dup(self, monkeypatch):
        """Stub ``os.dup``/``os.close`` inside the handler module: dup returns
        fd+DUP_OFFSET and both calls are recorded, so tests can assert writes
        target the private dup and that it is always closed."""
        dups: list[int] = []
        closes: list[int] = []
        monkeypatch.setattr(terminal.os, "dup", lambda fd: dups.append(fd) or fd + self.DUP_OFFSET)
        monkeypatch.setattr(terminal.os, "close", lambda fd: closes.append(fd))
        return dups, closes

    def _short_write_stub(self, chunk):
        """A fake ``os.write`` that accepts at most ``chunk`` bytes per call and
        records everything it received (and the fd each write targeted).
        Returns the short count, mirroring a backpressured PTY."""
        received = bytearray()
        fds: list[int] = []

        def _write(fd, data):
            fds.append(fd)
            n = min(len(data), chunk)
            received.extend(bytes(data[:n]))
            return n

        return _write, received, fds

    def test_write_all_delivers_full_payload_despite_short_writes(self, monkeypatch):
        payload = bytes(range(256)) * 40  # 10 KB, larger than the 64-byte chunk
        fake_write, received, _fds = self._short_write_stub(64)
        monkeypatch.setattr(terminal.os, "write", fake_write)
        self._stub_dup(monkeypatch)

        terminal._write_all(7, payload)

        assert bytes(received) == payload

    def test_old_single_write_shape_truncates(self, monkeypatch):
        """Red proof: the pattern this fix replaces — a single ``os.write`` whose
        return is discarded — loses every byte past the first short count."""
        payload = bytes(range(256)) * 40
        fake_write, received, _fds = self._short_write_stub(64)
        monkeypatch.setattr(terminal.os, "write", fake_write)

        # Exactly the old code shape: one write, return value ignored.
        terminal.os.write(7, payload)

        assert bytes(received) == payload[:64]
        assert bytes(received) != payload  # truncated — the bug _write_all fixes

    def test_write_all_targets_private_dup_and_always_closes_it(self, monkeypatch):
        """The loop must never write to the raw session fd: a concurrent kill can
        close it and the kernel can hand that NUMBER to an unrelated open(), so
        every chunk targets the private dup, which is closed even on error."""
        payload = bytes(range(256)) * 40
        fake_write, _received, fds = self._short_write_stub(64)
        monkeypatch.setattr(terminal.os, "write", fake_write)
        dups, closes = self._stub_dup(monkeypatch)

        terminal._write_all(7, payload)

        assert dups == [7]
        assert set(fds) == {7 + self.DUP_OFFSET}  # every chunk hit the dup
        assert closes == [7 + self.DUP_OFFSET]  # and the dup was released

    def test_write_all_closes_dup_when_write_raises(self, monkeypatch):
        def _write(fd, data):
            raise OSError(5, "Input/output error")

        monkeypatch.setattr(terminal.os, "write", _write)
        dups, closes = self._stub_dup(monkeypatch)

        with pytest.raises(OSError):
            terminal._write_all(7, b"payload")
        assert closes == [7 + self.DUP_OFFSET]

    def test_write_all_full_write_single_call(self, monkeypatch):
        """A backend that accepts everything at once needs exactly one write."""
        payload = b"echo hello\n"
        calls = []

        def _write(fd, data):
            calls.append(bytes(data))
            return len(data)

        monkeypatch.setattr(terminal.os, "write", _write)
        self._stub_dup(monkeypatch)
        terminal._write_all(7, payload)

        assert calls == [payload]

    def test_write_all_propagates_oserror(self, monkeypatch):
        """A concurrent kill sets the session fd to -1 before close; ``os.dup``
        on the dead fd raises and the ``OSError`` must propagate so the write
        loop's ``except OSError: break`` still terminates the session cleanly."""

        def _dup(fd):
            raise OSError(9, "Bad file descriptor")

        monkeypatch.setattr(terminal.os, "dup", _dup)
        with pytest.raises(OSError):
            terminal._write_all(-1, b"data after kill")

    def test_write_all_empty_payload_is_noop(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            terminal.os, "write", lambda fd, data: calls.append(bytes(data)) or len(data)
        )
        terminal._write_all(7, b"")
        assert calls == []


class TestWriteSerialization:
    """Input ownership and frame serialization share one ordering point.

    A reconnect does not wait for the displaced handler's socket loop to exit.
    The lock makes ownership replacement atomic with input and resize while
    keeping each accepted frame contiguous on the PTY.
    """

    @pytest.mark.asyncio
    async def test_concurrent_frames_stay_contiguous_under_write_lock(self, monkeypatch):
        import threading

        recorded: list[tuple[bytes, ...]] = []
        chunks: list[bytes] = []
        first_chunk_written = threading.Event()
        peer_wrote = threading.Event()

        def short_write(fd, data):
            chunk = bytes(data[:4])
            chunks.append(chunk)
            if not first_chunk_written.is_set():
                first_chunk_written.set()
                # Give a concurrent (unserialized) writer every opportunity to
                # slip a chunk in mid-frame. Under the lock this always times
                # out because the peer cannot start until we finish.
                # flake-ok: pre-existing 0.3 s timed wait that always expires under the lock and gives an unserialized peer a window to interleave, moved verbatim; tracked for a de-flake pass
                peer_wrote.wait(timeout=0.3)
            elif chunks and chunks[-1][:1] != chunks[0][:1]:
                peer_wrote.set()
            return len(chunk)

        monkeypatch.setattr(terminal.os, "write", short_write)
        monkeypatch.setattr(terminal.os, "dup", lambda fd: fd)
        monkeypatch.setattr(terminal.os, "close", lambda fd: None)

        lock = asyncio.Lock()
        loop = asyncio.get_running_loop()

        async def frame(payload: bytes):
            async with lock:
                # flake-ok: pre-existing unbounded await of the executor write that returns once the stubbed short writes drain the frame, moved verbatim; tracked for a de-flake pass
                await loop.run_in_executor(None, terminal._write_all, 7, payload)

        # flake-ok: pre-existing unbounded gather of the two locked frame writers that finishes once both executor writes return, moved verbatim; tracked for a de-flake pass
        await asyncio.gather(frame(b"A" * 12), frame(b"B" * 12))
        recorded.append(tuple(chunks))

        stream = b"".join(chunks)
        # Each frame's 12 bytes must be contiguous: the stream is one frame
        # then the other, never A-chunks interleaved with B-chunks.
        assert stream in (b"A" * 12 + b"B" * 12, b"B" * 12 + b"A" * 12), recorded

    def test_session_dataclass_has_write_lock(self):
        import dataclasses

        names = {f.name for f in dataclasses.fields(terminal._TerminalSession)}
        assert "write_lock" in names
        assert "replace_lock" in names
        assert "output_lock" in names
        assert "output_send_cancel" in names

    @pytest.mark.asyncio
    async def test_displaced_socket_cannot_write_or_resize(self):
        old_ws = MagicMock()
        new_ws = MagicMock()
        new_ws.send_str = AsyncMock()
        sess = _make_session(ws=old_ws)
        winpty = MagicMock()
        sess.winpty = winpty

        await terminal._replace_terminal_ws(sess, new_ws)
        wrote = await terminal._write_terminal_input(sess, old_ws, b"echo stale\r")
        resized = await terminal._resize_terminal(sess, old_ws, 160, 50)

        assert wrote is False
        assert resized is False
        winpty.write.assert_not_called()
        winpty.resize.assert_not_called()
        assert (sess.cols, sess.rows) == (80, 24)

    @pytest.mark.asyncio
    async def test_failed_replay_keeps_previous_owner(self):
        old_ws = MagicMock()
        new_ws = MagicMock()
        new_ws.send_bytes = AsyncMock(side_effect=ConnectionResetError)
        new_ws.send_str = AsyncMock()
        sess = _make_session(ws=old_ws, disconnect=123.0)
        sess.scrollback.extend(b"before")
        sess.output_bytes = len(sess.scrollback)
        sess.last_title = "old title"
        sess.last_cwd = "/old/cwd"
        sess.frames_dirty = False

        replaced = await terminal._replace_terminal_ws(sess, new_ws)

        assert replaced is False
        assert sess.ws is old_ws
        assert sess.last_ws_disconnect == 123.0
        assert sess.last_title == "old title"
        assert sess.last_cwd == "/old/cwd"
        assert sess.frames_dirty is False

    @pytest.mark.asyncio
    async def test_failed_ready_send_keeps_previous_owner(self):
        old_ws = MagicMock()
        new_ws = MagicMock()
        new_ws.send_bytes = AsyncMock()
        new_ws.send_str = AsyncMock(side_effect=ConnectionResetError)
        sess = _make_session(ws=old_ws, disconnect=123.0)
        sess.shell_ready = True
        sess.last_title = "old title"
        sess.last_cwd = "/old/cwd"
        sess.frames_dirty = False

        replaced = await terminal._replace_terminal_ws(sess, new_ws)

        assert replaced is False
        assert sess.ws is old_ws
        assert sess.last_ws_disconnect == 123.0
        assert sess.last_title == "old title"
        assert sess.last_cwd == "/old/cwd"
        assert sess.frames_dirty is False

    @pytest.mark.asyncio
    async def test_replacement_catches_output_produced_during_replay(self):
        old_ws = MagicMock()
        old_ws.closed = False
        old_ws.send_str = AsyncMock()
        old_ws.close = AsyncMock()
        old_ws.send_bytes = AsyncMock()
        new_ws = MagicMock()
        replay_started = asyncio.Event()
        allow_replay = asyncio.Event()

        async def send_bytes(data):
            if data == b"before":
                replay_started.set()
                # flake-ok: pre-existing unbounded Event wait that parks the fake replay send until the test records output mid-replay, moved verbatim; tracked for a de-flake pass
                await allow_replay.wait()

        new_ws.send_bytes = AsyncMock(side_effect=send_bytes)
        new_ws.send_str = AsyncMock()
        sess = _make_session(ws=old_ws)
        sess.scrollback.extend(b"before")
        sess.output_bytes = len(sess.scrollback)

        replacement = asyncio.create_task(terminal._replace_terminal_ws(sess, new_ws))
        # flake-ok: pre-existing unbounded wait for the fake replay send to start before output is recorded mid-replay, moved verbatim; tracked for a de-flake pass
        await replay_started.wait()
        await terminal._record_and_forward_terminal_output(sess, b"during")
        allow_replay.set()

        # flake-ok: pre-existing unbounded await of the bare takeover task once the held replay send is released, moved verbatim; tracked for a de-flake pass
        assert await replacement is True
        assert sess.ws is new_ws
        assert [call.args[0] for call in new_ws.send_bytes.await_args_list] == [
            b"before",
            b"during",
        ]
        old_ws.send_bytes.assert_awaited_once_with(b"during")

    @pytest.mark.asyncio
    async def test_takeover_cancels_displaced_send_and_releases_reader(self):
        old_ws = MagicMock()
        old_ws.closed = False
        old_ws.send_str = AsyncMock()
        old_ws.close = AsyncMock()
        new_ws = MagicMock()
        new_ws.closed = False
        new_ws.send_bytes = AsyncMock()
        new_ws.send_str = AsyncMock()
        sess = _make_session(ws=old_ws)
        send_started = asyncio.Event()

        async def blocked_send(_data):
            send_started.set()
            # flake-ok: pre-existing never-set Event that parks the displaced socket's send until the takeover cancels it, moved verbatim; tracked for a de-flake pass
            await asyncio.Event().wait()

        old_ws.send_bytes = AsyncMock(side_effect=blocked_send)
        output = asyncio.create_task(terminal._record_and_forward_terminal_output(sess, b"during"))
        # flake-ok: pre-existing unbounded wait for the displaced send to start before the takeover runs, moved verbatim; tracked for a de-flake pass
        await send_started.wait()

        assert await terminal._replace_terminal_ws(sess, new_ws) is True
        await await_until(output, what="the displaced send being cancelled")
        await await_until(
            asyncio.ensure_future(terminal._record_and_forward_terminal_output(sess, b"after")),
            what="the forward to the new owner",
        )

        assert sess.ws is new_ws
        assert [call.args[0] for call in new_ws.send_bytes.await_args_list] == [
            b"during",
            b"after",
        ]
        old_ws.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_displaced_socket_close_is_bounded_and_outside_output_lock(self, monkeypatch):
        old_ws = MagicMock()
        old_ws.closed = False
        old_ws.send_str = AsyncMock()
        new_ws = MagicMock()
        new_ws.send_str = AsyncMock()
        sess = _make_session(ws=old_ws)

        async def blocked_close():
            assert not sess.output_lock.locked()
            # flake-ok: pre-existing never-set Event that wedges the displaced socket's close until the cleanup bound cancels it, moved verbatim; tracked for a de-flake pass
            await asyncio.Event().wait()

        old_ws.close = AsyncMock(side_effect=blocked_close)
        # flake-ok: pre-existing 10 ms cleanup bound patch so the wedged close times out at once and the takeover completes, moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(terminal, "_TERMINAL_WS_CLEANUP_TIMEOUT_S", 0.01)

        assert await await_until(
            asyncio.ensure_future(terminal._replace_terminal_ws(sess, new_ws)),
            what="the takeover past the bounded close",
        )
        assert sess.ws is new_ws
        old_ws.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failed_reconnect_error_send_cannot_block_close(self, monkeypatch):
        ws = MagicMock()
        ws.closed = False
        send_started = asyncio.Event()

        async def blocked_send(_data):
            send_started.set()
            # flake-ok: pre-existing never-set Event that wedges the reconnect error send until the cleanup bound cancels it, moved verbatim; tracked for a de-flake pass
            await asyncio.Event().wait()

        ws.send_str = AsyncMock(side_effect=blocked_send)
        ws.close = AsyncMock()
        # flake-ok: pre-existing 10 ms cleanup bound patch so the wedged error send times out at once and close still runs, moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(terminal, "_TERMINAL_WS_CLEANUP_TIMEOUT_S", 0.01)

        cleanup = asyncio.create_task(
            terminal._close_terminal_ws_bounded(
                ws,
                error_message="Terminal reconnect failed",
            )
        )
        # flake-ok: pre-existing unbounded wait for the wedged error send to start before the bounded cleanup is awaited, moved verbatim; tracked for a de-flake pass
        await send_started.wait()
        await await_until(cleanup, what="the bounded cleanup finishing")

        ws.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stalled_replay_releases_lock_for_later_candidate(self, monkeypatch):
        old_ws = MagicMock()
        stalled_ws = MagicMock()
        healthy_ws = MagicMock()
        replay_started = asyncio.Event()

        async def blocked_replay(_data):
            replay_started.set()
            # flake-ok: pre-existing never-set Event that stalls the first candidate's replay send until the replay bound cancels it, moved verbatim; tracked for a de-flake pass
            await asyncio.Event().wait()

        stalled_ws.send_bytes = AsyncMock(side_effect=blocked_replay)
        healthy_ws.send_bytes = AsyncMock()
        healthy_ws.send_str = AsyncMock()
        sess = _make_session(ws=old_ws)
        sess.scrollback.extend(b"before")
        sess.output_bytes = len(sess.scrollback)
        # flake-ok: pre-existing 10 ms replay send bound patch so the stalled candidate gives up at once and frees the lock, moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(terminal, "_TAKEOVER_REPLAY_SEND_TIMEOUT_S", 0.01)

        stalled = asyncio.create_task(terminal._replace_terminal_ws(sess, stalled_ws))
        # flake-ok: pre-existing unbounded wait for the stalled replay send to start before the healthy candidate is created, moved verbatim; tracked for a de-flake pass
        await replay_started.wait()
        healthy = asyncio.create_task(terminal._replace_terminal_ws(sess, healthy_ws))

        # flake-ok: pre-existing unbounded await of the bare stalled takeover task that returns once the replay bound expires, moved verbatim; tracked for a de-flake pass
        assert await stalled is False
        assert await await_until(healthy, what="the healthy candidate's takeover") is True
        assert sess.ws is healthy_ws
        healthy_ws.send_bytes.assert_awaited_once_with(b"before")

    @pytest.mark.asyncio
    async def test_current_socket_can_write_and_resize(self):
        ws = MagicMock()
        sess = _make_session(ws=ws)
        winpty = MagicMock()
        sess.winpty = winpty
        # flake-ok: pre-existing real-clock stamp that seeds a fresh cwd probe the input write must clear, moved verbatim; tracked for a de-flake pass
        sess.cwd_probe = (time.monotonic(), "/tmp/old")

        wrote = await terminal._write_terminal_input(sess, ws, b"cd /tmp\r")
        resized = await terminal._resize_terminal(sess, ws, 160, 50)

        assert wrote is True
        assert resized is True
        winpty.write.assert_called_once_with(b"cd /tmp\r")
        winpty.resize.assert_called_once_with(160, 50)
        assert (sess.cols, sess.rows) == (160, 50)
        assert sess.cwd_probe is None
        assert sess.frames_dirty is True

    @pytest.mark.asyncio
    async def test_inflight_input_does_not_block_replacement(self):
        old_ws = MagicMock()
        new_ws = MagicMock()
        old_ws.closed = False
        old_ws.send_str = AsyncMock()
        old_ws.close = AsyncMock()
        new_ws.closed = False
        old_ws.send_bytes = AsyncMock()
        new_ws.send_bytes = AsyncMock()
        new_ws.send_str = AsyncMock()
        winpty = MagicMock()
        write_started = threading.Event()
        allow_write_to_finish = threading.Event()
        calls = []

        def blocked_write(data):
            calls.append(data)
            write_started.set()
            assert allow_write_to_finish.wait(timeout=5)
            return len(data)

        winpty.write.side_effect = blocked_write
        sess = _make_session(ws=old_ws)
        sess.winpty = winpty
        sess.scrollback.extend(b"before-")
        sess.output_bytes = len(sess.scrollback)

        write_task = asyncio.create_task(terminal._write_terminal_input(sess, old_ws, b"first"))
        assert await asyncio.to_thread(write_started.wait, 5)
        await terminal._record_and_forward_terminal_output(sess, b"during")
        old_ws.send_bytes.assert_awaited_once_with(b"during")
        replace_task = asyncio.create_task(terminal._replace_terminal_ws(sess, new_ws))
        assert await await_until(replace_task, what="the takeover") is True
        assert sess.ws is new_ws
        new_ws.send_bytes.assert_awaited_once_with(b"before-during")

        stale_task = asyncio.create_task(terminal._write_terminal_input(sess, old_ws, b"second"))
        # flake-ok: pre-existing zero-second yield that lets the stale write reach the write lock before the test asserts it is pending, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)
        assert stale_task.done() is False
        allow_write_to_finish.set()
        # flake-ok: pre-existing unbounded await of the bare in-flight write task once the blocked thread write is released, moved verbatim; tracked for a de-flake pass
        assert await write_task is True
        # flake-ok: pre-existing unbounded await of the bare stale write task that returns once it acquires the released write lock, moved verbatim; tracked for a de-flake pass
        stale = await stale_task

        assert stale is False
        assert calls == [b"first"]
        assert sess.ws is new_ws

    @pytest.mark.asyncio
    async def test_cancelled_takeover_after_publication_releases_identity(self):
        """A candidate cancelled while closing the socket it displaced never
        reaches the write loop's teardown, so publication must detach it here
        or the orphan reaper would keep the PTY alive indefinitely."""
        old_ws = MagicMock()
        old_ws.closed = False
        old_ws.send_str = AsyncMock()
        close_started = asyncio.Event()

        async def blocked_close():
            close_started.set()
            # flake-ok: pre-existing never-set Event that wedges the displaced socket's close until the test cancels the takeover, moved verbatim; tracked for a de-flake pass
            await asyncio.Event().wait()

        old_ws.close = AsyncMock(side_effect=blocked_close)
        new_ws = MagicMock()
        new_ws.closed = False
        new_ws.send_bytes = AsyncMock()
        new_ws.send_str = AsyncMock()
        sess = _make_session(ws=old_ws)

        replace_task = asyncio.create_task(terminal._replace_terminal_ws(sess, new_ws))
        await await_until(close_started, what="the displaced socket's close starting")
        assert sess.ws is new_ws
        assert sess.last_ws_disconnect is None

        replace_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            # flake-ok: pre-existing unbounded await of the bare cancelled takeover task that raises once the cancel unwinds the wedged close, moved verbatim; tracked for a de-flake pass
            await replace_task

        assert sess.ws is None
        assert sess.last_ws_disconnect is not None

    @pytest.mark.asyncio
    async def test_cancelled_takeover_after_publication_keeps_a_newer_owner(self):
        old_ws = MagicMock()
        old_ws.closed = False
        old_ws.send_str = AsyncMock()
        close_started = asyncio.Event()

        async def blocked_close():
            close_started.set()
            # flake-ok: pre-existing never-set Event that wedges the displaced socket's close until the test cancels the takeover, moved verbatim; tracked for a de-flake pass
            await asyncio.Event().wait()

        old_ws.close = AsyncMock(side_effect=blocked_close)
        mid_ws = MagicMock()
        mid_ws.closed = False
        mid_ws.send_bytes = AsyncMock()
        mid_ws.send_str = AsyncMock()
        newest_ws = MagicMock()
        newest_ws.closed = False
        sess = _make_session(ws=old_ws)

        replace_task = asyncio.create_task(terminal._replace_terminal_ws(sess, mid_ws))
        await await_until(close_started, what="the displaced socket's close starting")
        assert sess.ws is mid_ws
        sess.ws = newest_ws  # a later publication landed meanwhile

        replace_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            # flake-ok: pre-existing unbounded await of the bare cancelled takeover task that raises once the cancel unwinds the wedged close, moved verbatim; tracked for a de-flake pass
            await replace_task

        assert sess.ws is newest_ws
        assert sess.last_ws_disconnect is None

    @pytest.mark.asyncio
    async def test_reconnect_with_no_owner_converges_against_continuous_output(self):
        """A reload with ``sess.ws is None`` against a firehose: output advances
        after every unlocked catch-up send, so no unlocked round can settle. The
        final round sends under ``output_lock`` and must still publish, with
        every byte delivered in order and none duplicated."""
        new_ws = MagicMock()
        new_ws.closed = False
        new_ws.send_str = AsyncMock()
        sess = _make_session(ws=None, disconnect=5.0)
        sess.scrollback.extend(b"seed")
        sess.output_bytes = 4
        delivered = bytearray()
        chunk = 0

        async def send_and_stream(data):
            nonlocal chunk
            delivered.extend(data)
            # PTY output keeps landing while the socket is unlocked; it cannot
            # land while output_lock is held (the reader records under it).
            if not sess.output_lock.locked():
                chunk += 1
                more = f"[{chunk}]".encode()
                sess.scrollback.extend(more)
                sess.output_bytes += len(more)

        new_ws.send_bytes = AsyncMock(side_effect=send_and_stream)

        assert (
            await await_until(
                asyncio.ensure_future(terminal._replace_terminal_ws(sess, new_ws)),
                what="the takeover",
            )
            is True
        )

        assert sess.ws is new_ws
        assert sess.last_ws_disconnect is None
        assert bytes(delivered) == bytes(sess.scrollback)
        assert len(delivered) == sess.output_bytes
        assert chunk >= terminal._TAKEOVER_CATCH_UP_ROUNDS - 1

    @pytest.mark.asyncio
    async def test_reconnect_with_no_owner_publishes_after_ring_overrun(self):
        """If the stream outran the bounded ring while nobody was attached,
        the owner's only window still gets what the ring holds and ownership."""
        new_ws = MagicMock()
        new_ws.closed = False
        new_ws.send_str = AsyncMock()
        sess = _make_session(ws=None, disconnect=5.0)
        sess.scrollback.extend(b"tail")
        sess.output_bytes = 4

        async def overrun_then_record(_data):
            if not sess.output_lock.locked():
                sess.output_bytes += terminal._SCROLLBACK_MAX * 2  # ring lost bytes
                del sess.scrollback[:]
                sess.scrollback.extend(b"latest")

        new_ws.send_bytes = AsyncMock(side_effect=overrun_then_record)

        assert (
            await await_until(
                asyncio.ensure_future(terminal._replace_terminal_ws(sess, new_ws)),
                what="the takeover",
            )
            is True
        )
        assert sess.ws is new_ws
        assert new_ws.send_bytes.await_args_list[-1].args[0] == b"latest"

    @pytest.mark.asyncio
    async def test_contended_takeover_refuses_on_ring_overrun(self):
        """With a live owner attached, a gap is worse than a refused candidate."""
        old_ws = MagicMock()
        old_ws.closed = False
        old_ws.send_str = AsyncMock()
        old_ws.close = AsyncMock()
        new_ws = MagicMock()
        new_ws.closed = False
        new_ws.send_str = AsyncMock()
        sess = _make_session(ws=old_ws)
        sess.scrollback.extend(b"tail")
        sess.output_bytes = 4

        async def overrun(_data):
            if not sess.output_lock.locked():
                sess.output_bytes += terminal._SCROLLBACK_MAX * 2

        new_ws.send_bytes = AsyncMock(side_effect=overrun)

        assert (
            await await_until(
                asyncio.ensure_future(terminal._replace_terminal_ws(sess, new_ws)),
                what="the refused takeover",
            )
            is False
        )
        assert sess.ws is old_ws
        old_ws.close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_publication_notifies_displaced_socket_once_before_close(self):
        """The displaced window learns why its socket ended: exactly one coarse
        ``error`` frame, then close; the new owner receives no error frame."""
        events: list[str] = []
        old_ws = MagicMock()
        old_ws.closed = False

        async def record_send(data):
            events.append("send:" + data)

        async def record_close():
            events.append("close")

        old_ws.send_str = AsyncMock(side_effect=record_send)
        old_ws.close = AsyncMock(side_effect=record_close)
        new_ws = MagicMock()
        new_ws.closed = False
        new_ws.send_bytes = AsyncMock()
        new_ws.send_str = AsyncMock()
        sess = _make_session(ws=old_ws)

        assert await terminal._replace_terminal_ws(sess, new_ws) is True

        assert events == [
            "send:"
            + json.dumps(
                {
                    "type": "error",
                    "message": terminal._STALE_OWNER_ERROR_MESSAGE,
                    "code": terminal._STALE_OWNER_ERROR_CODE,
                }
            ),
            "close",
        ]
        assert sess.session_id not in terminal._STALE_OWNER_ERROR_MESSAGE
        new_frames = [json.loads(call.args[0]) for call in new_ws.send_str.await_args_list]
        assert "error" not in {f.get("type") for f in new_frames}

    @pytest.mark.asyncio
    async def test_owner_control_frame_skips_displaced_socket(self):
        old_ws = MagicMock()
        old_ws.closed = False
        old_ws.send_str = AsyncMock()
        new_ws = MagicMock()
        new_ws.closed = False
        new_ws.send_str = AsyncMock()
        sess = _make_session(ws=new_ws)

        sent = await terminal._send_owner_control_frame(
            sess, old_ws, {"type": "title", "text": "vim"}
        )

        assert sent is False
        old_ws.send_str.assert_not_awaited()
        new_ws.send_str.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_owner_control_frame_rechecks_identity_under_lock(self):
        """Ownership can move while the caller waits for the transport lock;
        the frame is then dropped rather than delivered to the displaced socket."""
        old_ws = MagicMock()
        old_ws.closed = False
        old_ws.send_str = AsyncMock()
        new_ws = MagicMock()
        new_ws.closed = False
        sess = _make_session(ws=old_ws)
        old_lock = sess.send_lock
        await old_lock.acquire()

        send_task = asyncio.create_task(
            terminal._send_owner_control_frame(sess, old_ws, {"type": "pong"})
        )
        # flake-ok: pre-existing zero-second yield that lets the control send block on the held transport lock before the pending assert, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)
        assert send_task.done() is False
        sess.ws = new_ws  # takeover publishes and rotates the lock
        sess.send_lock = asyncio.Lock()
        old_lock.release()

        assert await await_until(send_task, what="the owner control send") is False
        old_ws.send_str.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_owner_control_frame_is_bounded_by_a_held_lock(self, monkeypatch):
        # flake-ok: pre-existing 50 ms control send bound patch so the send gives up on the never-released transport lock, moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(terminal, "_OWNER_CONTROL_SEND_TIMEOUT_S", 0.05)
        ws = MagicMock()
        ws.closed = False
        ws.send_str = AsyncMock()
        sess = _make_session(ws=ws)
        await sess.send_lock.acquire()  # a wedged send never releases it

        sent = await await_until(
            asyncio.ensure_future(terminal._send_owner_control_frame(sess, ws, {"type": "pong"})),
            what="the bounded owner control send",
        )

        assert sent is False
        ws.send_str.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_owner_control_frame_is_bounded_by_a_blocked_send(self, monkeypatch):
        # flake-ok: pre-existing 50 ms control send bound patch so the wedged send times out and releases the transport lock, moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(terminal, "_OWNER_CONTROL_SEND_TIMEOUT_S", 0.05)
        ws = MagicMock()
        ws.closed = False

        async def blocked_send(_data):
            # flake-ok: pre-existing never-set Event that wedges the control frame send until the send bound cancels it, moved verbatim; tracked for a de-flake pass
            await asyncio.Event().wait()

        ws.send_str = AsyncMock(side_effect=blocked_send)
        sess = _make_session(ws=ws)

        sent = await await_until(
            asyncio.ensure_future(terminal._send_owner_control_frame(sess, ws, {"type": "pong"})),
            what="the bounded owner control send",
        )

        assert sent is False
        assert sess.send_lock.locked() is False

    @pytest.mark.asyncio
    async def test_owner_control_frame_delivers_to_current_owner(self):
        ws = MagicMock()
        ws.closed = False
        ws.send_str = AsyncMock()
        sess = _make_session(ws=ws)

        assert await terminal._send_owner_control_frame(sess, ws, {"type": "pong"}) is True
        assert json.loads(ws.send_str.await_args.args[0]) == {"type": "pong"}

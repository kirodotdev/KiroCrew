"""Why the Strands gate read-back saw no marker, worded from what the probe printed.

The read-back (``strands._verify_strands_gate``) drains the probe's stdout and stderr
into bounded buffers (:class:`OutputCapture`). When the gate never loads, or the
read-back fails once the probe runs, this module turns them into the refusal's
detail: whether the CLI exited (and its code) or ran out the wait, the error the CLI
answered ``session/new`` with (the Strands CLI reports a project worker's exception
there, on stdout, and keeps running), and the last stderr lines. Both quotes are
untrusted child output that can echo a credential, so each passes the stderr-tail
check (``acp/stderr_tail.py``), which withholds a flagged one whole.
"""

from __future__ import annotations

import json
import os
import selectors
import sys
import threading
from collections.abc import Callable
from contextlib import ExitStack, suppress
from typing import IO, Any

from kiro_crew import agent_sdk, platform_compat
from kiro_crew.acp.stderr_tail import safe_stderr_tail

#: The most of either stream a capture keeps: the head of stdout, where the answer
#: to the second request is, and the tail of stderr, where a traceback ends.
READBACK_OUTPUT_MAX_BYTES = 64 * 1024
_READ_CHUNK_BYTES = 16 * 1024
#: How often a waiting reader checks it was stopped, and how long finish() waits
#: for a stopped reader to end.
_STOP_POLL_S = 0.1
_STOP_JOIN_S = 2.0

#: The stderr lines the refusal quotes, and the most characters of the error it quotes.
READBACK_STDERR_LINES = 5
READBACK_ERROR_MAX_CHARS = 1000


class OutputCapture:
    """Drains one of the probe's pipes on a thread, keeping a bounded head or tail.

    The pipe is read to EOF whatever is kept, so the child never blocks on a full
    pipe, and at most :data:`READBACK_OUTPUT_MAX_BYTES` is held.
    """

    def __init__(self, stream: IO[bytes] | None, *, keep_tail: bool) -> None:
        self._stream = stream
        self._keep_tail = keep_tail
        self._data = bytearray()
        self._cut = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if stream is not None:
            self._thread = threading.Thread(
                target=self._drain, name="strands-readback-drain", daemon=True
            )
            self._thread.start()

    def _drain(self) -> None:
        assert self._stream is not None
        try:
            with suppress(OSError, ValueError), ExitStack() as stack:
                fd = self._fileno()
                ready = None if fd is None else self._waiter(fd, stack)
                if fd is None or ready is None:
                    read = getattr(self._stream, "read1", self._stream.read)
                    while chunk := read(_READ_CHUNK_BYTES):
                        self._store(chunk)
                else:
                    self._drain_until_stopped(fd, ready)
        finally:
            # Every way the reader ends releases the pipe.
            with suppress(OSError, ValueError):
                self._stream.close()

    def _fileno(self) -> int | None:
        try:
            return self._stream.fileno() if self._stream is not None else None
        except (OSError, ValueError):
            return None

    def _waiter(self, fd: int, stack: ExitStack) -> Callable[[float], bool] | None:
        """A bounded wait for *fd* to be readable, or ``None`` when it cannot have one.

        A select on POSIX; on Windows, where a pipe read cannot be interrupted, the
        pipe is peeked and the wait is on the stop event, so no read is left pending.
        """
        if platform_compat.IS_POSIX:
            selector = stack.enter_context(selectors.DefaultSelector())
            selector.register(fd, selectors.EVENT_READ)
            return lambda timeout: bool(selector.select(timeout))
        peek = _windows_pipe_peek(fd)
        return None if peek is None else _peek_waiter(peek, self._stop)

    def _drain_until_stopped(self, fd: int, ready: Callable[[float], bool]) -> None:
        # Waits in bounded steps, so finish() can stop a reader whose pipe a
        # descendant still holds open; once stopped it reads what is already there, at
        # most a buffer's worth, so a writer that never pauses cannot keep it going.
        while not self._stop.is_set():
            if ready(_STOP_POLL_S) and not self._read_once(fd):
                return
        for _ in range(READBACK_OUTPUT_MAX_BYTES // _READ_CHUNK_BYTES):
            if not ready(0) or not self._read_once(fd):
                return

    def _read_once(self, fd: int) -> bool:
        chunk = os.read(fd, _READ_CHUNK_BYTES)
        self._store(chunk)
        return bool(chunk)

    def _store(self, chunk: bytes) -> None:
        with self._lock:
            self._keep(chunk)

    def _keep(self, chunk: bytes) -> None:
        if self._keep_tail:
            self._data += chunk
            over = len(self._data) - READBACK_OUTPUT_MAX_BYTES
            if over > 0:
                del self._data[:over]
                self._cut = True
        else:
            self._data += chunk[: max(0, READBACK_OUTPUT_MAX_BYTES - len(self._data))]

    def finish(self, timeout: float) -> bytes:
        """What was kept, once the pipe reached EOF or *timeout* passed.

        A pipe a leftover grandchild still holds open stops being read then, and the
        reader closes it. A tail that was cut starts on a whole line.
        """
        if self._thread is not None:
            self._thread.join(timeout)
            self._stop.set()
            self._thread.join(_STOP_JOIN_S)
        with self._lock:
            data, cut = bytes(self._data), self._cut
        if cut:
            # The cut lands mid-line: drop the fragment rather than quote half of it.
            data = data.partition(b"\n")[2]
        return data


def _windows_pipe_peek(fd: int) -> Callable[[], int] | None:
    """How many bytes the Windows pipe *fd* holds, or ``None`` when it is no pipe."""
    if sys.platform != "win32":
        return None
    import msvcrt

    import _winapi

    handle = msvcrt.get_osfhandle(fd)

    def peek() -> int:
        return int(_winapi.PeekNamedPipe(handle, 0)[0])

    try:
        peek()
    except BrokenPipeError:
        return peek
    except OSError:
        return None
    return peek


def _peek_waiter(peek: Callable[[], int], stop: threading.Event) -> Callable[[float], bool]:
    """A wait that is ready once *peek* sees bytes or the end, else waits on *stop*."""

    def ready(timeout: float) -> bool:
        try:
            if peek() > 0:
                return True
        except BrokenPipeError:
            # The writer closed: the read that follows sees the end of input.
            return True
        stop.wait(timeout)
        return False

    return ready


def _error_text(error: dict[str, Any]) -> str:
    message = error.get("message")
    data = error.get("data")
    details = data.get("details") if isinstance(data, dict) else None
    parts = [part.strip() for part in (message, details) if isinstance(part, str) and part.strip()]
    return ": ".join(parts)


def session_new_error(stdout: bytes, request_id: int) -> str:
    """The error the CLI answered request *request_id* with, scrubbed, or ``""``."""
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        try:
            frame = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(frame, dict) or frame.get("id") != request_id:
            continue
        error = frame.get("error")
        if not isinstance(error, dict):
            return ""
        # Judged line by line, so a credential the error wraps across lines is seen
        # rejoined; the whitespace is folded only in a quote that passed.
        lines = [line.strip() for line in _error_text(error).splitlines() if line.strip()]
        judged = safe_stderr_tail(lines, " ", max_chars=READBACK_ERROR_MAX_CHARS)
        return " ".join(judged.split())
    return ""


def stderr_tail(stderr: bytes) -> str:
    """The last :data:`READBACK_STDERR_LINES` stderr lines, judged whole, or ``""``."""
    lines = [line for line in stderr.decode("utf-8", errors="replace").splitlines() if line.strip()]
    tail = agent_sdk.published_stderr_tail(lines, " | ", READBACK_STDERR_LINES)
    return tail[-READBACK_ERROR_MAX_CHARS:]


def gate_not_loaded_issue(
    returncode: int | None, timeout_s: float, stdout: bytes, stderr: bytes, request_id: int
) -> str:
    """The refusal for a read-back that saw no marker.

    *returncode* is the CLI's own exit code when it exited before the wait ran out,
    ``None`` when it was still running at the deadline.
    """
    if returncode is None:
        ended = f"within {timeout_s:.0f}s of starting the harness"
    else:
        ended = f"before the harness exited with code {returncode}"
    return (
        f"the Strands project did not load Kiro Crew's gate {ended} (the project failed "
        "to load, or its agent could not be gated), so its tools would not ask"
    ) + output_detail(stdout, stderr, request_id)


def output_detail(stdout: bytes, stderr: bytes, request_id: int) -> str:
    """The ``session/new`` error and the stderr tail, each as a ``"; ..."`` clause."""
    detail = ""
    error = session_new_error(stdout, request_id)
    if error:
        detail += f"; the harness answered session/new with: {error}"
    tail = stderr_tail(stderr)
    if tail:
        detail += f"; its stderr ended: {tail}"
    return detail


def readback_failed_issue(
    exc: BaseException, returncode: int | None, stdout: bytes, stderr: bytes, request_id: int
) -> str:
    """The refusal for a read-back that failed once the probe ran (*exc*).

    *returncode* is the CLI's exit code when it had already exited at the failure,
    as a CLI that dies at once does before taking the requests.
    """
    issue = f"the gate's load marker could not be read back ({exc})"
    if returncode is not None:
        issue += f"; the harness exited with code {returncode}"
    return issue + output_detail(stdout, stderr, request_id)

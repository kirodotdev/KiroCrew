"""Test harness for spawning isolated KiroCrew gateways.

Companion to the ``--test-mode`` / ``--json-ready`` / ``--port`` / ``--approval``
CLI flags on ``kirocrew gateway``. Provides a context manager that spins up
an isolated, headless gateway from the current workspace's source tree (not
the system-installed ``kirocrew``), reads the ``KIROCREW_READY:{...}`` line
off stdout, and tears down cleanly on exit.

Transport-agnostic: ``GatewayHandle`` exposes the URL plus a few metadata
fields. The caller chooses the driver (Playwright via DSO Frontend MCP is
the recommended one; plain HTTP for backend-only smoke tests is fine but
not the recommended path).

Usage:

    from kiro_crew.testing.harness import spawn_feature_gateway

    with spawn_feature_gateway() as handle:
        # drive Playwright / urllib / etc against handle.url
        ...
    # subprocess and tmp KIROCREW_HOME are gone by here

Reusable primitives:

    ``parse_ready_line`` and ``terminate_pgid`` are public, I/O-agnostic
    building blocks. Out-of-process supervisors that drive a *detached*
    gateway (tail its log for the READY line, terminate it later by pid
    without holding the ``Popen``) can reuse them instead of re-implementing
    the wire-contract parse and the SIGTERM→SIGKILL group kill.

Cross-platform: the harness runs on Linux, macOS and Windows. Two things had
to be platform-neutral for that, and both are load-bearing rather than
cosmetic:

    * The READY wait reads the child's stdout through a daemon reader thread
      feeding a ``queue.Queue`` (:class:`_StdoutPump`). ``selectors`` cannot
      poll a PIPE on Windows -- ``DefaultSelector`` there is select()-based and
      accepts sockets only -- so a selector-driven read is not merely slower on
      Windows, it raises. The queue's bounded ``get`` preserves what the
      selector poll bought: the deadline is enforced even while the child is
      alive and silent.
    * Teardown routes through ``platform_compat.kill_process_tree`` on Windows
      (``taskkill /T /F``) instead of ``terminate_pgid``; there is no
      ``setsid`` / ``killpg`` there. See
      ``docs/system-specs/common/platform-compat.md``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Callable, Iterator, Optional

from kiro_crew import platform_compat
from kiro_crew.dashboard.crash_dump_store import dump_replay_lines, newest_dump_with_stacks
from kiro_crew.kiro_prerequisite import FAKE_ACP_TEST_MODE_ENV
from kiro_crew.testing import fake_acp_backend

_LOGGER = logging.getLogger(__name__)

# 60 s default — config init + MCP probe + dashboard bind takes meaningful
# time on slow machines. 60 s gives headroom without masking real hangs.
# Override via ``KIROCREW_HARNESS_READY_TIMEOUT``.
DEFAULT_READY_TIMEOUT = 60.0

# How long to wait between SIGTERM and SIGKILL during teardown. Gateway's
# graceful-shutdown budget is 10 s internally; 5 s here is enough for the
# common case (no pending tool calls, no hung MCP servers) and bounds the
# pytest teardown latency for tests that exercise multiple invocations.
TERMINATE_GRACE_SECONDS = 5.0

# How long the READY wait keeps reading stdout after the child has exited, so a
# READY line already written but not yet handed over by the reader thread is not
# misreported as "exited before READY". The child's write end is closed by then,
# so EOF arrives in milliseconds; this is a ceiling, not a sleep.
EXIT_DRAIN_SECONDS = 2.0

#: Codec the Windows backend shim is written in. ``cmd.exe`` decodes a batch file
#: in the console's OEM code page, so ``ascii`` raises outright on a non-ASCII
#: interpreter path and ``utf-8`` would hand cmd.exe mojibake for one. Named
#: rather than inlined because the codec only EXISTS on Windows: the shim's
#: escaping rules are platform-independent and have to be testable on the shards
#: that can run them, while this choice is pinned on its own.
SHIM_ENCODING = "oem"

# Sentinel prefix the gateway prints to stdout once the dashboard is bound.
# Owned by ``slack/gateway_runtime/service_boot.py``; if you change it there, update here too.
READY_PREFIX = "KIROCREW_READY:"


def crash_dump_diagnostic(home: Path) -> str:
    """The gateway's loop-stall stack dump, if its watchdog wrote one into *home*.

    The watchdog (``dashboard/loop_watchdog.py``) writes every thread's stack into
    ``<home>/logs/crash-dumps/``, never stderr, and the harness removes the HOME
    right after, so this is folded into :meth:`GatewayHandle.diagnostics` to get
    the blocking frame into the test's failure message.

    The journal caps of :func:`~kiro_crew.dashboard.crash_dump_store.dump_replay_lines`
    are lifted: ``faulthandler`` lists threads newest-first, so a capped replay can
    end before the OTHER thread holding the GIL in a long native call, which is
    the one stack that says what stalled the gateway.

    Empty when no dump with stacks exists (a header-only file is the per-boot
    sentinel). Never raises: the diagnostic it decorates must still be reported.
    """
    try:
        dump = newest_dump_with_stacks(home / "logs" / "crash-dumps")
        if dump is None:
            return ""
        lines, truncated = dump_replay_lines(dump, max_lines=sys.maxsize, max_bytes=sys.maxsize)
    except Exception:  # noqa: BLE001 - diagnostics must never mask the failure
        return ""
    if not lines:
        return ""
    text = "\n".join(lines)
    if truncated:
        text += "\n... (dump truncated; wedged thread shown first)"
    return f"\n--- loop-stall crash dump ({dump.name}) ---\n{text}"


@dataclass(frozen=True)
class GatewayHandle:
    """Handle on a spawned gateway.

    Transport-agnostic on purpose: all I/O is left to the caller, so each test
    picks its own driver (Playwright, urllib, websockets).

    Attributes:
        url: Authenticated dashboard URL with token query param.
        port: OS-assigned ephemeral port the dashboard is bound to.
        token: Session token embedded in ``url``, for clients that build
            their own URLs (e.g. WebSocket connectors).
        home: The throwaway ``KIROCREW_HOME``, for inspecting on-disk state.
        proc: Underlying ``subprocess.Popen``; the context manager owns it.
    """

    url: str
    port: int
    token: str
    home: Path
    proc: subprocess.Popen[bytes]
    #: Bound diagnostics provider (exit status, stderr tail, stdout tail).
    #: ``None`` for handles built outside :func:`spawn_feature_gateway`.
    _diagnostics: Optional[Callable[[], str]] = None
    _teardown_confirmed: threading.Event = field(
        default_factory=threading.Event, init=False, repr=False, compare=False
    )

    _restart: Optional[Callable[[Optional[bool]], "GatewayHandle"]] = None

    @property
    def teardown_confirmed(self) -> bool:
        """Whether the harness confirmed the whole spawned tree stopped."""
        return self._teardown_confirmed.is_set()

    def restart(self, *, skip_model_download: Optional[bool] = None) -> "GatewayHandle":
        """Restart this harness-owned process, retaining its isolated data home.

        ``skip_model_download`` re-decides the one download switch for the NEW
        process (see :func:`spawn_feature_gateway`); ``None`` keeps the previous
        launch's value. The remaining environment retains its initial snapshot.
        """
        if self._restart is None:
            raise GatewaySpawnError("This handle has no active harness supervisor")
        return self._restart(skip_model_download)

    def diagnostics(self) -> str:
        """Exit status plus the stderr and stdout tails of the child, for a
        failure AFTER the READY line.

        Call this in the assertion message: on a platform nobody can reproduce
        locally, the child's stderr is the only evidence there is.
        """
        if self._diagnostics is None:
            return f"pid {self.proc.pid} exit={self.proc.poll()!r} (no captured output)"
        return self._diagnostics()


class GatewaySpawnError(RuntimeError):
    """Raised when the harness can't spin up a working gateway.

    Wraps the underlying cause (timeout, early exit, bad fixture name)
    with the subprocess's stderr so failures produce useful diagnostics
    instead of a bare ``TimeoutError`` from a buried ``readline()``.
    """


def _resolve_workspace_src() -> Path:
    """Locate the in-repo ``src/`` so PYTHONPATH points at feature-branch code.

    The system-installed ``kirocrew`` is avoided on purpose: a stale install on
    PATH would mask regressions, and may lack ``--test-mode``.
    """
    here = Path(__file__).resolve()
    # <pkg>/src/kiro_crew/testing/harness.py -> <pkg>/src
    src = here.parent.parent.parent
    if not (src / "kiro_crew" / "__init__.py").exists():
        raise GatewaySpawnError(
            f"Could not locate kiro_crew package at {src}. "
            f"Harness expects harness.py at <pkg>/src/kiro_crew/testing/, "
            f"with the source tree at <pkg>/src/kiro_crew/."
        )
    return src


def parse_ready_line(line: str) -> dict[str, Any]:
    """Parse and validate a single ``KIROCREW_READY:{...}`` line.

    Pure (no I/O), so the in-process pipe reader and out-of-process log tailers
    share one wire contract. Checks the ``READY_PREFIX`` itself and returns the
    parsed payload dict.

    Raises:
        ``GatewaySpawnError`` on a missing prefix, malformed JSON, a non-dict
        payload, or a missing ``port`` / ``token`` key, never a raw
        ``JSONDecodeError`` / ``KeyError``.
    """
    if not line.startswith(READY_PREFIX):
        raise GatewaySpawnError(f"line does not start with {READY_PREFIX}: {line!r}")
    try:
        payload = json.loads(line[len(READY_PREFIX) :])
    except json.JSONDecodeError as exc:
        raise GatewaySpawnError(f"malformed {READY_PREFIX} line: {line!r} ({exc})") from exc
    if not isinstance(payload, dict):
        raise GatewaySpawnError(
            f"{READY_PREFIX} payload was {type(payload).__name__}, " f"expected dict: {line!r}"
        )
    for required_key in ("port", "token"):
        if required_key not in payload:
            raise GatewaySpawnError(
                f"{READY_PREFIX} payload missing required key " f"{required_key!r}: {line!r}"
            )
    return payload


class _StdoutPump:
    """One daemon reader over the child's stdout, for the whole run.

    A thread, not ``selectors``: on Windows the selector accepts sockets only,
    so a subprocess PIPE raises there. The queue gives the READY waiter a bounded
    wait that does not spin on EOF. One thread for the whole run, because two
    readers on one pipe race for the same bytes: after :meth:`handoff` it keeps
    draining (so the gateway never blocks on a full pipe) into a bounded tail.
    """

    #: Chunks retained after handoff, for post-mortem diagnostics.
    TAIL_CHUNKS = 256

    def __init__(self, stdout: IO[bytes]) -> None:
        self._stdout = stdout
        self._queue: queue.Queue[Optional[bytes]] = queue.Queue()
        self._tail: deque[bytes] = deque(maxlen=self.TAIL_CHUNKS)
        self._handed_off = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        try:
            while True:
                chunk = self._stdout.read1(4096)  # type: ignore[attr-defined]
                if not chunk:
                    return
                if self._handed_off.is_set():
                    self._tail.append(chunk)
                else:
                    self._queue.put(chunk)
        except (OSError, ValueError):
            # Pipe closed under us (teardown races the read). Nothing to
            # report: the waiter's deadline and ``proc.poll()`` own the verdict.
            return
        finally:
            # EOF sentinel, so the waiter can stop expecting bytes without
            # having to poll the thread's liveness.
            self._queue.put(None)

    def get(self, timeout: float) -> Optional[bytes]:
        """Next stdout chunk, ``None`` at EOF, raising ``queue.Empty`` on timeout."""
        return self._queue.get(timeout=timeout)

    def drain_until_eof(self, timeout: float) -> bytes:
        """Everything the reader captures up to EOF, bounded by ``timeout``.

        Used on child exit. Waiting for the EOF sentinel, not a non-blocking
        drain, so a READY line the reader has not handed over yet is not
        reported as "exited before READY".
        """
        deadline = time.monotonic() + timeout
        chunks: list[bytes] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                chunk = self._queue.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                continue
            if chunk is None:
                break
            chunks.append(chunk)
        return b"".join(chunks)

    def handoff(self) -> None:
        """Stop queueing; keep draining the pipe into the bounded tail."""
        self._handed_off.set()

    def tail_text(self) -> str:
        """The post-handoff stdout tail, decoded for a diagnostic message."""
        return b"".join(self._tail).decode("utf-8", errors="replace")


def fake_acp_backend_launcher(directory: Path) -> Path:
    """The path to hand ``KIROCREW_KIRO_BIN`` so the gateway spawns the packaged
    fake ACP backend, on every platform.

    On POSIX that is ``fake_acp_backend.__file__`` itself (shebang, exec'd in
    place). Windows ``CreateProcess`` refuses a ``.py`` but runs a ``.cmd``, which
    is also in ``platform_compat``'s runnable-suffix set.

    Named ``kiro-backend.cmd`` ON PURPOSE, not ``kiro-cli.exe``: Kiro CLI sandbox
    delegation must come from the ACP call site passing ``is_kiro_cli=True``,
    never from the basename. ``directory`` must outlive the gateway.

    cmd.exe re-parses the batch file, so ``%`` in the interpreter path is doubled
    and a double quote or newline is REFUSED: cmd.exe cannot escape them, and
    booting the wrong path is worse than failing. Same rule as
    ``kiro_crew.pod.windows._cmd_literal``, which raises a pod-specific error.

    RESIDUAL: cmd.exe expands ``%VAR%`` and eats ``^`` in the ``%*`` arguments,
    the only way a batch file forwards argv. The ACP launch argv carries no
    ``%``; a caller that needs one must not use this shim.

    Written in the console's OEM code page, the one cmd.exe decodes a ``.cmd`` in.
    """
    backend = Path(str(fake_acp_backend.__file__))
    if not platform_compat.IS_WINDOWS:
        return backend
    exe = str(sys.executable)
    if '"' in exe or "\r" in exe or "\n" in exe:
        raise GatewaySpawnError(
            f"cannot build the Windows backend shim: the interpreter path {exe!r} "
            "contains a character cmd.exe cannot quote (a double quote or a "
            "newline), so the shim would boot a different path than intended"
        )
    launcher = Path(directory) / "kiro-backend.cmd"
    launcher.write_text(
        "@echo off\r\n" f'"{exe.replace("%", "%%")}" -m kiro_crew.testing.fake_acp_backend %*\r\n',
        encoding=SHIM_ENCODING,
    )
    return launcher


def _wait_for_ready_line(
    proc: subprocess.Popen[bytes],
    *,
    timeout: float,
    stderr_buffer: list[bytes],
    pump: Optional[_StdoutPump] = None,
) -> dict[str, Any]:
    """Read stdout until we see ``KIROCREW_READY:{...}`` or hit the timeout.

    Reads through a :class:`_StdoutPump`, so the deadline holds even when the
    child is alive but silent; a blocking ``readline()`` would hang forever.

    Child exit is an IMMEDIATE failure carrying the stderr tail, after the
    queued stdout is scanned for READY: a child that printed READY and exited at
    once did emit it.

    ``pump`` lets the caller keep the reader alive past this function; when
    omitted, one is created and owned here.

    Raises ``GatewaySpawnError`` on timeout, early exit, or malformed payload.
    Returns the parsed READY-line dict.
    """
    deadline = time.monotonic() + timeout
    stdout = proc.stdout
    if stdout is None:  # defensive — Popen always wires stdout when PIPE
        raise GatewaySpawnError("subprocess stdout is not piped")

    if pump is None:
        pump = _StdoutPump(stdout)
        pump.start()

    def _stderr_tail() -> str:
        return b"".join(stderr_buffer).decode("utf-8", errors="replace")[-4000:]

    def _scan(buffer: bytes) -> tuple[Optional[dict[str, Any]], bytes]:
        """Pull whole lines off ``buffer``; return the READY payload if seen."""
        while b"\n" in buffer:
            line_bytes, buffer = buffer.split(b"\n", 1)
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if line.startswith(READY_PREFIX):
                return parse_ready_line(line), buffer
        return None, buffer

    buf = b""
    # Bounded like the pump's tail; evicted chunks are counted so the timeout
    # message can report truncation.
    stdout_received: deque[bytes] = deque(maxlen=_StdoutPump.TAIL_CHUNKS)
    _stdout_dropped = 0  # chunks evicted by the deque cap
    while True:
        if proc.poll() is not None:
            # Scan whatever the reader captures up to EOF before declaring the
            # exit premature (see the docstring's race note). Bounded: the child
            # is already gone, so its write end is closed and EOF is imminent.
            payload, buf = _scan(buf + pump.drain_until_eof(EXIT_DRAIN_SECONDS))
            if payload is not None:
                return payload
            _trunc_exit = (
                f" [{_stdout_dropped} earlier chunk(s) dropped]" if _stdout_dropped else ""
            )
            stdout_at_exit = (
                b"".join(stdout_received).decode("utf-8", errors="replace")[-2000:] + _trunc_exit
            )
            raise GatewaySpawnError(
                f"gateway subprocess exited with code {proc.returncode} "
                f"before emitting {READY_PREFIX} line.\n"
                f"--- stderr (last) ---\n{_stderr_tail()}\n"
                f"--- stdout (so far) ---\n{stdout_at_exit}"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            alive = proc.poll() is None
            has_any_output = bool(stdout_received) or bool(stderr_buffer)
            # Build a diagnostic that names whether the process is alive, what
            # it wrote to stdout, and what state it is in.  Stderr is empty
            # for the whole Python import phase, so a cold-import stall is
            # named by the liveness state and by `cold_hint`, which fires when
            # neither stream has produced output.
            _trunc = f" [{_stdout_dropped} earlier chunk(s) dropped]" if _stdout_dropped else ""
            stdout_so_far = (
                b"".join(stdout_received).decode("utf-8", errors="replace")[-2000:] + _trunc
            )
            stderr_so_far = _stderr_tail()
            process_state = f"alive (pid {proc.pid})" if alive else f"exited {proc.returncode}"
            cold_hint = (
                "\n(no output from subprocess — likely still in cold import "
                "phase; raise KIROCREW_HARNESS_READY_TIMEOUT if the runner is slow)"
                if not has_any_output
                else ""
            )
            raise GatewaySpawnError(
                f"gateway did not emit {READY_PREFIX} line within "
                f"{timeout:.1f}s [{process_state}]. "
                f"Override with KIROCREW_HARNESS_READY_TIMEOUT.\n"
                f"--- stderr (last) ---\n{stderr_so_far}\n"
                f"--- stdout (so far) ---\n{stdout_so_far}"
                f"{cold_hint}"
            )
        # Cap each wait at 0.5s so the deadline + poll() checks above run
        # frequently even if the subprocess goes silent.
        try:
            chunk = pump.get(timeout=min(remaining, 0.5))
        except queue.Empty:
            continue  # poll interval elapsed — re-check deadline & proc
        if chunk is None:
            # EOF on stdout without READY. The child may still be alive (it can
            # close stdout and keep running), so keep honouring the deadline and
            # the exit check rather than failing here; the queue is empty from
            # now on, so each iteration sleeps its 0.5 s instead of spinning.
            # Deliberate: a child that closes stdout but keeps running must not
            # be failed immediately; the deadline and the exit check own the verdict.
            continue
        if len(stdout_received) == _StdoutPump.TAIL_CHUNKS:
            _stdout_dropped += 1
        stdout_received.append(chunk)
        payload, buf = _scan(buf + chunk)
        if payload is not None:
            return payload


def _drain_stderr(stderr: IO[bytes], buffer: list[bytes]) -> None:
    """Continuously read from stderr into ``buffer``.

    The gateway emits ~30+ WARNING lines on startup (config-loader meta-key
    spam, MCP probe failures for unconfigured servers). They all go to
    stderr, so without a drainer the pipe fills, the subprocess blocks on
    write, and we deadlock. Buffer the contents so we can surface them
    on failure.
    """
    while True:
        chunk = stderr.read1(4096)  # type: ignore[attr-defined]
        if not chunk:
            return
        buffer.append(chunk)


def terminate_pgid(
    pid: int,
    *,
    grace: Optional[float] = None,
    pgid: Optional[int] = None,
    wait: Optional[Callable[[float], object]] = None,
) -> bool:
    """SIGTERM a process group by pid, escalate to SIGKILL after ``grace``.

    POSIX ONLY, deliberately unguarded: on Windows ``os.getpgid`` raises
    ``AttributeError``, which loudly flags a caller using the wrong primitive.

    pid-based so out-of-process supervisors (a ``stop`` script for a detached
    gateway) share the harness's teardown. The target must be a process-group
    leader (spawned with ``start_new_session=True``).

    Args:
        pid: Process-group leader pid.
        grace: Seconds between SIGTERM and SIGKILL. Defaults to
            ``TERMINATE_GRACE_SECONDS``, read at CALL time so patches apply.
        pgid: The group to signal, when known (for a ``start_new_session``
            child it is the child's pid). Passing it keeps teardown working
            after the leader is reaped, when ``os.getpgid`` cannot name the
            group its children still run in.
        wait: Optional ``proc.wait``-style hook, called as ``wait(grace)``.
            Callers owning the ``Popen`` SHOULD pass it: it sees the exit even
            while the child is an unreaped zombie, which a pid poll cannot.
            Without it the whole GROUP is polled, so lingering children still
            escalate to SIGKILL.

    Does NOT reap. No-op if the process is already gone. ``PermissionError``
    (pid recycled, reduced privilege) aborts with a logged diagnostic.

    Returns:
        ``True`` when the group was already empty or was signalled to
        completion, ``False`` on every ``PermissionError`` path.
        ``_terminate_process_group`` spends it on whether the HOME may be removed.
    """
    if grace is None:
        grace = TERMINATE_GRACE_SECONDS
    if pgid is None:
        # A reaped leader resolves to its own pid: the group outlives it, and
        # signalling nothing would report a still-running tree as clean.
        pgid = platform_compat.pgroup_of_leader(pid)
        if pgid is None:
            _LOGGER.warning(
                "terminate_pgid(%d): the process group is not signalable by this "
                "user (pid recycled?) — aborting teardown",
                pid,
            )
            return False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True  # nothing in the group to signal
    except PermissionError:
        _LOGGER.warning(
            "terminate_pgid(%d): SIGTERM to pgid %d denied — target not "
            "signalable by this user, teardown skipped",
            pid,
            pgid,
        )
        return False

    if wait is not None:
        # Handle-based detection: returns the moment the child exits (even as
        # an unreaped zombie), so graceful teardowns don't burn the full grace
        # window the way a pid poll would.
        leader_exited = False
        with contextlib.suppress(subprocess.TimeoutExpired):
            wait(grace)
            leader_exited = True
        if leader_exited:
            # Leader is gone, but group children may linger (holding the
            # ephemeral port). One group probe; sweep if anything remains.
            if not platform_compat.pgroup_exists(pgid):
                return True  # whole group gone — done
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)
            return True
    else:
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            try:
                # Group liveness, not leader liveness: a reparented leader can
                # exit (and be reaped by init) while a SIGTERM-ignoring child
                # keeps the ephemeral port open — the exact tree-outlives-
                # parent case the group kill exists for.
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return True  # whole group exited within the grace window
            except PermissionError:
                # pgid recycled to another user mid-window: our group is gone
                # and SIGKILLing would hit an unrelated group. Stop here.
                _LOGGER.warning(
                    "terminate_pgid(%d): pgid %d no longer signalable during "
                    "grace poll (recycled?) — skipping SIGKILL escalation",
                    pid,
                    pgid,
                )
                return False
            time.sleep(0.05)

    # Still alive after the grace window — escalate.
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        _LOGGER.warning(
            "terminate_pgid(%d): SIGKILL to pgid %d denied — group may " "still be running",
            pid,
            pgid,
        )
        return False
    return True


# How long a SIGKILLed group is given to actually disappear. SIGKILL cannot be
# refused, but a member with I/O in flight is torn down by the kernel on its
# own schedule, and the caller's next act is to remove the HOME those members
# were writing into.
GROUP_EXIT_WAIT_SECONDS = 5.0


def _await_group_gone(pgid: int) -> bool:
    """True once no member of *pgid* remains; False if any outlives the wait.

    A SIGKILLed member mid-write into the HOME can outlast the leader's
    ``wait()``; a ``True`` here lets the caller remove that HOME without racing
    a dying writer.

    Call it only AFTER the leader is reaped: an unreaped zombie still answers
    ``killpg(pgid, 0)`` and would hold the wait for the whole window.
    """
    deadline = time.monotonic() + GROUP_EXIT_WAIT_SECONDS
    while platform_compat.pgroup_exists(pgid):
        if time.monotonic() >= deadline:
            _LOGGER.warning(
                "terminate_pgid: pgid %d still has members %.0fs after SIGKILL",
                pgid,
                GROUP_EXIT_WAIT_SECONDS,
            )
            return False
        time.sleep(0.05)
    return True


def _terminate_process_tree_windows(
    proc: subprocess.Popen[bytes], known_descendants: Optional[dict[int, str]] = None
) -> bool:
    """Tear down the gateway's tree on Windows via ``taskkill /T /F``.

    Windows has no ``setsid`` / ``killpg``; ``platform_compat.kill_process_tree``
    walks the real parent/child tree, reaching the MCP servers and kiro-cli
    children that ``proc.terminate()`` would leave holding the port. There is no
    graceful step: Windows has no SIGTERM for a child without a shared console.

    **Returns whether the TREE is gone, not whether the root was reaped**; the
    caller spends it on whether it may delete the HOME. A failed ``taskkill`` on
    a protected descendant is logged and swallowed, so the descendants are
    SNAPSHOTTED BEFORE the kill (a kill orphans them out of the parent map),
    attributed by :func:`platform_compat.attributed_descendants`, which validates
    every parent-child edge, and re-probed afterwards.
    """
    # Snapshot first, with each child's creation identity, while the root is alive
    # and this process holds its handle (so the root's own pid cannot be recycled
    # underneath the walk).
    survivors: dict[int, str] = dict(known_descendants or {})
    root_token = platform_compat.process_start_time(proc.pid) or ""
    if root_token:
        for child in platform_compat.attributed_descendants(proc.pid, root_token):
            child_token = platform_compat.process_start_time(child)
            if child_token:
                survivors[child] = child_token
    try:
        platform_compat.kill_process_tree(proc.pid, platform_compat.SIGTERM)
    except ProcessLookupError:
        pass  # already gone between the poll above and here
    except (PermissionError, OSError) as exc:
        # A protected descendant or a transient access denial. Log rather than
        # raise: teardown must not turn a passing test red, and the survivor
        # re-probe below is what decides whether the tree actually died.
        _LOGGER.warning("kill_process_tree(%d) failed during teardown: %s", proc.pid, exc)
    try:
        proc.wait(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    alive = sorted(
        pid
        for pid, token in survivors.items()
        if platform_compat.pid_exists(pid) and platform_compat.process_start_time(pid) == token
    )
    if alive:
        # Existence first, then identity: a creation token stays readable while any
        # handle to the exited process is open, so the token alone would report a
        # corpse as residue.
        _LOGGER.warning(
            "gateway teardown left %d descendant(s) running after taskkill: %s",
            len(alive),
            alive,
        )
        return False
    return True


def _terminate_process_group(
    proc: subprocess.Popen[bytes], known_descendants: Optional[dict[int, str]] = None
) -> bool:
    """Tear down the gateway's whole process tree, then reap.

    ``proc.terminate()`` signals only the parent; its children (MCP servers,
    kiro-cli sessions) can outlive it holding the port or cache files.

    POSIX uses ``terminate_pgid`` with ``proc.wait`` as the exit hook, then
    reaps. Windows takes ``_terminate_process_tree_windows``.

    **A root that has already exited is NOT a tree that is gone.** Its pid may
    already name an unrelated session leader, so teardown skips the process-group
    sweep and ends only descendants pinned in the last live snapshot. The harness
    certifies only teardown it drives from a live root: after a mid-test crash,
    descendants spawned after that snapshot cannot be attributed once their parent
    links disappear. That path returns ``False`` and keeps the failing gateway's
    HOME for inspection.
    """
    root_already_exited = proc.poll() is not None
    if root_already_exited:
        _LOGGER.warning(
            "gateway exited before teardown; descendants spawned after the last "
            "snapshot cannot be attributed, so its HOME is kept for inspection"
        )
    if platform_compat.IS_WINDOWS:
        swept = _terminate_process_tree_windows(proc, known_descendants)
        return swept and not root_already_exited
    if root_already_exited:
        _end_escaped_descendants(dict(known_descendants or {}))
        return False
    # Spawned with `start_new_session=True`, so the group id is the child's pid;
    # naming it keeps teardown working after the leader is reaped.
    pgid = proc.pid
    # Snapshot descendants OUTSIDE the gateway's group before signalling: a group
    # kill cannot reach them, and after teardown their parent link is gone. The
    # MCP probes (``kirocrew mcp-core`` / ``mcp-cron``, own session) are the
    # example; left running they keep writing into the HOME about to be removed.
    escaped: dict[int, str] = {}
    for child, known_token in (known_descendants or {}).items():
        group = platform_compat.pgroup_of(child)
        if group is not None and group != pgid:
            escaped[child] = known_token
    # NOT gated on the ROOT's creation token, deliberately: a CRASHED gateway is
    # when a detached MCP probe is left running AND when the root's token is least
    # readable. Gating would leave `escaped` empty and let the caller remove the
    # HOME beneath a live writer. The root token feeds only the Windows path.
    #
    # No `created_after` filter either: it compensates for Windows' stale
    # parent-pid edges, while a POSIX parent map is read live. On macOS the token
    # is `ps -o lstart=` text, so its `int()` comparison would drop every MCP
    # probe. `kill_pid_pinned` below keeps the kill off a stranger; the snapshot
    # only FINDS candidates.
    for child in platform_compat.process_descendants(proc.pid):
        token = platform_compat.process_start_time(child)
        if not token:
            # No identity means no safe kill, and a True here leads to `rmtree`.
            # A gone pid is just a listing race; a live one fails CLOSED.
            if platform_compat.pid_exists(child):
                return False
            continue
        # Through platform_compat: the portability gate flags a bare
        # `os.getpgid`. `None` (unreadable) is not evidence of an escape.
        group = platform_compat.pgroup_of(child)
        if group is None or group == pgid:
            continue  # unknown, or the group kill covers it
        escaped[child] = token
    swept = terminate_pgid(
        proc.pid,
        grace=TERMINATE_GRACE_SECONDS,
        pgid=pgid,
        wait=lambda timeout: proc.wait(timeout=timeout),
    )
    # The hook reaps on graceful exit; after a SIGKILL escalation the child
    # still needs reaping so it doesn't linger as a zombie.
    try:
        proc.wait(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    # Reaping the leader says nothing about the GROUP. Whether every member has
    # EXITED is read after the reap (a zombie leader keeps the group "alive"),
    # so the caller's rmtree never races a child unwinding from SIGKILL.
    if not (swept and _await_group_gone(pgid)):
        return False
    return _end_escaped_descendants(escaped)


def _sigkill_if_identity_matches(pid: int, token: str) -> None:
    """SIGKILL *pid* only if its creation identity still equals *token*.

    On POSIX ``kill_pid_pinned`` is a plain ``os.kill`` that leaves the identity
    check to its caller, so the check is made here. Where Linux offers a pidfd,
    the process is pinned BEFORE its identity is read: a pid reissued after the
    pin cannot retarget the fd, and one reissued before it reads a different
    start time. Without a pidfd (macOS) the identity is re-read just before the
    kill, which narrows the window but cannot close it. Errors from the signal
    propagate; the caller treats them as nothing left to end.
    """
    if not platform_compat.IS_WINDOWS and _pidfd_sigkill_if_identity_matches(pid, token):
        return
    # Windows: the primitive itself holds the verified handle across the
    # terminate. POSIX without a usable pidfd: the re-read is the only check.
    if platform_compat.IS_WINDOWS or platform_compat.process_start_time(pid) == token:
        platform_compat.kill_pid_pinned(pid, token, platform_compat.SIGKILL)


def _pidfd_sigkill_if_identity_matches(pid: int, token: str) -> bool:
    """Pinned half of :func:`_sigkill_if_identity_matches`; False if no pidfd.

    False means this host cannot pin (no wrapper, an old kernel answering
    ENOSYS, a sandbox answering EPERM) and the caller falls back. True means the
    pid was handled here: signalled, spared on a mismatch, or already gone.
    """
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is None or pidfd_send_signal is None:
        return False
    try:
        fd = pidfd_open(pid)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    try:
        if platform_compat.process_start_time(pid) == token:
            pidfd_send_signal(fd, platform_compat.SIGKILL)
    finally:
        os.close(fd)
    return True


def _end_escaped_descendants(escaped: dict[int, str]) -> bool:
    """SIGKILL saved descendants by pinned identity; return True once gone.

    Pinned: a pid is signalled only while its creation identity still matches
    the snapshot, so a pid recycled in the meantime is left alone. The wait is
    the same bounded window the group gets, for the same reason — the HOME is
    removed on a ``True`` from here.
    """
    for child, token in escaped.items():
        with contextlib.suppress(OSError):
            _sigkill_if_identity_matches(child, token)
    deadline = time.monotonic() + GROUP_EXIT_WAIT_SECONDS
    while True:
        alive = [
            child
            for child, token in escaped.items()
            if platform_compat.pid_exists(child)
            and platform_compat.process_start_time(child) == token
        ]
        if not alive:
            return True
        if time.monotonic() >= deadline:
            _LOGGER.warning(
                "gateway descendants still alive %.0fs after SIGKILL: %s",
                GROUP_EXIT_WAIT_SECONDS,
                alive,
            )
            return False
        time.sleep(0.05)


def _launch_gateway(
    home: Path, env: dict[str, str], *, fixture: str | None, approval: str, crons: bool
) -> subprocess.Popen:
    """Launch only our test gateway; no caller-supplied executable or argv."""
    cmd = platform_compat.isolated_python_argv(
        "-P",
        "-m",
        "kiro_crew",
        "gateway",
        "--test-mode",
        force_isolation=True,
    )
    if fixture is not None:
        cmd.extend(["--seed", fixture])
    cmd.extend(["--approval", approval])
    if not crons:
        cmd.append("--no-crons")
    return subprocess.Popen(
        cmd,
        # Seeding replaces home atomically; do not keep cwd on its old inode.
        cwd=home.parent,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=platform_compat.IS_POSIX,
        creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
    )


def harness_environment(
    home: Path, src: Path, *, skip_model_download: bool = True
) -> dict[str, str]:
    """The environment a harness gateway is launched with.

    Layered over ``os.environ`` so production knobs the test exports
    (``KIROCREW_EMBED_MODEL_URL``, ``OLLAMA_MODELS``) reach the gateway, then
    pinned below. ``KIROCREW_SKIP_MODEL_DOWNLOAD=1`` is the ONLY default a caller
    can drop, via ``skip_model_download``, for the download-failure evidence
    gateway; such a caller must also export a loopback
    ``KIROCREW_EMBED_MODEL_URL`` nothing serves (unchecked here).
    """
    env = {
        **os.environ,
        "PYTHONPATH": str(src) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        "KIROCREW_HOME": str(home),
        # Isolate the AGENT-SPEC home too. Boot's ``rebuild_agent_config`` writes
        # managed MCP specs into ``kiro_agents_dir()``; at the default that is the
        # operator's real ``~/.kiro/agents``, and the write guard in
        # ``agent_parts/shared_home.py`` would NOT decline for this ordinary
        # install, stamping this checkout's venv and tmp home into the real specs.
        # ``<home>/kiro/agents`` is exactly ``isolated_agents_dir(<home>)``, which
        # the guard lets this instance own, and is removed with ``home``.
        "KIRO_HOME": str(home / "kiro"),
        # Marks a test rig; grants no launch privilege (the fake backend is
        # exec'd by the ordinary in-place path).
        FAKE_ACP_TEST_MODE_ENV: "1",
        # Unbuffered so early prints before READY cannot block-buffer on a pipe
        # and mask failures.
        "PYTHONUNBUFFERED": "1",
        # Offline rig: the app store answers from the bundled seed, never an
        # uncached HTTPS round trip that eats the specs' ready budget. Spelled
        # literally (``apps.official_catalog.SKIP_FETCH_ENV``) to avoid importing apps.
        "KIROCREW_SKIP_APP_STORE_FETCH": "1",
        # Test-owned adoption pin table, so a boot never fetches a shipped pin
        # from GitHub; absent means empty. Spelled literally
        # (``apps.builtin_adoption_pins.HARNESS_PINS_ENV``).
        "KIROCREW_HARNESS_ADOPTION_PINS": str(home / "harness-adoption-pins.json"),
    }
    if skip_model_download:
        # Embeddings are default-on; never let a harness-spawned gateway
        # kick the 610MB embedding-model download during a test run.
        env["KIROCREW_SKIP_MODEL_DOWNLOAD"] = "1"
    else:
        # An inherited "1" would silently re-arm the skip the caller asked to drop.
        env.pop("KIROCREW_SKIP_MODEL_DOWNLOAD", None)
    return env


@contextlib.contextmanager
def spawn_feature_gateway(
    fixture: str = "minimal",
    approval: str = "reads",
    *,
    crons: bool = False,
    timeout: Optional[float] = None,
    kiro_bin: Optional[str | Path] = None,
    before_spawn: Optional[Callable[[dict[str, str], Path], None]] = None,
    skip_model_download: bool = True,
) -> Iterator[GatewayHandle]:
    """Spin up an isolated gateway from the current workspace checkout.

    Args:
        fixture: Named fixture (``empty`` / ``minimal`` / ``rich``) passed
            through to ``kirocrew gateway --seed`` so the tmp KIROCREW_HOME
            is populated atomically with gateway startup. A bad name causes
            the gateway to exit before READY; the readline loop surfaces
            seed's stderr in a ``GatewaySpawnError``.
        approval: Approval mode to pass through ``--approval``. ``"reads"``
            (default) auto-approves a conservative set of read verbs;
            ``"yolo"`` auto-approves everything but requires an isolated
            ``KIROCREW_HOME`` (the harness always provides one); pass
            ``"interactive"`` only if the test will drive the approval
            UI itself.
        crons: ``False`` (default) passes ``--no-crons``, since stray cron
            fires pollute unrelated tests' state. ``True`` keeps the scheduler
            alive for end-to-end ``cron_add`` tests.
        timeout: Override the ready-line timeout in seconds. Falls back to
            ``KIROCREW_HARNESS_READY_TIMEOUT`` env var, then to
            ``DEFAULT_READY_TIMEOUT``.
        kiro_bin: Exact ``kiro-cli`` path to publish as
            ``KIROCREW_KIRO_BIN`` in the child environment. Use this when a
            prerequisite probe and the gateway must execute the same binary;
            ``None`` preserves the inherited resolver behavior. An explicit
            binary also requires an existing launcher; its directory is
            prepended to child PATH to prevent host-wide launcher installation.
            ``KIRO_HOME`` is always the owned ``<throwaway home>/kiro`` tree.
        before_spawn: Optional preflight called with a copy of the final child
            environment and cwd before ``Popen``. The fixture is seeded first
            using the ordinary seed API in a child; preflight-created private
            files are preserved and gateway startup does not seed again.
            Mutating the environment copy does not alter the gateway launch.
        skip_model_download: ``True`` (default) exports
            ``KIROCREW_SKIP_MODEL_DOWNLOAD=1`` so no harness gateway fetches
            the 610MB embedding model. ``False`` arms the download for THIS
            gateway only; see :func:`harness_environment`.

    Yields:
        ``GatewayHandle`` once the gateway has bound its dashboard port
        and emitted the ``KIROCREW_READY:{...}`` line. The handle is only
        valid inside the ``with`` block; on exit the subprocess is
        terminated and ``handle.home`` is removed.

    Raises:
        ``GatewaySpawnError`` if the gateway exits before READY, doesn't
        emit READY within the timeout, or can't seed the fixture.
    """
    src = _resolve_workspace_src()
    home = Path(tempfile.mkdtemp(prefix="kirocrew-harness-"))
    # Outer try/finally so ``home`` is always cleaned up, even if
    # ``subprocess.Popen`` raises before we reach the inner block (bad
    # ``sys.executable``, fd exhaustion, fork failure, ...). ``exited`` starts
    # True for exactly that case: with no child spawned there is nothing alive
    # to protect the HOME from.
    exited = True
    try:
        if timeout is None:
            env_timeout = os.environ.get("KIROCREW_HARNESS_READY_TIMEOUT")
            timeout = float(env_timeout) if env_timeout else DEFAULT_READY_TIMEOUT

        env = harness_environment(home, src, skip_model_download=skip_model_download)
        if kiro_bin is not None:
            env["KIROCREW_KIRO_BIN"] = str(kiro_bin)
            from kiro_crew.agent import _resolve_kirocrew_bin

            # First-run setup may install a host PATH shim even when shared
            # agent-spec writes are declined. Expose an existing launcher instead.
            launcher = Path(_resolve_kirocrew_bin())
            if not launcher.is_absolute() or not launcher.is_file():
                raise GatewaySpawnError("explicit kiro_bin requires an existing kirocrew launcher")
            env["PATH"] = str(launcher.parent) + os.pathsep + env.get("PATH", "")
            reachable = shutil.which("kirocrew", path=env["PATH"])
            if not reachable or Path(reachable).resolve() != launcher.resolve():
                raise GatewaySpawnError("existing kirocrew launcher is not reachable on child PATH")

        spawn_cwd = home.parent
        if before_spawn is not None:
            # Native authentication/config preflights can create private files.
            # Seed the empty owned tree FIRST, retaining seed's ordinary guards.
            # Use a child so seed's environment lookup never changes our parent.
            seeded = subprocess.run(
                platform_compat.isolated_python_argv(
                    "-c",
                    "from kiro_crew.seed import seed; import sys; seed(sys.argv[1])",
                    fixture,
                    force_isolation=True,
                ),
                cwd=str(spawn_cwd),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if seeded.returncode != 0:
                raise GatewaySpawnError(
                    f"preflight fixture seed failed (exit {seeded.returncode}): {seeded.stderr}"
                )
            before_spawn(dict(env), spawn_cwd)
        proc = _launch_gateway(
            home,
            env,
            fixture=None if before_spawn is not None else fixture,
            approval=approval,
            crons=crons,
        )

        # Drain stderr asynchronously into a list so the buffer can't fill
        # and deadlock the subprocess. Using a daemon thread is fine: it
        # exits when the process closes its stderr.
        stderr_buffer: list[bytes] = []
        if proc.stderr is not None:
            drainer = threading.Thread(
                target=_drain_stderr, args=(proc.stderr, stderr_buffer), daemon=True
            )
            drainer.start()

        # ONE reader owns stdout for the whole run. It feeds the READY waiter
        # first, then keeps draining into a bounded tail -- the READY loop stops
        # consuming once it sees the sentinel, and without a continuing drain the
        # gateway blocks on a full stdout pipe partway through a long run
        # (per-turn logging fills the ~64KB buffer), stalling the event loop into
        # connection-refused for every later request. A second thread on the same
        # pipe would race this one for the same bytes, so the pump switches
        # destinations instead of being replaced.
        pump: Optional[_StdoutPump] = None
        if proc.stdout is not None:
            pump = _StdoutPump(proc.stdout)
            pump.start()

        known_descendants: dict[int, str] = {}
        handle: Optional[GatewayHandle] = None
        running = True
        active = True
        try:
            ready = _wait_for_ready_line(
                proc, timeout=timeout, stderr_buffer=stderr_buffer, pump=pump
            )
            if pump is not None:
                pump.handoff()
            if proc.poll() is None:
                if platform_compat.IS_WINDOWS:
                    root_token = platform_compat.process_start_time(proc.pid) or ""
                    if root_token:
                        for child in platform_compat.attributed_descendants(proc.pid, root_token):
                            token = platform_compat.process_start_time(child)
                            if token:
                                known_descendants[child] = token
                else:
                    for child in platform_compat.process_descendants(proc.pid):
                        token = platform_compat.process_start_time(child)
                        if token:
                            known_descendants[child] = token
            port = int(ready["port"])
            token = str(ready["token"])
            url = f"http://localhost:{port}/?token={token}"

            def _diagnostics() -> str:
                err = b"".join(stderr_buffer).decode("utf-8", errors="replace")[-4000:]
                out = ""
                if pump is not None:
                    out = pump.tail_text()[-2000:]
                return (
                    f"gateway pid {proc.pid} exit={proc.poll()!r}\n"
                    f"--- stderr (last) ---\n{err}\n"
                    f"--- stdout after READY (last) ---\n{out}" + crash_dump_diagnostic(home)
                )

            def _restart(skip_model_download: Optional[bool] = None) -> GatewayHandle:
                nonlocal proc, pump, stderr_buffer, known_descendants, running, handle
                if not active or not running:
                    raise GatewaySpawnError("The harness supervisor is no longer active")
                if not _terminate_process_group(proc, known_descendants):
                    raise GatewaySpawnError(
                        "The previous gateway tree did not stop; restart refused"
                    )
                if handle is not None:
                    handle._teardown_confirmed.set()
                running = False
                if skip_model_download is True:
                    env["KIROCREW_SKIP_MODEL_DOWNLOAD"] = "1"
                elif skip_model_download is False:
                    env.pop("KIROCREW_SKIP_MODEL_DOWNLOAD", None)
                # No seed on restart: persisted bindings and payloads are the input.
                proc = _launch_gateway(home, env, fixture=None, approval=approval, crons=crons)
                running = True
                known_descendants = {}
                stderr_buffer = []
                if proc.stderr is not None:
                    threading.Thread(
                        target=_drain_stderr, args=(proc.stderr, stderr_buffer), daemon=True
                    ).start()
                pump = _StdoutPump(proc.stdout) if proc.stdout is not None else None
                if pump is not None:
                    pump.start()
                restarted = _wait_for_ready_line(
                    proc, timeout=timeout, stderr_buffer=stderr_buffer, pump=pump
                )
                if pump is not None:
                    pump.handoff()
                if proc.poll() is None:
                    if platform_compat.IS_WINDOWS:
                        root_token = platform_compat.process_start_time(proc.pid) or ""
                        if root_token:
                            for child in platform_compat.attributed_descendants(
                                proc.pid, root_token
                            ):
                                child_token = platform_compat.process_start_time(child)
                                if child_token:
                                    known_descendants[child] = child_token
                    else:
                        for child in platform_compat.process_descendants(proc.pid):
                            child_token = platform_compat.process_start_time(child)
                            if child_token:
                                known_descendants[child] = child_token
                port = int(restarted["port"])
                token = str(restarted["token"])
                handle = GatewayHandle(
                    url=f"http://localhost:{port}/?token={token}",
                    port=port,
                    token=token,
                    home=home,
                    proc=proc,
                    _diagnostics=_diagnostics,
                    _restart=_restart,
                )
                return handle

            handle = GatewayHandle(
                url=url,
                port=port,
                token=token,
                home=home,
                proc=proc,
                _diagnostics=_diagnostics,
                _restart=_restart,
            )
            yield handle
        finally:
            active = False
            exited = _terminate_process_group(proc, known_descendants) if running else True
            if exited is True and handle is not None:
                handle._teardown_confirmed.set()
    finally:
        # Clean up the tmp home only once the gateway is confirmed gone: a
        # tree kill that failed (a protected descendant, an access denial on
        # Windows' taskkill) leaves a live gateway writing into it, and removing
        # the HOME under it would be the data loss this harness exists to keep
        # out of the developer's real home. The path is logged so the leak is
        # findable, and the test's own verdict is not turned red by teardown.
        # ``ignore_errors`` because pytest cleanup races with nested file
        # handles on macOS and the harness shouldn't fail teardown on stale FDs.
        if exited:
            shutil.rmtree(home, ignore_errors=True)
        else:
            _LOGGER.error(
                "gateway pid %d did not exit within %ss of teardown; leaving its "
                "HOME %s in place rather than deleting it under a live process",
                proc.pid,
                TERMINATE_GRACE_SECONDS,
                home,
            )

"""Background commands: gateway-owned shell processes on the workflow host-run substrate.

An agent starts a long-running command with ``background_run`` and ends its turn.
The gateway spawns the command at the agent shell's sandbox tier, streams its
output to a file, and settles the host run when the process exits, times out or
is stopped. The workflow registry's completion injection then wakes the chat
that started it, so no turn is spent waiting.

The gateway opens the output log and the exit-status file itself and hands the
command their descriptors, so the command outlives a gateway restart while the
folder that holds them stays read-only to every sandboxed process (the
``background`` leaf in ``sandbox._CREW_READONLY_LEAVES``). On boot
:meth:`reconcile` re-adopts a live process whose recorded start identity still
matches, settles one that exited while the gateway was down, and reports the
rest as interrupted.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import stat
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Optional

from kiro_crew import platform_compat, sandbox, shutdown_event
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home
from kiro_crew.constants import (
    BACKGROUND_RUN_DEFAULT_TIMEOUT_SECS,
    BACKGROUND_RUN_MAX_COMMAND_CHARS,
    BACKGROUND_RUN_MAX_TIMEOUT_SECS,
    BACKGROUND_RUN_MIN_TIMEOUT_SECS,
)
from kiro_crew.pinned_fs import fd_real_path
from kiro_crew.sandbox import (
    SandboxUnavailableError,
    create_subprocess_limited,
    sandboxed_spawn_argv_async,
)
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

DRIVER = "command"
SOURCE_FORMAT = "shell"
BACKGROUND_DIR_NAME = "background"

DEFAULT_TIMEOUT_SECS = BACKGROUND_RUN_DEFAULT_TIMEOUT_SECS
MIN_TIMEOUT_SECS = BACKGROUND_RUN_MIN_TIMEOUT_SECS
MAX_TIMEOUT_SECS = BACKGROUND_RUN_MAX_TIMEOUT_SECS
MAX_COMMAND_CHARS = BACKGROUND_RUN_MAX_COMMAND_CHARS
MAX_LABEL_CHARS = 120
MAX_RUNNING_PER_SESSION = 8
MAX_RUNNING_TOTAL = 32
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
TAIL_LINES = 40
TAIL_MAX_CHARS = 4000

OUTCOME_EXITED = "exited"
OUTCOME_TIMED_OUT = "timed_out"
OUTCOME_OUTPUT_LIMIT = "output_limit"
OUTCOME_STOPPED = "stopped"
OUTCOME_INTERRUPTED = "interrupted"

_RECORD_FILE = "record.json"
_OUTPUT_FILE = "output.log"
_EXIT_STATUS_FILE = "exit_status"
_TAIL_READ_BYTES = 64 * 1024
_COMPACT_CHUNK_BYTES = 1024 * 1024
_STATUS_READ_BYTES = 64
_COMMAND_ECHO_CHARS = 500
_POLL_SECS = 2.0
_KILL_GRACE_SECS = 5.0
# The watchdog fires this long after the deadline, so a live gateway stops the
# command first and the watchdog only acts while none is watching.
_WATCHDOG_MARGIN_SECS = 60
_TIMEOUT_MARK = "timeout"
# How long a spawn wrapped in a systemd scope may take to enter it.
_SCOPE_JOIN_SECS = 2.0
_CGROUP_FS = Path("/sys/fs/cgroup")
_EXIT_CONFIRM_SECS = 1.0
_KILL_POLL_SECS = 0.2
_SETTLE_WRITE_ATTEMPTS = 3
_SETTLE_RETRY_SECS = 1.0
_RETENTION_SECS = 7 * 86400
# Never looser than the agent's own shell: under ``off`` its isolation is kiro-cli's
# sandbox, which a gateway-side spawn cannot use.
_SANDBOX_FLOOR = "standard"
_RUN_ID_RE = re.compile(r"^wf_[0-9]+$")
_OPEN_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_PATH_ENV = "KIROCREW_BACKGROUND_PATH"
_UNSTORED_FIELDS = frozenset({"session_key"})


def _wrapper() -> str:
    """``sh -c`` body: ``$1`` is the command, ``$2`` the watchdog's seconds, ``$3`` the cap.

    The status file arrives as stdin and moves to fd 3 (dash takes one-digit fds
    only), the log moves to fd 4, and both are closed for the command's subshell,
    so the command can write neither. Its output reaches the log through
    ``head -c``, which bounds the log even while no gateway watches. The subshell
    confines an ``exit`` in the command and waits for the jobs it backgrounds. The
    watchdog holds the deadline while no gateway watches: after ``$2`` seconds it
    records a timeout and stops the group, which is the wrapper's own because the
    spawn starts a new session. ``PATH`` is restored from ``_PATH_ENV`` because
    the pinned-cwd spawn screens it.
    """
    return (
        f'if [ -n "${{{_PATH_ENV}-}}" ]; then PATH=${_PATH_ENV}; export PATH; fi; '
        f"unset {_PATH_ENV}; "
        "exec 3>&0 4>&1 </dev/null; "
        "( trap 'kill \"$s\" 2>/dev/null; exit 0' TERM; "
        'sleep "$2" & s=$!; wait "$s" || exit 0; '
        f"printf '{_TIMEOUT_MARK}\\n' >&3; trap '' TERM; "
        f"kill -TERM 0; sleep {int(_KILL_GRACE_SECS)}; kill -KILL 0 ) >/dev/null 2>&1 4>&- & "
        "w=$!; "
        'rc=$( { { ( eval "$1"; rc=$?; wait; exit "$rc" ) 2>&1 3>&- 4>&- 5>&-; '
        'echo "$?" >&5; } | head -c "$3" >&4 3>&- 5>&-; } 5>&1 ); rc=${rc:-1}; '
        'kill "$w" 2>/dev/null; printf "%s\\n" "$rc" >&3; exit "$rc"'
    )


async def _exited_within(proc: asyncio.subprocess.Process, secs: float) -> bool:
    """True once ``proc`` has exited, waiting at most ``secs`` for it."""
    try:
        await asyncio.wait_for(proc.wait(), timeout=secs)
    except asyncio.TimeoutError:
        return False
    return True


async def _to_completion(awaitable: Awaitable[Any]) -> Any:
    """Await ``awaitable`` to its end even if this task is cancelled, then re-raise."""
    inner = asyncio.ensure_future(awaitable)
    cancelled: Optional[asyncio.CancelledError] = None
    while not inner.done():
        try:
            await asyncio.shield(inner)
        except asyncio.CancelledError as exc:
            cancelled = exc
    result = inner.result()
    if cancelled is not None:
        raise cancelled
    return result


def _close_all(*fds: int) -> None:
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


async def _close_off_loop(*fds: int) -> None:
    """Close descriptors on a worker thread; a cancel cannot drop the close."""
    await _to_completion(asyncio.to_thread(_close_all, *fds))


class BackgroundCommandError(RuntimeError):
    """A background command could not start; ``code``/``status`` shape the reply."""

    def __init__(self, message: str, *, code: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class CommandRecord:
    """Durable identity of one background command, kept beside its output.

    ``log_id`` and ``status_id`` are the ``st_dev:st_ino`` of the two files the
    gateway created, so a reader never trusts a file that merely has the name.
    ``scope`` is the command's systemd scope, when the sandbox made one.
    """

    run_id: str
    session_key: str
    command: str
    cwd: str
    label: str
    started_at: float
    deadline: float
    pid: int = 0
    start_id: str = ""
    log_id: str = ""
    status_id: str = ""
    scope: str = ""
    settled: bool = False


def clamp_timeout(value: Optional[int]) -> int:
    """Default a missing timeout and bound a supplied one."""
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_TIMEOUT_SECS
    return max(MIN_TIMEOUT_SECS, min(MAX_TIMEOUT_SECS, value))


def run_name(label: str, command: str) -> str:
    """One-line run name: the label, else the command's first line."""
    source = label if label.strip() else command
    first = source.strip().splitlines()[0] if source.strip() else "command"
    return " ".join(first.split())[:MAX_LABEL_CHARS]


def _redact(text: str) -> str:
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


_SECRET_NAME = r"[A-Z0-9_]*(?:PASS(?:WORD|WD)?|SECRET|TOKEN|API_?KEY|PRIVATE_KEY|CREDENTIALS?)"
_URL_USERINFO_RE = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@", re.I)
_SECRET_ASSIGN_RE = re.compile(rf"\b({_SECRET_NAME}[A-Z0-9_]*=)(\"[^\"]*\"|'[^']*'|\S+)")
_SECRET_FLAG_RE = re.compile(
    r"((?:^|\s)--?(?:password|passwd|pass|token|secret|api-key)(?:=|\s+))"
    r"(\"[^\"]*\"|'[^']*'|\S+)",
    re.I,
)
_USERPASS_FLAG_RE = re.compile(r"((?:^|\s)(?:-u|--user)(?:=|\s*))(\S+:\S+)")


def _redact_command(command: str) -> str:
    """The command as it may be retained: credential patterns plus inline shell secrets.

    Past the baseline, a URL's ``user:password@``, a secret-named assignment
    (``PGPASSWORD=...``), a password or token flag, and ``-u user:password`` are
    replaced. Only the displayed and persisted copy changes; the command runs as given.
    """
    command = _URL_USERINFO_RE.sub(r"\1[REDACTED]@", command)
    for pattern in (_SECRET_ASSIGN_RE, _SECRET_FLAG_RE, _USERPASS_FLAG_RE):
        command = pattern.sub(r"\1[REDACTED]", command)
    return _redact(command)


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def describe_outcome(result: dict[str, Any]) -> str:
    """One sentence naming how a background command ended."""
    outcome = result.get("outcome")
    duration = _format_duration(float(result.get("duration_s") or 0))
    if outcome == OUTCOME_EXITED:
        if result.get("exit_code") is None:
            return f"was ended by a signal after {duration}"
        return f"exited with code {result.get('exit_code')} after {duration}"
    if outcome == OUTCOME_TIMED_OUT:
        if result.get("exit_code") is not None:
            return f"exited with code {result['exit_code']} after {duration}, past its timeout"
        return f"was stopped at its {duration} timeout"
    if outcome == OUTCOME_OUTPUT_LIMIT:
        limit_mib = MAX_OUTPUT_BYTES // (1024 * 1024)
        return f"was cut off after {duration}: its output passed {limit_mib} MiB"
    if outcome == OUTCOME_STOPPED:
        return f"was stopped after {duration}"
    return "was interrupted: the gateway restarted and its exit status is unknown"


def _file_id(fd: int) -> str:
    info = os.fstat(fd)
    return f"{info.st_dev}:{info.st_ino}"


def _open_owned(path: Path, expected_id: str, flags: int = os.O_RDONLY) -> Optional[int]:
    """Open a file the gateway created, refusing a link or any replacement."""
    if not expected_id:
        return None
    try:
        fd = os.open(path, flags | _OPEN_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
    except OSError:
        os.close(fd)
        return None
    owned = (
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and f"{info.st_dev}:{info.st_ino}" == expected_id
    )
    if not owned:
        os.close(fd)
        return None
    return fd


def _read_tail(path: Path, expected_id: str) -> tuple[str, int]:
    """Return the last lines of the owned log (bounded, redacted) and its size."""
    fd = _open_owned(path, expected_id)
    if fd is None:
        return "", 0
    try:
        size = os.fstat(fd).st_size
        start = max(0, size - _TAIL_READ_BYTES)
        raw = os.pread(fd, size - start, start)
    except OSError:
        return "", 0
    finally:
        os.close(fd)
    lines = raw.decode("utf-8", errors="replace").splitlines()[-TAIL_LINES:]
    tail = "\n".join(lines)
    if len(tail) > TAIL_MAX_CHARS:
        tail = tail[-TAIL_MAX_CHARS:]
    return _redact(tail), size


def _owned_size(path: Path, expected_id: str) -> int:
    fd = _open_owned(path, expected_id)
    if fd is None:
        return 0
    try:
        return os.fstat(fd).st_size
    except OSError:
        return 0
    finally:
        os.close(fd)


def _compact_log(path: Path, expected_id: str) -> None:
    """Cut an owned log that grew past the cap down to its last ``MAX_OUTPUT_BYTES``."""
    fd = _open_owned(path, expected_id, os.O_RDWR)
    if fd is None:
        return
    try:
        start = os.fstat(fd).st_size - MAX_OUTPUT_BYTES
        if start <= 0:
            return
        for offset in range(0, MAX_OUTPUT_BYTES, _COMPACT_CHUNK_BYTES):
            chunk = os.pread(
                fd, min(_COMPACT_CHUNK_BYTES, MAX_OUTPUT_BYTES - offset), start + offset
            )
            os.pwrite(fd, chunk, offset)
        os.ftruncate(fd, MAX_OUTPUT_BYTES)
    except OSError:
        pass
    finally:
        os.close(fd)


def _read_exit_status(path: Path, expected_id: str) -> tuple[Optional[str], float]:
    """The last status line (an exit code or the timeout mark) and when it was written.

    ``None`` until a whole line is there. Every writer appends, so the wrapper's
    closing line comes after anything the command wrote first.
    """
    fd = _open_owned(path, expected_id)
    if fd is None:
        return None, 0.0
    try:
        info = os.fstat(fd)
        start = max(0, info.st_size - _STATUS_READ_BYTES)
        raw = os.pread(fd, info.st_size - start, start).decode("ascii", errors="replace")
    except OSError:
        return None, 0.0
    finally:
        os.close(fd)
    complete = raw.split("\n")[:-1]
    line = complete[-1].strip() if complete else ""
    if not (line == _TIMEOUT_MARK or re.fullmatch(r"[0-9]{1,3}", line)):
        return None, 0.0
    return line, info.st_mtime


def _pin_directory(cwd: str) -> int:
    """Open ``cwd`` and re-check the directory actually held, not its name."""
    try:
        fd = os.open(cwd, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _OPEN_NOFOLLOW)
    except OSError as exc:
        raise BackgroundCommandError(
            f"cwd could not be opened: {exc.strerror}",
            code="background_run_cwd_invalid",
            status=400,
        ) from exc
    real = fd_real_path(fd)
    if real is None or is_sensitive_path(real):
        os.close(fd)
        raise BackgroundCommandError(
            "cwd is a protected location", code="background_run_cwd_invalid", status=400
        )
    return fd


def _unlink_quietly(path: str) -> None:
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def _expects_scope(argv: list[str]) -> bool:
    """True when the sandbox put the spawn in its own systemd scope."""
    return bool(argv) and Path(argv[0]).name == "systemd-run"


def _sandbox_mode() -> str:
    """The operator's ``agent.sandbox`` tier, raised to the agent shell's floor."""
    return sandbox._clamp_sandbox_mode_to_floor(sandbox.configured_sandbox_mode(), _SANDBOX_FLOOR)


def _scope_dir(scope: str) -> Optional[Path]:
    """The cgroup directory of ``scope`` if it is a scope in this instance's agent slice."""
    rel = PurePosixPath(scope)
    slice_name = sandbox._agents_slice_name()
    if (
        not rel.is_absolute()
        or ".." in rel.parts
        or not rel.name.endswith(".scope")
        or rel.parent.name != slice_name
        or slice_name == sandbox._CGROUP_AGENTS_SLICE
    ):
        return None
    return _CGROUP_FS.joinpath(*rel.parts[1:])


def _own_scope(pid: int) -> str:
    """The unified-cgroup path of ``pid`` when it is one of this instance's scopes."""
    if sys.platform != "linux":
        return ""
    try:
        raw = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = [line for line in raw.splitlines() if line]
    if len(lines) != 1 or not lines[0].startswith("0::/"):
        return ""
    scope = lines[0][len("0::") :]
    return scope if _scope_dir(scope) is not None else ""


def _scope_members(folder: Path) -> list[int]:
    try:
        raw = (folder / "cgroup.procs").read_text(encoding="ascii")
    except OSError:
        return []
    return [int(token) for token in raw.split() if token.isdigit()]


def _kill_scope(scope: str) -> bool:
    """SIGKILL everything in ``scope``, including members that left the process group.

    ``cgroup.kill`` kills the whole cgroup at once; without it (Linux before
    5.14) each member is pinned by pidfd and re-checked as a member before the
    signal. True once the scope is empty or gone.
    """
    folder = _scope_dir(scope)
    if folder is None or not folder.is_dir():
        return True
    try:
        (folder / "cgroup.kill").write_text("1", encoding="ascii")
    except OSError:
        _kill_members(folder)
    return not _scope_members(folder)


def _kill_members(folder: Path) -> None:
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is None or pidfd_send_signal is None:
        return
    for pid in _scope_members(folder):
        if pid <= 1 or pid == os.getpid():
            continue
        try:
            fd = pidfd_open(pid)
        except OSError:
            continue
        try:
            if pid in _scope_members(folder):
                pidfd_send_signal(fd, platform_compat.SIGKILL)
        except OSError:
            pass
        finally:
            os.close(fd)


class BackgroundCommandService:
    """Starts, supervises and re-adopts gateway-owned background commands."""

    def __init__(
        self,
        workflows: Any,
        *,
        root: Optional[Path] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._workflows = workflows
        self._root_override = root
        self._clock = clock
        self._supervisors: dict[str, asyncio.Task[Any]] = {}
        self._owners: dict[str, str] = {}
        # Slots claimed by a start still between its cap check and its first
        # tracked supervisor, so concurrent starts cannot all pass one check.
        self._reserved: dict[str, int] = {}
        # Sandbox launcher files, held in memory only: a path read back from disk
        # is never handed to unlink.
        self._cleanups: dict[str, str] = {}
        self._stopping = False

    @property
    def root(self) -> Path:
        return self._root_override or data_home() / BACKGROUND_DIR_NAME

    def _folder(self, run_id: str) -> Path:
        return self.root / run_id

    def log_path(self, run_id: str) -> Path:
        return self._folder(run_id) / _OUTPUT_FILE

    def _status_path(self, run_id: str) -> Path:
        return self._folder(run_id) / _EXIT_STATUS_FILE

    def running_for(self, session_key: str) -> int:
        tracked = sum(1 for owner in self._owners.values() if owner == session_key)
        return tracked + self._reserved.get(session_key, 0)

    def _running_total(self) -> int:
        return len(self._owners) + sum(self._reserved.values())

    def _release(self, session_key: str) -> None:
        left = self._reserved.get(session_key, 0) - 1
        if left > 0:
            self._reserved[session_key] = left
        else:
            self._reserved.pop(session_key, None)

    # --- start ---

    async def start(
        self,
        *,
        session_key: str,
        command: str,
        cwd: str,
        label: str = "",
        timeout_secs: Optional[int] = None,
        expected_store: Optional[str] = None,
    ) -> dict[str, Any]:
        """Spawn ``command`` for ``session_key`` and return its run identity."""
        if self._stopping:
            raise BackgroundCommandError(
                "The gateway is shutting down; start the command again after it restarts.",
                code="background_run_unavailable",
                status=503,
            )
        if not platform_compat.IS_POSIX:
            raise BackgroundCommandError(
                "background_run needs a POSIX shell and is not available on Windows.",
                code="background_run_unsupported",
                status=409,
            )
        if self.running_for(session_key) >= MAX_RUNNING_PER_SESSION:
            raise BackgroundCommandError(
                f"This chat already has {MAX_RUNNING_PER_SESSION} background commands "
                "running. Wait for one to finish or stop one with workflow_cancel.",
                code="background_run_limit",
                status=409,
            )
        if self._running_total() >= MAX_RUNNING_TOTAL:
            raise BackgroundCommandError(
                f"{MAX_RUNNING_TOTAL} background commands are already running on this "
                "host. Wait for one to finish.",
                code="background_run_limit",
                status=409,
            )
        # Claimed before the first await; released once tracked or on failure.
        self._reserved[session_key] = self._reserved.get(session_key, 0) + 1
        try:
            return await self._start_reserved(
                session_key=session_key,
                command=command,
                cwd=cwd,
                name=run_name(_redact_command(label), _redact_command(command)),
                timeout=clamp_timeout(timeout_secs),
                expected_store=expected_store,
            )
        finally:
            self._release(session_key)

    async def _start_reserved(
        self,
        *,
        session_key: str,
        command: str,
        cwd: str,
        name: str,
        timeout: int,
        expected_store: Optional[str],
    ) -> dict[str, Any]:
        # Only the redacted copy is retained: the run, its record and every echo.
        shown = _redact_command(command)
        run_id = await self._workflows.begin_host_run(
            name=name,
            source=shown,
            source_format=SOURCE_FORMAT,
            driver=DRIVER,
            author=session_key,
            session_key=session_key,
            expected_store=expected_store,
            capabilities=("cancel",),
            completion_injection=True,
        )
        folder = self._folder(run_id)
        try:
            await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
            proc, cleanup, log_id, status_id, scoped = await self._spawn(
                run_id, command, cwd, timeout
            )
        except (OSError, SandboxUnavailableError, BackgroundCommandError) as exc:
            await self._workflows.delete_run(run_id)
            await asyncio.to_thread(shutil.rmtree, folder, True)
            if isinstance(exc, BackgroundCommandError):
                raise
            raise BackgroundCommandError(
                f"The command could not start: {_redact(str(exc))}",
                code="background_run_spawn_failed",
                status=409,
            ) from exc
        now = self._clock()
        record = CommandRecord(
            run_id=run_id,
            session_key=session_key,
            command=shown,
            cwd=cwd,
            label=name,
            started_at=now,
            deadline=now + timeout,
            pid=proc.pid,
            log_id=log_id,
            status_id=status_id,
        )
        if cleanup:
            self._cleanups[run_id] = cleanup
        task: Optional[asyncio.Task[Any]] = None
        try:
            start_id = await asyncio.to_thread(platform_compat.process_start_time, proc.pid)
            if proc.returncode is not None or not start_id:
                # A fast command may be reaped before its identity is read, and
                # its pid may then name another process: keep no identity.
                if not await _exited_within(proc, _EXIT_CONFIRM_SECS):
                    raise BackgroundCommandError(
                        "The command's process identity could not be read, so it was stopped.",
                        code="background_run_spawn_failed",
                        status=409,
                    )
                start_id = ""
            record.start_id = start_id
            if scoped and start_id:
                record.scope = await self._join_scope(record)
            await asyncio.to_thread(self._write_record, record)
            await self._workflows.phase(run_id, "Running")
            task = asyncio.create_task(self._supervise(record, proc), name=f"background-{run_id}")
            self._track(record, task)
            # Let the supervisor reach its first await before the run is
            # cancellable: a task cancelled before it starts never runs its
            # stop-and-settle arm.
            await asyncio.sleep(0)
            await self._workflows.bind_task(run_id, task)
        except BaseException:
            # Nothing may keep running unsupervised: a live supervisor stops and
            # settles its own command; otherwise stop it and drop the run here.
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            else:
                await self._terminate(record, proc)
                await asyncio.to_thread(_unlink_quietly, self._cleanups.pop(run_id, ""))
                await self._workflows.delete_run(run_id)
                await asyncio.to_thread(shutil.rmtree, folder, True)
            raise
        return {
            "run_id": run_id,
            "name": name,
            "log_path": str(self.log_path(run_id)),
            "timeout_secs": timeout,
        }

    async def _spawn(
        self, run_id: str, command: str, cwd: str, timeout: int
    ) -> tuple[asyncio.subprocess.Process, str, str, str, bool]:
        dir_fd = await asyncio.to_thread(_pin_directory, cwd)
        try:
            log_fd, status_fd, log_id, status_id = await asyncio.to_thread(
                self._create_owned_files, run_id
            )
        except BaseException:
            await _close_off_loop(dir_fd)
            raise
        try:
            watchdog = str(timeout + _WATCHDOG_MARGIN_SECS)
            # One byte past the cap, so a command cut off at the cap reads as over it.
            cap = str(MAX_OUTPUT_BYTES + 1)
            argv = [
                "/bin/sh",
                "-c",
                _wrapper(),
                "kirocrew-background",
                command,
                watchdog,
                cap,
            ]
            mode = await asyncio.to_thread(_sandbox_mode)
            wrapped, env, cleanup = await sandboxed_spawn_argv_async(argv, mode=mode)
            if env is not None and env.get("PATH"):
                env = {**env, _PATH_ENV: env["PATH"]}
            try:
                # Output goes to a file, never a pipe: a pipe would break when the
                # gateway exits and take the command down with it. The working
                # directory is entered by descriptor, so a path swapped after
                # validation cannot redirect it.
                proc = await create_subprocess_limited(
                    *wrapped,
                    chdir_fd=dir_fd,
                    stdin=status_fd,
                    stdout=log_fd,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                    start_new_session=platform_compat.IS_POSIX,
                    creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
                )
            except BaseException:
                await asyncio.to_thread(_unlink_quietly, cleanup or "")
                raise
            return proc, cleanup or "", log_id, status_id, _expects_scope(wrapped)
        finally:
            await _close_off_loop(log_fd, status_fd, dir_fd)

    def _create_owned_files(self, run_id: str) -> tuple[int, int, str, str]:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _OPEN_NOFOLLOW
        log_fd = os.open(self.log_path(run_id), flags | os.O_APPEND, 0o600)
        status_fd = -1
        try:
            status_fd = os.open(self._status_path(run_id), flags | os.O_APPEND, 0o600)
            return log_fd, status_fd, _file_id(log_fd), _file_id(status_fd)
        except BaseException:
            _close_all(*(fd for fd in (log_fd, status_fd) if fd >= 0))
            raise

    # --- supervision ---

    def _track(self, record: CommandRecord, task: "asyncio.Task[Any]") -> None:
        self._supervisors[record.run_id] = task
        self._owners[record.run_id] = record.session_key

    def _untrack(self, run_id: str) -> None:
        self._supervisors.pop(run_id, None)
        self._owners.pop(run_id, None)

    async def _supervise(
        self,
        record: CommandRecord,
        proc: Optional[asyncio.subprocess.Process],
        *,
        ready: Optional[asyncio.Event] = None,
    ) -> None:
        try:
            if ready is not None:
                await ready.wait()
            outcome, exit_code = await self._watch(record, proc)
        except asyncio.CancelledError:
            if self._stopping or shutdown_event.is_set():
                # Gateway shutdown: the command keeps running and the next boot
                # re-adopts it, so neither kill it nor settle its run here.
                self._untrack(record.run_id)
                raise
            await _to_completion(self._stop_and_settle(record, proc))
            raise
        # A cancel landing now must not strand a run whose command already ended.
        await _to_completion(self._settle(record, outcome, exit_code))

    async def _stop_and_settle(
        self, record: CommandRecord, proc: Optional[asyncio.subprocess.Process]
    ) -> None:
        await self._terminate(record, proc)
        await self._settle(record, OUTCOME_STOPPED, None)

    async def _watch(
        self, record: CommandRecord, proc: Optional[asyncio.subprocess.Process]
    ) -> tuple[str, Optional[int]]:
        waiter = asyncio.ensure_future(proc.wait()) if proc is not None else None
        try:
            while True:
                if waiter is not None:
                    done, _pending = await asyncio.wait({waiter}, timeout=_POLL_SECS)
                    if done:
                        outcome, exit_code = await self._exit_outcome(record, waiter.result())
                        await self._sweep_group(record)
                        if outcome == OUTCOME_EXITED and await self._over_cap(record):
                            return OUTCOME_OUTPUT_LIMIT, exit_code
                        return outcome, exit_code
                elif not await asyncio.to_thread(self._is_alive, record):
                    await self._sweep_group(record)
                    outcome, exit_code = await self._exit_outcome(record, None)
                    if outcome == OUTCOME_EXITED and await self._over_cap(record):
                        return OUTCOME_OUTPUT_LIMIT, exit_code
                    return outcome, exit_code
                if self._clock() >= record.deadline:
                    await self._terminate(record, proc)
                    return OUTCOME_TIMED_OUT, None
                if await self._over_cap(record):
                    await self._terminate(record, proc)
                    return OUTCOME_OUTPUT_LIMIT, None
                if waiter is None:
                    # A re-adopted command is checked before the first sleep, so one
                    # that ran past its deadline while no gateway watched stops at once.
                    await asyncio.sleep(_POLL_SECS)
        finally:
            if waiter is not None and not waiter.done():
                waiter.cancel()

    async def _over_cap(self, record: CommandRecord) -> bool:
        size = await asyncio.to_thread(_owned_size, self.log_path(record.run_id), record.log_id)
        return size > MAX_OUTPUT_BYTES

    async def _exit_outcome(
        self, record: CommandRecord, returncode: Optional[int]
    ) -> tuple[str, Optional[int]]:
        """Outcome of a command that is gone; ``returncode`` is None for a re-adopted one.

        A wrapper the gateway saw exit reports through its own exit status, which
        the command cannot write; the file then only carries the watchdog's mark.
        The watchdog's mark, or an exit after the deadline, is a timeout.
        """
        line, written_at = await asyncio.to_thread(
            _read_exit_status, self._status_path(record.run_id), record.status_id
        )
        if line == _TIMEOUT_MARK:
            return OUTCOME_TIMED_OUT, None
        if returncode is not None:
            exit_code = returncode if returncode >= 0 else None
            ended_at = self._clock()
        elif line is None:
            return OUTCOME_INTERRUPTED, None
        else:
            exit_code, ended_at = int(line), written_at
        if exit_code is not None and ended_at > record.deadline:
            return OUTCOME_TIMED_OUT, exit_code
        return OUTCOME_EXITED, exit_code

    async def _join_scope(self, record: CommandRecord) -> str:
        """The command's systemd scope, waiting briefly for the spawn to enter it."""
        give_up = time.monotonic() + _SCOPE_JOIN_SECS
        while True:
            scope = await asyncio.to_thread(_own_scope, record.pid)
            alive = await asyncio.to_thread(self._is_alive, record)
            if scope and alive:
                return scope
            if not alive or time.monotonic() >= give_up:
                return ""
            await asyncio.sleep(_KILL_POLL_SECS)

    @staticmethod
    def _is_alive(record: CommandRecord) -> bool:
        """True only while ``record.pid`` still names the process it recorded."""
        if record.pid <= 0 or not record.start_id:
            return False
        if not platform_compat.pid_exists(record.pid):
            return False
        return platform_compat.process_start_time(record.pid) == record.start_id

    async def _terminate(
        self, record: CommandRecord, proc: Optional[asyncio.subprocess.Process]
    ) -> None:
        """Stop the command: its leader's group, then any member left behind."""
        await self._signal_leader(record, proc)
        await self._sweep_group(record)

    async def _signal_leader(
        self, record: CommandRecord, proc: Optional[asyncio.subprocess.Process]
    ) -> None:
        """SIGTERM the leader's process group, a grace period, then SIGKILL."""
        for sig in (platform_compat.SIGTERM, platform_compat.SIGKILL):
            if proc is not None:
                if proc.returncode is not None:
                    return
                pid = proc.pid
            else:
                # A re-adopted pid is re-confirmed before every signal, so a
                # recycled pid never reaches a stranger's process group.
                if not await asyncio.to_thread(self._is_alive, record):
                    return
                pid = record.pid
            try:
                await platform_compat.kill_process_tree_async(pid, sig)
            except (ProcessLookupError, OSError):
                return
            if await self._wait_gone(record, proc):
                return

    async def _wait_gone(
        self, record: CommandRecord, proc: Optional[asyncio.subprocess.Process]
    ) -> bool:
        if proc is not None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_SECS)
                return True
            except asyncio.TimeoutError:
                return False
        give_up = time.monotonic() + _KILL_GRACE_SECS
        while time.monotonic() < give_up:
            if not await asyncio.to_thread(self._is_alive, record):
                return True
            await asyncio.sleep(_KILL_POLL_SECS)
        return False

    @staticmethod
    def _group_is_ours(record: CommandRecord) -> bool:
        """True while the command's process group has members and is still its own.

        The group id is the leader's pid, and a pid is never reused while a group
        carries it, so a live group whose leader is gone is this command's. A live
        leader must still carry the recorded start identity.
        """
        pgid = record.pid
        if pgid <= 1 or not platform_compat.pgroup_exists(pgid):
            return False
        if not platform_compat.pid_exists(pgid):
            return True
        return bool(record.start_id) and (
            platform_compat.process_start_time(pgid) == record.start_id
        )

    async def _sweep_group(self, record: CommandRecord) -> None:
        """Stop what the command left behind: its group's members, then its scope's."""
        await self._sweep_process_group(record)
        if record.scope:
            give_up = time.monotonic() + _KILL_GRACE_SECS
            while not await asyncio.to_thread(_kill_scope, record.scope):
                if time.monotonic() >= give_up:
                    return
                await asyncio.sleep(_KILL_POLL_SECS)

    async def _sweep_process_group(self, record: CommandRecord) -> None:
        """Stop members the command left in its group, such as a job it backgrounded."""
        for sig in (platform_compat.SIGTERM, platform_compat.SIGKILL):
            if not await asyncio.to_thread(self._group_is_ours, record):
                return
            try:
                if not platform_compat.kill_process_group(record.pid, sig):
                    return
            except ValueError:
                return
            give_up = time.monotonic() + _KILL_GRACE_SECS
            while time.monotonic() < give_up:
                if not await asyncio.to_thread(self._group_is_ours, record):
                    return
                await asyncio.sleep(_KILL_POLL_SECS)

    async def _settle(self, record: CommandRecord, outcome: str, exit_code: Optional[int]) -> None:
        """Report the outcome once; the slot and launcher file are released whatever fails."""
        try:
            await self._report(record, outcome, exit_code)
        finally:
            self._untrack(record.run_id)
            await asyncio.to_thread(_unlink_quietly, self._cleanups.pop(record.run_id, ""))
            with contextlib.suppress(OSError):
                await asyncio.to_thread(self._prune_settled)

    async def _report(self, record: CommandRecord, outcome: str, exit_code: Optional[int]) -> None:
        tail, size = await asyncio.to_thread(
            _read_tail, self.log_path(record.run_id), record.log_id
        )
        if size > MAX_OUTPUT_BYTES:
            await asyncio.to_thread(_compact_log, self.log_path(record.run_id), record.log_id)
        result: dict[str, Any] = {
            "command": record.command[:_COMMAND_ECHO_CHARS],
            "cwd": record.cwd,
            "outcome": outcome,
            "exit_code": exit_code,
            "duration_s": round(max(0.0, self._clock() - record.started_at), 1),
            "log_path": str(self.log_path(record.run_id)),
            "output_bytes": size,
            "output_tail": tail,
        }
        # Persisted before it is published: a report whose record did not land would
        # be reported again by the next boot's re-adoption.
        record.settled = True
        for attempt in range(_SETTLE_WRITE_ATTEMPTS):
            try:
                await asyncio.to_thread(self._write_record, record)
                break
            except OSError as exc:
                if attempt + 1 == _SETTLE_WRITE_ATTEMPTS:
                    record.settled = False
                    logger.warning(
                        "background command %s: record not saved (%s); the next boot settles it",
                        record.run_id,
                        exc,
                    )
                    return
                await asyncio.sleep(_SETTLE_RETRY_SECS)
        if outcome == OUTCOME_EXITED and exit_code == 0:
            await self._workflows.finish(record.run_id, result)
        elif outcome == OUTCOME_STOPPED:
            await self._workflows.cancel_host_run(record.run_id, "stopped", result=result)
        else:
            await self._workflows.fail(
                record.run_id,
                f"The command {describe_outcome(result)}.",
                where="command",
                result=result,
            )

    # --- restart ---

    async def reconcile(self) -> int:
        """Re-adopt, settle or report every unsettled command left by a prior gateway.

        Each adopted run is reopened before this returns, so a caller never sees a
        re-adopted command reported as failed.
        """
        records = await asyncio.to_thread(self._load_unsettled)
        adopted = 0
        for record in records:
            if record.run_id in self._supervisors:
                continue
            handle = self._workflows.registry.get(record.run_id)
            if handle is None or getattr(handle, "driver", "") != DRIVER:
                # No durable run to report to (a private session, or the record
                # was evicted): stop the orphan rather than let it run unowned.
                await self._retire(record)
                continue
            record.session_key = str(getattr(handle, "session_key", "") or "")
            reopened = asyncio.Event()
            task = asyncio.create_task(
                self._supervise(record, None, ready=reopened), name=f"background-{record.run_id}"
            )
            self._track(record, task)
            await asyncio.sleep(0)
            if not await self._workflows.rebind(record.run_id, task):
                # The supervisor's stop arm terminates the command and settles it.
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                continue
            reopened.set()
            adopted += 1
        return adopted

    async def _retire(self, record: CommandRecord) -> None:
        """Stop a command nothing can report on and mark its record settled."""
        await self._terminate(record, None)
        record.settled = True
        await asyncio.to_thread(self._write_record, record)

    def begin_shutdown(self) -> None:
        """Refuse new commands and leave running ones for the next boot to re-adopt."""
        self._stopping = True

    async def stop(self) -> None:
        self._stopping = True
        tasks = list(self._supervisors.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # --- records ---

    def _write_record(self, record: CommandRecord) -> None:
        folder = self._folder(record.run_id)
        folder.mkdir(parents=True, exist_ok=True)
        # The leaf is agent-readable, and a session key authorizes internal calls:
        # the owner is taken back from the run on re-adoption instead.
        stored = {k: v for k, v in asdict(record).items() if k not in _UNSTORED_FIELDS}
        atomic_write(folder / _RECORD_FILE, json.dumps(stored), restrict_to_owner=True)

    def _load_unsettled(self) -> list[CommandRecord]:
        """Read every unsettled record, pruning settled folders past retention."""
        self._prune_settled()
        records: list[CommandRecord] = []
        for folder in self._run_folders():
            record = self._read_record(folder)
            if record is not None and not record.settled:
                records.append(record)
        return records

    def _prune_settled(self) -> None:
        """Delete folders of settled (or unreadable) runs older than retention."""
        cutoff = self._clock() - _RETENTION_SECS
        for folder in self._run_folders():
            if folder.name in self._supervisors:
                continue
            record = self._read_record(folder)
            if record is not None and not record.settled:
                continue
            try:
                if folder.stat().st_mtime < cutoff:
                    shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                pass

    def _run_folders(self) -> list[Path]:
        try:
            entries = list(self.root.iterdir())
        except OSError:
            return []
        return [
            entry
            for entry in entries
            if _RUN_ID_RE.match(entry.name) and entry.is_dir() and not entry.is_symlink()
        ]

    @staticmethod
    def _read_record(folder: Path) -> Optional[CommandRecord]:
        try:
            raw = json.loads((folder / _RECORD_FILE).read_text(encoding="utf-8"))
            for field_name in _UNSTORED_FIELDS:
                raw.pop(field_name, None)
            record = CommandRecord(session_key="", **raw)
        except (OSError, ValueError, TypeError):
            return None
        if record.run_id != folder.name:
            return None
        return record

"""Single-writer guard for a ``KIROCREW_HOME``.

Two ``kirocrew gateway`` processes bound to the same home each open the same
``sessions/*.jsonl`` as ``ConversationLog`` writers. The steady-save fast path
assumes a single writer per file, so the stale process's shutdown flush rolls
back newer on-disk content -- the dual-writer clobber that loses transcripts.

This module enforces the single-writer invariant at the source: the gateway
acquires an exclusive advisory ``flock`` on ``<home>/gateway.lock`` at startup
and holds it for the process lifetime. A second gateway on the same home is
refused.

What ``flock`` does and does not guarantee
-----------------------------------------
``flock`` belongs to the open file *description*, so it is immune to a hazard
that would otherwise be fatal here: an unrelated ``open()`` + ``close()`` of the
lock path elsewhere in this process cannot release it. That matters because the
gateway serves authenticated file reads over HTTP, and the lock file is inside
the home those reads can reach. A POSIX record lock (``fcntl.lockf``) is keyed by
(process, inode) instead, so one such read would silently drop this guard and let
a second gateway start -- exactly the corruption this module exists to prevent.
``flock`` is chosen deliberately for that reason.

The cost of that choice is the other half of the same property. ``fork()`` shares
one description between parent and child, so a forked child that inherits this fd
keeps the lock alive after the parent dies. A child that wedges before ``exec``
therefore pins the home: every later start is refused, and the pid recorded in
the file names a process that no longer exists. This occurs when ``preexec_fn``
forces a plain ``fork()`` of the multi-threaded gateway in
:mod:`kiro_crew.kiro_prerequisite`.

The remedy is to stop creating such children, not to weaken this guard;
``_run_process`` therefore does not use ``preexec_fn``. What this
module owes the operator meanwhile is an honest refusal: it resolves the process
that ACTUALLY holds the lock from ``/proc/*/fd`` rather than quoting the pid in
the file, reports what that process looks like, and names the reclaim command
when the evidence points at an inherited fd. It never kills anything itself.

Isolated homes (``--test-mode``/``--seed`` with a distinct ``KIROCREW_HOME``)
resolve to a different lock file and are unaffected.
"""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

LOCK_FILENAME = "gateway.lock"


class GatewayLockError(RuntimeError):
    """Raised when another process already owns this ``KIROCREW_HOME``."""

    def __init__(self, home: Path, holder_pid: int | None, diagnosis: str | None = None) -> None:
        self.home = home
        self.holder_pid = holder_pid
        self.diagnosis = diagnosis
        if diagnosis:
            super().__init__(diagnosis)
            return
        if holder_pid is not None:
            detail = f"another gateway (pid {holder_pid}) already owns {home}"
        else:
            detail = f"another gateway already owns {home}"
        super().__init__(f"{detail}; stop it first or set KIROCREW_HOME to an isolated directory")


class GatewayLock:
    """Process-lifetime exclusive lock on a single ``KIROCREW_HOME``.

    Usable as a context manager or via explicit ``acquire()`` / ``release()``.
    The lock is advisory (``flock``) and scoped to the lock file's inode, so it
    works across bind mounts (e.g. a jailed gateway) but not across hosts/NFS --
    matching the single-host scope of ``KIROCREW_HOME``.

    *port* is diagnostic only. When given, a refusal reports whether the holder
    also owns the dashboard port and whether that port answers, which is what
    separates a running gateway from a wedged fork squatting on an inherited fd.
    """

    def __init__(self, home: Path, port: int | None = None) -> None:
        self._home = home
        self._path = home / LOCK_FILENAME
        self._port = port
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> "GatewayLock":
        """Take the exclusive lock or raise ``GatewayLockError``.

        Fail-closed: any inability to take the lock refuses startup rather than
        proceeding as a second writer.
        """
        self._home.mkdir(parents=True, exist_ok=True)
        # O_RDWR | O_CREAT without truncation: a failed acquire must leave the
        # incumbent holder's pid intact so we can name it in the error.
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        # platform_compat.try_acquire_lock: fcntl.flock LOCK_EX|LOCK_NB on
        # POSIX; msvcrt.locking LK_NBLCK on Windows. Returns True iff acquired.
        if not platform_compat.try_acquire_lock(fd, exclusive=True):
            recorded = _read_pid(fd)
            os.close(fd)
            holder, diagnosis = self._diagnose(recorded)
            raise GatewayLockError(self._home, holder, diagnosis)

        # We hold the lock. Stamp our pid over whatever was there so the file
        # keeps naming the most recent acquirer.
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{os.getpid()}\n".encode())
            os.fsync(fd)
        except OSError:
            # The lock itself is held (the invariant we care about); a failure to
            # record the pid only degrades the diagnostic message. Keep the lock.
            logger.warning("acquired gateway lock on %s but could not record pid", self._home)

        self._fd = fd
        logger.info("acquired gateway singleton lock on %s (pid %d)", self._home, os.getpid())
        return self

    def release(self) -> None:
        """Release the lock if held. Idempotent."""
        if self._fd is None:
            return
        platform_compat.release_lock(self._fd)
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None

    def __enter__(self) -> "GatewayLock":
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()

    # -- diagnostics ------------------------------------------------------

    def _diagnose(self, recorded_pid: int | None) -> tuple[int | None, str | None]:
        """Resolve who holds the lock, distinguishing owner from mere opener.

        ``/proc/locks`` names the pid that ACQUIRED the flock, authoritatively.
        That pid can be dead: an ``flock`` belongs to the open file description,
        so it survives in a forked child while the kernel keeps reporting the
        dead acquirer. In that case no ``/proc`` surface names the inheritor, so
        we list the current openers as CANDIDATES and never as the owner.

        Returns ``(pid, message)``. ``message`` is ``None`` when we learned
        nothing, which leaves :class:`GatewayLockError` on its generic wording.
        """
        owner = platform_compat.flock_owner_pid(self._path)
        openers = platform_compat.pids_holding_file(self._path)
        if openers is not None:
            openers = [pid for pid in openers if pid != os.getpid()]

        if owner is not None and platform_compat.pid_exists(owner):
            return owner, self._describe_live_owner(owner, recorded_pid)
        if owner is not None:
            return owner, self._describe_orphaned_lock(owner, openers)
        # No /proc/locks (non-Linux, or unreadable): the recorded pid is all we
        # have. Say that, rather than presenting it as the proven holder.
        if recorded_pid is None:
            return None, None
        return recorded_pid, (
            f"{self._path} is locked, but the holder could not be identified. "
            f"The file records pid {recorded_pid}, which may be stale. "
            "Stop the running gateway, or set KIROCREW_HOME to an isolated directory."
        )

    def _describe_live_owner(self, pid: int, recorded_pid: int | None) -> str:
        """The ordinary case: a live process holds the lock, so name it."""
        facts: list[str] = []
        threads = platform_compat.process_thread_count(pid)
        if threads is not None:
            facts.append(f"{threads} thread{'s' if threads != 1 else ''}")
        facts.extend(self._port_facts(pid))
        detail = f" ({', '.join(facts)})" if facts else ""
        message = (
            f"{self._path} is held by pid {pid}{detail} -- another gateway already owns "
            f"{self._home}; stop it first (kirocrew stop) or set KIROCREW_HOME to an "
            "isolated directory"
        )
        if recorded_pid is not None and recorded_pid != pid:
            message += f" (the lock file records pid {recorded_pid} -- stale)"
        return message

    def _describe_orphaned_lock(self, dead_owner: int, openers: list[int] | None) -> str:
        """The wedge: the acquirer is gone but its flock lives on in an inheritor.

        This is the state that leaves an orphaned lock. It is worth naming
        precisely, because the pid the operator would otherwise reach for -- the
        one in the lock file, and the one ``/proc/locks`` reports -- is the dead
        parent, and killing it does nothing.

        The inheritor cannot be proven from ``/proc``: openers may include
        processes that merely read the file. A reclaim command is therefore
        offered only when THREE independent facts line up -- exactly one
        candidate, that candidate's own parent is gone, and it is not serving
        HTTP -- and the message states each one, so the operator is checking
        evidence rather than trusting a verdict.

        Any pid in a printed command is a snapshot, and the operator reads it
        seconds later; that gap is unavoidable for any tool that names a process,
        ``lsof`` included. The corroborating facts are what make the pid worth
        acting on, so they are printed with it.
        """
        lines = [
            f"{self._path} is locked, but the process that acquired it (pid {dead_owner}) "
            "no longer exists. An flock belongs to the open file description, so it "
            "survives in a process that inherited that descriptor -- typically a child "
            "forked from the crashed gateway."
        ]
        if not openers:
            lines.append(
                "No current opener of the file could be identified, so the inheritor "
                "cannot be named here. Find it with: lsof " + str(self._path)
            )
            return " ".join(lines)
        described = []
        for pid in openers:
            threads = platform_compat.process_thread_count(pid)
            bits = [f"{threads} thread{'s' if threads != 1 else ''}"] if threads else []
            bits.extend(self._port_facts(pid))
            described.append(f"pid {pid}" + (f" ({', '.join(bits)})" if bits else ""))
        lines.append("The file is currently open in " + ", ".join(described) + ".")
        if len(openers) > 1:
            lines.append(
                "More than one process has it open, so the inheritor is ambiguous -- "
                "confirm which is a child of the dead gateway before killing anything."
            )
            return " ".join(lines)

        candidate = openers[0]
        ppid = platform_compat.parent_pid(candidate)
        orphaned = ppid is not None and (ppid == 1 or not platform_compat.pid_exists(ppid))
        serving = self._port is not None and _port_answers_http(self._port)
        if orphaned and not serving:
            lines.append(
                f"Its parent (pid {ppid}) is gone too and it is not serving HTTP, so it is "
                f"the likely inheritor; reclaim the home with: kill -9 {candidate}"
            )
        elif serving:
            lines.append(
                f"But pid {candidate} IS serving HTTP on port {self._port}, so it is a live "
                "gateway, not a wedged leftover -- stop it with kirocrew stop instead."
            )
        else:
            parent = "unknown" if ppid is None else f"pid {ppid}, still alive"
            lines.append(
                f"Its parent is {parent}, so it may be a healthy gateway that has just "
                f"started and not yet bound its port. Confirm before killing it: ps -f -p "
                f"{candidate}"
            )
        return " ".join(lines)

    def _port_facts(self, pid: int) -> list[str]:
        """Port ownership facts for *pid*, empty when no port was supplied."""
        if self._port is None:
            return []
        if pid not in platform_compat.find_listening_pids(self._port):
            return [f"does not hold port {self._port}"]
        answering = "answering" if _port_answers_http(self._port) else "not answering"
        return [f"holds port {self._port}, {answering} HTTP"]


def _port_answers_http(port: int, timeout: float = 1.5) -> bool:
    """True iff ``127.0.0.1:port`` returns an HTTP status line within *timeout*.

    A plain connect is not enough: a wedged holder's kernel still completes the
    handshake into the listen backlog even though nothing will ever ``accept()``,
    so connect-success would misclassify an orphan as a live gateway. This
    mirrors :func:`kiro_crew.dashboard.port_reclaim._probe_gateway_healthy` in a
    synchronous form, because the lock is taken before the event loop exists.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            return sock.recv(5) == b"HTTP/"
    except OSError:
        return False


def _read_pid(fd: int) -> int | None:
    """Best-effort read of the holder pid recorded in the lock file."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 64).decode(errors="replace").strip()
    except OSError:
        return None
    try:
        return int(raw) if raw else None
    except ValueError:
        return None

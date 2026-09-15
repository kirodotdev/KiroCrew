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
from dataclasses import dataclass
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


class LockProbeError(RuntimeError):
    """Raised by :func:`lock_holder` when the lock's state cannot be established.

    Two cases. The lock file exists but the non-destructive probe could not
    OPEN it (an ``OSError`` from ``os.open``), so whether a gateway holds the
    lock is INDETERMINATE -- a failure inside the lock call itself is not one
    of them: ``platform_compat.try_acquire_lock`` folds that into ``False``,
    which the probe reads as held, the conservative answer. Or the probe says
    held but nothing can name a LIVE holder (the recorded acquirer is gone, or
    the file records no live pid). Neither is folded into "free" or into a
    named holder: "free" would let a caller spawn a second writer, and naming
    the pid recorded in the file -- a number ``release`` never clears and the
    kernel reuses -- would let a caller signal an unrelated live process.
    Callers that act on the answer (``kirocrew stop``/``restart``) report this
    and exit without signalling anything.
    """

    def __init__(self, path: Path, cause: OSError) -> None:
        self.path = path
        self.cause = cause
        super().__init__(f"could not determine whether a gateway holds the lock at {path}: {cause}")


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

        # Windows stale-PID reclaim: on Windows, msvcrt.locking may leave a
        # lock file that cannot be re-locked after a crash (the OS does not
        # guarantee release on abnormal termination the way POSIX flock does).
        # If the recorded PID is confirmed dead, delete the stale file and
        # re-open a fresh one so try_acquire_lock sees a clean state.
        if platform_compat.IS_WINDOWS:
            stale_pid = _read_pid(fd)
            if (
                stale_pid is not None
                and platform_compat.pid_liveness(stale_pid) == platform_compat.PID_DEAD
            ):
                logger.info(
                    "reclaiming stale gateway lock on %s (dead pid %d)",
                    self._home,
                    stale_pid,
                )
                os.close(fd)
                try:
                    os.unlink(self._path)
                except OSError:
                    pass
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


@dataclass(frozen=True)
class LockHolder:
    """Who holds ``<home>/gateway.lock`` right now, per :func:`lock_holder`.

    ``pid`` is ``None`` when nothing holds the lock (no split-brain refusal to
    diagnose). A named ``pid`` is always a LIVE process (``alive`` is ``True``
    for it): :func:`lock_holder` reports a held lock whose acquirer is gone --
    the orphaned-flock wedge :class:`GatewayLock` diagnoses in prose
    (``_describe_orphaned_lock``) -- as indeterminate (:class:`LockProbeError`)
    rather than as a dead holder, so no caller can read it as nobody running.
    ``source`` says which surface produced the pid.
    """

    pid: int | None
    alive: bool
    source: str  # "flock_owner" | "recorded_pid" | "none"


_NO_HOLDER = LockHolder(pid=None, alive=False, source="none")


def lock_holder(home: Path) -> LockHolder:
    """Non-destructive oracle: who (if anyone) holds ``<home>/gateway.lock``.

    Shares its resolution logic with :meth:`GatewayLock._diagnose` so a caller
    that never intends to ACQUIRE the lock -- ``cli_perf``'s profiler target,
    and ``_stop``/``_restart``'s port-probe fallback -- can still ask who owns a
    home without opening it for writing first. A missing lock file reports
    ``pid=None, source="none"``, the same "nothing to diagnose" shape as no
    lock existing at all; a file that exists but cannot be read is still
    probed, since being unable to read it is not evidence that nobody holds it.

    The file's contents are NOT evidence on their own. ``acquire`` stamps the
    holder's pid but ``release`` never clears it, so after a clean stop the file
    keeps naming a dead pid -- and pids are reused, so that number can later
    name an unrelated live process (on macOS and Windows, where ``/proc/locks``
    does not exist, that would be the ONLY source consulted). ``_diagnose`` is
    safe from this because it only ever runs after an acquire has failed, when
    the lock is known to be held; this oracle has no such precondition, so it
    establishes it first with a non-destructive probe: try the lock
    non-blockingly, release at once if it was free. A free lock is ``pid=None,
    source="none"`` whatever the file says.

    A ``LockHolder`` naming a pid is returned only on POSITIVE ownership of a
    LIVE process: either ``/proc/locks`` names a live acquirer
    (``source="flock_owner"``, whether or not the probe answered), or the probe
    positively says held AND the pid recorded in the file is alive
    (``source="recorded_pid"``, the non-Linux fallback). A named holder is
    therefore always ``alive=True``; the field stays so callers read one shape. A recorded pid alone
    cannot outrank the authoritative source, since a forked inheritor keeps the
    flock alive under a DIFFERENT pid than the one last written to the file,
    and a recorded pid that is dead is reported as nobody rather than as a
    holder.

    Raises :class:`LockProbeError` when the probe cannot answer and
    ``/proc/locks`` does not name a live acquirer either, and also when the
    probe says HELD but nothing can name a LIVE holder: the kernel names an
    acquirer that is gone (the forked-inheritor wedge, where a child keeps the
    flock alive under a pid nothing records), or neither the kernel nor the
    file names a live pid (the non-Linux case of an unreadable or stale pid).
    These states are
    indeterminate, not "held" and not "free": "free" would let a caller spawn a
    second writer, and a "held" that names the recorded pid would point at
    whatever process happens to carry that number now. Liveness is always checked fresh: a caller acting on this a
    moment later still wants that gap kept as small as the check itself, not
    stretched by an extra round trip.
    """
    path = home / LOCK_FILENAME
    if not path.exists():
        return _NO_HOLDER
    recorded: int | None = None
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        # A file that exists but cannot be opened for reading (a Windows
        # mandatory lock held by the gateway, a permission gap) is not
        # evidence of "nobody": the probe below still decides held or free,
        # and a held lock with no readable pid is reported as indeterminate.
        pass
    else:
        try:
            recorded = _read_pid(fd)
        finally:
            os.close(fd)

    try:
        held = _lock_is_held(path)
    except LockProbeError:
        # The probe could not answer. The one thing that still counts as
        # positive ownership is the kernel naming a LIVE acquirer.
        owner = platform_compat.flock_owner_pid(path)
        if owner is not None and platform_compat.pid_exists(owner):
            return LockHolder(pid=owner, alive=True, source="flock_owner")
        raise

    if not held:
        return _NO_HOLDER

    owner = platform_compat.flock_owner_pid(path)
    if owner is not None:
        if platform_compat.pid_exists(owner):
            return LockHolder(pid=owner, alive=True, source="flock_owner")
        # Held, and the kernel names an acquirer that is gone: the flock lives
        # on in a process that inherited the descriptor (a forked child), which
        # nothing here can name. Reporting the dead pid as "not alive" reads to
        # stop and restart as nobody running, and restart then spawns a
        # replacement straight into the held lock. Indeterminate instead.
        raise LockProbeError(
            path,
            OSError(f"the lock is held but its recorded acquirer (pid {owner}) is gone"),
        )
    if recorded is not None and recorded > 0 and platform_compat.pid_exists(recorded):
        return LockHolder(pid=recorded, alive=True, source="recorded_pid")
    # Positively held, yet nothing names the holder: the kernel has no owner
    # surface here and the file records no live pid (unreadable under a
    # mandatory lock, garbage, or a dead pid left by a forked inheritor). That
    # is a running holder this process cannot identify, so it is indeterminate
    # rather than "nobody": "nobody" would make stop and restart report nothing
    # running while `kirocrew gateway` refuses to start on the very same lock.
    raise LockProbeError(
        path, OSError("the lock is held but no live holder pid can be established")
    )


def _lock_is_held(path: Path) -> bool:
    """True when something holds the lock at *path* (i.e. a gateway is running).

    Non-destructive: the acquire is only a probe and is released at once, so a
    real holder is never disturbed. An ``OSError`` from OPENING the file raises
    :class:`LockProbeError` instead of being folded into either answer: the
    callers of :func:`lock_holder` act on the answer (refuse naming the pid,
    profile it), so a false "free" would let them spawn a second writer. A
    failure inside the lock call itself is folded by
    ``platform_compat.try_acquire_lock`` into ``False``, which reads here as
    "held": the conservative answer, since every caller treats a held lock as
    a reason to refuse rather than to act on the recorded pid.

    The probe window itself is the one this process could win a race against a
    gateway acquiring at the same instant; it is microseconds wide and the
    gateway's own refusal names this pid, which is the same exposure the
    ``cli_perf`` probe carries.
    """
    fd: int | None = None
    try:
        fd = os.open(path, os.O_RDWR)
        if platform_compat.try_acquire_lock(fd, exclusive=True):
            platform_compat.release_lock(fd)
            return False
        return True
    except OSError as exc:
        raise LockProbeError(path, exc) from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


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

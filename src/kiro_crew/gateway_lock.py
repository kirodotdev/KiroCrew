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

Why the home directory is locked too
------------------------------------
A lock file is only as strong as its name. Deleting ``<home>/gateway.lock``
releases nothing -- the incumbent's ``flock`` lives on -- but it does strip the
lock of the one thing that made it a rendezvous: no later gateway can reach that
inode by name, so the next start creates a FRESH inode, locks that unopposed,
and runs as a second writer on the same home. Neither a zombie nor an inherited
descriptor is needed; a healthy running gateway is enough. Operators reach that
state by following recovery advice that says to remove the file.

No check inside the second gateway can see the first one's orphaned inode, so
the fix is a rendezvous that deletion cannot reach: the home DIRECTORY. A
directory cannot be unlinked while it holds entries, so an exclusive ``flock``
on the home survives every deletion of what is inside it, and the second
gateway is refused on it. The lock file keeps its own ``flock`` unchanged --
it carries the pid stamp, the Windows path, and the non-destructive probe in
:func:`lock_holder` -- and is additionally checked for identity after locking,
so a name replaced mid-acquire is never mistaken for a held rendezvous.

A filesystem that positively demonstrates it cannot lock a directory yields no
anchor verdict and does not refuse a legitimate start. Failure to measure that
capability is indeterminate and fails closed. Windows does not need the anchor
at all, because a Windows gateway's open lock file cannot be deleted out from
under it.

Isolated homes (``--test-mode``/``--seed`` with a distinct ``KIROCREW_HOME``)
resolve to a different lock file and are unaffected.
"""

from __future__ import annotations

import errno
import ipaddress
import logging
import os
import socket
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

LOCK_FILENAME = "gateway.lock"

#: Process exit status for the one lock refusal a restart cannot heal: the
#: lock is held by a running process, positively identified as its acquirer,
#: that holds the configured dashboard port with its OWN socket at the
#: address this gateway is configured to bind and answers HTTP there -- the
#: serving-holder predicate ``GatewayLock._serving_verdict`` answering True,
#: carried as ``GatewayLockError.live_holder`` -- a sibling gateway serving
#: this home.
#: That condition stands for as long as the sibling serves, so a supervised
#: relaunch meets the identical refusal every time while the sibling keeps
#: serving; there is nothing for a second gateway to do here.
#: ``service/linux.py`` feeds this value into the unit's
#: ``RestartPreventExitStatus`` so a ``Restart=always`` unit goes ``failed``
#: once, with the refusal line in the journal, instead of relaunching every
#: ``RestartSec`` against a home that is already served. Every other refusal
#: keeps exit 1 and IS relaunched, because a later attempt can find it cleared
#: or because the evidence for standing down is not there: a lock file
#: replaced faster than it can be locked, a home that cannot be opened or
#: measured for directory locks, an flock whose acquirer is gone (a wedged
#: inheritor holds it until that process dies), a live acquirer that does not
#: hold the port (a sibling still starting, or one shutting down that has
#: closed its listener and will release the lock next), a live acquirer whose
#: own socket at the probed address is silent (a wedged gateway: a hung
#: process keeps its listening socket, and the relaunch is what takes the home
#: over once it dies), a live acquirer that holds the port only at another
#: address than this one would bind or at one the platform did not report (the
#: residual row this design leaves unasserted on purpose, so a stranger
#: answering at the probed address is never credited to it), and a holder no
#: surface could identify -- no ``/proc/locks`` (macOS, Windows), or a Linux
#: filesystem whose device numbers never match the lock table (btrfs
#: subvolumes, overlayfs) -- where the recorded pid may be alive and on the
#: port and still be a reused number rather than the process that holds the
#: lock.
#:
#: 78 is ``EX_CONFIG`` from ``sysexits.h`` ("unconfigured or misconfigured
#: state"): two supervisors pointed at one home is a host configuration, and
#: the remedy is to change it -- stop the sibling or isolate ``KIROCREW_HOME``
#: -- never to try again. Distinct from the two statuses the RUNNING gateway
#: exits with on purpose to be relaunched, 69 (``EX_UNAVAILABLE``, listener
#: lost) and 75 (``EX_TEMPFAIL``, stale assets), so a journal histogram tells
#: the three apart.
#:
#: Lives here rather than in ``cli`` because ``cli`` returns the value while
#: ``service/linux`` exempts it, and both import this module -- one
#: definition, so the code that exits and the unit that exempts cannot drift.
LIVE_HOLDER_EXIT_CODE = 78

# How many times ``acquire`` re-opens the lock path and re-takes the home
# anchor, and how many times :func:`_anchor_holder_or_nobody` re-probes an
# anchor that read as held. It bounds two transient causes: the inode
# ``acquire`` locked differing from the inode the path names, and the home
# anchor being held for the two syscalls :func:`_home_is_anchored` needs for
# its non-destructive probe. Either clearing takes microseconds, while a real
# incumbent holds the anchor for its whole lifetime and fails every attempt, so
# the ceiling is small and the refusal is loud. Each retry costs one open, one
# flock and one short wait.
_IDENTITY_ATTEMPTS = 3

# Wait before each retry. Long enough that a probe holding the anchor has
# released it, short enough that the whole ceiling stays under a tenth of a
# second on a startup path that already does disk I/O.
_RETRY_BACKOFF_SECS = 0.05


class _DirectoryLockSupport(Enum):
    """Measured support for locking a directory on the home filesystem."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    INDETERMINATE = "indeterminate"


# The errnos with which ``flock`` says a filesystem has no directory locks at
# all. Only these may downgrade a held-home refusal to "no verdict". Every other
# failure -- ENOLCK when the lock table is full, EIO, EWOULDBLOCK on a directory
# nobody else can open -- is a measurement that did not happen, not a
# measurement of "unsupported", and a start on a held home must not ride on it.
_NO_DIRECTORY_LOCK_ERRNOS = frozenset(
    code
    for code in (
        getattr(errno, "EINVAL", None),
        getattr(errno, "EOPNOTSUPP", None),
        getattr(errno, "ENOTSUP", None),
    )
    if code is not None
)


class GatewayLockError(RuntimeError):
    """Raised when another process already owns this ``KIROCREW_HOME``.

    ``live_holder`` is the serving-holder predicate's answer,
    :attr:`_ServingVerdict.serving` from :meth:`GatewayLock._serving_verdict`:
    True only when the refusal rests on a POSITIVELY identified acquirer -- the
    process ``/proc/locks`` names as holding the flock (on the lock file, or on
    the home directory when the file has been deleted or replaced) -- that is
    running, holds the configured dashboard port with its OWN socket at the
    address this gateway is configured to bind, AND answers HTTP there: a
    gateway serving this home, which this one can displace on neither front
    while it lives. It is the structured form of that verdict, so a caller
    deciding how to exit reads it rather than the message text. Every other
    refusal leaves it False: a transient identity race, an indeterminate home
    anchor, an flock whose acquirer is gone, a live acquirer that does not hold
    the port (a sibling still starting or already shutting down -- a later
    attempt resolves either), a live acquirer whose own socket at the probed
    address is silent (a wedged gateway: a hung process keeps its socket bound,
    and the retry is what takes the home over once it dies), a live acquirer
    that holds the port only at another address than this one would bind, or
    at one the platform did not report (the residual row, kept unasserted by
    design, so a stranger answering at the probed address is never credited to
    it), and every refusal in which no surface names the acquirer (no
    ``/proc/locks``, or a filesystem it never matches), where the recorded pid
    may be alive and on the port yet proves nothing about who holds the lock,
    so the predicate is never asked.
    """

    def __init__(
        self,
        home: Path,
        holder_pid: int | None,
        diagnosis: str | None = None,
        *,
        live_holder: bool = False,
    ) -> None:
        self.home = home
        self.holder_pid = holder_pid
        self.diagnosis = diagnosis
        self.live_holder = live_holder
        if diagnosis:
            super().__init__(diagnosis)
            return
        if holder_pid is not None:
            detail = f"another gateway (pid {holder_pid}) already owns {home}"
        else:
            detail = f"another gateway already owns {home}"
        super().__init__(f"{detail}; stop it first or set KIROCREW_HOME to an isolated directory")


#: The address the serving-holder predicate probes when the configured bind is
#: absent or the IPv4 wildcard: loopback is what an unconfigured gateway binds,
#: and a ``0.0.0.0`` listener receives a connect to it.
_LOOPBACK_PROBE_HOST = "127.0.0.1"
#: The address probed for an IPv6-wildcard bind. The dashboard binds ``::`` with
#: ``IPV6_V6ONLY`` (``dashboard/server.py::_bind_once``), so that listener never
#: receives an IPv4 connect: probing 127.0.0.1 would call a healthy IPv6
#: sibling silent. ``::1`` is what a ``::`` listener does answer.
_V6_LOOPBACK_PROBE_HOST = "::1"

# Bind values that mean "every interface" of one family, keyed by the address the
# probe reaches them at. Family is load-bearing (see above): the two wildcards
# are probed at different loopbacks.
_WILDCARD_PROBE_HOSTS = {
    "": _LOOPBACK_PROBE_HOST,
    "0.0.0.0": _LOOPBACK_PROBE_HOST,
    "::": _V6_LOOPBACK_PROBE_HOST,
}


def probe_host_for_bind(bind_address: str | None) -> str:
    """The address at which to ask a holder of the port whether it serves HTTP.

    The serving-holder predicate (:meth:`GatewayLock._serving_verdict`) probes
    the address THIS gateway is configured to bind (``KIROCREW_BIND``, resolved
    by the caller), never one it guesses. An absent configuration or the IPv4
    wildcard ``0.0.0.0`` is probed at ``127.0.0.1``; the IPv6 wildcard ``::`` at
    ``::1``, because the dashboard binds it ``IPV6_V6ONLY`` and a v4 connect
    would never reach it; a specific address is probed as given (brackets
    stripped, so an IPv6 literal such as ``[::1]`` connects). The residual this
    leaves -- a holder bound to some OTHER address is not reached here and is
    therefore not asserted as serving -- is deliberate: the assertion stays as
    narrow as the evidence, and mapping the probe to the holder's own listener
    address is tracked as a follow-up rather than guessed at.
    """
    host = (bind_address or "").strip().strip("[]")
    return _WILDCARD_PROBE_HOSTS.get(host, host)


def _listener_reaches(listener: platform_compat.PortListener, host: str) -> bool:
    """Would a connect to *host* on the listener's port land on THIS socket?

    The address-bound half of the port conjunct. *host* is the probe address
    (:func:`probe_host_for_bind`), so this answers whether the identified
    owner's own LISTEN socket is the one the HTTP probe reaches -- without it,
    port ownership (address-agnostic) and HTTP health (measured at one address)
    could belong to two different processes sharing a port on different local
    addresses, and a stranger's health would be attributed to the lock owner.

    A specific bind reaches only its own address (a v4-mapped spelling of a v4
    address counts as that address). The IPv4 wildcard reaches every v4 host.
    The IPv6 wildcard reaches every v6 host and NOT a v4 one: the dashboard
    binds it ``IPV6_V6ONLY``, and whether some other process's ``::`` socket
    accepts v4-mapped connects cannot be observed from here, so it is not
    assumed. lsof spells either family's wildcard ``*`` and reports the family
    separately; with the family unreported, and whenever the source gave no
    address at all, the answer is unknowable and therefore False -- the
    conjunct is never asserted on a fact the platform did not supply.
    """
    try:
        target = ipaddress.ip_address(host)
    except ValueError:
        return False
    v6_host = target.version == 6
    address = listener.address.strip().strip("[]").lower()
    if address.startswith("::ffff:"):
        address = address[len("::ffff:") :]
    if not address:
        return False
    if address == "*":
        if listener.family == "4":
            return not v6_host
        if listener.family == "6":
            return v6_host
        return False
    if address == "0.0.0.0":
        return not v6_host
    if address == "::":
        return v6_host
    try:
        bound = ipaddress.ip_address(address)
    except ValueError:
        return False
    return bound == target


@dataclass(frozen=True)
class _ServingVerdict:
    """The serving-holder predicate's answer about ONE positively identified acquirer.

    Produced only by :meth:`GatewayLock._serving_verdict`, and only for the pid
    ``/proc/locks`` names as the flock's ACQUIRER (of the lock file, or of the
    home directory when the file has been deleted or replaced) -- never for the
    pid the lock file merely records. Each fact is measured once and the message
    and the exit status both read this record, so they cannot disagree.

    ``serving`` is the conjunction a supervisor may act on: the acquirer is
    alive, holds the CONFIGURED dashboard port, holds it AT the address this
    gateway is configured to bind (``probe_host``; wildcards covering it by
    family, see :func:`_listener_reaches`), and answers HTTP there.
    ``holds_port`` is False when no port was supplied to measure (``--port
    auto``, ``--slack-only``); ``listens_at_probe_host`` is False when the
    platform reported no address for the owner's sockets (unknowable, so not
    asserted); ``answers_http`` is probed only when the owner's own socket is
    the one the probe would reach, so one predicate call spends at most one
    HTTP probe and never attributes another process's answer to the owner. The
    identified-acquirer diagnoses call it exactly once; the orphaned-lock
    diagnosis (a dead acquirer, candidate openers) is a separate path with its
    own per-candidate facts.
    ``bound_addresses`` are the owner's listener addresses, for the message.
    """

    pid: int
    alive: bool
    holds_port: bool
    listens_at_probe_host: bool
    answers_http: bool
    probe_host: str
    bound_addresses: tuple[str, ...] = ()

    @property
    def serving(self) -> bool:
        """True iff every conjunct holds -- the one shape that exits terminally."""
        return self.alive and self.holds_port and self.listens_at_probe_host and self.answers_http

    @property
    def silent_listener(self) -> bool:
        """Alive, on the port at the probed address, yet nothing answered there.

        A wedged gateway: a hung process keeps its listening socket bound, so
        the socket the probe reaches is the owner's and still nothing answers.
        Not asserted -- the retry is what takes the home over once it dies, and
        a terminal exit would leave the unit ``failed`` with nothing to relaunch.
        """
        return (
            self.alive and self.holds_port and self.listens_at_probe_host and not self.answers_http
        )

    @property
    def bound_elsewhere(self) -> bool:
        """The RESIDUAL row: alive and on the port, but not at the probed address.

        The owner holds port N only on some other local address (or the
        platform could not say where), so the probe never reaches its socket
        and whatever answers at ``probe_host`` is not evidence about it. Not
        asserted, by design: ``live_holder`` False, exit 1, the pre-PR
        behaviour bounded by the unit's ``StartLimit*``. Probing the holder's
        own address instead is the tracked follow-up, not a guess made here.
        """
        return self.alive and self.holds_port and not self.listens_at_probe_host


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

    def __init__(
        self, home: Path, port: int | None = None, bind_address: str | None = None
    ) -> None:
        self._home = home
        self._path = home / LOCK_FILENAME
        self._port = port
        # The address the serving-holder predicate probes: the one THIS gateway
        # is configured to bind, a wildcard mapped to loopback. Fixed at
        # construction so every fact in one diagnosis is measured at one address.
        self._probe_host = probe_host_for_bind(bind_address)
        self._fd: int | None = None
        self._home_fd: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> "GatewayLock":
        """Take the exclusive lock or raise ``GatewayLockError``.

        Fail-closed: any inability to take the lock refuses startup rather than
        proceeding as a second writer. Two things are locked, for two different
        failure modes -- the lock file, which carries the pid stamp and the
        Windows path, and the home directory, which stays reachable by name
        after the lock file is deleted (see the module docstring).
        """
        self._home.mkdir(parents=True, exist_ok=True)
        for attempt in range(_IDENTITY_ATTEMPTS):
            if attempt:
                time.sleep(_RETRY_BACKOFF_SECS)
            fd = self._open_lock_file()
            # platform_compat.try_acquire_lock: fcntl.flock LOCK_EX|LOCK_NB on
            # POSIX; msvcrt.locking LK_NBLCK on Windows. Returns True iff acquired.
            if not platform_compat.try_acquire_lock(fd, exclusive=True):
                recorded = _read_pid(fd)
                os.close(fd)
                if (
                    not platform_compat.IS_WINDOWS
                    and attempt + 1 < _IDENTITY_ATTEMPTS
                    and recorded is not None
                    and platform_compat.pid_liveness(recorded) == platform_compat.PID_DEAD
                ):
                    # POSIX releases flock when the final inherited descriptor
                    # closes, but a desktop relaunch can race that teardown.
                    # Retry only when the stamp's process is confirmed dead: a
                    # live incumbent must still be refused immediately. A true
                    # orphaned-flock wedge remains held through every bounded
                    # attempt and receives the existing diagnosis below.
                    continue
                holder, diagnosis, live = self._diagnose(recorded)
                raise GatewayLockError(self._home, holder, diagnosis, live_holder=live)
            if not _is_same_file(fd, self._path):
                # The path was unlinked or replaced between the open and the
                # lock, so the inode we hold is not the one the next gateway
                # will open: this lock guards nothing. Drop it and re-open the
                # name that is there now.
                _release_fd(fd)
                continue
            try:
                home_fd = self._acquire_home_anchor()
            except GatewayLockError:
                _release_fd(fd)
                if attempt + 1 < _IDENTITY_ATTEMPTS:
                    # :func:`lock_holder`'s probe takes the same anchor
                    # non-destructively and releases it two syscalls later, so a
                    # ``stop`` or ``restart`` running beside a legitimate start
                    # can refuse it. Retrying separates that from an incumbent,
                    # which holds the anchor until it exits.
                    continue
                raise
            self._stamp_pid(fd)
            self._fd = fd
            self._home_fd = home_fd
            logger.info("acquired gateway singleton lock on %s (pid %d)", self._home, os.getpid())
            return self
        raise GatewayLockError(
            self._home,
            None,
            f"{self._path} is being replaced faster than it can be locked "
            f"({_IDENTITY_ATTEMPTS} attempts); refusing to start rather than hold a lock on "
            "an inode no other gateway will open",
        )

    def release(self) -> None:
        """Release both descriptors if held. Idempotent."""
        if self._home_fd is not None:
            _release_fd(self._home_fd)
            self._home_fd = None
        if self._fd is not None:
            _release_fd(self._fd)
            self._fd = None

    def _open_lock_file(self) -> int:
        """Open (creating if absent) the lock file, reclaiming a stale Windows one."""
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
        return fd

    def _stamp_pid(self, fd: int) -> None:
        """Record this pid in the lock file so it names the most recent acquirer."""
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{os.getpid()}\n".encode())
            os.fsync(fd)
        except OSError:
            # The lock itself is held (the invariant we care about); a failure to
            # record the pid only degrades the diagnostic message. Keep the lock.
            logger.warning("acquired gateway lock on %s but could not record pid", self._home)

    def _acquire_home_anchor(self) -> int | None:
        """Lock the home directory, the rendezvous deleting the lock file cannot reach.

        Returns the held descriptor, or ``None`` on Windows (``msvcrt.locking``
        needs a byte range in a file, and an open Windows lock file cannot be
        deleted anyway) or after positively measuring a filesystem with no
        directory-lock support. A held home or an indeterminate measurement
        raises. The caller retries that raise a bounded number of times, because
        a non-destructive probe holds the same anchor for two syscalls while an
        incumbent holds it for its lifetime.
        """
        if platform_compat.IS_WINDOWS:
            return None
        try:
            fd = os.open(self._home, os.O_RDONLY)
        except OSError as exc:
            raise GatewayLockError(
                self._home, None, _indeterminate_anchor_message(self._home, exc)
            ) from exc
        if platform_compat.try_acquire_lock(fd, exclusive=True):
            return fd
        os.close(fd)
        support, probe_error = _directory_locks_supported(self._home)
        if support is _DirectoryLockSupport.UNSUPPORTED:
            return None
        if support is _DirectoryLockSupport.INDETERMINATE:
            if probe_error is None:
                probe_error = OSError("directory-lock probe supplied no error")
            raise GatewayLockError(
                self._home,
                None,
                _indeterminate_anchor_message(self._home, probe_error),
            ) from probe_error
        holder, diagnosis, live = self._diagnose_replaced_lock_file()
        raise GatewayLockError(self._home, holder, diagnosis, live_holder=live)

    def __enter__(self) -> "GatewayLock":
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()

    # -- diagnostics ------------------------------------------------------

    def _diagnose(self, recorded_pid: int | None) -> tuple[int | None, str | None, bool]:
        """Resolve who holds the lock, distinguishing owner from mere opener.

        ``/proc/locks`` names the pid that ACQUIRED the flock, authoritatively.
        That pid can be dead: an ``flock`` belongs to the open file description,
        so it survives in a forked child while the kernel keeps reporting the
        dead acquirer. In that case no ``/proc`` surface names the inheritor, so
        we list the current openers as CANDIDATES and never as the owner.

        Returns ``(pid, message, live_holder)``. ``message`` is ``None`` when we
        learned nothing, which leaves :class:`GatewayLockError` on its generic
        wording. ``live_holder`` is :attr:`_ServingVerdict.serving` from ONE
        call of the serving-holder predicate, :meth:`_serving_verdict`, on the
        acquirer ``/proc/locks`` names: True only when that process is running,
        holds the configured dashboard port with its own socket at the address
        this gateway is configured to bind, and answers HTTP there. It is False
        on every other row: a dead acquirer (its inheritor may yet exit and
        free the home), a live acquirer without the port, a live acquirer whose
        own socket at the probed address is silent (a wedged gateway), a live
        acquirer that holds the port only at another address or at one the
        platform did not report (the residual row, kept unasserted by design),
        and every shape in which no surface names the acquirer at all -- there
        the recorded pid is a number
        that may be reused, so the predicate is never asked about it and even
        alive-and-on-the-port is a strong hint, not the verdict (see
        :meth:`_describe_unidentified_owner`).
        """
        owner = platform_compat.flock_owner_pid(self._path)
        openers = platform_compat.pids_holding_file(self._path)
        if openers is not None:
            openers = [pid for pid in openers if pid != os.getpid()]

        if owner is not None:
            verdict = self._serving_verdict(owner)
            if verdict.alive:
                return (
                    owner,
                    self._describe_live_owner(owner, recorded_pid, verdict),
                    verdict.serving,
                )
            return owner, self._describe_orphaned_lock(owner, openers), False
        # No /proc/locks (non-Linux, or unreadable) or no entry that matches
        # this file: the recorded pid is all we have, so weigh its own facts
        # rather than presenting it as the holder -- and never as a LIVE holder,
        # since nothing here shows that pid acquired the lock. The predicate is
        # not consulted: there is no identified owner to ask it about.
        if recorded_pid is None:
            return None, None, False
        return recorded_pid, self._describe_unidentified_owner(recorded_pid), False

    def _describe_unidentified_owner(self, recorded_pid: int) -> str:
        """No surface names the flock owner, so report what the recorded pid proves.

        Reached without ``/proc/locks`` -- macOS and Windows cannot identify an
        flock owner at all (``F_GETLK`` reports ``l_pid = -1`` for a conflicting
        flock and ``lsof`` leaves the lock field blank), and an unreadable
        ``/proc`` is the same -- and on a Linux home whose filesystem reports a
        device the lock table never matches (btrfs subvolumes, overlayfs; see
        :func:`platform_compat.flock_owner_pid`). Naming the owner is not what
        the operator needs, though. Liveness and port ownership work on every
        platform, and between them they separate the three states that lead to
        different actions: the recorded pid is gone (an inherited descriptor
        holds the lock, and nothing here can name the inheritor); it is running
        and holds the dashboard port (what a gateway serving this home looks
        like, so ``kirocrew stop`` comes first); or it is running without the
        port, where the number may belong to an unrelated process that reused
        it. Every state keeps a hedge on WHO holds the lock, because that is the
        one thing none of them establishes.

        Never a live holder, whatever the port says -- which is why this
        returns only the message and :meth:`_diagnose` hands
        :class:`GatewayLockError` a fixed ``False`` for this branch. The
        recorded pid is a number stamped by whoever locked the file LAST, and
        pid numbers are reused; that the process now wearing it listens on the
        port is consistent with a serving gateway, but nothing shows that
        process ACQUIRED the lock. Treating it as one would exit
        :data:`LIVE_HOLDER_EXIT_CODE` and stand a supervised unit down for good
        on no evidence that anyone holds the lock. The verdict therefore needs
        the acquirer positively identified, which only the ``/proc/locks``
        surface can do, so the serving-holder predicate
        (:meth:`_serving_verdict`) is asked about that acquirer alone and never
        about the recorded pid.
        """
        if not platform_compat.pid_exists(recorded_pid):
            return (
                f"{self._path} is locked, but the pid it records ({recorded_pid}) no longer "
                "exists. An flock belongs to the open file description, so it survives in a "
                "process that inherited that descriptor -- typically a child forked from a "
                "crashed gateway. This platform cannot name that process; find it with: "
                f"lsof {self._path}"
            )
        if self._port is None:
            return (
                f"{self._path} is locked, but the holder could not be identified. "
                f"The file records pid {recorded_pid}, which may be stale. "
                "Stop the running gateway, or set KIROCREW_HOME to an isolated directory."
            )
        if self._holds_port(recorded_pid):
            return (
                f"{self._path} is locked and the pid it records ({recorded_pid}) is running and "
                f"holds port {self._port} -- most likely another gateway already owns "
                f"{self._home}; stop it first (kirocrew stop) or set KIROCREW_HOME to an "
                "isolated directory. This platform or filesystem cannot confirm which process "
                "holds the lock, so this refusal is not treated as permanent; a supervisor, if "
                "one manages this gateway, will retry it."
            )
        return (
            f"{self._path} is locked and the pid it records ({recorded_pid}) is running, but "
            f"that pid does not hold port {self._port}, so it may be an unrelated process that "
            "reused the number rather than the gateway. Stop the running gateway, or set "
            "KIROCREW_HOME to an isolated directory."
        )

    def _diagnose_replaced_lock_file(self) -> tuple[int | None, str, bool]:
        """The home is still held, but its lock file does not name the holder.

        Reached when the lock file could be locked -- because it was deleted and
        this call re-created it -- while another gateway still holds the home
        directory. The pid inside the file is no evidence at all here: either
        this process just created the file, or whatever is in it predates the
        deletion. The directory's own owner is the only thing worth reading, and
        only Linux can name it.

        Returns ``(pid, message, live_holder)``; ``live_holder`` is
        :attr:`_ServingVerdict.serving` from one call of the serving-holder
        predicate (:meth:`_serving_verdict`) on the directory's acquirer -- the
        same three conjuncts the lock file's own owner must show
        (:meth:`_describe_live_owner`), measured once.
        """
        owner = platform_compat.flock_owner_pid(self._home)
        verdict = self._serving_verdict(owner) if owner is not None else None
        live = False
        unasserted = ""
        if owner is not None and verdict is not None and verdict.alive:
            live = verdict.serving
            facts = self._port_facts(owner, verdict)
            who = f"pid {owner}" + (f" ({', '.join(facts)})" if facts else "")
            clause = self._unasserted_clause(verdict)
            if clause:
                unasserted = " That process " + clause
        else:
            owner = None
            who = "a running gateway"
        return (
            owner,
            (
                f"{self._home} is still held by {who}, but {self._path} no longer names it -- "
                "the lock file was deleted or replaced while that gateway was running. Deleting "
                "the lock file neither stops a gateway nor releases its lock; stop it first "
                "(kirocrew stop) or set KIROCREW_HOME to an isolated directory." + unasserted
            ),
            live,
        )

    def _unasserted_clause(self, verdict: _ServingVerdict) -> str | None:
        """Words for a live acquirer on the port that the predicate does NOT assert.

        ``None`` for a serving holder and for an acquirer without the port (the
        facts already say ``does not hold port N``). Two rows get a sentence:
        the wedged gateway (the owner's own socket at the probed address, yet
        nothing answered) and the residual row (the owner holds the port only
        at another address, or at one the platform did not report). Both end
        the same way, because both stay restartable.
        """
        if verdict.silent_listener:
            shape = (
                f"is listening on port {self._port} but not answering HTTP at "
                f"{verdict.probe_host} (the address this gateway would bind), so it looks like a "
                "wedged gateway rather than one serving this home"
            )
        elif verdict.bound_elsewhere:
            where = ", ".join(verdict.bound_addresses) or "an address this platform did not report"
            shape = (
                f"holds port {self._port} at {where}, not at {verdict.probe_host} (the address "
                "this gateway would bind), so whether it serves this home cannot be told from "
                "here"
            )
        else:
            return None
        return (
            shape + "; this refusal is not treated as permanent, and a supervisor, if one "
            "manages this gateway, will retry it."
        )

    def _describe_live_owner(
        self, pid: int, recorded_pid: int | None, verdict: _ServingVerdict
    ) -> str:
        """The ordinary case: a live process holds the lock, so name it.

        *verdict* is the serving-holder predicate's answer for *pid*
        (:meth:`_serving_verdict`), which the caller also reads for
        ``live_holder``; this method only puts its facts into words. Only the
        serving shape -- on the configured port and answering HTTP at the
        address this gateway would bind -- is terminal. Two live-acquirer shapes
        stay restartable and say so: WITHOUT the port it is a gateway still
        starting (the next attempt sees the port held) or shutting down (the
        next attempt takes the lock); on the port but SILENT at the probed
        address it is a wedged gateway (a hung process keeps its listening
        socket bound) or one bound to another address, and a terminal exit
        there would park a supervised unit ``failed`` for good while a wedged
        incumbent dies on its own or is killed, leaving the home unserved with
        nothing left to relaunch.
        """
        facts: list[str] = []
        threads = platform_compat.process_thread_count(pid)
        if threads is not None:
            facts.append(f"{threads} thread{'s' if threads != 1 else ''}")
        facts.extend(self._port_facts(pid, verdict))
        detail = f" ({', '.join(facts)})" if facts else ""
        clause = self._unasserted_clause(verdict)
        if clause:
            message = (
                f"{self._path} is held by pid {pid}{detail} -- it "
                + clause
                + " Stop it first (kirocrew stop) or set KIROCREW_HOME to an isolated directory"
            )
        else:
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
        serving = self._port is not None and _port_answers_http(self._port, host=self._probe_host)
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

    def _holds_port(self, pid: int) -> bool:
        """Whether *pid* listens on the dashboard port; False when none was supplied."""
        return self._port is not None and pid in platform_compat.find_listening_pids(self._port)

    def _serving_verdict(self, owner: int) -> _ServingVerdict:
        """THE serving-holder predicate: is the identified acquirer *owner* serving this home?

        *owner* MUST be the pid ``/proc/locks`` names as the flock's acquirer --
        of the lock file (:meth:`_diagnose`) or of the home directory
        (:meth:`_diagnose_replaced_lock_file`). The pid the lock file merely
        records is never passed here: with no identified owner there is nothing
        to assert, and :meth:`_describe_unidentified_owner` stays False without
        asking (identity unknowable = never assert).

        Four conjuncts, measured once each and in order, each skipped once an
        earlier one fails: the acquirer is ALIVE; it HOLDS the configured
        dashboard port (one listener enumeration,
        :func:`platform_compat.find_port_listeners`, filtered to the owner's
        own sockets; False when no port was supplied); it holds it AT THE
        ADDRESS the probe reaches -- the address this gateway is configured to
        bind, :attr:`_probe_host`, with wildcards covering it by family
        (:func:`_listener_reaches`); and it ANSWERS HTTP there (one
        :func:`_port_answers_http` call, its own 1.5 s budget). Only all four
        make :attr:`_ServingVerdict.serving` True -- the one refusal a restart
        cannot heal, so the one that exits :data:`LIVE_HOLDER_EXIT_CODE`. The
        address conjunct is what keeps port ownership and HTTP health tied to
        ONE process: without it a stranger answering at the probe address on
        the same port would be credited to a lock owner bound elsewhere.

        The RESIDUAL ROW, by design: a holder that is alive and holds the port
        only on some OTHER address than the one this gateway would bind -- or
        one whose socket addresses the platform could not report -- is NOT
        asserted: ``live_holder`` False, exit 1, the pre-PR behaviour bounded
        by the unit's ``StartLimit*``. The assertion stays as narrow as the
        evidence; probing the holder's own listener address instead is the
        follow-up tracked in the PR's deferred-finding issue, not a guess made
        here.
        """
        alive = platform_compat.pid_exists(owner)
        own: list[platform_compat.PortListener] = []
        if alive and self._port is not None:
            own = [
                entry
                for entry in platform_compat.find_port_listeners(self._port)
                if entry.pid == owner
            ]
        holds_port = bool(own)
        listens_at_probe_host = any(_listener_reaches(entry, self._probe_host) for entry in own)
        answers_http = (
            listens_at_probe_host
            and self._port is not None
            and _port_answers_http(self._port, host=self._probe_host)
        )
        return _ServingVerdict(
            owner,
            alive,
            holds_port,
            listens_at_probe_host,
            answers_http,
            self._probe_host,
            tuple(dict.fromkeys(entry.address or "?" for entry in own)),
        )

    def _port_facts(self, pid: int, verdict: _ServingVerdict | None = None) -> list[str]:
        """Port ownership facts for *pid*, empty when no port was supplied.

        *verdict* is the predicate's answer a caller already holds, so the words
        come from the same measurement as the exit status; a caller without one
        (an orphaned lock's candidate openers) asks the predicate here.
        """
        if self._port is None:
            return []
        if verdict is None:
            verdict = self._serving_verdict(pid)
        if not verdict.holds_port:
            return [f"does not hold port {self._port}"]
        if not verdict.listens_at_probe_host:
            where = ", ".join(verdict.bound_addresses) or "an unreported address"
            return [f"holds port {self._port} at {where}, not at {verdict.probe_host}"]
        answering = "answering" if verdict.answers_http else "not answering"
        return [f"holds port {self._port}, {answering} HTTP at {verdict.probe_host}"]


@dataclass(frozen=True)
class LockHolder:
    """Who holds ``<home>/gateway.lock`` right now, per :func:`lock_holder`.

    ``pid`` is ``None`` when nothing holds the lock (no split-brain refusal to
    diagnose). A named ``pid`` is always a LIVE process (``alive`` is ``True``
    for it): :func:`lock_holder` reports a held lock whose acquirer is gone --
    the orphaned-flock wedge :class:`GatewayLock` diagnoses in prose
    (``_describe_orphaned_lock``) -- as indeterminate (:class:`LockProbeError`)
    rather than as a dead holder, so no caller can read it as nobody running.
    ``source`` says which surface produced the pid; ``"home_anchor"`` is the
    home directory's own lock, which is the only surface left once the lock
    file has been deleted.
    """

    pid: int | None
    alive: bool
    source: str  # "flock_owner" | "recorded_pid" | "home_anchor" | "none"


_NO_HOLDER = LockHolder(pid=None, alive=False, source="none")


def lock_holder(home: Path) -> LockHolder:
    """Non-destructive oracle: who (if anyone) holds ``<home>/gateway.lock``.

    Shares its resolution logic with :meth:`GatewayLock._diagnose` so a caller
    that never intends to ACQUIRE the lock -- ``cli_perf``'s profiler target,
    and ``_stop``/``_restart``'s port-probe fallback -- can still ask who owns a
    home without opening it for writing first. A missing or free lock file is
    not a free home -- the file can be deleted out from under a running gateway
    without releasing anything -- so both cases fall through to the home anchor
    (:func:`_anchor_holder_or_nobody`) before ``nobody`` is reported. A file
    that exists but cannot be read is still probed, since being unable to read
    it is not evidence that nobody holds it.

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
        return _anchor_holder_or_nobody(home, path)
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
        return _anchor_holder_or_nobody(home, path)

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


def _anchor_holder_or_nobody(home: Path, path: Path) -> LockHolder:
    """Who holds *home* when its lock FILE proves nothing -- missing, or free.

    Deleting the lock file releases no ``flock``; it only makes that inode
    unreachable by name. So a missing or free lock file is not a free home, and
    ``acquire`` refuses on the home anchor in exactly that state -- answering
    "nobody" here would report nothing running in the same turn ``kirocrew
    gateway`` refuses to start. Returns ``nobody`` only when the anchor yields
    positively free or unsupported, and raises :class:`LockProbeError` when
    the home is held by someone this platform cannot name or its anchor state
    cannot be measured, the same indeterminate answer the lock file's own
    unnameable-holder case produces.

    An anchored reading is a candidate, not a verdict. Another non-destructive
    reader (a ``stop`` beside a ``cli_perf`` probe) holds the anchor for two
    syscalls, and two such readers on an idle home can each see the other's
    hold. The anchor is therefore re-probed up to ``_IDENTITY_ATTEMPTS`` times,
    ``_RETRY_BACKOFF_SECS`` apart, and only a home that reads anchored on every
    attempt goes on to the owner lookup: a passing reader has released within
    one backoff, an incumbent never does. A single free reading is positive
    evidence and returns ``nobody`` at once. A :class:`LockProbeError` from the
    probe itself (an unopenable home, an unmeasurable filesystem) is a different
    fact and propagates on the attempt that raised it.

    Probing the anchor can create and remove one throwaway directory inside
    *home* to tell an unsupported directory lock from a held one. That is a
    write, but it disturbs no holder, which is the sense in which this oracle
    is non-destructive.
    """
    for attempt in range(_IDENTITY_ATTEMPTS):
        if attempt:
            time.sleep(_RETRY_BACKOFF_SECS)
        if not _home_is_anchored(home, path):
            return _NO_HOLDER
    owner = platform_compat.flock_owner_pid(home)
    if owner is not None and platform_compat.pid_exists(owner):
        return LockHolder(pid=owner, alive=True, source="home_anchor")
    raise LockProbeError(
        path,
        OSError(f"{home} is still held by a gateway that {LOCK_FILENAME} no longer names"),
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


def _is_same_file(fd: int, path: Path) -> bool:
    """True iff the inode open as *fd* is still the inode *path* names.

    False also when *path* has no inode at all (it is unlinked), because both
    answers mean the same thing to a lock: the descriptor we hold is not what
    another process opening that name would get.
    """
    try:
        held = os.fstat(fd)
        named = os.stat(path)
    except OSError:
        return False
    return (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino)


def _directory_locks_supported(
    home: Path,
) -> tuple[_DirectoryLockSupport, OSError | None]:
    """Measure whether an exclusive ``flock`` works on a directory under *home*.

    Separates the two reasons locking the home directory can fail: another
    gateway holds it, or this filesystem does not implement directory locks.
    Unsupported is returned only after measuring a throwaway directory that
    provably has no holder, and only when ``flock`` itself says so with one of
    :data:`_NO_DIRECTORY_LOCK_ERRNOS`. The lock is taken here rather than through
    ``platform_compat.try_acquire_lock`` because that helper folds every error
    into ``False``, and on this probe a ``False`` from a full lock table would
    read as "unsupported" and let a start past a held home. Any other
    ``OSError`` -- creating, opening, locking, closing, or cleaning the probe --
    is indeterminate and carries its cause so callers can fail closed without
    inventing a holder. Never reached on Windows, which takes no anchor.
    """
    try:
        with tempfile.TemporaryDirectory(dir=home, prefix=".lockprobe-") as probe:
            fd = os.open(probe, os.O_RDONLY)
            try:
                try:
                    platform_compat.fcntl.flock(
                        fd, platform_compat.fcntl.LOCK_EX | platform_compat.fcntl.LOCK_NB
                    )
                except OSError as exc:
                    if exc.errno in _NO_DIRECTORY_LOCK_ERRNOS:
                        return _DirectoryLockSupport.UNSUPPORTED, None
                    return _DirectoryLockSupport.INDETERMINATE, exc
                platform_compat.release_lock(fd)
                return _DirectoryLockSupport.SUPPORTED, None
            finally:
                os.close(fd)
    except OSError as exc:
        return _DirectoryLockSupport.INDETERMINATE, exc


def _home_is_anchored(home: Path, path: Path) -> bool:
    """True iff something holds the exclusive ``flock`` on *home* itself.

    Non-destructive: a free directory is unlocked again immediately. Windows,
    an absent home, or positively unsupported directory locks report False.
    Other failures to open or measure the home raise :class:`LockProbeError`,
    because callers must not read missing evidence as nobody running.
    """
    if platform_compat.IS_WINDOWS:
        return False
    try:
        fd = os.open(home, os.O_RDONLY)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise LockProbeError(path, exc) from exc
    try:
        if platform_compat.try_acquire_lock(fd, exclusive=True):
            platform_compat.release_lock(fd)
            return False
    finally:
        os.close(fd)
    support, probe_error = _directory_locks_supported(home)
    if support is _DirectoryLockSupport.SUPPORTED:
        return True
    if support is _DirectoryLockSupport.UNSUPPORTED:
        return False
    if probe_error is None:
        probe_error = OSError("directory-lock probe supplied no error")
    raise LockProbeError(path, probe_error) from probe_error


def _indeterminate_anchor_message(home: Path, cause: OSError) -> str:
    """Honest refusal when the home anchor or its capability cannot be measured."""
    return (
        f"{home} is held or its lock support could not be determined: {cause}; "
        "stop the running gateway or set KIROCREW_HOME to an isolated directory"
    )


def _release_fd(fd: int) -> None:
    """Release any lock on *fd* and close it, tolerating a closed descriptor."""
    platform_compat.release_lock(fd)
    try:
        os.close(fd)
    except OSError:
        pass


def _port_answers_http(
    port: int, timeout: float = 1.5, *, host: str = _LOOPBACK_PROBE_HOST
) -> bool:
    """True iff ``host:port`` returns an HTTP status line within *timeout*.

    *host* is the address the caller is configured to bind, a wildcard already
    mapped to loopback (:func:`probe_host_for_bind`); an IPv6 literal connects as
    given and is bracketed in the ``Host`` header. A plain connect is not
    enough: a wedged holder's kernel still completes the handshake into the
    listen backlog even though nothing will ever ``accept()``, so
    connect-success would misclassify an orphan as a live gateway. This mirrors
    :func:`kiro_crew.dashboard.port_reclaim._probe_gateway_healthy` in a
    synchronous form, because the lock is taken before the event loop exists.
    """
    header_host = f"[{host}]" if ":" in host else host
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(
                b"GET / HTTP/1.0\r\nHost: "
                + header_host.encode("ascii", "replace")
                + b"\r\nConnection: close\r\n\r\n"
            )
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

"""Crash-dump store — dedicated file routing for loop-stall watchdog dumps.

The existing loop_watchdog.py captures thread stacks on
event-loop wedge via faulthandler.  However, those dumps land in raw stderr
(interleaved with all other output in journal/terminal) and are effectively
undiscoverable.

This module provides:

1. A DEDICATED crash-dump file opened at gateway startup that faulthandler writes
   to directly (faulthandler needs a stable fd for process lifetime).
2. Rotation: keeps last N dumps, removes oldest on startup.
3. Newest-dump detection for doctor/startup surfacing.

Dump directory: ``<data home>/logs/crash-dumps/`` (data home = ``config_dir()``,
i.e. ``~/.kiro/crew`` or ``$KIROCREW_HOME``)
Filename pattern: ``loopstall-<ISO timestamp>.txt``

**fd lifetime guarantee (issue #1571):**

``faulthandler.dump_traceback_later`` captures a raw C file descriptor at arm
time and writes to it on its own C thread when the timer fires.  If the fd is
invalidated (closed, reassigned, or GC'd) between arm and fire, the dump writes
to nothing — or worse, to a recycled fd — and the crash file contains only the
header written at open time.

To prevent this, :func:`open_dump_file` obtains the fd via :func:`os.open`
(lowest-level, no Python buffering layer that could close/dup the fd behind our
back), wraps it in a *non-closing* Python file object for the header write, and
returns a :class:`DumpFile` that exposes ``.fileno()`` (what faulthandler needs)
while guaranteeing the underlying fd is never closed until the process exits.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import socket
import stat
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from kiro_crew.config.paths import config_dir
from kiro_crew.platform_compat import pid_exists

logger = logging.getLogger(__name__)

_DEFAULT_MAX_DUMPS = 10
_DUMP_DIR_NAME = "crash-dumps"
DUMP_PREFIX = "loopstall-"
DUMP_SUFFIX = ".txt"

# Module-level reference to the open dump file — kept alive for process lifetime
# because faulthandler requires the fd to remain valid.
_active_dump_file: "DumpFile | None" = None


class DumpFile:
    """Thin wrapper around a raw OS file descriptor for faulthandler.

    faulthandler's C code calls ``fileno()`` on the file object we pass it and
    then uses that integer fd for all subsequent writes.  A regular Python
    ``open()`` returns a buffered text wrapper whose ``close()`` invalidates the
    fd — and the GC, a stray ``with`` block, or even internal ``io`` layer
    reshuffling can trigger that ``close()`` unexpectedly.

    This class:
    * Holds the fd obtained from :func:`os.open` directly.
    * Exposes ``fileno()`` so faulthandler can extract the fd.
    * Exposes ``write()`` and ``flush()`` so :func:`_default_dump` (which calls
      ``faulthandler.dump_traceback(file=...)`` ) and the header write work.
    * Never closes the fd (the OS reclaims it on process exit).
    """

    def __init__(self, fd: int, path: Path) -> None:
        self._fd = fd
        self._path = path

    def fileno(self) -> int:
        return self._fd

    @property
    def path(self) -> Path:
        return self._path

    @property
    def closed(self) -> bool:
        """Return True only if the fd has been explicitly closed (never, in normal use)."""
        try:
            os.fstat(self._fd)
            return False
        except OSError:
            return True

    def write(self, data: str) -> int:
        """Write a string to the fd (UTF-8 encoded, unbuffered)."""
        encoded = data.encode("utf-8")
        return os.write(self._fd, encoded)

    def flush(self) -> None:
        """Flush the fd to disk (fsync is too aggressive; fdatasync where available)."""
        # os.write is unbuffered at the Python level; the kernel buffer is
        # flushed on its own schedule.  An explicit fsync here would hurt
        # latency on every beat() for no diagnostic gain — the dump content
        # that matters is written by faulthandler's C thread moments before
        # _exit(), and the kernel flushes dirty pages on exit.  No-op by design.
        pass

    def close(self) -> None:
        """Intentional no-op.  The fd lives until process exit.

        This exists so code that expects a file-like interface (e.g. a
        ``finally: f.close()`` in tests) does not raise AttributeError.
        The fd is *not* closed — faulthandler's C timer may fire at any moment.
        """
        pass

    if sys.platform == "win32":
        @property
        def name(self) -> str:
            """Provide the file path as ``name`` for diagnostics."""
            return str(self._path)
    else:
        @property
        def name(self) -> str:
            return str(self._path)


def get_dumps_dir() -> Path:
    """Resolve the crash-dumps directory under the data home's ``logs/``.

    ``config_dir()`` resolves to ``~/.kiro/crew`` (or ``$KIROCREW_HOME`` when
    set), so dumps land in ``<data home>/logs/crash-dumps/``.
    """
    d = config_dir() / "logs" / _DUMP_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _list_dumps(dumps_dir: Path | None = None) -> list[Path]:
    """Return existing dump files sorted oldest-first.

    A concurrent gateway on the same data home (isolated pod, overlapping
    restart) can unlink a dump between ``iterdir()`` and ``stat()``. Vanished
    entries are skipped instead of letting ``FileNotFoundError`` propagate —
    the startup sweep runs on every boot, so that raise would abort gateway
    startup.
    """
    d = dumps_dir or get_dumps_dir()
    if not d.is_dir():
        return []
    entries: list[tuple[float, Path]] = []
    for f in d.iterdir():
        if not (f.name.startswith(DUMP_PREFIX) and f.suffix == DUMP_SUFFIX):
            continue
        try:
            entries.append((f.stat().st_mtime, f))
        except OSError:
            continue  # vanished mid-listing — a concurrent sweep got it first
    entries.sort(key=lambda t: t[0])
    return [p for _, p in entries]


# The header written by open_dump_file() is 3 comment lines + 1 blank line.
# Anything beyond that is real faulthandler stack content.
_HEADER_LINES = 4

_PID_LINE_RE = re.compile(r"^# PID: (\d+)(?: @ (\S+))?(?: start=(\S+))?\s*$", re.MULTILINE)

# Ceiling for a plausible PID. Linux pid_max tops out at 2**22; Windows and
# macOS stay far below 2**31-1. Anything above this is a corrupt header, not a
# process — and values past the C int range would overflow ``os.kill``.
_PID_MAX = 2**31 - 1


# A real header is 4 short lines (~200 bytes); anything the sweep needs to see
# — header-only-ness, the ``# PID:`` line — sits comfortably inside this bound.
_HEADER_SCAN_BYTES = 8192


def _read_dump_head(dump_path: Path) -> tuple[str, bool]:
    """Read at most ``_HEADER_SCAN_BYTES`` bytes of a REGULAR dump file.

    Returns ``(text, truncated)`` — ``truncated`` is computed on BYTES before
    decoding (multibyte characters shrink the decoded length, so a character
    count cannot detect the cut). Opens with ``O_NOFOLLOW`` (symlink ->
    ``ELOOP``) and verifies the fd is a regular file, so a ``loopstall-*.txt``
    symlinked at ``/dev/zero`` (or a FIFO) cannot pull an unbounded read into
    the startup sweep. Raises ``OSError`` on refusal or read failure — callers
    already treat that as "leave the file alone".
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(str(dump_path), flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"not a regular file: {dump_path}")
        chunks: list[bytes] = []
        remaining = _HEADER_SCAN_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    truncated = len(data) > _HEADER_SCAN_BYTES
    return data[:_HEADER_SCAN_BYTES].decode("utf-8", errors="replace"), truncated


def _is_header_only(dump_path: Path) -> bool:
    """True iff *dump_path* contains only the startup header (no thread stacks).

    A header-only dump means that gateway session never wedged — the file was
    pre-created at startup (faulthandler needs a stable fd for the process
    lifetime) and faulthandler never fired into it.  Raises ``OSError`` through
    to the caller on read failure so callers can choose their own conservative
    fallback.
    """
    content, truncated = _read_dump_head(dump_path)
    if truncated:
        return False  # far larger than any header — has stack content
    return len(content.splitlines()) <= _HEADER_LINES


@functools.lru_cache(maxsize=1)
def _pid_domain() -> str:
    """Identify the PID domain this process's PID is meaningful in.

    A PID only names a process within one kernel PID table. The data home can
    be shared across tables — an NFS/network home mounted on several hosts, or
    a container bind-mounting the host's data home into its own PID namespace
    — and there a locally-unknown PID says nothing about the owning gateway's
    liveness. The domain string (hostname, plus the PID-namespace id on Linux)
    is recorded next to the PID at header-write time so later readers can tell
    "this PID is checkable here" from "this PID belongs to a table I cannot
    see".
    """
    host = socket.gethostname() or "unknown-host"
    try:
        # Two containers sharing a bind-mounted data home can report the same
        # hostname while having disjoint PID tables; the namespace id
        # disambiguates. Absent /proc (macOS, Windows), hostname suffices.
        ns = os.readlink("/proc/self/ns/pid")
        host = f"{host}/{ns}"
    except OSError:
        pass
    # The header line is parsed with a whitespace-delimited token; keep the
    # recorded domain one token even if the hostname contains whitespace.
    return re.sub(r"\s+", "-", host)


def _pid_start_id(pid: int) -> str | None:
    """Best-effort start identity of *pid* — distinguishes PID reuse.

    A PID probing alive is necessary but not sufficient for ownership: the
    recorded gateway may have exited and the kernel may have handed its PID to
    an unrelated process. The starttime field (22nd in ``/proc/<pid>/stat``,
    clock ticks since boot) is fixed for a process's lifetime, so a recorded
    start ID that no longer matches means the owner is GONE even though the
    PID is live. Returns ``None`` where the probe is unavailable (no procfs:
    macOS, Windows) or unreadable — callers must then fall back to plain PID
    liveness (conservative: protects a possibly-reused PID's file rather than
    risking deletion of a live owner's fd target).
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            stat = f.read(4096)
        # Field 2 (comm) may contain spaces/parens; fields after the LAST ')'
        # are unambiguous. starttime is field 22 overall -> index 19 after it.
        tail = stat.rsplit(b")", 1)[1].split()
        return tail[19].decode("ascii")
    except (OSError, IndexError, UnicodeDecodeError):
        return None


def _dump_owner(dump_path: Path) -> tuple[int, str | None, str | None] | None:
    """Extract ``(pid, pid_domain, start_id)`` from a dump file's header.

    ``pid_domain`` and ``start_id`` are ``None`` for headers written before
    each was recorded. Returns ``None`` outright for anything that cannot be a
    real PID — including digit strings too long to convert or values beyond
    the pid_t ceiling, which would otherwise raise out of ``int()`` or
    overflow the liveness probe's ``os.kill`` — so a corrupt header degrades
    to "leave the file alone" instead of aborting the startup sweep.
    """
    try:
        m = _PID_LINE_RE.search(_read_dump_head(dump_path)[0])
    except OSError:
        return None
    if m is None:
        return None
    try:
        pid = int(m.group(1))
    except ValueError:
        return None
    if not 0 < pid <= _PID_MAX:
        return None
    return pid, m.group(2), m.group(3)


def _owner_alive(dump_path: Path, is_pid_alive: Callable[[int], bool]) -> bool | None:
    """Best-effort liveness of the gateway that owns *dump_path*.

    Returns ``True`` (owner confirmed alive), ``False`` (owner confirmed
    dead), or ``None`` when ownership cannot be established: no parseable
    ``# PID:`` line, or a PID from a different PID domain (another host
    sharing the data home, another PID namespace) where a local liveness
    probe is meaningless — probing such a PID locally would misread an
    active remote gateway as dead.

    PID reuse: a live PID alone does not prove the OWNER is alive — the
    recorded gateway may have exited and the kernel may have recycled its PID.
    When the header recorded a start ID and the live process's start ID
    differs, the owner is confirmed dead (``False``) despite the live PID.
    When either side lacks a start ID (legacy header, no procfs), plain PID
    liveness stands — conservative, since ``True`` only ever protects a file.
    """
    owner = _dump_owner(dump_path)
    if owner is None:
        return None
    pid, domain, recorded_start = owner
    if domain is None or domain != _pid_domain():
        return None
    if pid == os.getpid():
        return True
    if not is_pid_alive(pid):
        return False
    if recorded_start is not None:
        current_start = _pid_start_id(pid)
        if current_start is not None and current_start != recorded_start:
            return False  # PID recycled: live process is not the owner
    return True


def _owner_foreign(dump_path: Path) -> bool:
    """True iff the dump's header names an owner in a FOREIGN PID domain.

    Distinct from "ownership unknown" (no/corrupt PID line): a foreign-domain
    owner may be a LIVE gateway on another host or namespace sharing the data
    home, whose faulthandler still holds this file's fd — deleting the path
    would send its future stall evidence to an unreachable inode. Files with
    no attributable owner carry no such risk and stay reapable.
    """
    owner = _dump_owner(dump_path)
    return owner is not None and owner[1] is not None and owner[1] != _pid_domain()


def sweep_stale_dumps(
    dumps_dir: Path | None = None,
    *,
    is_pid_alive: Callable[[int], bool] = pid_exists,
) -> int:
    """Remove header-only dumps left behind by dead gateway sessions.

    Every gateway startup pre-creates a dump file so faulthandler has a stable
    fd; a session that exits without ever wedging leaves that file behind as a
    4-line header with zero diagnostic content.  Restart the gateway often
    enough and those empty files pile up to the rotation cap — padding the
    diagnostics bundle's per-bundle dump quota and, worse, aging REAL stall
    dumps out of rotation (``rotate_dumps`` removes oldest-first by mtime, so
    nine clean restarts after a wedge would delete the only evidence of it).

    A dump is swept iff ALL of:
    * it is header-only (a dump with stacks is evidence — never touched), and
    * its header carries a parseable ``# PID:`` line from THIS PID domain
      (same host and, on Linux, same PID namespace — a PID recorded by a
      gateway on another host sharing the data home, or in another PID
      namespace, cannot be liveness-checked locally and is left alone), and
    * that PID is confirmed no longer alive (a live PID means a concurrently
      running gateway on this data home still owns the file — e.g. an
      isolated pod or an overlapping restart — so it is left alone).

    Unreadable files, headers without a PID line, and headers whose PID
    belongs to a foreign PID domain are all left alone (conservative:
    rotation will reap them eventually).  Returns the number of files
    removed.
    """
    removed = 0
    for path in _list_dumps(dumps_dir):
        if _owner_alive(path, is_pid_alive) is not False:
            continue  # owner alive, or ownership unknowable — leave it alone
        # Classify only AFTER the owner is confirmed dead: a dead process can
        # no longer append stacks, so the header-only verdict cannot be
        # invalidated between this check and the unlink. The reverse order
        # would race a gateway that wedges (writes stacks) and exits right
        # after classification — deleting fresh evidence.
        try:
            if not _is_header_only(path):
                continue
        except OSError:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            logger.debug("could not sweep stale dump %s", path, exc_info=True)
    if removed:
        logger.info("swept %d stale header-only crash dump(s) from prior sessions", removed)
    return removed


def rotate_dumps(
    max_dumps: int = _DEFAULT_MAX_DUMPS,
    dumps_dir: Path | None = None,
    *,
    is_pid_alive: Callable[[int], bool] = pid_exists,
) -> int:
    """Remove dumps if count exceeds max_dumps.  Returns number removed.

    Header-only dumps (no stack content — the session never wedged) are
    sacrificed first, oldest-first; dumps with real stacks are only removed
    once no header-only candidates remain.  This keeps genuine stall evidence
    alive as long as possible when empty startup files share the directory.

    A dump whose ``# PID:`` owner is this process, or is confirmed alive in
    this PID domain, is never a victim: faulthandler holds that file's fd for
    the owning session's whole lifetime, so unlinking it would send any later
    stall evidence to an unreachable inode. Every live gateway's active dump
    is header-only until it wedges — exactly the class sacrificed first.
    A dump whose header names a FOREIGN-domain owner (another host or PID
    namespace sharing the data home) is also never a victim: its owner may be
    alive with faulthandler holding the fd, and that cannot be checked from
    here — its own domain's rotation reaps it. Dumps with NO attributable
    owner (no/corrupt PID line, legacy domain-less headers) stay
    rotation-eligible and are ranked purely by content — under cap pressure
    they are removed just as the pre-sweep rotation removed them, so
    unattributable files cannot pile up unboundedly.
    """
    dumps = _list_dumps(dumps_dir)

    def _sacrifice_order(dumps: list[Path]) -> list[Path]:
        header_only: list[Path] = []
        unreadable: list[Path] = []
        stacked: list[Path] = []
        for p in dumps:
            if _owner_alive(p, is_pid_alive) is True:
                continue  # a live session still owns this fd — never a victim
            if _owner_foreign(p):
                # A foreign-domain owner (another host/namespace sharing the
                # data home) cannot be liveness-checked here, and if it IS
                # alive its faulthandler holds this file's fd — unlinking the
                # path would send its future stall evidence to an unreachable
                # inode. Its own gateway's rotation reaps it in its domain.
                continue
            try:
                (header_only if _is_header_only(p) else stacked).append(p)
            except OSError:
                # Unreadable is ambiguous: junk (symlink, wrong type) or real
                # evidence behind a transient read error. Sacrifice it after
                # known header-only files but before confirmed stack evidence.
                unreadable.append(p)
        return header_only + unreadable + stacked

    victims = _sacrifice_order(dumps)
    # Compute the excess ONCE and attempt exactly that many victims (keep
    # max_dumps - 1 so there's room for the new one we're about to create).
    # Counting only successful unlinks would let two overlapping rotations
    # each treat the other's deletions as "still excess" and remove twice
    # the intended number of files.
    excess = len(dumps) - (max_dumps - 1)
    removed = 0
    for victim in victims[: max(excess, 0)]:
        try:
            victim.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def open_dump_file(dumps_dir: Path | None = None) -> DumpFile:
    """Create and open a new dump file for this gateway session.

    The returned :class:`DumpFile` wraps a raw OS fd obtained via :func:`os.open`.
    That fd is never closed by Python — it lives until the process exits — so
    ``faulthandler.dump_traceback_later`` can capture it at arm time and rely on
    it remaining valid when the timer fires seconds (or minutes) later.

    Returns the :class:`DumpFile` (caller stores it to prevent GC of the wrapper,
    though the fd itself is OS-level and not GC'd).
    """
    global _active_dump_file  # noqa: PLW0603
    d = dumps_dir or get_dumps_dir()
    d.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = d / f"{DUMP_PREFIX}{ts}{DUMP_SUFFIX}"

    # Use os.open() for a raw fd that is never wrapped in a closable Python
    # buffered layer.  O_WRONLY|O_CREAT|O_TRUNC mirrors open("w") semantics.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if sys.platform == "win32":
        flags |= os.O_NOINHERIT
    else:
        flags |= os.O_CLOEXEC
    fd = os.open(str(path), flags, 0o644)

    f = DumpFile(fd, path)
    # Write a header so the file is identifiable even before a dump fires.
    # The PID is qualified with its PID domain (host + PID namespace) so a
    # later sweep only trusts a local liveness probe for PIDs it can see, and
    # with the process start ID so a recycled PID cannot masquerade as the
    # owner (omitted where procfs is unavailable — readers then fall back to
    # plain PID liveness).
    start_id = _pid_start_id(os.getpid())
    start_tok = f" start={start_id}" if start_id is not None else ""
    f.write(f"# KiroCrew loop-stall crash dump — opened {ts}\n")
    f.write(f"# PID: {os.getpid()} @ {_pid_domain()}{start_tok}\n")
    f.write("# If thread stacks appear below, the event loop wedged and faulthandler fired.\n")
    f.write("\n")
    _active_dump_file = f
    return f


def newest_dump(dumps_dir: Path | None = None) -> Path | None:
    """Return the most recent dump file, or None if no dumps exist."""
    dumps = _list_dumps(dumps_dir)
    return dumps[-1] if dumps else None


def newest_dump_with_stacks(dumps_dir: Path | None = None) -> Path | None:
    """Return the newest dump that actually contains thread stacks (not just the header).

    A dump file that only has the 4-line header means the gateway exited cleanly
    without ever wedging.  We only surface dumps that have real content.
    """
    dumps = _list_dumps(dumps_dir)
    for path in reversed(dumps):
        try:
            if not _is_header_only(path):
                return path
        except OSError:
            continue
    return None


def claim_dump_notification(dump_path: Path, dumps_dir: Path | None = None) -> bool:
    """Claim the right to notify about *dump_path*, once per dump.

    A dump stays on disk for up to a week and is re-detected on every gateway
    start, so notifying unconditionally would turn one stall into a week of
    identical alerts on every restart. The dump's own filename is the natural
    idempotency key. Returns True the first time it is claimed, False after.

    Best-effort: on any I/O failure it returns True (notify rather than go
    silent about a crash), since a duplicate alert is a much cheaper failure
    than a suppressed one.
    """
    try:
        marker = (dumps_dir or get_dumps_dir()) / ".notified"
        already = ""
        if marker.is_file():
            already = marker.read_text(encoding="utf-8", errors="replace").strip()
        if already == dump_path.name:
            return False
        marker.write_text(dump_path.name + "\n", encoding="utf-8")
        return True
    except OSError:
        logger.debug("crash-dump notification marker unavailable", exc_info=True)
        return True


def dump_age_seconds(dump_path: Path) -> float:
    """Return age of a dump file in seconds (never negative).

    ``st_mtime`` and ``time.time()`` are both derived from the wall clock, but a
    just-written file's mtime can round marginally AHEAD of an immediately
    following ``time.time()`` (sub-microsecond float jitter, or higher-resolution
    filesystem timestamps), yielding a tiny negative delta. An age is physically
    never negative, so clamp to 0.0 — otherwise callers comparing/formatting the
    age see a nonsensical negative right after a dump is created.
    """
    return max(0.0, time.time() - dump_path.stat().st_mtime)


def dump_first_stack_lines(dump_path: Path, max_lines: int = 5) -> list[str]:
    """Extract the first N lines of actual stack content from a dump file."""
    try:
        lines = dump_path.read_text(encoding="utf-8", errors="replace").splitlines()
        stack_lines = [ln for ln in lines[_HEADER_LINES:] if ln.strip()]
        return stack_lines[:max_lines]
    except OSError:
        return []


def dump_replay_lines(
    dump_path: Path, *, max_lines: int = 120, max_bytes: int = 8192
) -> tuple[list[str], bool]:
    """Read dump stack content for journal replay, respecting size caps.

    Returns (lines, truncated) — up to *max_lines* non-empty stack lines
    totalling at most *max_bytes* of text.  *truncated* is True when the
    dump exceeded either limit.
    """
    try:
        content = dump_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], False
    all_lines = content.splitlines()
    stack_lines = [ln for ln in all_lines[_HEADER_LINES:] if ln.strip()]
    result: list[str] = []
    total = 0
    for ln in stack_lines:
        if len(result) >= max_lines or total + len(ln) > max_bytes:
            return result, True
        result.append(ln)
        total += len(ln)
    return result, False


def get_active_dump_file() -> DumpFile | None:
    """Return the currently active dump file (for passing to faulthandler)."""
    return _active_dump_file

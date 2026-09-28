"""Reclaim the per-run work directories that subagent and cron sessions leave behind.

Every ACP session runs in a work directory. A dashboard or channel session gets a
long-lived one keyed on its slot; a subagent or a stateless cron run gets one keyed
on its own session key -- ``workspace_root()/subagent_<id>``,
``workspace_root()/cron_<job>_<run>`` -- that nothing ever revisits once the run
ends. Preparing the spawn writes one file into it, ``.kiro/settings/cli.json``
(the skill-projection overlay, plus the lock sidecar that serializes writers), so a
host that runs many subagents or a stateless cron every minute accumulates
thousands of directories holding exactly that file. Two things pay for them: the
directory listing itself, and the skill-view aliases in ``~/.kiro/agents`` whose
work directory they are: a reclaim that keyed on the directory's existence could
never retire one, because the directory never went away.

This module removes such a directory when it holds ONLY that residue. The rule is
the safety argument, so it is stated once: the only files this module ever unlinks
are the two names Crew itself writes, inside the two directories Crew itself
creates, and every directory is removed with ``rmdir`` -- which fails on anything
non-empty. A run that wrote a report into its cwd, a resumed conversation's
project checkout, an operator's own file: none of it is reachable by this code,
because a directory holding it is simply not empty. Refusing is the default
answer; reclaiming needs the whole tree to be exactly the residue.

THE INVARIANT. A reclaim decision uses only evidence the deciding process OWNS:
its session registry for its own runs, and its own data home's pid ledger
(``kiro_session_pids.txt``, reaped at boot by ``cleanup_orphaned_sessions``) for
a dead predecessor gateway. It never probes the liveness of a pid another
process wrote to decide a deletion, and it never judges a directory marked by
another data home.

Provenance is written, never inferred. The provider that DERIVED a directory
from a one-run session key drops a marker file into it before the first spawn
(:func:`mark_run_dir`); that marker, and only that marker, says "Crew made this
directory for one run" and names the DATA HOME and the gateway process that made
it. A name under the workspace root says nothing -- a person can make
``subagent_deadbeef`` there -- and the contents of ``cli.json`` say nothing
either, because the projection merges its keys INTO a file that already exists,
so a Crew key in the file proves Crew touched it, not that Crew owns the
directory. Directories older builds left behind carry no marker and are never
reclaimed automatically; ``kirocrew doctor`` counts them and names the manual
remedy, the same way it treats the alias backlog.

Two callers. The session's own provider reclaims its directory at shutdown,
once its client has confirmed the process tree dead; its provenance is the
factory flag, so an absent marker is allowed, and a present marker must name
this data home AND this process exactly. The gateway sweeps the workspace root
for what a predecessor of its OWN data home left -- at boot, right after the
pid ledger was reaped, and on the hourly maintenance wake -- taking only MARKED
directories of this home whose gateway is another pid with no entry left in the
ledger, skipping any a live session in this process names and any
younger than a grace window. A directory marked by another data home is never
touched by either caller, whatever the state of the process it names: one
gateway runs per data home (``GatewayLock``), so the only same-home gateway pid
that is not ours is a predecessor's.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import stat
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.config.paths import config_dir
from kiro_crew.workspace_cli_settings import CLI_SETTINGS_LOCK_NAME

logger = logging.getLogger(__name__)

#: The one directory chain Crew creates inside a work dir, root first.
_RESIDUE_DIRS: tuple[str, ...] = (".kiro", "settings")
#: The only files Crew writes at the leaf. Anything else is somebody's data.
_RESIDUE_FILES: frozenset[str] = frozenset({"cli.json", CLI_SETTINGS_LOCK_NAME})
#: The provenance marker, at the work dir's root. Written by :func:`mark_run_dir`
#: for a DERIVED one-run directory only, never for a caller's cwd. Two ASCII
#: lines: the data-home identity (:func:`data_home_id`), then the gateway pid.
RUN_DIR_MARKER = ".kirocrew-run-dir"
#: Session-key shapes that mint ONE-RUN directories. Subagent ids are the hex
#: run ids ``SubagentManager._mint_agent_id`` draws -- sixteen characters on
#: this tree, eight on the shipped builds whose directories the sweep also has
#: to recognise; cron job and run ids are the eight-hex UUID prefixes minted in
#: ``cron.py``. A named agent never reaches a key in these shapes: the stable
#: sequence key ``cron:<job>:<agent>`` carries a name, not eight hex digits.
#: Both the key predicate and the directory-name filter are generated from this
#: table so their shapes cannot diverge.
_DERIVED_SESSION_SHAPES: tuple[tuple[str, tuple[int, ...]], ...] = (
    ("subagent", (16,)),
    ("subagent", (8,)),
    ("cron", (8, 8)),
)


def _derived_shape(separator: str) -> str:
    return "|".join(
        re.escape(prefix) + separator + separator.join(rf"[0-9a-f]{{{width}}}" for width in widths)
        for prefix, widths in _DERIVED_SESSION_SHAPES
    )


_DISPOSABLE_SESSION_KEY_RE = re.compile(rf"^(?:{_derived_shape(':')})$")
#: Used as the sweep's candidate filter and to COUNT unmarked leftovers for the
#: doctor; it authorizes no deletion on its own.
DERIVED_NAME_RE = re.compile(rf"^(?:{_derived_shape('_')})$")

#: Mirrors agent_scratch's bounded owner-marker discipline. The marker is inside
#: a directory the spawned process can write, so it is never read without a cap.
_OWNER_MARKER_MAX_BYTES = 4096
_PID_MAX = 2**31 - 1
_HOME_ID_HEX_CHARS = 24
_HOME_ID_RE = re.compile(rf"^[0-9a-f]{{{_HOME_ID_HEX_CHARS}}}$")

#: A swept directory must be idle this long. Not a liveness signal -- the
#: ledger and the live set are -- but a plain extra brake covering the window
#: between a spawn's ``mkdir`` and its provider showing up in the registry.
SWEEP_MIN_AGE_SECS = 3600.0
#: Bounds on one sweep. The root can hold tens of thousands of entries; a wake
#: that could not finish leaves the rest for the next one.
SWEEP_MAX_ENTRIES = 5000
SWEEP_MAX_SECONDS = 20.0

#: The marker rule a reclaim applies to a marker it finds; ``True`` permits.
MarkerRule = Callable[["_RunDirMarker"], bool]


def is_disposable_session_key(session_key: str | None) -> bool:
    """Whether a work directory DERIVED from *session_key* is one run's and disposable.

    Subagent sessions use ``subagent:<id>``. A cron's stable
    ``cron:<job_id>`` key belongs to its persistent session; only the stateless
    ``cron:<job_id>:<run_id>`` key is a one-run directory. Only a derived
    directory qualifies: a caller-supplied ``cwd`` is always the user's.
    """
    if not isinstance(session_key, str):
        return False
    return _DISPOSABLE_SESSION_KEY_RE.fullmatch(session_key) is not None


def _is_disposable_dir_name(name: str) -> bool:
    """Whether *name* has the exact one-run directory shape.

    This is only a sweep candidate filter; :data:`RUN_DIR_MARKER` remains the
    provenance required before deletion.
    """
    return DERIVED_NAME_RE.match(name) is not None


def data_home_id() -> str:
    """A stable identity of THIS data home, as the marker records it.

    The digest of the resolved ``config_dir()`` path: two gateways on separate
    data homes but one default workspace root get different ids, and the same
    home gets the same id across restarts and pid reuse. Never a path itself --
    the marker sits in a directory the spawned process can write.
    """
    resolved = os.path.normcase(os.path.realpath(config_dir()))
    return hashlib.sha256(resolved.encode("utf-8", "surrogateescape")).hexdigest()[
        :_HOME_ID_HEX_CHARS
    ]


@dataclass(frozen=True)
class _RunDirMarker:
    home: str
    gateway_pid: int


def _valid_pid(value: str) -> int:
    if not value or not value.isascii() or not value.isdigit():
        raise ValueError("run-directory marker names a non-integer pid")
    pid = int(value)
    if pid < 1 or pid > _PID_MAX:
        raise ValueError("run-directory marker names an impossible pid")
    return pid


def _marker_from_fd(fd: int) -> _RunDirMarker:
    """Read one bounded marker from an already-open, no-follow descriptor."""
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > _OWNER_MARKER_MAX_BYTES:
        raise ValueError("run-directory owner marker is not a bounded regular file")
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = _OWNER_MARKER_MAX_BYTES + 1
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > _OWNER_MARKER_MAX_BYTES:
        raise ValueError("run-directory owner marker is oversized")
    lines = data.decode("ascii").splitlines()
    if len(lines) != 2 or _HOME_ID_RE.fullmatch(lines[0]) is None:
        raise ValueError("run-directory owner marker has an unknown shape")
    return _RunDirMarker(lines[0], _valid_pid(lines[1]))


def _write_marker_to_fd(fd: int, marker: _RunDirMarker) -> None:
    encoded = f"{marker.home}\n{marker.gateway_pid}".encode("ascii")
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    view = memoryview(encoded)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _claim_owner_fd(fd: int, *, created: bool, identity_is_current: Callable[[], bool]) -> bool:
    """Claim a locked marker for this (home, pid) unless another home holds it.

    A marker of this home naming another pid is a predecessor's: one gateway
    runs per data home, so that process is not running, and the mark is
    replaced. A marker of another home is never touched, and neither is one
    this code cannot read.
    """
    own = _RunDirMarker(data_home_id(), os.getpid())
    try:
        with platform_compat.file_lock(fd, exclusive=True, timeout=2.0):
            if not created:
                existing = _marker_from_fd(fd)
                if existing.home != own.home:
                    return False
                if existing == own:
                    return identity_is_current()
            _write_marker_to_fd(fd, own)
            return identity_is_current()
    except (OSError, RecursionError, UnicodeError, ValueError):
        return False


def _open_marker_at(root_fd: int) -> tuple[int, bool]:
    flags = os.O_RDWR | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return (
            os.open(
                RUN_DIR_MARKER,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=root_fd,
            ),
            True,
        )
    except FileExistsError:
        return os.open(RUN_DIR_MARKER, flags, dir_fd=root_fd), False


def _open_marker_by_name(marker: Path) -> tuple[int, bool]:
    flags = os.O_RDWR | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(marker, flags | os.O_CREAT | os.O_EXCL, 0o600), True
    except FileExistsError:
        return os.open(marker, flags), False


def mark_run_dir(work_dir: Path) -> bool:
    """Record this data home and gateway as *work_dir*'s owner.

    Creates the marker, or leaves one already naming this (home, pid) as it is;
    replaces one this home's predecessor wrote; never touches another home's
    or an unreadable one, and returns ``False`` for those. The shutdown path
    keys on the factory flag, so a refused marker never fails session start --
    the directory simply continues unmarked and unsweepable.
    """
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    if not pinned_fs.supports_pinned_walk():
        if platform_compat.is_link_or_junction(work_dir):
            return False
        marker = work_dir / RUN_DIR_MARKER
        if platform_compat.is_link_or_junction(marker):
            return False
        try:
            marker_fd, created = _open_marker_by_name(marker)
        except OSError:
            return False
        try:
            opened = os.fstat(marker_fd)

            def current() -> bool:
                if platform_compat.is_link_or_junction(marker):
                    return False
                info = pinned_fs.lstat_by_name(marker)
                return info is not None and (info.st_dev, info.st_ino) == (
                    opened.st_dev,
                    opened.st_ino,
                )

            return _claim_owner_fd(marker_fd, created=created, identity_is_current=current)
        finally:
            os.close(marker_fd)
    try:
        root_fd = pinned_fs.open_dir_pinned(work_dir, what="session work directory")
    except (pinned_fs.PinnedPathRefusal, OSError):
        return False
    try:
        fd, created = _open_marker_at(root_fd)
    except OSError:
        os.close(root_fd)
        return False
    opened = os.fstat(fd)
    try:

        def current() -> bool:
            info = pinned_fs.stat_at(root_fd, RUN_DIR_MARKER)
            return info is not None and (info.st_dev, info.st_ino) == (
                opened.st_dev,
                opened.st_ino,
            )

        return _claim_owner_fd(fd, created=created, identity_is_current=current)
    finally:
        os.close(fd)
        os.close(root_fd)


def _read_marker_at(root_fd: int) -> tuple[_RunDirMarker, float] | None:
    """The marker and its mtime, relative to a pinned work-directory descriptor.

    ``None`` for anything that is not a readable marker: absent, a link, not a
    regular file, oversized, or garbled. Every caller keeps the directory then.
    """
    try:
        fd = os.open(
            RUN_DIR_MARKER,
            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
    except OSError:
        return None
    try:
        opened = os.fstat(fd)
        info = pinned_fs.stat_at(root_fd, RUN_DIR_MARKER)
        if info is None or (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            return None
        return _marker_from_fd(fd), info.st_mtime
    except (OSError, RecursionError, UnicodeError, ValueError):
        return None
    finally:
        os.close(fd)


def _read_marker_by_name(marker: Path) -> tuple[_RunDirMarker, float] | None:
    """The by-name form of :func:`_read_marker_at` (Windows)."""
    if platform_compat.is_link_or_junction(marker):
        return None
    try:
        fd = os.open(
            marker,
            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError:
        return None
    try:
        opened = os.fstat(fd)
        info = pinned_fs.lstat_by_name(marker)
        if info is None or (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            return None
        return _marker_from_fd(fd), info.st_mtime
    except (OSError, RecursionError, UnicodeError, ValueError):
        return None
    finally:
        os.close(fd)


def _read_run_dir_marker(work_dir: Path) -> _RunDirMarker | None:
    """Read *work_dir*'s marker through the platform's bounded path; fail closed."""
    if not pinned_fs.supports_pinned_walk():
        if platform_compat.is_link_or_junction(work_dir):
            return None
        read = _read_marker_by_name(work_dir / RUN_DIR_MARKER)
        return None if read is None else read[0]
    try:
        root_fd = pinned_fs.open_dir_pinned(work_dir, what="session work directory")
    except (pinned_fs.PinnedPathRefusal, OSError):
        return None
    try:
        read = _read_marker_at(root_fd)
    finally:
        os.close(root_fd)
    return None if read is None else read[0]


def own_marker_rule() -> MarkerRule:
    """The shutdown rule: the marker must name THIS data home and THIS process.

    Nothing about another process is probed. A marker of another home, or of a
    predecessor of this home, refuses the shutdown reclaim; the sweep judges the
    predecessor's from the ledger.
    """
    own = _RunDirMarker(data_home_id(), os.getpid())
    return lambda marker: marker == own


def predecessor_marker_rule(retained_gateway_pids: Collection[int]) -> MarkerRule:
    """The sweep rule: a dead predecessor of THIS data home, per this home's ledger.

    Permits a marker of this home whose gateway pid is not this process and has
    no entry left in ``kiro_session_pids.txt`` (*retained_gateway_pids*): the
    boot reap has already confirmed and removed every child it could account
    for, so a pid that still has an entry may have something alive and is kept.
    Another home's marker is never permitted, whatever its pid is doing.
    """
    home = data_home_id()
    own_pid = os.getpid()
    retained = frozenset(retained_gateway_pids)
    return (
        lambda marker: marker.home == home
        and marker.gateway_pid != own_pid
        and marker.gateway_pid not in retained
    )


def reclaim_session_work_dir(
    work_dir: Path,
    *,
    min_age_secs: float = 0.0,
    require_marker: bool = False,
    marker_permits: MarkerRule | None = None,
    now: float | None = None,
) -> bool:
    """Remove *work_dir* when it holds only Crew's own residue; otherwise leave it.

    ``True`` means the directory is gone. ``False`` covers every other outcome and
    is never an error: the directory was already gone, holds something that is
    not residue, is younger than *min_age_secs* (judged by the newest mtime in the
    residue tree), sits behind a link, or the filesystem refused. Nothing here
    raises to the caller -- reclaiming is hygiene, and hygiene must never be the
    thing that fails a session's shutdown.

    With *require_marker*, :data:`RUN_DIR_MARKER` must be present at the root:
    the sweep passes it because it knows the directory only by its name, and a
    name is not provenance. The shutdown path does not require one, because the
    factory that derived the name is its provenance. A marker that IS present is
    read once, bounded and without following links, and must satisfy
    *marker_permits* -- :func:`own_marker_rule` when omitted, which is the
    shutdown path's (home, pid) equality; the sweep passes
    :func:`predecessor_marker_rule`. A marker that cannot be read keeps the
    directory under either rule.

    Every step addresses the tree through descriptors where the platform allows,
    so a component swapped for a link after the check is refused rather than
    followed. What ``rmdir`` alone cannot rule out is a swap for another EMPTY
    directory between two adjacent syscalls, whose worst outcome is that empty
    directory removed; the two file names it unlinks are Crew's own, so a swap
    there can cost a file of that name and nothing else.
    """
    permits = own_marker_rule() if marker_permits is None else marker_permits
    if not pinned_fs.supports_pinned_walk():
        return _reclaim_by_name(
            work_dir,
            min_age_secs=min_age_secs,
            require_marker=require_marker,
            marker_permits=permits,
            now=now,
        )
    try:
        root_fd = pinned_fs.open_dir_pinned(work_dir, what="session work directory")
    except (pinned_fs.PinnedPathRefusal, OSError):
        return False
    opened = [root_fd]
    try:
        newest = os.fstat(root_fd).st_mtime
        fd = root_fd
        # Walk down the one chain Crew creates. Each level may hold ONLY the next
        # level (or, at the leaf, only the residue files); anything else ends the
        # reclaim before a single unlink.
        marked = False
        for depth, child in enumerate(_RESIDUE_DIRS):
            entries = list(os.scandir(fd))
            names = {entry.name for entry in entries}
            if depth == 0 and RUN_DIR_MARKER in names:
                read = _read_marker_at(fd)
                if read is None or not permits(read[0]):
                    return False
                newest = max(newest, read[1])
                marked = True
                names.discard(RUN_DIR_MARKER)
            if not names:
                # An already-emptied prefix of the chain: still ours to remove.
                break
            if names != {child}:
                return False
            entry_info = pinned_fs.stat_at(fd, child)
            if entry_info is None or not stat.S_ISDIR(entry_info.st_mode):
                return False
            newest = max(newest, entry_info.st_mtime)
            try:
                fd = os.open(child, pinned_fs.dir_flags(), dir_fd=fd)
            except OSError:
                return False
            opened.append(fd)
            if depth == len(_RESIDUE_DIRS) - 1:
                leaf_entries = list(os.scandir(fd))
                for entry in leaf_entries:
                    if entry.name not in _RESIDUE_FILES:
                        return False
                    info = pinned_fs.stat_at(fd, entry.name)
                    if info is None or not stat.S_ISREG(info.st_mode):
                        return False
                    newest = max(newest, info.st_mtime)
        if require_marker and not marked:
            return False
        current = time.time() if now is None else now
        if min_age_secs > 0 and current - newest < min_age_secs:
            return False
        # Unlink the residue files at the deepest opened level (only the leaf has
        # any), then rmdir each level from the leaf up through the parent's
        # descriptor. A directory that gained an entry meanwhile fails its rmdir
        # and the reclaim stops there, leaving what arrived in place.
        leaf_fd = opened[-1]
        if len(opened) == len(_RESIDUE_DIRS) + 1:
            for entry in list(os.scandir(leaf_fd)):
                if entry.name not in _RESIDUE_FILES or not pinned_fs.is_regular_at(
                    leaf_fd, entry.name
                ):
                    return False
                try:
                    os.unlink(entry.name, dir_fd=leaf_fd)
                except OSError:
                    return False
        for level in range(len(opened) - 1, 0, -1):
            try:
                os.rmdir(_RESIDUE_DIRS[level - 1], dir_fd=opened[level - 1])
            except OSError:
                return False
        if marked:
            try:
                os.unlink(RUN_DIR_MARKER, dir_fd=root_fd)
            except OSError:
                return False
        parent = os.path.realpath(work_dir.parent)
        try:
            parent_fd = pinned_fs.pin_parent(parent, what="session work directory")
        except (pinned_fs.PinnedPathRefusal, OSError):
            return False
        try:
            os.rmdir(work_dir.name, dir_fd=parent_fd)
        except OSError:
            return False
        finally:
            os.close(parent_fd)
        return True
    except (OSError, RecursionError, UnicodeError, ValueError):
        return False
    finally:
        pinned_fs.close_all(opened)


def _reclaim_by_name(
    work_dir: Path,
    *,
    min_age_secs: float,
    require_marker: bool,
    marker_permits: MarkerRule,
    now: float | None,
) -> bool:
    """The by-name form for platforms without descriptor-relative opens (Windows).

    Same rule, same refusals; what it lacks is the pin, which the platform's
    mandatory file locking partly stands in for -- an open ``cli.json`` cannot be
    unlinked there, so a live writer is refused by the OS itself.
    """
    if platform_compat.is_link_or_junction(work_dir):
        return False
    info = pinned_fs.lstat_by_name(work_dir)
    if info is None or not stat.S_ISDIR(info.st_mode):
        return False
    newest = info.st_mtime
    levels = [work_dir]
    marked = False
    try:
        for depth, child in enumerate(_RESIDUE_DIRS):
            names = set(os.listdir(levels[-1]))
            if depth == 0 and RUN_DIR_MARKER in names:
                read = _read_marker_by_name(work_dir / RUN_DIR_MARKER)
                if read is None or not marker_permits(read[0]):
                    return False
                newest = max(newest, read[1])
                marked = True
                names.discard(RUN_DIR_MARKER)
            if not names:
                break
            if names != {child}:
                return False
            path = levels[-1] / child
            if platform_compat.is_link_or_junction(path):
                return False
            child_info = pinned_fs.lstat_by_name(path)
            if child_info is None or not stat.S_ISDIR(child_info.st_mode):
                return False
            newest = max(newest, child_info.st_mtime)
            levels.append(path)
            if depth == len(_RESIDUE_DIRS) - 1:
                for name in os.listdir(path):
                    if name not in _RESIDUE_FILES or platform_compat.is_link_or_junction(
                        path / name
                    ):
                        return False
                    file_info = pinned_fs.lstat_by_name(path / name)
                    if file_info is None or not stat.S_ISREG(file_info.st_mode):
                        return False
                    newest = max(newest, file_info.st_mtime)
        if require_marker and not marked:
            return False
        current = time.time() if now is None else now
        if min_age_secs > 0 and current - newest < min_age_secs:
            return False
        if len(levels) == len(_RESIDUE_DIRS) + 1:
            for name in os.listdir(levels[-1]):
                if name not in _RESIDUE_FILES:
                    return False
                (levels[-1] / name).unlink()
        for path in reversed(levels[1:]):
            path.rmdir()
        if marked:
            marker = work_dir / RUN_DIR_MARKER
            if platform_compat.is_link_or_junction(marker):
                return False
            marker.unlink()
        work_dir.rmdir()
    except (OSError, RecursionError, UnicodeError, ValueError):
        return False
    return True


def sweep_predecessor_work_dirs(
    root: Path,
    *,
    retained_gateway_pids: Collection[int],
    live_work_dirs: Collection[str],
    min_age_secs: float = SWEEP_MIN_AGE_SECS,
    max_entries: int = SWEEP_MAX_ENTRIES,
    max_seconds: float = SWEEP_MAX_SECONDS,
    now: float | None = None,
) -> int:
    """Reclaim the residue-only run directories a predecessor gateway of this home left.

    Runs at gateway boot, right after ``cleanup_orphaned_sessions`` reaped this
    home's pid ledger, and again on the hourly wake for whatever the bounds
    left. *retained_gateway_pids* is what the ledger still holds
    (``session_pid.retained_gateway_pids``); *live_work_dirs* are the work
    directories of every session this process currently holds, as the provider
    spells them, and an entry whose path is in that set is skipped whatever it
    holds. The rest are judged by :func:`reclaim_session_work_dir` with the
    marker required and :func:`predecessor_marker_rule`: a directory marked by
    THIS process is never its business, a directory marked by another data home
    is never touched, a directory whose gateway still has ledger entries is kept,
    and a directory Crew did not mark -- a person's, or an older build's -- is
    never residue however old it is. *min_age_secs* is the plain extra brake for
    a spawn's directory between its ``mkdir`` and its registration.

    Bounded twice, by entries examined and by wall clock, because the root this
    exists for holds tens of thousands of entries and a maintenance wake must
    not turn into a minutes-long scan -- and bounded in what it RETAINS: the
    listing is streamed, never collected, so a root of any size costs the sweep
    a few counters and nothing that grows with the backlog. Whatever a bound
    leaves is reached by a later wake: the walk starts at a random offset into
    the directory's own order (counted in one streaming pass, then skipped in
    the next) and wraps, so an unreclaimable prefix cannot hide the rest forever.
    """
    if platform_compat.is_link_or_junction(root):
        return 0
    deadline = time.monotonic() + max_seconds
    try:
        total = 0
        with os.scandir(root) as it:
            for entry in it:
                if _is_disposable_dir_name(entry.name):
                    total += 1
                if time.monotonic() >= deadline:
                    # A root too large to even count inside the budget is left
                    # for a wake with more of it; nothing is judged blind.
                    logger.debug("session work dirs: sweep budget spent counting %d", total)
                    return 0
    except OSError:
        return 0
    if not total:
        return 0
    permits = predecessor_marker_rule(retained_gateway_pids)
    live = {os.path.normcase(str(path)) for path in live_work_dirs}
    offset = secrets.randbelow(total)
    examined = 0
    reclaimed = 0
    bounded = False

    def judge(name: str) -> None:
        nonlocal examined, reclaimed
        examined += 1
        candidate = root / name
        if os.path.normcase(str(candidate)) in live:
            return
        if reclaim_session_work_dir(
            candidate,
            min_age_secs=min_age_secs,
            require_marker=True,
            marker_permits=permits,
            now=now,
        ):
            reclaimed += 1

    # Two streaming passes over the same order: the tail from the offset, then
    # the head up to it. Each pass is its own listing, so an entry the first
    # pass removed is simply not there for the second.
    for tail in (True, False):
        position = 0
        try:
            with os.scandir(root) as it:
                for entry in it:
                    if not _is_disposable_dir_name(entry.name):
                        continue
                    position += 1
                    if tail != (position > offset):
                        continue
                    if examined >= max_entries or time.monotonic() >= deadline:
                        bounded = True
                        break
                    judge(entry.name)
        except OSError:
            break
        if bounded:
            break
    if bounded:
        logger.debug(
            "session work dirs: sweep bound reached after %d of %d entries", examined, total
        )
    if reclaimed:
        logger.info("session work dirs: reclaimed %d idle run director(ies)", reclaimed)
    return reclaimed


@dataclass(frozen=True)
class RunDirCensus:
    """What ``kirocrew doctor`` reports about the run directories under the workspace root.

    *unmarked* directories carry no :data:`RUN_DIR_MARKER` (an older build's);
    *refused* ones carry a marker this data home's sweep cannot act on -- another
    home's, one that cannot be read, or one whose gateway the pid ledger still
    retains entries for. The sweep touches neither. *floor* says the walk hit
    its entry cap, so both counts are minimums.
    """

    unmarked: int = 0
    refused: int = 0
    floor: bool = False


def count_run_dirs(
    root: Path, *, retained_gateway_pids: Collection[int], max_entries: int = 100000
) -> RunDirCensus:
    """Count the run-shaped directories under *root* the sweep will never reclaim.

    Read-only, for ``kirocrew doctor``, and judged by the sweep's own rule
    (:func:`predecessor_marker_rule` over *retained_gateway_pids*): the
    directories older builds left behind and the marked ones the rule refuses
    are exactly what the sweep leaves, so the operator has to hear about them
    somewhere. One walk over names matching :data:`DERIVED_NAME_RE`, two
    counters; stops at *max_entries* names examined and says so, since a root
    this exists for can hold tens of thousands.
    """
    if platform_compat.is_link_or_junction(root):
        return RunDirCensus()
    permits = predecessor_marker_rule(retained_gateway_pids)
    unmarked = 0
    refused = 0
    examined = 0
    try:
        with os.scandir(root) as it:
            for entry in it:
                if not DERIVED_NAME_RE.match(entry.name):
                    continue
                examined += 1
                if examined > max_entries:
                    return RunDirCensus(unmarked, refused, True)
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    marker = pinned_fs.lstat_by_name(root / entry.name / RUN_DIR_MARKER)
                except OSError:
                    continue
                if marker is None:
                    unmarked += 1
                    continue
                record = _read_run_dir_marker(root / entry.name)
                if record is None or not permits(record):
                    refused += 1
    except OSError:
        return RunDirCensus(unmarked, refused, False)
    return RunDirCensus(unmarked, refused, False)

"""The projection's side files under this data home, and the protocol publishers share over them.

Their layout and the legacy locations in the agents directory, the alias-name shape
every reader admits, the bounded publication lock with its two timeouts and its
section budget, the per-process leases with their one bounded liveness scan, and the
identity-checked unlinks, with their one rate-limited refusal report, that retire any
of them.

:mod:`kiro_crew.acp.skill_projection` stays the import path and the patch surface; see
:mod:`kiro_crew.acp.skill_projection_parts`.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import stat
import threading
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.agent_spec_format import NATIVE_SKILL_ALIAS_PREFIX
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.acp.skill_projection")


# The projection's SIDE FILES -- the publication lock, the per-process lease
# records and the ownership sidecars -- live under Kiro Crew's own data home,
# NOT beside the views in the shared ``~/.kiro/agents/`` directory. Only the
# views themselves must be there (kiro-cli resolves ``--agent <name>`` against
# that directory by name and knows no other); everything else there is noise the
# Kiro IDE lists in its agent drop-down as if it were an agent. The three names
# below are the LEGACY locations, which builds before this layout wrote into the
# agents directory: they are still READ for one release cycle (an older build
# sharing the directory keeps publishing there) and migrated at boot
# (:func:`_migrate_legacy_side_files`), but nothing is written there anymore.
_PROJECTION_LOCK_NAME = ".kirocrew-skill-projection.lock"
_PROJECTION_LEASE_DIR_NAME = ".kirocrew-skill-projection-leases"
_PROJECTION_SIDE_DIR_NAME = "skill-projection"
_PROJECTION_SIDE_LOCK_NAME = "publication.lock"
_PROJECTION_SIDE_LEASE_DIR_NAME = "leases"
_PROJECTION_SIDE_METADATA_DIR_NAME = "metadata"
# A lease is a readable record plus an unread lock target. Windows file locks are
# mandatory, so a lock on the record itself makes every reader's probe fail.
_PROJECTION_LEASE_RECORD_SUFFIX = ".json"
_PROJECTION_LEASE_HOLDER_SUFFIX = ".hold"
# The exact stem _acquire_projection_lease writes (``<pid>-<uuid4 hex>``). A
# record-less ``.hold`` is reclaimed only when its stem matches, because nothing
# else about an empty file proves this module wrote it: an operator's
# ``notes.hold`` in the lease directory is left alone. (A record/holder pair is
# reclaimed only after its record parses as a lease record, which is that proof.)
_PROJECTION_LEASE_STEM_RE = re.compile(r"[0-9]+-[0-9a-f]{32}")
# ONE bound for both ends of the lease. The reader answers "live" above it, so a
# writer allowed to exceed it could publish a record that is unreclaimable by
# construction: a crash would leave it on disk and every later probe would read
# it as held, disabling pruning for good. Refusing the publication instead falls
# back to authored native agents, which is recoverable on the next spawn.
_PROJECTION_LEASE_MAX_ALIASES = 1024
_PROJECTION_LEASE_MAX_BYTES = 65536
# Lease discovery is a startup-path liveness check. Stream records and stop
# after bounded work; an incomplete scan is uncertainty and therefore live.
_PROJECTION_LEASE_SCAN_LIMIT = 4096
# The scan's wall-clock bound, BETWEEN entries: the entry cap above bounds how
# many parse-plus-lock-probe steps run, not how long a slow filesystem takes
# over them, and the scan runs under the publication lock. Expiry is handled
# exactly like the cap (validated residue drains, the pass answers uncertain).
# It shares _PROJECTION_LOCK_TIMEOUT_SECS with the prune walk's own
# _PRUNE_MAX_SECONDS_PER_RUN and the publication writes, so it is sized to match
# that walk's budget rather than to fill the ceiling.
_PROJECTION_LEASE_SCAN_MAX_SECONDS = 0.4
# The exact shape prepare_native_skill_projection derives, so a legacy reclaim
# admits only names the projection could have produced. The digest length is pinned
# here rather than recomputed from the writer, because widening the writer must
# not silently widen what the reclaim is willing to delete.
_LEGACY_ALIAS_NAME_RE = re.compile(re.escape(NATIVE_SKILL_ALIAS_PREFIX) + r"[0-9a-f]{24}")
# One warning per this many seconds when an alias unlink is refused by the OS.
# Silence here is what turned a permission problem into a wrong root cause: a
# read-only or foreign-owned agents directory made every reclaim a no-op and
# nothing said so. Per-file logging would flood at backlog scale, so the line
# carries how many refusals it stands for.
_UNLINK_WARNING_INTERVAL_SECS = 300.0
# The refusals that DO mean the directory is not writable by this process. Any
# other errno (ENOENT after a concurrent prune elsewhere took the file, EMFILE,
# EIO, ...) is reported by name without that diagnosis, which would send an
# operator to fix a mount or an owner that is fine.
_UNWRITABLE_ERRNOS: frozenset[int] = frozenset({errno.EACCES, errno.EPERM, errno.EROFS})
_PROJECTION_METADATA_DIR_NAME = ".kirocrew-skill-projection-metadata"


#: The side directory a projection that is ending was prepared under, for the
#: thread running its eager prune. A finalizer fires at an arbitrary later moment, so
#: it must not ask the process which data home is current: it prunes the home it
#: published into.
_PINNED_SIDE_DIR = threading.local()


def _projection_side_dir() -> Path:
    """Where this data home keeps the projection's side files (never a view)."""
    pinned = getattr(_PINNED_SIDE_DIR, "path", None)
    return pinned if pinned is not None else data_home() / _PROJECTION_SIDE_DIR_NAME


def _projection_lock_path() -> Path:
    """The publication lock every publisher and pruner of THIS data home takes."""
    return _projection_side_dir() / _PROJECTION_SIDE_LOCK_NAME


def _projection_lease_dir() -> Path:
    """Where this data home's processes publish their lease records."""
    return _projection_side_dir() / _PROJECTION_SIDE_LEASE_DIR_NAME


def _projection_metadata_dir() -> Path:
    """Where this data home records which views it owns (sidecars and the ledger)."""
    return _projection_side_dir() / _PROJECTION_SIDE_METADATA_DIR_NAME


def _legacy_lock_path(directory: Path) -> Path:
    """The publication lock a build before the side-file layout took in *directory*."""
    return directory / _PROJECTION_LOCK_NAME


def _legacy_lease_dir(directory: Path) -> Path:
    """Where a build before the side-file layout published leases: in *directory*."""
    return directory / _PROJECTION_LEASE_DIR_NAME


def _legacy_metadata_dir(directory: Path) -> Path:
    """Where a build before the side-file layout wrote ownership sidecars."""
    return directory / _PROJECTION_METADATA_DIR_NAME


def _metadata_dirs(directory: Path) -> tuple[Path, Path]:
    """Ownership dirs in read order: this home's own, then the legacy one in *directory*.

    Both are READ (an alias published by an older build has its record only in
    the legacy one until the boot migration moves it); only the first is written.
    """
    return _projection_metadata_dir(), _legacy_metadata_dir(directory)


# Publication plus pruning is normally sub-second. Two seconds absorbs scheduler
# jitter and short Windows rename retries without inheriting the generic five-minute
# lock ceiling on the native startup path.
_PROJECTION_LOCK_TIMEOUT_SECS = 2.0
# Threads of THIS process queue on an in-process lock before the file lock, so
# the file lock's ceiling above measures cross-process contention only. Without
# it, N concurrent session starts in one gateway each race the file lock on
# their own clock, and in a burst the later ones time out and start without
# their skill view although no other process holds the lock. The in-process
# wait is bounded well under the session/new budget; every caller runs off the
# event loop.
_IN_PROCESS_PROJECTION_WAIT_SECS = 30.0


class InProcessProjectionWaitTimeout(OSError):
    """The in-process queue for the alias lock timed out.

    Another start in THIS process held the lock the whole wait; no other
    process is involved, so a caller reporting it must not blame one.
    """


# Named by the path callers import it from: a traceback or a log line renders the
# raised class as ``module.qualname``, so the owner module must not leak into it.
InProcessProjectionWaitTimeout.__module__ = "kiro_crew.acp.skill_projection"


class CrossProcessProjectionLockTimeout(OSError):
    """The alias file lock could not be taken: another process holds it."""


# Named by the path callers import it from: a traceback or a log line renders the
# raised class as ``module.qualname``, so the owner module must not leak into it.
CrossProcessProjectionLockTimeout.__module__ = "kiro_crew.acp.skill_projection"


_IN_PROCESS_PROJECTION_LOCKS: dict[str, threading.Lock] = {}
_IN_PROCESS_PROJECTION_LOCKS_GUARD = threading.Lock()
# Queued starts pay for each other's locked sections one after another, and the
# per-spawn prune walk is most of a section. Pruning only reclaims stale files,
# so a burst gets one walk per this interval per agents directory; the boot
# drain prunes on its own schedule and is unaffected.
_PER_SPAWN_PRUNE_MIN_INTERVAL_SECS = 5.0
_LAST_PER_SPAWN_PRUNE: dict[str, float] = {}
# The share of that ceiling the prune walk must leave to the rest of its locked
# section: the publication writes (two atomic writes per alias plus the settings
# commit) and scheduler jitter. The lease scan's budget is taken out on its own.
_PROJECTION_PUBLICATION_RESERVE_SECS = 1.2


def _prune_budget_within(ceiling: float) -> float:
    """The walk's share of ``ceiling`` once the lease scan and publication reserve are out."""
    return ceiling - _PROJECTION_PUBLICATION_RESERVE_SECS - _PROJECTION_LEASE_SCAN_MAX_SECONDS


def _in_process_projection_lock(directory: Path) -> threading.Lock:
    """The one in-process lock for *directory*'s alias publication and pruning."""
    key = directory.absolute().as_posix()
    with _IN_PROCESS_PROJECTION_LOCKS_GUARD:
        lock = _IN_PROCESS_PROJECTION_LOCKS.get(key)
        if lock is None:
            lock = _IN_PROCESS_PROJECTION_LOCKS[key] = threading.Lock()
        return lock


def _per_spawn_prune_due(directory: Path) -> bool:
    """Whether this spawn runs the prune walk; call under the alias lock."""
    key = directory.absolute().as_posix()
    now = time.monotonic()
    last = _LAST_PER_SPAWN_PRUNE.get(key)
    if last is not None and 0.0 <= now - last < _PER_SPAWN_PRUNE_MIN_INTERVAL_SECS:
        return False
    _LAST_PER_SPAWN_PRUNE[key] = now
    return True


def _ensure_real_directory(path: Path, what: str) -> Path:
    """Create *path* and verify it is a real directory, not a link or junction."""
    if platform_compat.is_link_or_junction(path):
        raise OSError(f"skill projection {what} is a symlink or junction")
    path.mkdir(parents=True, exist_ok=True)
    info = pinned_fs.lstat_by_name(path)
    if platform_compat.is_link_or_junction(path) or info is None or not stat.S_ISDIR(info.st_mode):
        raise OSError(f"skill projection {what} is not a real directory")
    return path


def _projection_alias_lock(directory: Path, *, in_process_wait: float | None = None) -> ExitStack:
    """Acquire the bounded cross-process lock for alias publication and pruning.

    Threads of this process first queue on :func:`_in_process_projection_lock`,
    so only one of them at a time competes for the file lock. *in_process_wait*
    is how long to queue there (:data:`_IN_PROCESS_PROJECTION_WAIT_SECS` when
    ``None``); ``0.0`` takes the lock only if it is free. The file lock itself
    lives under this data home (:func:`_projection_lock_path`), not in
    *directory*: it serialises the publishers and pruners of THIS home, which
    are the only ones that write this home's views and side files.
    """
    if in_process_wait is None:
        in_process_wait = _IN_PROCESS_PROJECTION_WAIT_SECS
    stack = ExitStack()
    local = _in_process_projection_lock(directory)
    if not local.acquire(timeout=in_process_wait):
        raise InProcessProjectionWaitTimeout(
            "skill projection lock still busy inside this process after " f"{in_process_wait:g}s"
        )
    stack.callback(local.release)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        lock_path = _projection_lock_path()
        _ensure_real_directory(lock_path.parent, "side directory")
        if platform_compat.is_link_or_junction(lock_path):
            raise OSError("skill projection lock is a symlink or junction")
        lock_fd = stack.enter_context(platform_compat.open_lock_file(lock_path))
        opened = os.fstat(lock_fd)
        named = pinned_fs.lstat_by_name(lock_path)
        if (
            platform_compat.is_link_or_junction(lock_path)
            or named is None
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise OSError("skill projection lock changed while it was opened")
        try:
            stack.enter_context(
                platform_compat.file_lock(
                    lock_fd, exclusive=True, timeout=_PROJECTION_LOCK_TIMEOUT_SECS
                )
            )
        except OSError as exc:
            # file_lock reports its timed-out wait as an errno-less OSError; an
            # errno (ENOLCK, EBADF) is about this fd, not another holder.
            if exc.errno is not None:
                raise
            raise CrossProcessProjectionLockTimeout(str(exc)) from exc
        current = pinned_fs.lstat_by_name(lock_path)
        if (
            platform_compat.is_link_or_junction(lock_path)
            or current is None
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise OSError("skill projection lock changed while it was acquired")
    except BaseException:
        # Any failure, not only OSError: the stack holds the in-process lock, and
        # a leaked one makes every later start in this process wait it out.
        stack.close()
        raise
    return stack


def _ensure_projection_metadata_directory() -> Path:
    """Create and verify this data home's directory of ownership sidecars."""
    return _ensure_real_directory(_projection_metadata_dir(), "metadata directory")


def _unlink_projection_lease_if_unchanged(
    path: Path, identity: tuple[int, int], *, what: str = "projection record"
) -> bool:
    """Remove one unlocked lease only while its random name keeps its identity.

    *what* names the kind of file for the refusal report: a lease record, its
    holder sidecar, or an ownership sidecar. A refusal from the filesystem is
    reported through the same rate-limited seam as an alias unlink -- the same
    environment fault silences both, and one that is only half reported is the
    wrong root cause again. An identity change stays a silent ``False``.
    """
    current = pinned_fs.lstat_by_name(path)
    if (
        current is None
        or platform_compat.is_link_or_junction(path)
        or not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino) != identity
    ):
        return False
    if pinned_fs.supports_pinned_walk() and os.unlink in os.supports_dir_fd:
        try:
            parent_fd = os.open(path.parent, pinned_fs.dir_flags())
        except OSError as exc:
            _warn_unlink_refused(path, exc, what=what)
            return False
        try:
            return pinned_fs.unlink_verified(
                parent_fd,
                path.name,
                identity,
                on_error=lambda exc: _warn_unlink_refused(path, exc, what=what),
            )
        finally:
            os.close(parent_fd)
    if not platform_compat.IS_WINDOWS:
        return False
    try:
        path.unlink()
    except OSError as exc:
        _warn_unlink_refused(path, exc, what=what)
        return False
    return True


def _acquire_projection_lease(directory: Path, aliases: set[str]) -> ExitStack:
    """Publish and hold one process lease covering this projection's aliases.

    The lease is TWO files: a ``.json`` record naming the aliases, which is never
    locked, and a ``.lock`` sidecar that carries the lifetime lock and is never
    read. They are split because Windows file locks are MANDATORY, not advisory:
    :func:`platform_compat.file_lock` takes ``msvcrt.locking`` on byte 0, and a
    read of a locked byte from any other handle -- including another handle in
    this same process -- fails with a lock violation. Holding the lock on the
    record a reader must parse therefore made every liveness probe raise, which
    :func:`_scan_projection_leases` reads as uncertainty and answers "live", so
    nothing was ever reclaimed on Windows while a single lease was held. Locking
    a file nobody reads keeps the OS liveness proof and leaves the record legible.
    """
    del directory  # the lease lives under this data home, not beside the views
    stack = ExitStack()
    if not aliases:
        return stack
    try:
        lease_dir = _ensure_real_directory(_projection_lease_dir(), "lease directory")
        stem = f"{os.getpid()}-{uuid.uuid4().hex}"
        lease_path = lease_dir / f"{stem}{_PROJECTION_LEASE_RECORD_SUFFIX}"
        holder_path = lease_dir / f"{stem}{_PROJECTION_LEASE_HOLDER_SUFFIX}"
        record = json.dumps({"aliases": sorted(aliases)}, separators=(",", ":"))
        if (
            len(aliases) > _PROJECTION_LEASE_MAX_ALIASES
            or len(record.encode()) > _PROJECTION_LEASE_MAX_BYTES
        ):
            # Publishing past the reader's own bound would leave a record no
            # reclaim can ever retire. Refuse instead: the caller falls back to
            # authored native agents and the next spawn tries again.
            raise OSError(
                f"skill projection lease would exceed its reader's bound "
                f"({len(aliases)} aliases, {len(record.encode())} bytes)"
            )
        atomic_write(lease_path, record, restrict_to_owner=True)
        created = pinned_fs.lstat_by_name(lease_path)
        if created is None or not stat.S_ISREG(created.st_mode):
            raise OSError("skill projection lease was not published as a regular file")
        identity = (created.st_dev, created.st_ino)
        # Registered before the descriptor contexts so ExitStack releases the
        # lease lock and file handle first (required for unlink on Windows).
        stack.callback(
            _unlink_projection_lease_if_unchanged, lease_path, identity, what="lease record"
        )
        atomic_write(holder_path, "", restrict_to_owner=True)
        holder_created = pinned_fs.lstat_by_name(holder_path)
        if holder_created is None or not stat.S_ISREG(holder_created.st_mode):
            raise OSError("skill projection lease holder was not published as a regular file")
        holder_identity = (holder_created.st_dev, holder_created.st_ino)
        stack.callback(
            _unlink_projection_lease_if_unchanged,
            holder_path,
            holder_identity,
            what="lease holder",
        )
        holder_fd = stack.enter_context(platform_compat.open_lock_file(holder_path))
        opened = os.fstat(holder_fd)
        named = pinned_fs.lstat_by_name(holder_path)
        if (
            platform_compat.is_link_or_junction(lease_path)
            or platform_compat.is_link_or_junction(holder_path)
            or named is None
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or (opened.st_dev, opened.st_ino) != holder_identity
            or (named.st_dev, named.st_ino) != holder_identity
        ):
            raise OSError("skill projection lease changed while it was opened")
        stack.enter_context(platform_compat.file_lock(holder_fd, exclusive=True, wait=False))
    except OSError:
        stack.close()
        raise
    return stack


def _read_lease_record(lease_path: Path) -> list[str] | None:
    """The alias list one lease record names, or ``None`` when it cannot be trusted.

    The ONE parse of the record shape, shared by the liveness probe and the
    census so the two cannot disagree. Bounded by the writer's own limits
    (:data:`_PROJECTION_LEASE_MAX_BYTES`, :data:`_PROJECTION_LEASE_MAX_ALIASES`):
    an oversized, malformed, or non-list record is ``None``, and so is any read
    error. A plain bounded read, not the hardened one: this runs once per lease
    on EVERY spawn and every set_mode, and the hardened reader adds path
    validation and an audit write per call, which is measurable on the
    projected-MCP E2E -- it passes at 288s against a 300s ceiling, so a few
    percent decides it. No lock is ever taken on this file, so the read cannot
    collide with a holder the way the pre-split single-file lease did.
    """
    try:
        record_fd = os.open(lease_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            raw = os.read(record_fd, _PROJECTION_LEASE_MAX_BYTES + 1)
        finally:
            os.close(record_fd)
        if len(raw) > _PROJECTION_LEASE_MAX_BYTES:
            return None
        body = json.loads(raw)
    except (OSError, ValueError, TypeError, RecursionError):
        # RecursionError is a RuntimeError, not a ValueError: a hand-authored
        # record nested past the interpreter limit must read as untrusted, not
        # abort the caller. No writer in this module produces one.
        return None
    listed = body.get("aliases") if isinstance(body, dict) else None
    if (
        not isinstance(listed, list)
        or len(listed) > _PROJECTION_LEASE_MAX_ALIASES
        or any(not isinstance(value, str) for value in listed)
    ):
        return None
    return listed


def _is_projection_lease_stem(stem: str) -> bool:
    """Whether *stem* has the ``<pid>-<uuid4 hex>`` shape this module's writer uses."""
    return _PROJECTION_LEASE_STEM_RE.fullmatch(stem) is not None


def _path_exists(path: Path) -> bool:
    """Whether *path* is present by name; any error other than absence is present.

    Only a clean ``FileNotFoundError`` proves a sidecar is gone, so a reclaim
    count can never claim a lease whose pair could not be observed as removed.
    """
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


_LeaseIdentities = tuple[tuple[int, int], tuple[int, int]]


def _probe_projection_lease(lease_path: Path) -> tuple[list[str], _LeaseIdentities | None] | None:
    """Parse one lease record and lock-probe its ``.hold``; ``None`` is uncertainty.

    Returns the aliases the record names plus, when the holder lock could be
    taken, the record and holder identities of that crash/finalizer residue.
    A held lock returns no identities: its process is live. Any link, non-regular
    file, replaced holder, unreadable record or probe failure answers ``None``.
    """
    holder_path = lease_path.with_name(
        lease_path.name[: -len(_PROJECTION_LEASE_RECORD_SUFFIX)] + _PROJECTION_LEASE_HOLDER_SUFFIX
    )
    stack = ExitStack()
    try:
        if platform_compat.is_link_or_junction(lease_path) or platform_compat.is_link_or_junction(
            holder_path
        ):
            return None
        record_info = pinned_fs.lstat_by_name(lease_path)
        if record_info is None or not stat.S_ISREG(record_info.st_mode):
            return None
        record_identity = (record_info.st_dev, record_info.st_ino)
        # The record is already identity-checked and non-link above; the
        # bounded parse itself is shared with the census (see the helper).
        listed = _read_lease_record(lease_path)
        if listed is None:
            return None
        holder_fd = stack.enter_context(platform_compat.open_lock_file(holder_path))
        opened = os.fstat(holder_fd)
        named = pinned_fs.lstat_by_name(holder_path)
        if (
            platform_compat.is_link_or_junction(holder_path)
            or named is None
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            return None
        holder_identity = (opened.st_dev, opened.st_ino)
        try:
            with platform_compat.file_lock(holder_fd, exclusive=True, wait=False):
                return listed, (record_identity, holder_identity)
        except (BlockingIOError, OSError):
            return listed, None
    except (OSError, ValueError, TypeError):
        return None
    finally:
        stack.close()


def _probe_orphan_projection_holder(holder_path: Path) -> tuple[int, int] | None:
    """Return the identity of an unlocked ``.hold`` whose record is gone, else ``None``.

    A lease is published record-first and finalized holder-first, so a holder
    with no record is never part of a live lease: it is litter a failed record
    or holder unlink left behind. It names no alias, so a holder that cannot be
    proven unlocked is simply left alone rather than making the pass uncertain.

    The writer publishes every holder EMPTY and never writes to it again (the
    lock occupies no bytes), so a non-empty file carries content this module did
    not put there. The stem shape alone is not provenance for an irreversible
    unlink, so such a file is kept whatever its name.
    """
    stack = ExitStack()
    try:
        named = pinned_fs.lstat_by_name(holder_path)
        if (
            named is None
            or platform_compat.is_link_or_junction(holder_path)
            or not stat.S_ISREG(named.st_mode)
            or named.st_size != 0
        ):
            return None
        holder_fd = stack.enter_context(platform_compat.open_lock_file(holder_path))
        opened = os.fstat(holder_fd)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino) or opened.st_size != 0:
            return None
        try:
            with platform_compat.file_lock(holder_fd, exclusive=True, wait=False):
                return (opened.st_dev, opened.st_ino)
        except (BlockingIOError, OSError):
            return None
    except (OSError, ValueError, TypeError):
        return None
    finally:
        stack.close()


def _scan_projection_leases(
    directory: Path, *, legacy_locked: bool = False
) -> tuple[set[str], bool]:
    """Scan the lease directory ONCE, returning ``(live_aliases, uncertain)``.

    The single lease-liveness scan, which a prune calls exactly once rather than
    once per candidate: with a candidate cap and a lease-scan cap both in the
    thousands, a per-candidate scan multiplies into a quadratic sweep under the
    held publication lock. One bounded scan parses and lock-probes each lease a
    single time, unions the aliases named by every HELD lease into
    ``live_aliases``, and reports ``uncertain`` when it could not see the whole
    directory with confidence. Callers test membership in the returned
    ``live_aliases`` set for a single alias; there is no per-alias wrapper.

    Uncertainty is fail-closed: the caller authorizes NO candidate deletion when
    it is True, because a candidate whose covering lease the scan could not read
    must be treated as live. Held leases contribute to the union without
    short-circuiting, so the scan runs to the end and can still
    reclaim crash/finalizer residue after closure even when held leases exist.
    Residue is only ever unlinked AFTER the ``scandir`` context closes, never
    mid-iteration. A clean completion, a cap and an unreadable record all drain
    the residue they lock-validated before stopping, because each unlink is
    identity-checked on its own pair; a failed open or iteration of the directory
    itself trusts nothing it observed and discards the queued residue. A cap, an
    unreadable record and any error all answer uncertain, and the first two are
    logged at INFO with the count reclaimed. A spent
    :data:`_PROJECTION_LEASE_SCAN_MAX_SECONDS` deadline, checked between entries,
    is treated exactly like the cap. An unlocked ``.hold`` whose record
    is gone is reclaimed as residue too, so that litter cannot pin the ceiling.
    That orphan reclaim is limited to the writer's ``<pid>-<uuid4 hex>`` stem:
    an empty ``.hold`` carries no other proof that this module wrote it.

    The record is read WITHOUT taking any lock on it -- see
    :func:`_acquire_projection_lease` for why the lock lives on a separate file.

    TWO directories are scanned under ONE deadline: this data home's own lease
    directory, and the legacy one inside *directory* (the agents directory),
    where a build before the side-file layout -- one that may still be running
    against this same agents directory -- publishes its leases. A held lease in
    either makes its aliases live; uncertainty in either makes the pass
    uncertain. The legacy directory is usually absent (one ``lstat``).
    *legacy_locked* says the caller already holds the older build's publication
    lock (:func:`_hold_legacy_publication_lock`), so the legacy scan takes none.
    """
    deadline = time.monotonic() + _PROJECTION_LEASE_SCAN_MAX_SECONDS
    live_aliases: set[str] = set()
    uncertain = False
    for found, unsure in (
        _scan_lease_dir(_projection_lease_dir(), deadline),
        _scan_legacy_lease_dir(directory, deadline, locked=legacy_locked),
    ):
        live_aliases |= found
        uncertain = uncertain or unsure
    return live_aliases, uncertain


def _hold_legacy_publication_lock(directory: Path, stack: ExitStack) -> bool:
    """Take the older build's publication lock onto *stack*; ``False`` is uncertainty.

    An older build -- one from before the side-file layout, which may still run
    against this same agents directory during an upgrade -- publishes and prunes
    under the lock it keeps IN *directory*. This build's own lock lives under its
    data home, so the two do not exclude each other on their own. A prune that
    held the older build's lock only for its lease scan would let that publisher
    in between the scan and the reclaim: it reuses an alias the scan found
    unleased (same name, same bytes, same inode, so the reclaim's revalidation
    passes), writes its lease, and the reclaim then deletes a live view. So a
    prune holds this lock from its scan through its last reclaim; the hold is
    bounded by the prune's own budget, well inside the older build's lock
    ceiling. A lock that cannot be taken in the lease scan's budget, or that is
    not a plain file, is uncertainty: the caller deletes nothing this run.

    With no legacy lock file there is no older publisher to exclude (one creates
    it before its first lease), and none is created here: the point of the
    side-file layout is to leave nothing of this build's in *directory*. An
    older build's FIRST-EVER publication into this agents directory landing
    between that ``lstat`` and the end of this prune is the residual; it needs
    an older build that has never run here (or whose lock file this home's boot
    drain already retired) to start exactly then.
    """
    lock_path = _legacy_lock_path(directory)
    info = pinned_fs.lstat_by_name(lock_path)
    if info is None:
        return True
    if platform_compat.is_link_or_junction(lock_path) or not stat.S_ISREG(info.st_mode):
        return False
    try:
        lock_fd = stack.enter_context(platform_compat.open_lock_file(lock_path, create=False))
        stack.enter_context(
            platform_compat.file_lock(
                lock_fd, exclusive=True, timeout=_PROJECTION_LEASE_SCAN_MAX_SECONDS
            )
        )
    except OSError:
        return False
    return True


def _scan_legacy_lease_dir(
    directory: Path, deadline: float, *, locked: bool = False
) -> tuple[set[str], bool]:
    """:func:`_scan_lease_dir` over the legacy lease directory, under the legacy lock.

    An older build publishes its lease (record first, then holder) while holding
    the publication lock it keeps in *directory*. This build holds a different one,
    so a scan taken without that lock could catch the pair between the two writes,
    find an unlocked holder it just created and reclaim the record of a live lease.
    Taking the older build's lock for the scan excludes every older publisher for
    its length; a lock that cannot be taken in the scan's own budget is uncertainty,
    which keeps every alias. With no legacy lock file there is no older publisher
    to exclude (one creates it before its first lease), and none is created here.
    A caller that already holds that lock for longer than the scan -- a prune,
    through :func:`_hold_legacy_publication_lock` -- passes *locked*; taking it
    again here would wait on itself.
    """
    lease_dir = _legacy_lease_dir(directory)
    if pinned_fs.lstat_by_name(lease_dir) is None:
        return set(), False
    if locked:
        return _scan_lease_dir(lease_dir, deadline)
    with ExitStack() as stack:
        if not _hold_legacy_publication_lock(directory, stack):
            return set(), True
        return _scan_lease_dir(lease_dir, deadline)


def _scan_lease_dir(lease_dir: Path, deadline: float) -> tuple[set[str], bool]:
    """:func:`_scan_projection_leases` over ONE directory, stopping at *deadline*."""
    lease_info = pinned_fs.lstat_by_name(lease_dir)
    if lease_info is None:
        return set(), False
    if platform_compat.is_link_or_junction(lease_dir) or not stat.S_ISDIR(lease_info.st_mode):
        return set(), True
    live_aliases: set[str] = set()
    deferred_reclaims: list[tuple[Path, tuple[int, int] | None, Path, tuple[int, int]]] = []
    scan_capped = False
    scan_expired = False
    unreadable: str | None = None
    scanned = 0
    try:
        with os.scandir(lease_dir) as entries:
            for entry in entries:
                # Bound EVERY directory entry, not just the ``.json`` records:
                # the cap is a ceiling on directory traversal under the held
                # publication lock, so ``.hold`` sidecars and unrelated files
                # consume it before the suffix filter runs. The check runs before
                # the increment: at most the limit is counted, and the next
                # fetched entry triggers the break.
                if scanned >= _PROJECTION_LEASE_SCAN_LIMIT:
                    scan_capped = True
                    break
                # A count alone cannot bound wall-clock time, so the scan also
                # stops between entries once its deadline is spent.
                if time.monotonic() >= deadline:
                    scan_expired = True
                    break
                scanned += 1
                if entry.name.endswith(_PROJECTION_LEASE_HOLDER_SUFFIX):
                    # A holder whose record is gone still consumes the entry
                    # ceiling on every scan, so it is reclaimed as residue;
                    # otherwise enough of that litter would keep every pass
                    # capped and pruning deferred for good.
                    holder_path = lease_dir / entry.name
                    record_path = holder_path.with_name(
                        entry.name[: -len(_PROJECTION_LEASE_HOLDER_SUFFIX)]
                        + _PROJECTION_LEASE_RECORD_SUFFIX
                    )
                    if _is_projection_lease_stem(
                        entry.name[: -len(_PROJECTION_LEASE_HOLDER_SUFFIX)]
                    ) and not _path_exists(record_path):
                        orphan = _probe_orphan_projection_holder(holder_path)
                        if orphan is not None:
                            deferred_reclaims.append((record_path, None, holder_path, orphan))
                    continue
                if not entry.name.endswith(_PROJECTION_LEASE_RECORD_SUFFIX):
                    continue
                lease_path = lease_dir / entry.name
                probed = _probe_projection_lease(lease_path)
                if probed is None:
                    # An unreadable, linked or replaced record makes the pass
                    # uncertain, so no alias is deleted. The scan still
                    # continues within its cap and deadline, and the residue it
                    # validates is drained below: each unlink is identity-checked
                    # on its own pair, so one bad sibling does not invalidate that
                    # proof. Stopping here would let a single persistent bad
                    # record early in directory order block cleanup of every
                    # stale lease after it forever.
                    if unreadable is None:
                        unreadable = entry.name
                    continue
                listed, unlocked = probed
                if unlocked is None:
                    # Held: this lease's process is live, so EVERY alias it
                    # names is live. Union them and keep scanning, so a later
                    # error can still discard the queued cleanup and a clean
                    # completion can still reclaim residue behind this lease.
                    live_aliases.update(listed)
                    continue
                record_identity, holder_identity = unlocked
                holder_path = lease_path.with_name(
                    entry.name[: -len(_PROJECTION_LEASE_RECORD_SUFFIX)]
                    + _PROJECTION_LEASE_HOLDER_SUFFIX
                )
                deferred_reclaims.append(
                    (lease_path, record_identity, holder_path, holder_identity)
                )
    except OSError:
        # A failed open or iteration trusts nothing this pass observed, so it
        # discards the queued residue and answers uncertain.
        return live_aliases, True

    # Residue is unlinked only after the scandir context has closed. A cap still
    # drains the residue validated before it, and an unreadable record the
    # residue validated anywhere in the walk, then each answers uncertain.
    reclaimed_leases = 0
    for lease_path, pending_record, holder_path, holder_identity in deferred_reclaims:
        if not _unlink_projection_lease_if_unchanged(holder_path, holder_identity) and (
            _path_exists(holder_path)
        ):
            # Keep the record while its holder stays, so the pair is retried
            # whole by the next scan rather than left as a record-less holder.
            continue
        if pending_record is not None:
            _unlink_projection_lease_if_unchanged(lease_path, pending_record)
        # Count a lease reclaimed only once BOTH sidecars are gone: one failed
        # unlink leaves a half-removed pair, which the next scan still sees.
        if not _path_exists(lease_path) and not _path_exists(holder_path):
            logger.debug("skill projection: reclaimed stale lease %s", lease_path.name)
            reclaimed_leases += 1
    if unreadable is not None:
        # Persistent while the record stays, and it blocks every alias prune in
        # this directory, so it is reported where an operator will see it.
        logger.info(
            "skill projection: lease record %s is unreadable; reclaimed %d stale lease(s), "
            "alias pruning deferred until it is removed or becomes readable",
            unreadable,
            reclaimed_leases,
        )
        return live_aliases, True
    if scan_capped:
        # The cap is the signal that a backlog is draining incrementally, and the
        # caller authorizes no alias deletion on it, so say so with the count
        # this bounded prefix did reclaim rather than returning silently.
        logger.info(
            "skill projection: lease scan reached its %d-entry ceiling; reclaimed %d stale "
            "lease(s), alias pruning deferred to a later spawn",
            _PROJECTION_LEASE_SCAN_LIMIT,
            reclaimed_leases,
        )
    if scan_expired:
        logger.info(
            "skill projection: lease scan spent its %.1f-second budget after %d entr(ies); "
            "reclaimed %d stale lease(s), alias pruning deferred to a later spawn",
            _PROJECTION_LEASE_SCAN_MAX_SECONDS,
            scanned,
            reclaimed_leases,
        )
    # A clean scan proves the union is complete; a capped or expired scan drained
    # the residue it validated but could not see past where it stopped, so it
    # answers uncertain.
    return live_aliases, scan_capped or scan_expired


_UNLINK_WARNING_LOCK = threading.Lock()
_UNLINK_WARNING_LAST = 0.0
_UNLINK_WARNING_SUPPRESSED = 0


def _warn_unlink_refused(path: Path, exc: OSError, *, what: str = "stale alias") -> None:
    """Report a projection-file unlink the OS refused, at most once per interval.

    The prune's every other "no" is a deliberate keep -- kept, active, leased,
    changed under the walk -- and stays at debug. This one is not: the walk
    classified the file as reclaimable and the filesystem would not let it go,
    which is a permission or mount problem an operator has to fix, and at
    backlog scale it is the same answer thousands of times per spawn. One line
    per interval, carrying the count it stands for, is what makes it visible
    without making it the log.

    This is the ONE reporter and the ONE throttle for every such path: aliases
    and the lease records, holder sidecars and ownership sidecars that travel
    with them share the interval and the suppressed count, because they share
    the fault. *what* names the kind of file so the line stays honest about
    which one it saw, and the line names the operation and the errno it got:
    only a permission-class errno is diagnosed as an unwritable directory --
    a file that vanished between the walk and the unlink, or a descriptor
    limit, is a refusal too, but not that one.
    """
    global _UNLINK_WARNING_LAST, _UNLINK_WARNING_SUPPRESSED
    now = time.monotonic()
    with _UNLINK_WARNING_LOCK:
        if _UNLINK_WARNING_LAST and now - _UNLINK_WARNING_LAST < _UNLINK_WARNING_INTERVAL_SECS:
            _UNLINK_WARNING_SUPPRESSED += 1
            return
        suppressed = _UNLINK_WARNING_SUPPRESSED
        _UNLINK_WARNING_SUPPRESSED = 0
        _UNLINK_WARNING_LAST = now
    code = errno.errorcode.get(exc.errno, str(exc.errno)) if exc.errno is not None else "?"
    reason = exc.strerror or exc.__class__.__name__
    if exc.errno in _UNWRITABLE_ERRNOS:
        logger.warning(
            "skill projection: cannot remove %s %s -- unlink refused, %s (%s); %d similar "
            "refusal(s) since the last report -- the directory %s is not writable by this "
            "process, so no backlog there can drain",
            what,
            path.name,
            code,
            reason,
            suppressed,
            path.parent,
        )
        return
    logger.warning(
        "skill projection: cannot remove %s %s -- unlink refused, %s (%s); %d similar "
        "refusal(s) since the last report, in the directory %s",
        what,
        path.name,
        code,
        reason,
        suppressed,
        path.parent,
    )


def _unlink_alias_if_unchanged(path: Path, identity: tuple[int, int]) -> bool:
    """Unlink *path* only while it still names the classified alias inode.

    The caller holds the global projection lock, which excludes every product
    publisher. POSIX additionally pins the parent descriptor. Windows lacks
    unlink-at, so it performs one final no-link identity check before the
    by-name unlink; other platforms without a pinned walk retain the alias.

    A refusal from the filesystem itself is reported (rate-limited); every
    other ``False`` is an identity change and stays silent.
    """
    if pinned_fs.supports_pinned_walk() and os.unlink in os.supports_dir_fd:
        try:
            parent_fd = os.open(path.parent, pinned_fs.dir_flags())
        except OSError as exc:
            _warn_unlink_refused(path, exc)
            return False
        try:
            return pinned_fs.unlink_verified(
                parent_fd,
                path.name,
                identity,
                on_error=lambda exc: _warn_unlink_refused(path, exc),
            )
        finally:
            os.close(parent_fd)

    if platform_compat.IS_WINDOWS:
        current = pinned_fs.lstat_by_name(path)
        if (
            current is None
            or platform_compat.is_link_or_junction(path)
            or not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != identity
        ):
            return False
        try:
            path.unlink()
        except OSError as exc:
            _warn_unlink_refused(path, exc)
            return False
        return True

    # An unknown non-Windows platform without descriptor-relative unlink has
    # neither the POSIX identity pin nor Windows' publication-lock contract.
    return False

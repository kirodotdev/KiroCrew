"""Native Kiro launch views whose skill directory is supplied by Crew.

The authored resource mapping remains the authority for Crew search/list/read.
Native aliases preserve the other spec fields but carry no skill:// resources:
Kiro 2.21.2 progressively loads bodies, yet enumerates all their metadata before
the first prompt. Bounding only the Crew prompt cannot bound that native cost.
"""

from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import threading
import time
import uuid
import weakref
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.agent_discovery import SCOPE_PROJECT, _read_agent_spec, list_agents
from kiro_crew.agent_spec_format import NATIVE_SKILL_ALIAS_PREFIX
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home, kiro_agents_dir, kiro_home, project_agents_dir
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes
from kiro_crew.workspace_cli_settings import workspace_cli_settings_lock

logger = logging.getLogger(__name__)

_MANAGED_SETTING = "kirocrew.skillDiscovery.inheritFiles"
_INHERIT_SETTING = "chat.disableInheritingDefaultResources"
_INHERIT_SOURCE = "kirocrew.skillDiscovery.inheritSource"
_PREVIOUS_INHERITANCE = "kirocrew.skillDiscovery.previousInheritance"
_SEARCH_TOOL = "@kirocrew-core/skill_search"
_PROJECTION_LOCK_NAME = ".kirocrew-skill-projection.lock"
_PROJECTION_LEASE_DIR_NAME = ".kirocrew-skill-projection-leases"
# A lease is a readable record plus an unread lock target. Windows file locks are
# mandatory, so a lock on the record itself makes every reader's probe fail.
_PROJECTION_LEASE_RECORD_SUFFIX = ".json"
_PROJECTION_LEASE_HOLDER_SUFFIX = ".hold"
# ONE bound for both ends of the lease. The reader answers "live" above it, so a
# writer allowed to exceed it could publish a record that is unreclaimable by
# construction: a crash would leave it on disk and every later probe would read
# it as held, disabling pruning for good. Refusing the publication instead falls
# back to authored native agents, which is recoverable on the next spawn.
_PROJECTION_LEASE_MAX_ALIASES = 1024
_PROJECTION_LEASE_MAX_BYTES = 65536
# The exact shape prepare_native_skill_projection derives, so a legacy reclaim
# admits only names this module could have produced. The digest length is pinned
# here rather than recomputed from the writer, because widening the writer must
# not silently widen what the reclaim is willing to delete.
_LEGACY_ALIAS_NAME_RE = re.compile(re.escape(NATIVE_SKILL_ALIAS_PREFIX) + r"[0-9a-f]{24}")
# A CEILING on reclaims per run, never a floor: the time budget below can end a
# call having reclaimed none at all. It is headroom over the aliases one run
# publishes, so a call that does reach them covers the steady-state orphan rate
# as well as some backlog. It bounds no part of the critical section: a candidate
# that is kept, active or leased costs a full classification and never increments
# it, which is why the section carries its own budget below.
_PRUNE_MAX_RECLAIMS_PER_RUN = 64
# The budget for one call's classification work, and a BETWEEN-candidate one: it
# bounds how many candidates are walked, not how long any single one takes, and
# the directory enumeration that precedes the walk is outside it. Sized well under
# _PROJECTION_LOCK_TIMEOUT_SECS and sharing that ceiling with the publication
# writes in the same section -- two atomic writes per alias plus the settings
# commit -- so it has to leave room for those, not merely fit under the ceiling.
_PRUNE_MAX_SECONDS_PER_RUN = 0.4
# The ONE window the re-preparation contract does not cover, and the only thing
# this age excludes. A publisher from a build that predates the lease holds no
# lease, so between its write and kiro-cli reading `--agent` its alias looks
# exactly like backlog -- and that process will NOT re-prepare, because it
# already did, so a deletion there is a failed spawn rather than an eviction.
# This is deliberately NOT a liveness proxy (the reason an age cut-off was
# rejected for the recorded path): it only has to exceed publish-to-spawn, which
# is milliseconds, and the real backlog is hours to days old.
_LEGACY_RECLAIM_MIN_AGE_SECS = 600.0
_PROJECTION_METADATA_DIR_NAME = ".kirocrew-skill-projection-metadata"
# Publication plus pruning is normally sub-second. Two seconds absorbs scheduler
# jitter and short Windows rename retries without inheriting the generic five-minute
# lock ceiling on the native startup path.
_PROJECTION_LOCK_TIMEOUT_SECS = 2.0

# Generated specs stay in Kiro's shared agents directory, so metadata identifies
# them for direct scanners and scopes cleanup to the owning Kiro Crew data home.
_MANAGED_MARKER = "x-kirocrew-managed"
_MANAGED_MARKER_VALUE = "skill-view"
_MANAGED_CREW_HOME = "x-kirocrew-home"
_MANAGED_WORK_DIR = "x-kirocrew-work-dir"
_MANAGED_AGENT = "x-kirocrew-agent"
_MANAGED_SOURCE = "x-kirocrew-source"
_MANAGED_ALIAS_SHA256 = "x-kirocrew-alias-sha256"


@dataclass
class NativeSkillProjection:
    """Translate transport identities while Crew keeps the authored agent name."""

    aliases: dict[str, str]
    specs: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    search_agents: set[str] = field(default_factory=set)
    _lease_finalizer: Any = field(default=None, repr=False, compare=False)

    def agent(self, name: str) -> str:
        if name not in self.aliases:
            if name in self.errors:
                raise ValueError(f"Agent {name!r}: {self.errors[name]}")
            raise ValueError(f"Agent {name!r} has no prepared skill discovery view")
        return self.aliases[name]

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "session/set_mode":
            return {**params, "modeId": self.agent(str(params.get("modeId", "")))}
        if method == "_kiro.dev/commands/execute":
            command = params.get("command", "")
            if isinstance(command, dict):
                name = str(command.get("command", "")).lstrip("/")
                args = command.get("args") or {}
                value = str(args.get("value", "")) if isinstance(args, dict) else ""
            else:
                words = str(command).strip().lstrip("/").split(None, 1)
                name = words[0] if words else ""
                value = words[1] if len(words) > 1 else ""
            if name == "agent" and value.strip() not in {"list", "schema"}:
                raise ValueError(
                    "Use Crew's agent selector to change agents so its skill scope stays in sync."
                )
        return params

    def frame(self, frame: dict[str, Any]) -> dict[str, Any]:
        reverse = {alias: name for name, alias in self.aliases.items()}

        def visit(value: Any, field: str = "") -> Any:
            if isinstance(value, dict):
                return {key: visit(item, key) for key, item in value.items()}
            if isinstance(value, list):
                if field == "availableModes":
                    value = [
                        item
                        for item in value
                        if isinstance(item, dict) and item.get("id") in reverse
                    ]
                return [visit(item) for item in value]
            if field in {"id", "name", "agentName", "modeId", "currentModeId"} and isinstance(
                value, str
            ):
                return reverse.get(value, value)
            return value

        return visit(frame)


_ACTIVE_PROJECTIONS: weakref.WeakValueDictionary[int, NativeSkillProjection] = (
    weakref.WeakValueDictionary()
)
_ACTIVE_PROJECTIONS_LOCK = threading.Lock()


def _register_active_projection(projection: NativeSkillProjection) -> None:
    with _ACTIVE_PROJECTIONS_LOCK:
        _ACTIVE_PROJECTIONS[id(projection)] = projection


def _active_aliases() -> set[str]:
    with _ACTIVE_PROJECTIONS_LOCK:
        projections = tuple(_ACTIVE_PROJECTIONS.values())
    return {alias for projection in projections for alias in projection.aliases.values()}


def _projection_alias_lock(directory: Path) -> ExitStack:
    """Acquire the bounded cross-process lock for alias publication and pruning."""
    stack = ExitStack()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        lock_path = directory / _PROJECTION_LOCK_NAME
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
        stack.enter_context(
            platform_compat.file_lock(
                lock_fd, exclusive=True, timeout=_PROJECTION_LOCK_TIMEOUT_SECS
            )
        )
        current = pinned_fs.lstat_by_name(lock_path)
        if (
            platform_compat.is_link_or_junction(lock_path)
            or current is None
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise OSError("skill projection lock changed while it was acquired")
    except OSError:
        stack.close()
        raise
    return stack


def _ensure_projection_metadata_directory(directory: Path) -> Path:
    """Create and verify the hidden directory that owns projection sidecars."""
    metadata_dir = directory / _PROJECTION_METADATA_DIR_NAME
    if platform_compat.is_link_or_junction(metadata_dir):
        raise OSError("skill projection metadata directory is a symlink or junction")
    metadata_dir.mkdir(parents=True, exist_ok=True)
    info = pinned_fs.lstat_by_name(metadata_dir)
    if (
        platform_compat.is_link_or_junction(metadata_dir)
        or info is None
        or not stat.S_ISDIR(info.st_mode)
    ):
        raise OSError("skill projection metadata directory is not a real directory")
    return metadata_dir


def _unlink_projection_lease_if_unchanged(path: Path, identity: tuple[int, int]) -> bool:
    """Remove one unlocked lease only while its random name keeps its identity."""
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
        except OSError:
            return False
        try:
            return pinned_fs.unlink_verified(parent_fd, path.name, identity)
        finally:
            os.close(parent_fd)
    if not platform_compat.IS_WINDOWS:
        return False
    try:
        path.unlink()
    except OSError:
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
    :func:`_alias_has_external_lease` reads as uncertainty and answers "live", so
    nothing was ever reclaimed on Windows while a single lease was held. Locking
    a file nobody reads keeps the OS liveness proof and leaves the record legible.
    """
    stack = ExitStack()
    if not aliases:
        return stack
    try:
        lease_dir = directory / _PROJECTION_LEASE_DIR_NAME
        if platform_compat.is_link_or_junction(lease_dir):
            raise OSError("skill projection lease directory is a symlink or junction")
        lease_dir.mkdir(parents=True, exist_ok=True)
        lease_info = pinned_fs.lstat_by_name(lease_dir)
        if (
            platform_compat.is_link_or_junction(lease_dir)
            or lease_info is None
            or not stat.S_ISDIR(lease_info.st_mode)
        ):
            raise OSError("skill projection lease directory is not a real directory")
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
        stack.callback(_unlink_projection_lease_if_unchanged, lease_path, identity)
        atomic_write(holder_path, "", restrict_to_owner=True)
        holder_created = pinned_fs.lstat_by_name(holder_path)
        if holder_created is None or not stat.S_ISREG(holder_created.st_mode):
            raise OSError("skill projection lease holder was not published as a regular file")
        holder_identity = (holder_created.st_dev, holder_created.st_ino)
        stack.callback(_unlink_projection_lease_if_unchanged, holder_path, holder_identity)
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


def _alias_has_external_lease(directory: Path, alias: str) -> bool:
    """Return whether another process may still use *alias*; uncertainty is live.

    A valid lease whose holder lock can be acquired is crash/finalizer residue:
    no projection can still own it, so both identity-verified sidecars are
    reclaimed. The record is read WITHOUT taking any lock on it -- see
    :func:`_acquire_projection_lease` for why the lock lives on a separate file.
    """
    lease_dir = directory / _PROJECTION_LEASE_DIR_NAME
    lease_info = pinned_fs.lstat_by_name(lease_dir)
    if lease_info is None:
        return False
    if platform_compat.is_link_or_junction(lease_dir) or not stat.S_ISDIR(lease_info.st_mode):
        return True
    try:
        leases = list(lease_dir.glob(f"*{_PROJECTION_LEASE_RECORD_SUFFIX}"))
    except OSError:
        return True
    for lease_path in leases:
        holder_path = lease_path.with_name(
            lease_path.name[: -len(_PROJECTION_LEASE_RECORD_SUFFIX)]
            + _PROJECTION_LEASE_HOLDER_SUFFIX
        )
        stack = ExitStack()
        unlocked: tuple[tuple[int, int], tuple[int, int]] | None = None
        try:
            if platform_compat.is_link_or_junction(
                lease_path
            ) or platform_compat.is_link_or_junction(holder_path):
                return True
            record_info = pinned_fs.lstat_by_name(lease_path)
            if record_info is None or not stat.S_ISREG(record_info.st_mode):
                return True
            record_identity = (record_info.st_dev, record_info.st_ino)
            # The record is already identity-checked and non-link above; the
            # bounded parse itself is shared with the census (see the helper).
            listed = _read_lease_record(lease_path)
            if listed is None:
                return True
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
                return True
            holder_identity = (opened.st_dev, opened.st_ino)
            try:
                with platform_compat.file_lock(holder_fd, exclusive=True, wait=False):
                    unlocked = (record_identity, holder_identity)
            except (BlockingIOError, OSError):
                if alias in listed:
                    return True
        except (OSError, ValueError, TypeError):
            return True
        finally:
            stack.close()
        if unlocked is not None:
            record_identity, holder_identity = unlocked
            reclaimed = _unlink_projection_lease_if_unchanged(holder_path, holder_identity)
            if _unlink_projection_lease_if_unchanged(lease_path, record_identity) or reclaimed:
                logger.debug("skill projection: reclaimed stale lease %s", lease_path.name)
    return False


def _settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = safe_read_file_bytes(str(path))
    if raw is None:
        raise ValueError(f"Cannot read Kiro settings at {path}")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"Kiro settings must be an object: {path}")
    return data


def _restore_inheritance(path: Path, local: dict[str, Any]) -> None:
    """Undo only our overlay; a changed or removed native setting wins."""
    inherited = local.get(_MANAGED_SETTING)
    source = local.get(_INHERIT_SOURCE)
    if not isinstance(inherited, bool) or source not in ("local", "global"):
        return
    previous = local.get(_PREVIOUS_INHERITANCE)
    if previous is None:
        # Views prepared before rollback support recorded source and a boolean.
        previous = {"present": source == "local", "value": not inherited}
    if (
        not isinstance(previous, dict)
        or not isinstance(previous.get("present"), bool)
        or (previous["present"] and "value" not in previous)
    ):
        raise ValueError(f"Cannot restore Crew's inheritance overlay at {path}")
    if local.get(_INHERIT_SETTING) is True:
        if previous["present"]:
            local[_INHERIT_SETTING] = previous["value"]
        else:
            local.pop(_INHERIT_SETTING, None)
    for key in (_MANAGED_SETTING, _INHERIT_SOURCE, _PREVIOUS_INHERITANCE):
        local.pop(key, None)
    atomic_write(path, json.dumps(local, indent=2))


def _managed_marker(spec: object) -> bool:
    """Return whether a generated spec carries this lifecycle's marker."""
    return isinstance(spec, dict) and spec.get(_MANAGED_MARKER) == _MANAGED_MARKER_VALUE


def _unlink_alias_if_unchanged(path: Path, identity: tuple[int, int]) -> bool:
    """Unlink *path* only while it still names the classified alias inode.

    The caller holds the global projection lock, which excludes every product
    publisher. POSIX additionally pins the parent descriptor. Windows lacks
    unlink-at, so it performs one final no-link identity check before the
    by-name unlink; other platforms without a pinned walk retain the alias.
    """
    if pinned_fs.supports_pinned_walk() and os.unlink in os.supports_dir_fd:
        try:
            parent_fd = os.open(path.parent, pinned_fs.dir_flags())
        except OSError:
            return False
        try:
            return pinned_fs.unlink_verified(parent_fd, path.name, identity)
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
        except OSError:
            return False
        return True

    # An unknown non-Windows platform without descriptor-relative unlink has
    # neither the POSIX identity pin nor Windows' publication-lock contract.
    return False


def _is_legacy_projected_view(path: Path, alias_raw: bytes) -> bool:
    """Whether *path* is a projected view from a build that wrote no ownership.

    Builds shipped before this lifecycle published aliases with neither a
    metadata sidecar nor an in-spec marker, so :func:`_managed_metadata_for_alias`
    cannot admit them and a reclaim keyed on ownership alone leaves the ENTIRE
    accumulated backlog on disk -- the exact per-turn tool-spec cost this module
    exists to bound. Those aliases are still identifiable without a record: the
    name is Crew's own prefix plus the 24-hex digest :func:`prepare_native_skill_projection`
    derives, and a projected view always renames itself to that alias and carries
    no ``skill://`` resource (both are what the projection strips and rewrites).

    Deleting one cannot prove the pair unregenerable the way a recorded work
    directory can, so the safety argument is the caller's instead: every consumer
    re-prepares first -- the spawn argv, and ``session/set_mode``, which re-runs
    preparation before it sends the alias -- and the `/agent` command is refused
    rather than translated. A removal is therefore a cache eviction for a live
    pre-upgrade session, which republishes the same name WITH a record, and a
    reclaim for every dead work directory.

    Name and shape alone are NOT provenance, and an unlink is not undoable: an
    operator's own agent could in principle carry this name. So one POSITIVE mark
    the projection itself writes is also required -- Crew's managed
    ``kirocrew-core`` server entry, or the absolute steering resource pointing at
    THIS host's kiro home -- both of which the shipped builds that produced the
    backlog already write. An unrecorded view carrying neither is left alone; it
    is a smaller reclaim than the name shape would allow, and the right side to
    err on when the alternative is deleting a file somebody else authored.
    """
    if not _LEGACY_ALIAS_NAME_RE.fullmatch(path.stem):
        return False
    try:
        spec = json.loads(alias_raw)
    except (ValueError, TypeError):
        return False
    if not isinstance(spec, dict) or spec.get("name") != path.stem:
        return False
    resources = spec.get("resources", [])
    if not isinstance(resources, list):
        return False
    if any(isinstance(r, str) and r.startswith("skill://") for r in resources):
        return False
    servers = spec.get("mcpServers")
    if isinstance(servers, dict) and "kirocrew-core" in servers:
        return True
    try:
        steering = f"file://{kiro_home().as_posix()}/steering/**/*.md"
    except (OSError, ValueError, RuntimeError):
        return False
    return steering in resources


def _managed_metadata_for_alias(
    directory: Path, path: Path, alias_raw: bytes
) -> tuple[dict[str, Any], Path | None, tuple[int, int] | None, bytes | None] | None:
    """Load ownership outside the Kiro agent spec, or one legacy in-spec record."""
    metadata_dir = directory / _PROJECTION_METADATA_DIR_NAME
    directory_info = pinned_fs.lstat_by_name(metadata_dir)
    if directory_info is not None:
        if platform_compat.is_link_or_junction(metadata_dir) or not stat.S_ISDIR(
            directory_info.st_mode
        ):
            return None
        metadata_path = metadata_dir / f"{path.stem}.json"
        metadata_info = pinned_fs.lstat_by_name(metadata_path)
        if metadata_info is not None:
            if platform_compat.is_link_or_junction(metadata_path) or not stat.S_ISREG(
                metadata_info.st_mode
            ):
                return None
            try:
                metadata_raw = safe_read_file_bytes(str(metadata_path))
            except FileTooLargeError:
                return None
            if metadata_raw is None:
                return None
            try:
                metadata = json.loads(metadata_raw)
            except (ValueError, TypeError):
                return None
            if (
                not _managed_marker(metadata)
                or metadata.get(_MANAGED_ALIAS_SHA256) != hashlib.sha256(alias_raw).hexdigest()
            ):
                return None
            return (
                metadata,
                metadata_path,
                (metadata_info.st_dev, metadata_info.st_ino),
                metadata_raw,
            )

    # No released build wrote lifecycle keys INTO a spec -- kiro-cli denies
    # unknown fields, so the projection never could. An alias without a sidecar
    # is therefore unrecorded, and `_is_legacy_projected_view` decides it from
    # the name shape and the view's own form instead.
    return None


def _prune_start_offset(count: int) -> int:
    """Where this call begins its bounded walk over *count* candidates.

    A bounded walk over a stable directory order examines the same prefix every
    call, so a prefix of entries that are kept, active or leased hides the whole
    reclaimable remainder behind it -- permanently, because the walk never gets
    past its own budget to see it. Moving the start makes every entry reachable
    across calls. It cannot be a cursor in memory: the workload this bound exists
    for spawns a fresh process per cron run, so a process-local cursor restarts at
    zero every time and rotates nothing.
    """
    if count <= 0:
        return 0
    return secrets.randbelow(count)


def _prune_stale_managed_aliases(directory: Path, crew_home_id: str, *, keep: set[str]) -> None:
    """Remove aliases owned by this Kiro Crew data home that no projection uses.

    Runs while the publication lock is held, so a deletion cannot land on an alias
    a publisher that takes that lock is writing; a build predating the lease takes
    no part in it, and the minimum age is what covers that one. A time budget keeps
    the held lock down to a slice of the walk rather than all of it. An alias is
    kept when this run publishes it, a projection in this process holds it, or a
    held lease in any process names it. Everything else this data home
    recorded is a cache entry for a projection that has ended: every consumer
    re-prepares before it sends an alias, so removing one costs the next spawn
    for that work directory one rewrite and nothing else. Whether the recorded work directory still exists
    is not consulted: a per-run work directory outlives its run, so keying on
    it keeps one alias per agent for every run ever spawned.
    """
    try:
        candidates = list(directory.glob(f"{NATIVE_SKILL_ALIAS_PREFIX}*.json"))
    except OSError:
        logger.debug("skill projection: cannot list %s to prune aliases", directory, exc_info=True)
        return
    active = _active_aliases()
    # Each run publishes len(keep) aliases and leaves that many behind when it
    # ends, so the cap covers that steady-state rate plus bounded backlog drain.
    cap = _PRUNE_MAX_RECLAIMS_PER_RUN + len(keep)
    offset = _prune_start_offset(len(candidates))
    candidates = candidates[offset:] + candidates[:offset]
    deadline = time.monotonic() + _PRUNE_MAX_SECONDS_PER_RUN
    reclaimed = 0
    examined = 0
    for path in candidates:
        if reclaimed >= cap:
            logger.info(
                "skill projection: reclaim cap reached (%d); the rest drains on later spawns",
                cap,
            )
            break
        if time.monotonic() >= deadline:
            logger.info(
                "skill projection: prune budget spent after %d candidate(s); the rest drains on later spawns",
                examined,
            )
            break
        # Counted for EVERY candidate, not only the reclaimed ones: what the budget
        # has to cover is the classification, which a skip pays in full.
        examined += 1
        if (
            path.stem in keep
            or path.stem in active
            or _alias_has_external_lease(directory, path.stem)
        ):
            continue
        candidate = pinned_fs.lstat_by_name(path)
        if candidate is None:
            continue
        identity = (candidate.st_dev, candidate.st_ino)
        try:
            raw = safe_read_file_bytes(str(path))
        except FileTooLargeError:
            continue
        if raw is None:
            continue
        managed = _managed_metadata_for_alias(directory, path, raw)
        if managed is None:
            # No ownership record at all. A pre-lifecycle build wrote this, so
            # the recorded-pair proof is unavailable and the re-preparation
            # contract carries the removal instead (see _is_legacy_projected_view).
            # Every gate above still applies: it is not in this run's set, no live
            # projection claims it, and no held lease names it.
            if _is_legacy_projected_view(path, raw):
                if time.time() - candidate.st_mtime < _LEGACY_RECLAIM_MIN_AGE_SECS:
                    # Possibly mid-publish by a build that holds no lease. A
                    # negative age (clock moved) lands here too, which is the
                    # safe side.
                    continue
                current = pinned_fs.lstat_by_name(path)
                if current is None or (current.st_dev, current.st_ino) != identity:
                    continue
                try:
                    current_raw = safe_read_file_bytes(str(path))
                except FileTooLargeError:
                    continue
                if current_raw != raw or not _is_legacy_projected_view(path, current_raw):
                    continue
                if _managed_metadata_for_alias(directory, path, current_raw) is not None:
                    # A concurrent preparation republished it WITH a record
                    # between the two reads; that owner decides its lifetime.
                    continue
                if _unlink_alias_if_unchanged(path, identity):
                    logger.debug("skill projection: pruned unrecorded legacy alias %s", path.name)
                    reclaimed += 1
                else:
                    logger.debug("skill projection: legacy alias changed before removal: %s", path)
            continue
        metadata, metadata_path, metadata_identity, metadata_raw = managed
        if metadata.get(_MANAGED_CREW_HOME) != crew_home_id:
            continue

        # Re-open and revalidate the exact alias and ownership sidecar at
        # deletion time. A sidecar digest binds the ownership record to these
        # projected bytes; any replacement or uncertainty keeps both files.
        current = pinned_fs.lstat_by_name(path)
        if current is None or (current.st_dev, current.st_ino) != identity:
            continue
        try:
            current_raw = safe_read_file_bytes(str(path))
        except FileTooLargeError:
            continue
        if current_raw != raw:
            continue
        current_managed = _managed_metadata_for_alias(directory, path, current_raw)
        if current_managed is None:
            continue
        current_metadata, current_metadata_path, current_metadata_identity, current_metadata_raw = (
            current_managed
        )
        if (
            current_metadata != metadata
            or current_metadata_path != metadata_path
            or current_metadata_identity != metadata_identity
            or current_metadata_raw != metadata_raw
            or current_metadata.get(_MANAGED_CREW_HOME) != crew_home_id
        ):
            continue
        if _unlink_alias_if_unchanged(path, identity):
            if metadata_path is not None and metadata_identity is not None:
                _unlink_projection_lease_if_unchanged(metadata_path, metadata_identity)
            logger.debug("skill projection: pruned unused managed alias %s", path.name)
            reclaimed += 1
        else:
            logger.debug("skill projection: unused alias changed before removal: %s", path)
    if reclaimed > 0:
        logger.info("skill projection: reclaimed %d unused alias(es)", reclaimed)


# Bounds on what the census RETAINS, not on what it counts: every retained
# collection has a ceiling and the result says when one was hit. Both are the
# diagnostic's own memory and I/O budget, nothing more: the alias ceiling is far
# above the backlogs that motivated the census (28k on one host) and comfortably
# below what a doctor run may hold in memory; the lease ceiling bounds how many
# record files one run opens. The reclaim itself scans the lease directory whole
# and caps reclaims per run, not records -- so past the lease ceiling the census
# cannot say what the reclaim will do, and reports that instead of guessing.
_CENSUS_MAX_ALIASES = 65536
_CENSUS_MAX_LEASES = 4096


def census_projected_aliases(directory: Path) -> dict[str, int]:
    """Count the projected aliases in *directory* without touching any of them.

    A read-only census for diagnostics, in this module so the lease-record and
    sidecar shapes it reads are the ones :func:`_prune_stale_managed_aliases`
    reclaims by. Returns plain counts:

    ``total``
        regular ``<prefix>*.json`` files directly in *directory*;
    ``leased``
        of those, how many a lease record names. The record is read with the
        reclaim's own parse and NO lock is probed -- a probe would reclaim
        residue as a side effect, which a census must not do -- so this is
        "published by some projection", not "held right now": a crash-stale
        record counts here until the next spawn's probe reclaims it;
    ``foreign_home`` / ``foreign_leased``
        of the unreferenced and of the lease-named aliases respectively, how
        many an ownership sidecar attributes to a Kiro Crew data home other
        than this process's own -- spelled exactly as the publisher records it,
        so a caller cannot pass a differently normalised id. Two homes share
        one agents directory whenever they share ``~/.kiro``, and this
        gateway's reclaim skips the other home's aliases unconditionally, so
        neither bucket drains here and the leased one is not "held or
        crash-stale" from this gateway's point of view either;
    ``unreadable_leases``
        lease records the reclaim reads as uncertainty. While one exists
        :func:`_alias_has_external_lease` answers "live" for EVERY alias and
        nothing is reclaimed, so a diagnostic must not promise a drain;
    ``truncated``
        1 when a retention bound (:data:`_CENSUS_MAX_ALIASES`,
        :data:`_CENSUS_MAX_LEASES`) was hit, so the other counts are floors.

    Every read failure counts toward the side that claims less: an unreadable
    directory is an empty census, an unreadable sidecar is not foreign.
    """
    counts = {
        "total": 0,
        "leased": 0,
        "foreign_home": 0,
        "foreign_leased": 0,
        "unreadable_leases": 0,
        "truncated": 0,
    }
    crew_home_id = data_home().absolute().as_posix()
    stems: set[str] = set()
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not (
                    entry.name.startswith(NATIVE_SKILL_ALIAS_PREFIX)
                    and entry.name.endswith(".json")
                    and entry.is_file(follow_symlinks=False)
                ):
                    continue
                if len(stems) >= _CENSUS_MAX_ALIASES:
                    counts["truncated"] = 1
                    break
                stems.add(entry.name[: -len(".json")])
    except OSError:
        return counts
    counts["total"] = len(stems)
    if not stems:
        return counts

    named: set[str] = set()
    lease_dir = directory / _PROJECTION_LEASE_DIR_NAME
    records = 0
    try:
        with os.scandir(lease_dir) as entries:
            for entry in entries:
                if not (
                    entry.name.endswith(_PROJECTION_LEASE_RECORD_SUFFIX)
                    and entry.is_file(follow_symlinks=False)
                ):
                    continue
                if records >= _CENSUS_MAX_LEASES:
                    counts["truncated"] = 1
                    break
                records += 1
                listed = _read_lease_record(Path(entry.path))
                if listed is None:
                    counts["unreadable_leases"] += 1
                    continue
                # Only stems this census retained: bounded by the alias ceiling.
                named.update(stems.intersection(listed))
    except FileNotFoundError:
        pass
    except OSError:
        counts["unreadable_leases"] += 1
    counts["leased"] = len(named)

    # Plain bounded reads, like the lease records: the hardened reader audits
    # every call, and a backlog is tens of thousands of sidecars.
    metadata_dir = directory / _PROJECTION_METADATA_DIR_NAME
    for stem in stems:
        try:
            fd = os.open(metadata_dir / f"{stem}.json", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                raw = os.read(fd, _PROJECTION_LEASE_MAX_BYTES + 1)
            finally:
                os.close(fd)
            if len(raw) > _PROJECTION_LEASE_MAX_BYTES:
                continue
            metadata = json.loads(raw)
        except (OSError, ValueError, TypeError, RecursionError):
            continue
        if (
            _managed_marker(metadata)
            and isinstance(metadata.get(_MANAGED_CREW_HOME), str)
            and metadata[_MANAGED_CREW_HOME] != crew_home_id
        ):
            counts["foreign_leased" if stem in named else "foreign_home"] += 1
    return counts


def prepare_native_skill_projection(
    work_dir: Path, *, enabled: bool | None = None
) -> NativeSkillProjection | None:
    """Prepare native views after spec freshness admission, before spawning.

    Uses the existing workspace CLI settings channel. No home, identity store,
    session store or authored agent file is relocated or rewritten.
    """
    directory = kiro_agents_dir()
    crew_home_id = data_home().absolute().as_posix()
    work_dir_id = work_dir.absolute().as_posix()
    if enabled is None:
        enabled = os.environ.get("KIROCREW_NATIVE_SKILL_PROJECTION", "1") != "0"
    if not enabled:
        if not (work_dir / ".kiro" / "settings" / "cli.json").exists():
            return None
        try:
            with workspace_cli_settings_lock(work_dir) as locked_settings:
                _restore_inheritance(locked_settings, _settings(locked_settings))
        except OSError:
            logger.warning(
                "skill projection: workspace settings lock unavailable during rollback",
                exc_info=True,
            )
        return None
    global_settings = _settings(kiro_home() / "settings" / "cli.json")
    aliases: dict[str, str] = {}
    specs: dict[str, dict[str, Any]] = {}
    ownership: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    search_agents: set[str] = set()
    for agent in list_agents(project_dir=str(work_dir)):
        if not agent.filename:
            continue
        source_dir = (
            project_agents_dir(str(work_dir)) if agent.scope == SCOPE_PROJECT else directory
        )
        source = source_dir / agent.filename
        spec = _read_agent_spec(source, operation="native_skill_projection", source="acp")
        if spec is None:
            continue
        # The RECORD carries the posix spelling for legibility, but the alias
        # identity keeps the platform's own. Hashing the posix form would re-key
        # every existing (work_dir, agent) pair on Windows, where str() spells
        # the separator differently, and nothing reads this digest across hosts:
        # the agents directory is per-host, so a platform-stable hash buys
        # nothing and costs one orphaned file per pair on upgrade.
        identity = f"{work_dir.absolute()}\n{agent.name}"
        alias = NATIVE_SKILL_ALIAS_PREFIX + hashlib.sha256(identity.encode()).hexdigest()[:24]
        view = copy.deepcopy(spec)
        view["name"] = alias
        resources = view.get("resources", [])
        resources = resources if isinstance(resources, list) else []
        view["resources"] = [
            r for r in resources if not (isinstance(r, str) and r.startswith("skill://"))
        ]
        needs_search = agent.name == "kirocrew" or any(
            isinstance(r, str) and r.startswith("skill://") for r in resources
        )
        if needs_search:
            excluded = view.get("excludedTools", [])
            if isinstance(excluded, list) and any(
                isinstance(t, str)
                and (t == "@kirocrew-core" or fnmatch.fnmatchcase(_SEARCH_TOOL, t))
                for t in excluded
            ):
                errors[agent.name] = (
                    "skill_search is explicitly excluded; bounded skill discovery requires it"
                )
                continue
            # The bounded directory must have a loading path even for a custom spec
            # whose authored resources rely on native skill activation. Expose
            # only the read/search capability; do not grant server-wide tools or
            # change the author's approval policy.
            from kiro_crew.agent import managed_mcp_spec_entry

            servers = view.setdefault("mcpServers", {})
            if not isinstance(servers, dict):
                errors[agent.name] = "mcpServers must be an object"
                continue
            original_core = servers.get("kirocrew-core", {})
            if not isinstance(original_core, dict):
                errors[agent.name] = "kirocrew-core must be a server object"
                continue
            disabled = original_core.get("disabled", False)
            disabled_tools = original_core.get("disabledTools", [])
            if not isinstance(disabled, bool):
                errors[agent.name] = "kirocrew-core.disabled must be a boolean"
                continue
            if not isinstance(disabled_tools, list) or any(
                not isinstance(tool, str) for tool in disabled_tools
            ):
                errors[agent.name] = "kirocrew-core.disabledTools must be a list of strings"
                continue
            if disabled or "skill_search" in disabled_tools:
                errors[agent.name] = "skill_search is disabled; bounded skill discovery requires it"
                continue
            entry = managed_mcp_spec_entry("kirocrew-core")
            if entry is None:
                errors[agent.name] = "Crew's managed skill search server is unavailable"
                continue
            for key in ("autoApprove", "disabledTools", "timeout"):
                if key in original_core:
                    entry[key] = original_core[key]
            servers["kirocrew-core"] = entry
            tools = view.get("tools", [])
            if tools != "*" and isinstance(tools, list):
                if not any(t in tools for t in ("*", "@kirocrew-core", _SEARCH_TOOL)):
                    view["tools"] = [*tools, _SEARCH_TOOL]
            search_agents.add(agent.name)
        prompt = view.get("prompt")
        if isinstance(prompt, str) and prompt.startswith("file://"):
            path = Path(prompt[7:]).expanduser()
            if not path.is_absolute():
                view["prompt"] = "file://" + (source.parent / path).absolute().as_posix()
        aliases[agent.name] = alias
        specs[agent.name] = view
        ownership[alias] = {
            _MANAGED_MARKER: _MANAGED_MARKER_VALUE,
            _MANAGED_CREW_HOME: crew_home_id,
            _MANAGED_WORK_DIR: work_dir_id,
            _MANAGED_AGENT: agent.name,
            _MANAGED_SOURCE: source.absolute().as_posix(),
        }

    try:
        alias_lock = _projection_alias_lock(directory)
    except OSError:
        logger.warning(
            "skill projection: alias lock unavailable; retaining aliases, settings, and using "
            "authored agents",
            exc_info=True,
        )
        # `local` was read before agent enumeration and lock acquisition. A
        # concurrent projection can write a newer overlay or unrelated setting
        # while this process waits, so writing this stale snapshot would clobber
        # that update. Keep the current file byte-for-byte; a later successful
        # preparation or explicit rollback can update it under normal ownership.
        return None
    with alias_lock:
        try:
            settings_lock = workspace_cli_settings_lock(work_dir)
            with settings_lock as locked_settings:
                # This is the authoritative read for both projected resources and
                # the write below. Every in-product workspace cli.json writer uses
                # the same sidecar lock, so no effort or Tool Search update can land
                # between this read and commit.
                local = _settings(locked_settings)
                inherited = local.get(_MANAGED_SETTING)
                preference_source = local.get(_INHERIT_SOURCE)
                if not isinstance(inherited, bool) or local.get(_INHERIT_SETTING) is not True:
                    local[_PREVIOUS_INHERITANCE] = {
                        "present": _INHERIT_SETTING in local,
                        "value": local.get(_INHERIT_SETTING),
                    }
                    preference_source = "local" if _INHERIT_SETTING in local else "global"
                    inherited = (
                        local.get(_INHERIT_SETTING, global_settings.get(_INHERIT_SETTING))
                        is not True
                    )
                elif preference_source == "global":
                    inherited = global_settings.get(_INHERIT_SETTING) is not True

                if inherited:
                    for view in specs.values():
                        for resource in (
                            f"file://{kiro_home().as_posix()}/steering/**/*.md",
                            "file://.kiro/steering/**/*.md",
                            "file://AGENTS.md",
                        ):
                            if resource not in view["resources"]:
                                view["resources"].append(resource)

                metadata_dir = _ensure_projection_metadata_directory(directory) if aliases else None
                lease_stack = _acquire_projection_lease(directory, set(aliases.values()))
                try:
                    for agent_name, alias in aliases.items():
                        alias_raw = json.dumps(specs[agent_name], ensure_ascii=False)
                        metadata = {
                            **ownership[alias],
                            _MANAGED_ALIAS_SHA256: hashlib.sha256(alias_raw.encode()).hexdigest(),
                        }
                        atomic_write(
                            directory / f"{alias}.json",
                            alias_raw,
                            restrict_to_owner=True,
                        )
                        assert metadata_dir is not None
                        atomic_write(
                            metadata_dir / f"{alias}.json",
                            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                            restrict_to_owner=True,
                        )
                    local[_MANAGED_SETTING] = inherited
                    local[_INHERIT_SOURCE] = preference_source
                    local[_INHERIT_SETTING] = True
                    atomic_write(locked_settings, json.dumps(local, indent=2))
                    prepared = NativeSkillProjection(aliases, specs, errors, search_agents)
                    prepared._lease_finalizer = weakref.finalize(prepared, lease_stack.close)
                except BaseException:
                    lease_stack.close()
                    raise
        except OSError:
            logger.warning(
                "skill projection: workspace settings or lease lock unavailable; retaining "
                "aliases and using authored agents",
                exc_info=True,
            )
            return None
        _register_active_projection(prepared)
        _prune_stale_managed_aliases(directory, crew_home_id, keep=set(aliases.values()))

    return prepared

"""Which views are this data home's.

The ownership-record keys, the managed description and the home tag it carries, the
one home comparison, the view identity and digest a sidecar binds, the sidecar and
metadata reads, the test for a view published before ownership records, and whether a
published copy is already current.

:mod:`kiro_crew.acp.skill_projection` stays the import path and the patch surface; see
:mod:`kiro_crew.acp.skill_projection_parts`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.acp.skill_projection_parts.side_files import (
    _LEGACY_ALIAS_NAME_RE,
    _PROJECTION_LEASE_MAX_BYTES,
    _metadata_dirs,
    _path_exists,
    _projection_metadata_dir,
)
from kiro_crew.acp.skill_projection_parts.view_publication import _ALIAS_DESCRIPTION_MAX_CHARS
from kiro_crew.agent_spec_format import volatile_env_keys
from kiro_crew.config.paths import kiro_home
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes

# Generated specs stay in Kiro's shared agents directory, so metadata identifies
# them for direct scanners and scopes cleanup to the owning Kiro Crew data home.
_MANAGED_MARKER = "x-kirocrew-managed"
_MANAGED_MARKER_VALUE = "skill-view"
_MANAGED_CREW_HOME = "x-kirocrew-home"
_MANAGED_AGENT = "x-kirocrew-agent"
_MANAGED_SOURCE = "x-kirocrew-source"
_MANAGED_ALIAS_SHA256 = "x-kirocrew-alias-sha256"
# The digest of the alias's IDENTITY view (:func:`_alias_identity`), recorded next
# to the byte digest. A launcher that re-serializes every spec in the agents
# directory, or stamps a fresh VOLATILE value (:func:`volatile_env_keys`) into an
# MCP server's ``env``, changes the
# bytes but not what the alias is, so ownership survives it where the byte digest
# alone does not. Sidecars written without this key carry only the byte digest
# and are judged by it.
_MANAGED_VIEW_SHA256 = "x-kirocrew-view-sha256"

# The ``description`` every view carries. kiro-cli denies unknown spec fields,
# so this display field is the ONE place a view can say what it is to a reader
# that lists the agents directory as agents (the Kiro IDE drop-down): it names
# the agent to pick instead. The eight-hex tag after the agent is a digest of
# the publishing data home (:func:`_home_tag`). Two Kiro Crew homes share one
# agents directory whenever they share ``~/.kiro``, and with the ownership
# sidecars under each home's own data home a view of the OTHER home has no
# sidecar this home can see -- which is exactly what a view from a build before
# any sidecar looked like, and the legacy reclaim deletes those. The tag tells
# the two apart: a sidecar-less view carrying another home's tag is that home's
# live view and is left alone; one carrying no tag at all is the legacy case.
_MANAGED_VIEW_DESCRIPTION_PREFIX = "Kiro Crew managed view of "
_MANAGED_VIEW_DESCRIPTION_RE = re.compile(
    re.escape(_MANAGED_VIEW_DESCRIPTION_PREFIX) + r"(?P<agent>\S+) \[(?P<home>[0-9a-f]{8})\]"
)
_MANAGED_VIEW_DESCRIPTION_TEMPLATE = (
    "{prefix}{agent} [{home}]: auto-managed and auto-pruned by Kiro Crew; "
    "do not pick it. Use {agent} instead."
)
_HOME_TAG_HEX_CHARS = 8


def _home_tag(crew_home_id: str) -> str:
    """A short digest of *crew_home_id*, resolved the way :func:`_same_crew_home` compares.

    Hashed from the realpath on POSIX so the two spellings of one home (see
    :func:`_same_crew_home`) carry one tag; the literal id on Windows, where that
    resolution is deliberately not done.
    """
    resolved = crew_home_id
    if not platform_compat.IS_WINDOWS:
        try:
            resolved = os.path.realpath(crew_home_id)
        except (OSError, ValueError):
            resolved = crew_home_id
    return hashlib.sha256(resolved.encode("utf-8", "replace")).hexdigest()[:_HOME_TAG_HEX_CHARS]


def _managed_view_description(agent_name: str, crew_home_id: str) -> str:
    """The display text a view carries; see :data:`_MANAGED_VIEW_DESCRIPTION_PREFIX`.

    Never longer than :data:`_ALIAS_DESCRIPTION_MAX_CHARS`, the bound every other
    alias description is cut to: this one is written AFTER that cut, so it keeps
    the bound itself. The agent's name is the only part that varies and a spec
    on disk may carry one of any length, so a name that would push the text
    over is shortened (with an ellipsis, still one whitespace-free token, which
    is what :data:`_MANAGED_VIEW_DESCRIPTION_RE` matches); the home tag, which
    the prune reads back, is never touched.
    """
    home = _home_tag(crew_home_id)
    fixed = _MANAGED_VIEW_DESCRIPTION_TEMPLATE.format(
        prefix=_MANAGED_VIEW_DESCRIPTION_PREFIX, agent="", home=home
    )
    room = (_ALIAS_DESCRIPTION_MAX_CHARS - len(fixed)) // 2  # the name appears twice
    if len(agent_name) > room:
        agent_name = agent_name[: room - 1] + "\u2026"
    return _MANAGED_VIEW_DESCRIPTION_TEMPLATE.format(
        prefix=_MANAGED_VIEW_DESCRIPTION_PREFIX, agent=agent_name, home=home
    )


def _view_home_tag(spec: object) -> str | None:
    """The home tag a view's own description carries, or ``None`` for an untagged view."""
    if not isinstance(spec, dict):
        return None
    description = spec.get("description")
    if not isinstance(description, str):
        return None
    match = _MANAGED_VIEW_DESCRIPTION_RE.match(description)
    return match.group("home") if match is not None else None


def _same_crew_home(recorded: object, current: str) -> bool:
    """Whether a sidecar's recorded data home is THIS process's own data home.

    The ONE ownership-home comparison. The recorded value is the string a
    publisher stored (``data_home().absolute().as_posix()``) and *current* is the
    same string for this process; the stored value and the alias name hash are
    never touched, so no file is renamed or re-hashed by this.

    The strings are compared by their RESOLVED paths rather than literally, but
    ONLY on POSIX. One data home reaches disk under more than one spelling there:
    on a cloud desktop ``/home/<u>`` is a symlink to ``/local/home/<u>``, so a
    launch that uses the other spelling, a pod sharing ``~/.kiro``, or a moved
    home records ownership under one spelling and reads it under another. Compared
    literally those look like two homes and neither reclaim nor prune nor drain
    ever cleans up the other's views; compared by realpath they are the one home
    they are. ``os.path.realpath`` is best-effort and does not raise for an absent
    path, but a symlink loop can raise ``OSError`` (and ``ValueError`` for an
    embedded NUL); on any such failure this falls back to the exact literal
    comparison -- never widening what counts as this home.

    On Windows the resolution is NOT done at all. The two-spelling problem is a
    POSIX ``/home`` -> ``/local/home`` phenomenon that does not arise on Windows,
    while *recorded* is untrusted sidecar content, and ``os.path.realpath`` on
    Windows reaches the filesystem: an attacker-planted value that IS or RESOLVES
    THROUGH a UNC/junction target (a bare ``\\\\host\\share``, or a local
    ``C:\\link`` that is a directory junction to one) would make resolution open
    the remote host and leak SMB credentials. Enumerating those spellings one at
    a time does not converge -- the property that does is "never resolve the
    untrusted path on Windows", so Windows keeps the pre-fix literal comparison,
    which touched the filesystem for neither side. A legitimate Windows home is
    recorded and read under one spelling, so this loses no real match.
    """
    if not isinstance(recorded, str):
        return False
    if recorded == current:
        return True
    if platform_compat.IS_WINDOWS:
        # Resolution here would be filesystem I/O on an untrusted path; the
        # literal comparison above already failed, so the home stays foreign.
        return False
    try:
        return os.path.realpath(recorded) == os.path.realpath(current)
    except (OSError, ValueError):
        return False


def _managed_marker(spec: object) -> bool:
    """Return whether a generated spec carries this lifecycle's marker."""
    return isinstance(spec, dict) and spec.get(_MANAGED_MARKER) == _MANAGED_MARKER_VALUE


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _view_digest(view: dict[str, Any]) -> str:
    """The sha256 of *view*'s identity (:func:`_alias_identity`), serialization-free.

    What the ownership sidecar binds besides the exact bytes, so a launcher that
    re-serializes an alias or re-stamps an ``env`` value in it does not strand
    the pair. The identity is the one the alias is NAMED by.
    """
    return hashlib.sha256(_canonical_json(_alias_identity(view)).encode()).hexdigest()


def _parsed_spec(raw: bytes) -> dict[str, Any] | None:
    try:
        spec = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return None
    return spec if isinstance(spec, dict) else None


def _is_legacy_projected_view(path: Path, alias_raw: bytes) -> bool:
    """Whether *path* is a projected view from a build that wrote no ownership.

    Builds shipped before this lifecycle published aliases with neither a
    metadata sidecar nor an in-spec marker, so :func:`_managed_metadata_for_alias`
    cannot admit them and a reclaim keyed on ownership alone leaves the ENTIRE
    accumulated backlog on disk -- the exact per-turn tool-spec cost the
    projection exists to bound. Those aliases are still identifiable without a record: the
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
    except (ValueError, TypeError, RecursionError):
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
    """Load the ownership record bound to the alias at *path*, from either records dir.

    This home's own metadata directory is consulted first, then the legacy one in
    *directory* (:func:`_metadata_dirs`). A directory that HAS an entry by that
    name decides: a record there that does not bind these bytes is ``None`` and
    the legacy directory is not consulted, so a stale legacy record can never
    stand in for a current one that failed to bind. Only an ABSENT entry falls
    through to the next directory.
    """
    for metadata_dir in _metadata_dirs(directory):
        if _path_exists(metadata_dir / f"{path.stem}.json"):
            return _managed_metadata_in(metadata_dir, path, alias_raw)
    return None


def _managed_metadata_in(
    metadata_dir: Path, path: Path, alias_raw: bytes
) -> tuple[dict[str, Any], Path | None, tuple[int, int] | None, bytes | None] | None:
    """:func:`_managed_metadata_for_alias` over ONE records directory."""
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
            except (ValueError, TypeError, RecursionError):
                return None
            if not _managed_marker(metadata):
                return None
            if metadata.get(_MANAGED_ALIAS_SHA256) != hashlib.sha256(alias_raw).hexdigest():
                # The bytes moved. Still ours when the record binds the view's
                # IDENTITY and the file still says that: a launcher that
                # re-serialized it or re-stamped an ``env`` value changed no
                # part of what the projection published.
                recorded_view = metadata.get(_MANAGED_VIEW_SHA256)
                alias_spec = _parsed_spec(alias_raw) if isinstance(recorded_view, str) else None
                if alias_spec is None or _view_digest(alias_spec) != recorded_view:
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


def _sidecar_record(
    directory: Path, stem: str
) -> tuple[dict[str, Any] | None, Path, tuple[int, int] | None]:
    """``(record, path, identity)`` of the ownership sidecar named *stem*, digest unchecked.

    ``record`` is the parsed sidecar when it is a regular, non-link, bounded,
    well-formed file carrying this lifecycle's marker; ``identity`` is set when
    ANY entry exists by that name (so a caller can tell "absent" from
    "present but not provably ours"). Unlike :func:`_managed_metadata_for_alias`
    this does not bind the record to alias bytes: it answers who wrote the
    record, for the two callers that need exactly that -- a pair whose alias
    bytes moved, and a sidecar whose alias is gone.

    Reads this home's records directory first and the legacy one in *directory*
    second (:func:`_metadata_dirs`); the first directory that has ANY entry by
    that name answers, and the returned ``path`` is in that directory.
    """
    for metadata_dir in _metadata_dirs(directory):
        answer = _sidecar_record_in(metadata_dir, stem)
        if answer[2] is not None:
            return answer
    # Absent everywhere: the path names where this home WOULD record it.
    return None, _projection_metadata_dir() / f"{stem}.json", None


def _sidecar_record_in(
    metadata_dir: Path, stem: str
) -> tuple[dict[str, Any] | None, Path, tuple[int, int] | None]:
    """:func:`_sidecar_record` over ONE records directory."""
    path = metadata_dir / f"{stem}.json"
    directory_info = pinned_fs.lstat_by_name(metadata_dir)
    if directory_info is None:
        return None, path, None
    if platform_compat.is_link_or_junction(metadata_dir) or not stat.S_ISDIR(
        directory_info.st_mode
    ):
        return None, path, (directory_info.st_dev, directory_info.st_ino)
    info = pinned_fs.lstat_by_name(path)
    if info is None:
        return None, path, None
    identity = (info.st_dev, info.st_ino)
    if platform_compat.is_link_or_junction(path) or not stat.S_ISREG(info.st_mode):
        return None, path, identity
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
            raw = os.read(fd, _PROJECTION_LEASE_MAX_BYTES + 1)
        finally:
            os.close(fd)
    except OSError:
        return None, path, identity
    if (opened.st_dev, opened.st_ino) != identity or len(raw) > _PROJECTION_LEASE_MAX_BYTES:
        return None, path, identity
    record = _parsed_spec(raw)
    if not _managed_marker(record):
        return None, path, identity
    return record, path, identity


def _is_current_publication(
    directory: Path, alias_path: Path, alias_raw: str, crew_home_id: str
) -> bool:
    """Whether *alias_path* already says *alias_raw* and carries this home's sidecar.

    "Says", not "holds byte for byte": the comparison is of the parsed specs, so
    a launcher that re-serialized the file does not force a rewrite of
    every alias, and its sidecar, on every preparation under the shared lock.
    Every VALUE still counts, ``env`` included -- a changed value is a changed
    spec kiro-cli must be given, unlike the alias name and the ownership proof,
    which deliberately ignore ``env`` values (:func:`_alias_identity`).
    """
    info = pinned_fs.lstat_by_name(alias_path)
    if (
        info is None
        or platform_compat.is_link_or_junction(alias_path)
        or not stat.S_ISREG(info.st_mode)
    ):
        return False
    try:
        existing = safe_read_file_bytes(str(alias_path))
    except FileTooLargeError:
        return False
    if existing is None:
        return False
    if existing != alias_raw.encode():
        parsed = _parsed_spec(existing)
        if parsed is None or _canonical_json(parsed) != _canonical_json(json.loads(alias_raw)):
            return False
    managed = _managed_metadata_for_alias(directory, alias_path, existing)
    return managed is not None and _same_crew_home(managed[0].get(_MANAGED_CREW_HOME), crew_home_id)


def _alias_identity(view: dict[str, Any]) -> dict[str, Any]:
    """The part of *view* an alias is named by: the view minus volatile env values.

    A launcher that re-injects a per-launch nonce into each agent file's server
    env (a fresh id every sandbox start) changes the view on every spawn, and
    naming the alias by it mints a new file per spawn without bound. So the values
    of :func:`volatile_env_keys` are left out; every other env value is in, as a
    digest. A rotated credential therefore names a NEW alias -- one kiro-cli has
    not loaded, so ``set_mode`` never activates a copy still carrying the old
    credential -- and two launch contexts with different credentials never share
    one. Keys stay in too: adding or removing a variable names a new alias.
    """
    servers = view.get("mcpServers")
    if not isinstance(servers, dict):
        return view
    volatile = volatile_env_keys()
    identity = dict(view)
    identity["mcpServers"] = {
        name: (
            {**entry, "env": _env_identity(entry["env"], volatile)}
            if isinstance(entry, dict) and isinstance(entry.get("env"), dict)
            else entry
        )
        for name, entry in servers.items()
    }
    return identity


def _env_identity(env: dict[Any, Any], volatile: frozenset[str]) -> dict[str, str | None]:
    """*env* as identity: every key, and a digest of each value but the volatile ones."""
    return {
        str(key): (
            None
            if str(key) in volatile
            else hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()
        )
        for key, value in env.items()
    }

"""KiroCrew snapshot and restore — portable state management."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import socket
import stat as _stat
import tarfile
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import kiro_sessions_dir
from kiro_crew.gateway_lock import GatewayLock, GatewayLockError
from kiro_crew.history import (
    ARCHIVE_DIR_NAME,
    ARCHIVE_SEGMENT_DELIMITER,
    INCOGNITO_MEMORY_MODES,
    transcript_stem,
    transcript_stems,
)
from kiro_crew.messaging.link import canonical_key, is_legacy_slack_key

try:
    import pysqlite3 as sqlite3
except ImportError:
    import sqlite3

try:
    from kiro_crew.config.loader import DASHBOARD_PORT as _DASHBOARD_PORT
except Exception:  # pragma: no cover - optional during early/standalone import
    _DASHBOARD_PORT = int(os.environ.get("KIROCREW_PORT", 5476))

VALID_COMPONENTS = (
    "memory",
    "crons",
    "config",
    "skills",
    "workspace",
    "notifications",
    "security",
    "sessions",
)

# Files that must always have 0o600 permissions in snapshots and on restore.
SECURITY_SENSITIVE_FILES: frozenset = frozenset({"sel_hmac.key", "telemetry_salt"})

# Files that must NEVER ride a snapshot: sel_hmac.key is regenerated on restore
# so audit-log HMACs stay bound to the host that wrote them.
#
# This set is matched by BASENAME inside `_data_filter`, which runs over the
# ENTIRE tar — including the staged workspace/, plan_memory/, skills/ and
# sessions/ trees. So any name added here also silently drops a USER file that
# happens to share it. Keep the set minimal for that reason.
#
# The beacon's per-install identity (beacon_install_id / beacon_last_sent) is
# deliberately NOT here: snapshot staging copies an explicit per-component file
# list (CORE_FILES) plus a fixed set of directories, and no component lists a
# beacon file, so a root beacon file is never staged in the first place. The
# id-cloning hazard is closed by that non-selection, not by a basename filter.
NEVER_SNAPSHOT_FILES: frozenset = frozenset({"sel_hmac.key"})


def _data_filter(info: tarfile.TarInfo, _dest: str = "") -> tarfile.TarInfo | None:
    """Equivalent to tarfile ``"data"`` filter (Python 3.12+), with 3.10 fallback.

    Also rejects path traversal, symlinks, and hardlinks to eliminate TOCTOU
    race between pre-scan and extraction.
    Excludes sel_hmac.key (must be regenerated on restore, not shipped).
    Security-sensitive files get 0o600 permissions.
    """
    # Reject path traversal. POSIX checks apply everywhere; the Windows-syntax
    # checks (backslash separators, drive letters — incl. the drive-RELATIVE
    # `C:foo` form is_absolute() misses, which resolves against the drive CWD
    # at extraction) apply ONLY when extracting on Windows, where tarfile
    # honors '\' as a native separator. They must NOT run on POSIX: ':' and
    # '\' are legal characters in Linux/macOS filenames, so a workspace file
    # named `a:1` or `notes..\old` would be silently dropped from a
    # Linux-to-Linux restore.
    name = info.name
    traversal = (
        name.startswith("/")
        or ".." in PurePosixPath(name).parts
        or PurePosixPath(name).is_absolute()
    )
    if not traversal and platform_compat.IS_WINDOWS:
        traversal = (
            name.startswith("\\")
            or ".." in PureWindowsPath(name).parts
            or PureWindowsPath(name).is_absolute()
            or bool(PureWindowsPath(name).drive)
        )
    if traversal:
        print(f"⚠️  Rejecting path traversal entry: {info.name}")
        return None
    # Reject symlinks and hardlinks
    if info.issym() or info.islnk():
        print(f"⚠️  Rejecting symlink/hardlink entry: {info.name}")
        return None
    # Never ship these — each must be regenerated on the restoring host.
    basename = PurePosixPath(info.name).name
    if basename in NEVER_SNAPSHOT_FILES:
        return None
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    # Security-sensitive files get restricted permissions
    if not info.isdir() and basename in SECURITY_SENSITIVE_FILES:
        info.mode = 0o600
    else:
        info.mode = 0o755 if info.isdir() else 0o644
    return info


def _default_snapshot_dir() -> str:
    """Return snapshot directory from config, falling back to <config_dir>/snapshots."""
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        d = KiroCrewConfig.load().snapshot_dir
        if d:
            return str(Path(d).expanduser())
    except Exception:
        pass
    try:
        from kiro_crew.config.paths import config_dir

        return str(config_dir() / "snapshots")
    except Exception:
        return str(Path.home() / ".kiro" / "crew" / "snapshots")


def _audit(event_type: str, resources: str) -> None:
    """Emit a SEL audit event for snapshot/restore operations."""
    try:
        from kiro_crew.sel import SecurityEvent, sel

        sel().log(
            SecurityEvent(
                event_id=os.urandom(8).hex(),
                timestamp=datetime.now(timezone.utc).isoformat(),
                event_type=event_type,
                caller_identity=os.environ.get("USER", "unknown"),
                agent="kirocrew",
                source="cli",
                operation=event_type,
                outcome="completed",
                resources=resources,
            )
        )
    except Exception as e:
        import logging

        logging.getLogger(__name__).warning("SEL audit event '%s' failed: %s", event_type, e)


CORE_FILES: dict[str, tuple[str, ...]] = {
    "memory": ("memory.db", "memory_index.db"),
    "crons": ("crons.json",),
    "config": ("config.json", "session_map.json", "hooks.json", "project_dir", "workspace_dir"),
    "notifications": ("notifications.jsonl",),
    "security": ("telemetry_salt",),  # sel_hmac.key excluded — regenerated on restore
}

COMPONENT_HELP = {
    "memory": "memory.db, memory_index.db (semantic, episodic, knowledge graph)",
    "crons": "crons.json (scheduled jobs)",
    "config": "config.json, session_map.json, hooks.json, project_dir, workspace_dir",
    "skills": "skills/ directory",
    "workspace": "workspace/, plan_memory/ directories",
    "notifications": "notifications.jsonl (notification history)",
    "security": "telemetry_salt (sel_hmac.key excluded — regenerated on restore)",
    "sessions": "sessions/ (chat transcripts + archives; incognito/temporary sessions excluded)",
}


def _mc_dir() -> Path:
    # Use the shared resolver so snapshot/restore honor the documented
    # KIROCREW_HOME override (and the same ~/.kiro/crew default) as every other
    # module — not an undocumented KIROCREW_DIR, which would make snapshots
    # silently target the real home even when state was relocated.
    from kiro_crew.config.loader import config_dir

    return config_dir()


def _fsize(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _want(components: list[str] | None, name: str) -> bool:
    return components is None or name in components


def _list_components() -> None:
    print("Available components:")
    for k, v in COMPONENT_HELP.items():
        print(f"  {k:16s} {v}")
    print("\nCombine with commas: --components memory,crons,skills")


def _pinned_copy_file(
    s: str, d: str, *, dir_fd: int | None = None, name: str | None = None
) -> None:
    """Copy one file's bytes from a descriptor pinned to a validated inode.

    The session-staging walk copies user-writable trees, so the copy itself
    must not trust the path name: ``shutil.copy2`` dereferences a hardlink
    into innocent-looking regular bytes, and the tar pass's hardlink
    rejection (``_data_filter``) never sees a link to reject — a hardlink to
    a credential (e.g. ``~/.aws/credentials``) planted inside an allowlisted
    session workspace would ride the snapshot as plain content. Same
    open-first discipline as ``hooks.safe_read_file_bytes_nolink`` (and the
    ``on_hardlink`` refusal in the artifacts staging path): open with
    ``O_NOFOLLOW`` where the platform has it, then ``fstat`` the DESCRIPTOR —
    the inode that is validated is exactly the inode whose bytes are copied,
    so no check-to-use window remains. Anything not a regular file with
    ``st_nlink == 1`` is skipped with a warning, matching this module's
    existing skip conventions. Mode and timestamps are applied from the same
    ``fstat`` result rather than a fresh by-name stat.

    When ``dir_fd``/``name`` are given, the source is opened relative to the
    caller's pinned parent-directory descriptor (``_stage_tree_pinned``), so
    the ``O_NOFOLLOW`` here protects the final component of a path whose
    every ancestor was itself opened ``O_NOFOLLOW`` against its parent — no
    by-name re-traversal anywhere. ``s`` remains the by-name path, used only
    for messages.

    ``FileNotFoundError`` propagates for the caller's vanish tolerance; every
    other ``OSError`` propagates so real failures still abort the snapshot.
    """
    try:
        if dir_fd is not None and name is not None:
            fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
        else:
            fd = os.open(s, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            # A symlink final component that appeared after the listing-time
            # link screen — refuse it the same way the screen would have.
            print(f"⚠️  Skipping symlink in source tree: {s}")
            return
        raise
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            print(f"⚠️  Skipping hardlinked or non-regular file during snapshot copy: {s}")
            return
        with os.fdopen(fd, "rb") as fsrc:
            fd = -1  # ownership passed to the file object
            with open(d, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst)
        os.chmod(d, _stat.S_IMODE(st.st_mode))
        os.utime(d, ns=(st.st_atime_ns, st.st_mtime_ns))
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _copytree_safe(src: Path, dst: Path, *, tolerate_vanished: bool = False, **kwargs) -> None:
    """copytree that skips symlinks to prevent sensitive file leakage.

    ``tolerate_vanished=True`` additionally ignores entries that DISAPPEAR
    between the directory listing and their copy (a live session being
    deleted while the snapshot walks the tree) — and ONLY those: every other
    error (EACCES, ENOSPC, ...) still aborts, so a snapshot never silently
    ships with files it failed to read. On this path every file is copied
    through :func:`_pinned_copy_file`, which additionally refuses hardlinked
    and non-regular sources at copy time.
    """
    outer_ignore = kwargs.pop("ignore", None)

    def _ignore_symlinks(directory, contents):
        skipped = {name for name in contents if os.path.islink(os.path.join(directory, name))}
        for name in skipped:
            print(f"⚠️  Skipping symlink in source tree: {os.path.join(directory, name)}")
        if outer_ignore:
            skipped |= set(outer_ignore(directory, contents))
        return skipped

    if not tolerate_vanished:
        shutil.copytree(str(src), str(dst), ignore=_ignore_symlinks, **kwargs)
        return

    def _copy2_tolerant(s, d, **kw):
        try:
            _pinned_copy_file(s, d)
        except FileNotFoundError:
            print(f"⚠️  Skipping vanished file during snapshot copy: {s}")

    try:
        shutil.copytree(
            str(src), str(dst), ignore=_ignore_symlinks, copy_function=_copy2_tolerant, **kwargs
        )
    except shutil.Error as err:
        # A subdirectory vanishing mid-walk surfaces here as an aggregated
        # (src, dst, why) entry rather than through copy_function. Filter the
        # vanished ones by errno marker; anything else re-raises untouched.
        remaining = [
            e
            for e in err.args[0]
            if not any(m in str(e[2]) for m in ("[Errno 2]", "[WinError 2]", "[WinError 3]"))
        ]
        if remaining:
            raise shutil.Error(remaining) from err
        for e in err.args[0]:
            print(f"⚠️  Skipping vanished entry during snapshot copy: {e[0]}")


# Staging pins the SOURCE traversal to directory descriptors where the
# platform supports it (POSIX); Windows keeps the by-name walk.
_STAGE_DIR_FD_OK = os.open in os.supports_dir_fd and os.listdir in os.supports_fd


def _stage_tree_pinned(
    src: Path, dst: Path, ignore: Callable[[str, list[str]], set[str]] | None = None
) -> None:
    """Source-pinned staging copy: no by-name re-traversal of any component.

    ``_copytree_safe``'s walk re-resolves source paths BY NAME for every
    entry, so its link screens protect only the final component: an agent
    that swaps an allowlisted ancestor DIRECTORY for a credential-directory
    link mid-walk redirects every deeper by-name open through the link, and
    ``_pinned_copy_file``'s ``O_NOFOLLOW`` never fires — the final component
    inside the replaced tree is a plain regular file. Here every directory
    is opened ``O_NOFOLLOW|O_DIRECTORY`` relative to its PARENT's descriptor
    and every file is opened ``O_NOFOLLOW`` relative to its pinned parent:
    the directory that was validated is exactly the directory descended
    into. Shares the containment invariant with the destination-side
    ``_copy_tree_no_overwrite_guarded`` in the artifacts restore path — both
    refuse any component that stops being a plain directory between
    validation and use.

    Otherwise matches ``_copytree_safe(tolerate_vanished=True)`` semantics:
    symlinks and non-regular files are skipped with a warning, entries that
    vanish mid-walk are skipped, ``ignore`` sees ``(directory, contents)``
    by name, and every other error aborts the snapshot. Platforms without
    ``dir_fd`` support (Windows) fall back to the by-name walk at the call
    site — junction refusal there lives in the ``ignore`` screens.
    """
    o_dir = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)

    def _walk(dir_fd: int, by_name: str, cur_dst: Path) -> None:
        names = os.listdir(dir_fd)
        skipped = set(ignore(by_name, names)) if ignore else set()
        for entry in sorted(names):
            if entry in skipped:
                continue
            path = os.path.join(by_name, entry)
            try:
                st = os.lstat(entry, dir_fd=dir_fd)
            except FileNotFoundError:
                print(f"⚠️  Skipping vanished entry during snapshot copy: {path}")
                continue
            if _stat.S_ISLNK(st.st_mode):
                print(f"⚠️  Skipping symlink in source tree: {path}")
            elif _stat.S_ISDIR(st.st_mode):
                try:
                    child_fd = os.open(entry, o_dir, dir_fd=dir_fd)
                except FileNotFoundError:
                    print(f"⚠️  Skipping vanished entry during snapshot copy: {path}")
                    continue
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        # Swapped for a link (or replaced by a file) between
                        # the lstat above and this pinned open — refuse it,
                        # exactly as the listing-time screen would have.
                        print(f"⚠️  Skipping symlink in source tree: {path}")
                        continue
                    raise
                try:
                    child_dst = cur_dst / entry
                    child_dst.mkdir(exist_ok=True)
                    _walk(child_fd, path, child_dst)
                finally:
                    os.close(child_fd)
            elif _stat.S_ISREG(st.st_mode):
                try:
                    _pinned_copy_file(path, str(cur_dst / entry), dir_fd=dir_fd, name=entry)
                except FileNotFoundError:
                    print(f"⚠️  Skipping vanished file during snapshot copy: {path}")
            else:
                print(f"⚠️  Skipping hardlinked or non-regular file during snapshot copy: {path}")

    try:
        root_fd = os.open(str(src), o_dir)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT):
            # The root itself was swapped or removed after the caller's
            # listing-time screen.
            print(f"⚠️  Skipping symlink in source tree: {src}")
            return
        raise
    try:
        dst.mkdir(parents=True, exist_ok=True)
        _walk(root_fd, str(src), dst)
    finally:
        os.close(root_fd)


def _copy_tree_no_overwrite(src: Path, dst: Path) -> None:
    for item in src.rglob("*"):
        if item.is_symlink():
            continue
        target = dst / item.relative_to(src)
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(item), str(target))


# ── Sessions component ────────────────────────────────────────────────────────

_TRANSCRIPT_SUFFIX = ".jsonl"
# How many head lines to scan for the transcript's metadata record. Mirrors the
# head-read in ``history.ConversationLog.recent_from_source``, which reads the
# same marker for the same purpose (skipping private sessions).
_TRANSCRIPT_HEAD_LINES = 5
# kiro-cli session ids are UUID-shaped; the id is joined onto a directory path
# below, so its shape is enforced rather than trusted (it arrives from a
# snapshot produced elsewhere).
_SAFE_SID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
#: Sentinel for "memory_mode key absent from the metadata record" — distinct
#: from every JSON-representable value, including null.
_MODE_ABSENT = object()


def _transcript_candidates(sessions_root: Path) -> list[Path]:
    """Live transcript files eligible for staging decisions.

    Link/junction entries are excluded at the source: a linked transcript is
    never copied (the copy-time guard skips it), so it must not be able to
    vouch for anything either — a stem in the staging allowlist authorizes
    that session's archive segments and workspace directory, and a link is
    not evidence the session's data actually lives here.
    """
    return [
        p
        for p in sessions_root.glob(f"*{_TRANSCRIPT_SUFFIX}")
        if not platform_compat.is_link_or_junction(p)
    ]


def _restricted_session_stems(
    transcript_paths: list[Path], restricted_modes: frozenset
) -> set[str]:
    """Filename stems of transcripts NOT positively classified as persistent.

    The privacy marker is the ``memory_mode`` field of the metadata record at
    the head of each transcript. Classification fails closed: a transcript is
    restricted unless a metadata record is found whose ``memory_mode`` is a
    recognized persistent value — an unreadable head, a head with no metadata
    record, and an unrecognized or restricted mode all keep the transcript
    out of the snapshot. An ABSENT ``memory_mode`` field is persistent by the
    store's own contract (``ConversationLog`` defaults it to ``"persistent"``
    on read), so legacy transcripts written before the field existed still
    ride.
    """
    restricted: set[str] = set()
    for path in transcript_paths:
        stem = path.name[: -len(_TRANSCRIPT_SUFFIX)]
        persistent = False
        try:
            with open(path, encoding="utf-8") as f:
                for _, line in zip(range(_TRANSCRIPT_HEAD_LINES), f):
                    try:
                        d = json.loads(line.strip())
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if isinstance(d, dict) and d.get("_type") == "metadata":
                        # Sentinel distinguishes a genuinely ABSENT field
                        # (persistent by the store's setdefault contract) from
                        # a present-but-malformed value: null, "", false, 0,
                        # [] and {} are all present, unclassifiable, and must
                        # fail closed rather than collapse to "".
                        raw = d.get("memory_mode", _MODE_ABSENT)
                        if raw is _MODE_ABSENT:
                            persistent = True
                        elif isinstance(raw, str):
                            mode = raw.lower()
                            persistent = mode not in restricted_modes and mode == "persistent"
                        break  # the first metadata record decides
        except (OSError, UnicodeError):
            # Unreadable OR undecodable head: cannot classify, fail closed.
            persistent = False
        if not persistent:
            restricted.add(stem)
    return restricted


def _flag_restricted_stems(mc: Path) -> set[str] | None:
    """Stems restricted via session-map privacy flags, canonical + legacy.

    Channel sessions (e.g. Slack ``!incognito``) persist privacy as a boolean
    flag on the ``session_map.json`` entry rather than in the transcript's
    metadata head, so the head-based classifier alone would export them.
    Returns ``None`` when the map exists but cannot be parsed — the caller
    must fail closed (stage no sessions), since flags cannot be ruled out.
    """
    map_path = mc / "session_map.json"
    if not map_path.is_file():
        return set()
    try:
        raw = json.loads(map_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(raw, dict):
        return None
    out: set[str] = set()
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        flags = entry.get("flags")
        if isinstance(flags, dict) and any(flags.get(f) for f in INCOGNITO_MEMORY_MODES):
            out |= set(transcript_stems(str(key)))
    return out


def _stage_sessions(mc: Path, stage: Path) -> None:
    """Stage the sessions/ tree (chat transcripts, archives, session workspaces).

    Excluded from staging:

    * Incognito/temporary transcripts — private by contract everywhere else
      (never searched, listed, or summarized), so they must not leave the host
      inside a snapshot either. Their archive segments and per-session
      workspace directories are excluded with them.
    * ``.lock`` files — per-process runtime artifacts with no meaning on
      another host.
    """
    src = mc / "sessions"
    if not src.is_dir():
        return
    if platform_compat.is_link_or_junction(src):
        # A linked sessions root re-targets the walk to an arbitrary tree
        # (e.g. a credentials directory), which would then ride the portable
        # snapshot. POSIX symlinks AND Windows junctions must both be refused;
        # the plain copytree symlink guard does not see junctions.
        print("⚠️  sessions not snapshotted (sessions/ is a link or junction)")
        return

    candidates = _transcript_candidates(src)
    flag_restricted = _flag_restricted_stems(mc)
    if flag_restricted is None:
        # The session map exists but cannot be read: privacy flags cannot be
        # ruled out for any session, so nothing is staged (fail closed).
        print("⚠️  sessions not snapshotted (session_map.json unreadable; privacy flags unknown)")
        return
    restricted = _restricted_session_stems(candidates, INCOGNITO_MEMORY_MODES) | flag_restricted
    # Archive segments are allowlisted by their owning live transcript rather
    # than denylisted by the restricted set: a rotated segment has no reliable
    # privacy marker of its own, so a segment whose live transcript is absent
    # (or restricted) cannot be classified and must not ride the snapshot.
    staged_stems = {p.name[: -len(_TRANSCRIPT_SUFFIX)] for p in candidates} - restricted
    # A pre-migration Slack session keeps its live transcript under the bare
    # thread_ts filename, while its archive segments and workspace directory
    # are always named by the canonical ``slack:`` key (_archive_lines derives
    # its stem from the session key, not the transcript path). Allowlist the
    # canonical spelling too — unless that canonical transcript itself exists
    # and is restricted, in which case privacy wins over the alias.
    owner_stems = set(staged_stems)
    for stem in staged_stems:
        if is_legacy_slack_key(stem):
            alias = transcript_stem(canonical_key(stem))
            if alias not in restricted:
                owner_stems.add(alias)
    root = src.resolve()
    archive_rel = Path(ARCHIVE_DIR_NAME)

    def _dir_is_staged(name: str) -> bool:
        # A per-session workspace directory is named by the session key its
        # subagent results belong to. Its transcript stem is either the
        # sanitized key itself (channel sessions) or carries the ``dashboard_``
        # prefix (dashboard slot keys — see chat_utils._normalize_slot_key's
        # documented invariant). Allowlisted against staged live transcripts —
        # like archive segments, a workspace whose transcript is absent (e.g.
        # a deleted incognito session with retained results) cannot be
        # classified and must not ride the snapshot.
        stem_channel = transcript_stem(name)
        stem_dashboard = transcript_stem(f"dashboard:{name}")
        # The name maps to TWO possible owning sessions; when EITHER
        # interpretation is restricted the directory may hold that restricted
        # session's results, so privacy wins over the staged sibling (an
        # incognito ``slack:<ts>`` must not leak through a persistent
        # ``dashboard:slack_<ts>`` sharing the sanitized name).
        if stem_channel in restricted or stem_dashboard in restricted:
            return False
        return stem_channel in owner_stems or stem_dashboard in owner_stems

    def _archive_owner_is_staged(stem: str) -> bool:
        # Exact owner match only: the segment name is <owner><DELIM><stamp>,
        # and the owner itself may contain the delimiter, so the owner is
        # everything before the LAST delimiter. A prefix test would leak a
        # restricted sibling whose stem merely extends a staged stem
        # (owner__private__stamp startswith owner__).
        if ARCHIVE_SEGMENT_DELIMITER not in stem:
            return False
        return stem.rsplit(ARCHIVE_SEGMENT_DELIMITER, 1)[0] in owner_stems

    def _ignore(directory: str, contents: list[str]) -> set[str]:
        try:
            rel = Path(directory).resolve().relative_to(root)
        except (OSError, ValueError):
            return set()
        skipped: set[str] = set()
        for name in contents:
            child = Path(directory) / name
            if platform_compat.is_link_or_junction(child):
                # Windows junctions pass the plain symlink check inside
                # _copytree_safe; refuse them here so a junctioned child
                # cannot pull an external tree into the snapshot.
                skipped.add(name)
            elif name.endswith((".lock", ".tmp")):
                # ``.tmp``: atomic_write stages full transcript bodies in
                # mkstemp files beside the target; one caught mid-replace
                # would ride the snapshot with NO stem classification, so an
                # incognito transcript could leak through the privacy filter.
                skipped.add(name)
            elif rel == Path(".") and name.endswith(_TRANSCRIPT_SUFFIX):
                # Allowlist against the scanned candidates, not a denylist
                # against the restricted set: a transcript created after the
                # candidate scan (e.g. a new incognito session starting while
                # the copy walk runs) has no classification and must not ride.
                if name[: -len(_TRANSCRIPT_SUFFIX)] not in staged_stems:
                    skipped.add(name)
            elif rel == Path(".") and name != ARCHIVE_DIR_NAME:
                if child.is_dir() and not _dir_is_staged(name):
                    skipped.add(name)
            elif rel == archive_rel and name.endswith(_TRANSCRIPT_SUFFIX):
                stem = name[: -len(_TRANSCRIPT_SUFFIX)]
                if not _archive_owner_is_staged(stem):
                    skipped.add(name)
        return skipped

    dst = stage / "sessions"
    if _STAGE_DIR_FD_OK:
        _stage_tree_pinned(src, dst, _ignore)
    else:  # pragma: no cover - Windows fallback (by-name walk + link screens)
        _copytree_safe(src, dst, tolerate_vanished=True, ignore=_ignore)

    # Post-copy privacy sweep: classification above ran BEFORE the copy walk,
    # so a session flagged restricted while the walk was running (a Slack
    # ``!incognito`` landing on the map, or a transcript head rewritten to a
    # restricted mode) was copied under a stale persistent verdict. Re-read
    # the map and re-scan the STAGED transcript heads (the post-copy state),
    # recompute the allowlist, and evict anything no longer authorized. The
    # stage is private scratch, so deleting from it cannot race the gateway.
    post_flag_restricted = _flag_restricted_stems(mc)
    if post_flag_restricted is None:
        # The map became unreadable after the pre-copy read: privacy flags can
        # no longer be ruled out for anything already staged (fail closed).
        shutil.rmtree(dst, ignore_errors=True)
        print("⚠️  sessions not snapshotted (session_map.json unreadable; privacy flags unknown)")
        return
    post_candidates = _transcript_candidates(dst)
    post_restricted = (
        _restricted_session_stems(post_candidates, INCOGNITO_MEMORY_MODES) | post_flag_restricted
    )
    post_staged = {p.name[: -len(_TRANSCRIPT_SUFFIX)] for p in post_candidates} - post_restricted
    post_owners = set(post_staged)
    for stem in post_staged:
        if is_legacy_slack_key(stem):
            alias = transcript_stem(canonical_key(stem))
            if alias not in post_restricted:
                post_owners.add(alias)
    for p in post_candidates:
        if p.name[: -len(_TRANSCRIPT_SUFFIX)] not in post_staged:
            p.unlink(missing_ok=True)
    arch_dir = dst / ARCHIVE_DIR_NAME
    if arch_dir.is_dir():
        for p in arch_dir.glob(f"*{_TRANSCRIPT_SUFFIX}"):
            stem = p.name[: -len(_TRANSCRIPT_SUFFIX)]
            if (
                ARCHIVE_SEGMENT_DELIMITER not in stem
                or stem.rsplit(ARCHIVE_SEGMENT_DELIMITER, 1)[0] not in post_owners
            ):
                p.unlink(missing_ok=True)
    for child in dst.iterdir():
        if child.name == ARCHIVE_DIR_NAME or not child.is_dir():
            continue
        sweep_channel = transcript_stem(child.name)
        sweep_dashboard = transcript_stem(f"dashboard:{child.name}")
        # Same ambiguity rule as _dir_is_staged: a name whose EITHER
        # interpretation is restricted may hold that restricted session's
        # results — privacy wins over the staged sibling.
        if (
            sweep_channel in post_restricted
            or sweep_dashboard in post_restricted
            or (sweep_channel not in post_owners and sweep_dashboard not in post_owners)
        ):
            shutil.rmtree(child, ignore_errors=True)


def _sessions_privacy_filter(
    mc: Path, arcname: str
) -> Callable[[tarfile.TarInfo], "tarfile.TarInfo | None"]:
    """Tar filter that re-enforces session privacy FLAGS at archive time.

    ``_stage_sessions`` classifies before its copy walk and sweeps after it,
    but the tarball is written later still: a privacy flag landing on
    ``session_map.json`` between the post-copy sweep and ``tar.add`` (a
    Slack ``!incognito`` on a just-staged thread) would ship the
    already-staged transcript under a stale persistent verdict. The tar
    filter runs per entry at the moment that entry is added, so consulting a
    fresh read of the flag map here makes the privacy verdict atomic with
    archive creation. The map is re-parsed only when its stat identity
    changes; an unreadable map fails closed for every sessions entry, the
    same rule as the staging-time reads. Staged transcript HEADS are not
    re-read: the stage is private scratch whose bytes were fixed at copy
    time — after the post-copy sweep only the live flag map keeps moving.

    Wraps :func:`_data_filter`, which keeps handling traversal/link/mode
    hygiene for the whole tar.
    """
    prefix = f"{arcname}/sessions/"
    cached_sig: object = object()  # never equal to a real signature
    cached: set[str] | None = None

    def _restricted_now() -> set[str] | None:
        nonlocal cached_sig, cached
        map_path = mc / "session_map.json"
        try:
            st = map_path.stat()
            sig: object = (st.st_ino, st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        if sig != cached_sig:
            cached = _flag_restricted_stems(mc)
            cached_sig = sig
        return cached

    def _filter(info: tarfile.TarInfo, dest: str = "") -> "tarfile.TarInfo | None":
        out = _data_filter(info, dest)
        if out is None or not out.name.startswith(prefix):
            return out
        parts = PurePosixPath(out.name[len(prefix) :]).parts
        if not parts:
            return out
        restricted = _restricted_now()
        if restricted is None:
            print(f"⚠️  Dropping sessions entry (session_map.json unreadable): {out.name}")
            return None
        first = parts[0]
        if first == ARCHIVE_DIR_NAME:
            if len(parts) < 2 or not parts[1].endswith(_TRANSCRIPT_SUFFIX):
                return out
            stem = parts[1][: -len(_TRANSCRIPT_SUFFIX)]
            if ARCHIVE_SEGMENT_DELIMITER not in stem:
                return out
            if stem.rsplit(ARCHIVE_SEGMENT_DELIMITER, 1)[0] in restricted:
                print(f"⚠️  Dropping freshly flag-restricted sessions entry: {out.name}")
                return None
            return out
        if len(parts) == 1 and first.endswith(_TRANSCRIPT_SUFFIX):
            if first[: -len(_TRANSCRIPT_SUFFIX)] in restricted:
                print(f"⚠️  Dropping freshly flag-restricted sessions entry: {out.name}")
                return None
            return out
        # Per-session workspace directory (or a file inside one). The name
        # maps to TWO possible owning sessions — same ambiguity rule as
        # ``_dir_is_staged``: privacy wins over either interpretation.
        if transcript_stem(first) in restricted or transcript_stem(f"dashboard:{first}") in (
            restricted
        ):
            print(f"⚠️  Dropping freshly flag-restricted sessions entry: {out.name}")
            return None
        return out

    return _filter


def _copy_sessions_no_overwrite(sd: Path, dd: Path) -> None:
    """Sessions-specific additive copy; never writes through a linked target.

    ``_copy_tree_no_overwrite`` guards the SOURCE against symlinks but trusts
    the destination. Sessions restore cannot: an existing local symlink or
    junction at any destination component (including a dangling one, which
    ``exists()`` reports False for) would redirect restored files outside the
    sessions tree. Every write is refused when any path component from the
    sessions root down to the target is a link.
    """

    def _path_is_linked(p: Path) -> bool:
        while True:
            if platform_compat.is_link_or_junction(p):
                return True
            if p == dd:
                return False
            parent = p.parent
            if parent == p:  # filesystem root — out of scope, refuse
                return True
            p = parent

    for item in sorted(sd.rglob("*")):
        if item.is_symlink():
            continue
        target = dd / item.relative_to(sd)
        if _path_is_linked(target):
            print(f"  ⚠️  skipping {item.relative_to(sd)} (linked restore destination)")
            continue
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive create closes the exists()-then-copy race: a --force
            # restore under a live gateway could otherwise truncate a
            # transcript the gateway created between the probe and the copy.
            # FileExistsError = the local side owns that file; keep it.
            try:
                fdesc = os.open(
                    str(target),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
                )
            except FileExistsError:
                continue
            try:
                # Copy THROUGH the exclusively created descriptor: closing it
                # and letting copy2 reopen by name would truncate anything a
                # live gateway appended to the path in between.
                with open(item, "rb") as src_f, os.fdopen(fdesc, "wb") as dst_f:
                    shutil.copyfileobj(src_f, dst_f)
                shutil.copystat(str(item), str(target))
            except BaseException:
                # BaseException, not OSError: a Ctrl-C mid-copy would
                # otherwise leave a partial transcript that every later
                # restore skips as existing. We reserved the target above,
                # so removing it on ANY failure is safe.
                target.unlink(missing_ok=True)
                raise


def _restore_sessions(snap: Path, mc: Path, components: list[str] | None) -> bool:
    """Additively restore sessions/; True only when restoration actually ran.

    Transcripts are irreplaceable conversation history, so unlike other
    components the sessions tree has no replace semantics and no backup step:
    files missing locally are copied in, files already present locally are kept
    as-is, in both restore modes. A snapshot created before the sessions
    component existed has no sessions/ directory and restores as a no-op.
    The return value gates session-map reconciliation — reconciling after a
    REFUSED restore (e.g. a linked local sessions root) would drop restored
    mappings whose transcripts were never copied in.
    """
    sd = snap / "sessions"
    if not sd.is_dir():
        if components is not None and "sessions" in components:
            print("  sessions: not present in snapshot (created before sessions were included)")
        return False
    dd = mc / "sessions"
    if platform_compat.is_link_or_junction(dd):
        # A linked local sessions root would redirect every restored transcript
        # outside the data home. Refuse rather than follow.
        print("  ⚠️  sessions not restored (local sessions/ is a link or junction)")
        return False
    dd.mkdir(parents=True, exist_ok=True)
    _copy_sessions_no_overwrite(sd, dd)
    print("  ✅ sessions (existing local files kept)")
    return True


def _reconcile_session_map(mc: Path) -> None:
    """Drop restored session-map entries whose session exists nowhere on this host.

    A restored ``session_map.json`` can reference sessions the restoring host
    does not have — most commonly entries whose transcript predates the
    sessions component, or whose incognito transcript was deliberately not
    shipped. Such an entry maps a key to a conversation that can never be
    resumed here.

    Conservative by construction: an entry is removed only when it names a
    kiro-cli session id AND neither its Kiro Crew transcript nor its kiro-cli
    session file exists locally. Linkage-only entries (no sid) and
    externally-stored providers are kept, mirroring ``SessionMap.prune``.

    An entry whose sid is unusable here but which is backed by something
    durable — a local transcript, privacy flags (e.g. Slack ``!incognito``),
    or thread linkage — is kept with the sid CLEARED rather than left stale:
    ``SessionMap.prune`` deletes any stale-sid entry that lacks a durable
    flag (only ``mirror_opt_out``) at gateway startup — thread linkage,
    mirrors, and session-scoped flags do NOT save it — so a kept-but-stale
    sid would take the whole entry (flags and linkage included) down on the
    first start. A cleared sid survives that prune when the entry carries
    thread linkage or a mirror (prune keeps linkage-shaped SIDLESS entries);
    for the rest, clearing still strictly beats a stale sid — the transcript
    file itself is never at stake either way.

    The pass runs under the gateway's own single-writer lock
    (``gateway.lock``): reconciliation is a read-modify-write of the whole
    map, so a live gateway writing an entry between this pass's read and its
    write would see that entry deleted by the stale rewrite. Holding the
    lock makes the pass atomic against gateway map writes in both
    directions — a gateway starting mid-pass is refused by its own startup
    acquire. When a live gateway already holds the lock (a ``--force``
    restore), the pass is skipped rather than raced: no cross-process map
    write can stick under a live gateway anyway — its next whole-map save
    from in-memory state overwrites the restored file, so the entries this
    pass would preserve are lost to that save with or without it. That is
    NOT because ``SessionMap.prune`` is an equivalent substitute: prune
    DELETES a stale-sid entry outright unless it carries a durable flag
    (``_DURABLE_FLAGS`` is ``mirror_opt_out`` only) — thread linkage and
    mirrors included. Clearing such sids so linkage-bearing entries survive
    prune's startup pass is exactly what this reconciliation exists to do.
    """
    try:
        lock = GatewayLock(mc).acquire()
    except GatewayLockError:
        print(
            "  ⚠️  session_map reconciliation skipped (a running gateway owns "
            "the map; restored entries cannot outlive its next in-memory save)"
        )
        return
    try:
        _reconcile_session_map_locked(mc)
    finally:
        lock.release()


def _reconcile_session_map_locked(mc: Path) -> None:
    """The reconciliation pass itself. Caller holds ``gateway.lock``."""
    map_path = mc / "session_map.json"
    if not map_path.is_file():
        return
    try:
        raw = json.loads(map_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        print(f"⚠️  session_map reconciliation skipped (unreadable map: {e})")
        return
    if not isinstance(raw, dict):
        return
    sessions_root = mc / "sessions"
    cli_dir = kiro_sessions_dir()
    dropped: list = []
    cleared: list = []
    for key, val in raw.items():
        entry = {"sid": val} if isinstance(val, str) else val
        if not isinstance(entry, dict):
            continue
        sid = entry.get("sid")
        if not sid or entry.get("provider") == "claude_code":
            continue

        def _transcript_exists(stem: str) -> bool:
            # A key of filesystem-component length (corrupt or hostile map)
            # makes ``is_file`` raise ENAMETOOLONG — one bad entry must not
            # crash reconciliation mid-restore. Unprobeable = absent.
            try:
                return (sessions_root / f"{stem}{_TRANSCRIPT_SUFFIX}").is_file()
            except OSError:
                return False

        has_transcript = any(_transcript_exists(stem) for stem in transcript_stems(key))
        has_cli_session = (
            isinstance(sid, str)
            and bool(_SAFE_SID_RE.match(sid))
            and (cli_dir / f"{sid}.json").is_file()
        )
        if has_cli_session:
            continue
        # The sid is unusable on this host: unsafe shape, or no matching
        # kiro-cli session file. Leaving it in place is worse than clearing
        # it — the gateway's ``SessionMap.prune`` deletes any entry whose sid
        # lacks a local session file, taking the durable per-conversation
        # state (privacy flags, thread linkage) and the transcript mapping
        # down with it. Clear just the sid when anything durable backs the
        # entry (a local transcript, flags, linkage); drop bare entries.
        flags = entry.get("flags")
        has_state = bool(flags) or entry.get("slack_thread_ts") or entry.get("mirror")
        if has_transcript or has_state:
            cleared.append(key)
        else:
            dropped.append(key)
    if not dropped and not cleared:
        return
    for k in dropped:
        del raw[k]
    for k in cleared:
        entry = raw[k]
        if isinstance(entry, dict):
            entry["sid"] = ""
        else:
            raw[k] = {"sid": ""}
    # atomic_write, not a bare tmp+replace: it retries the rename on the
    # transient PermissionError a Windows antivirus/indexer hold produces,
    # so reconciliation cannot abort a restore after partially applying state.
    atomic_write(map_path, json.dumps(raw, indent=2))
    print(
        f"  Session map: dropped {len(dropped)} bare entries, cleared unusable "
        f"sids on {len(cleared)} entries with a local transcript or durable state"
    )


# ── Snapshot ──────────────────────────────────────────────────────────────────


def snapshot_main(
    argv: list[str] | None = None, *, parsed: argparse.Namespace | None = None
) -> int:
    if parsed is None:
        p = argparse.ArgumentParser(
            prog="kirocrew-snapshot",
            description="Create a portable .tar.gz snapshot of KiroCrew state.",
        )
        p.add_argument("output_dir", nargs="?", default=_default_snapshot_dir())
        p.add_argument("--keep", type=int, default=7)
        p.add_argument("--list", action="store_true", dest="list_snapshots")
        parsed = p.parse_args(argv)
    args = parsed

    if args.keep <= 0:
        print(f"❌ --keep value must be a positive integer, got: {args.keep}")
        return 1

    out = Path(args.output_dir or _default_snapshot_dir())

    if args.list_snapshots:
        if not out.is_dir():
            print(f"No snapshots found in {out}")
            return 0
        snaps = sorted(
            out.glob("kirocrew-snapshot-*.tar.gz"), key=lambda x: x.stat().st_mtime, reverse=True
        )
        for s in snaps:
            print(s)
        if not snaps:
            print(f"No snapshots found in {out}")
        return 0

    mc = _mc_dir()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"kirocrew-snapshot-{ts}"

    # Pre-flight size estimate
    if mc.is_dir():
        total_bytes = sum(
            f.stat().st_size for f in mc.rglob("*") if f.is_file() and not f.is_symlink()
        )
        total_mb = total_bytes / (1024 * 1024)
        if total_mb > 500:
            print(f"⚠️  {mc} is {total_mb:.0f} MB — snapshot may be large and slow")

    # WAL checkpoint
    if (mc / "memory.db").is_file():
        try:
            from contextlib import closing

            with closing(sqlite3.connect(str(mc / "memory.db"))) as c:
                c.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception:
            print(
                "⚠️  WAL checkpoint failed (DB may be locked by gateway). "
                "The backup API still produces a consistent copy."
            )

    with tempfile.TemporaryDirectory() as work:
        stage = Path(work) / name
        for d in ("workspace", "skills", "plan_memory"):
            (stage / d).mkdir(parents=True, exist_ok=True)

        # Core files
        for files in CORE_FILES.values():
            for f in files:
                src = mc / f
                if src.is_file():
                    if os.path.islink(src):
                        print(f"⚠️  Skipping symlinked core file: {src}")
                        continue
                    if f.endswith(".db"):
                        from contextlib import closing

                        with (
                            closing(sqlite3.connect(str(src))) as src_conn,
                            closing(sqlite3.connect(str(stage / f))) as dst_conn,
                        ):
                            src_conn.backup(dst_conn)
                    else:
                        shutil.copy2(str(src), str(stage / f))

        # Workspace (exclude hygiene_data, insert_facts*.py)
        if (mc / "workspace").is_dir():
            _copytree_safe(
                mc / "workspace",
                stage / "workspace",
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("hygiene_data", "insert_facts*.py"),
            )

        # Plan memory
        if (mc / "plan_memory").is_dir():
            _copytree_safe(mc / "plan_memory", stage / "plan_memory", dirs_exist_ok=True)

        # Skills
        if (mc / "skills").is_dir():
            _copytree_safe(mc / "skills", stage / "skills", dirs_exist_ok=True)

        # Sessions (chat transcripts; incognito/temporary excluded)
        _stage_sessions(mc, stage)

        # Manifest
        ws_files = sum(1 for _ in (stage / "workspace").rglob("*") if _.is_file())
        pm_files = sum(1 for _ in (stage / "plan_memory").rglob("*") if _.is_file())
        sk_count = sum(1 for _ in (stage / "skills").iterdir() if _.is_dir())
        sess_stage = stage / "sessions"
        session_transcripts = (
            sum(1 for _ in sess_stage.glob(f"*{_TRANSCRIPT_SUFFIX}")) if sess_stage.is_dir() else 0
        )
        session_files = (
            sum(1 for _ in sess_stage.rglob("*") if _.is_file()) if sess_stage.is_dir() else 0
        )
        manifest = {
            "version": 2,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hostname": socket.gethostname(),
            "user": os.environ.get("USER", "unknown"),
            "kirocrew_dir": str(mc),
            "contents": {
                "memory_db": _fsize(stage / "memory.db"),
                "memory_index_db": _fsize(stage / "memory_index.db"),
                "crons_json": _fsize(stage / "crons.json"),
                "config_json": _fsize(stage / "config.json"),
                "notifications_jsonl": _fsize(stage / "notifications.jsonl"),
                "workspace_files": ws_files,
                "plan_memory_files": pm_files,
                "skill_count": sk_count,
                "session_transcripts": session_transcripts,
                "session_files": session_files,
            },
        }
        (stage / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))

        # Tarball — write to temp file and rename atomically to avoid corrupt partials
        out.mkdir(parents=True, exist_ok=True)
        outfile = out / f"{name}.tar.gz"
        tmp_tar = outfile.with_suffix(".tar.gz.tmp")
        try:
            with tarfile.open(str(tmp_tar), "w:gz") as tar:
                tar.add(str(stage), arcname=name, filter=_sessions_privacy_filter(mc, name))
            tmp_tar.rename(outfile)
        except BaseException:
            tmp_tar.unlink(missing_ok=True)
            raise

    sz = outfile.stat().st_size
    # restrict_to_owner (fail-loud), NOT chmod_safe: this tarball can contain
    # sel_hmac.key (see the warning below). chmod_safe swallows OSError and
    # would let the snapshot land group/world-readable while still printing
    # success. Fail loudly instead — better to abort than ship a
    # secret-bearing archive under-protected. POSIX applies chmod 0o600;
    # Windows applies an owner-only DACL via icacls.
    # Unlink+reraise on failure so the "abort" the comment promises actually
    # removes the exposed artifact — otherwise the tarball would sit on disk
    # with the destination's inherited DACL after a Python traceback.
    try:
        platform_compat.restrict_to_owner(str(outfile))
    except OSError:
        outfile.unlink(missing_ok=True)
        raise
    human = f"{sz // 1024}K" if sz < 1024 * 1024 else f"{sz / 1024 / 1024:.1f}M"
    print(f"✅ Snapshot created: {outfile} ({human})")

    _audit("snapshot_created", f"{outfile} ({human})")

    # Prune
    snaps = sorted(
        out.glob("kirocrew-snapshot-*.tar.gz"), key=lambda x: x.stat().st_mtime, reverse=True
    )
    for old in snaps[args.keep :]:
        old.unlink()
        print(f"🗑  Pruned: {old.name}")

    remaining = len(list(out.glob("kirocrew-snapshot-*.tar.gz")))
    print(f"📦 Snapshots in {out}: {remaining} (keep={args.keep})")
    return 0


# ── Restore ───────────────────────────────────────────────────────────────────


def _print_manifest(snap: Path) -> None:
    mf = snap / "MANIFEST.json"
    if not mf.is_file():
        return
    try:
        m = json.loads(mf.read_text())
        print("📋 Snapshot info:")
        print(f"  Created: {m.get('created_at', 'unknown')}")
        print(f"  From: {m.get('user', 'unknown')}@{m.get('hostname', 'unknown')}")
        c = m.get("contents", {})
        print(f"  Memory DB: {c.get('memory_db', 0) // 1024} KB")
        print(f"  Crons: {c.get('crons_json', 0) // 1024} KB")
        print(f"  Workspace files: {c.get('workspace_files', 0)}")
        print(f"  Skills: {c.get('skill_count', 0)}")
        print(f"  Notifications: {c.get('notifications_jsonl', 0) // 1024} KB")
        print(f"  Plan memory files: {c.get('plan_memory_files', 0)}")
        if "session_transcripts" in c:
            print(f"  Session transcripts: {c.get('session_transcripts', 0)}")
    except Exception as e:
        print(f"  (Could not read manifest: {e})")


_MERGE_ALLOWED_TABLES = frozenset(
    {
        "semantic_memory",
        "episodic_memories",
        "knowledge_facts",
        "knowledge_edges",
    }
)
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_identifier(name: str) -> str:
    """Validate a SQL identifier against allowlist pattern. Raises ValueError if invalid."""
    if not _SAFE_IDENTIFIER_RE.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


def _merge_memory(src_db: Path, dst_db: Path) -> None:
    # Integrity check on source DB before ATTACH
    try:
        with sqlite3.connect(str(src_db)) as check_conn:
            result = check_conn.execute("PRAGMA integrity_check;").fetchone()[0]
        if result != "ok":
            print(f"  ⚠️  Source DB integrity check failed: {result} — skipping merge")
            return
    except Exception as e:
        print(f"  ⚠️  Source DB unreadable: {e} — skipping merge")
        return

    conn = sqlite3.connect(str(dst_db))
    conn.execute("BEGIN")
    attached = False
    try:
        conn.execute("ATTACH DATABASE ? AS src", (str(src_db),))
        attached = True
        for table, cols, where in [
            (
                "semantic_memory",
                "key, value_json, confidence, source, created_at, updated_at, embedding",
                "WHERE is_deleted=0",
            ),
            (
                "episodic_memories",
                "id, conversation_id, text, embedding, tags, importance, created_at, last_accessed_at",
                "WHERE is_deleted=0",
            ),
            ("knowledge_facts", "subject, predicate, object, episode_id, created_at", ""),
            (
                "knowledge_edges",
                "source_key, target_key, relation, weight, metadata, created_at",
                "",
            ),
        ]:
            if table not in _MERGE_ALLOWED_TABLES:
                raise ValueError(f"Table {table!r} not in merge allowlist")
            for col in cols.split(", "):
                _validate_identifier(col.strip())
            try:
                before = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                conn.execute(
                    f"INSERT OR IGNORE INTO {table} ({cols}) "
                    f"SELECT {cols} FROM src.{table} {where}"
                )
                after = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                label = table.replace("_", " ").title()
                print(f"  {label} imported: {after - before}")
            except sqlite3.OperationalError as e:
                import logging

                logging.getLogger(__name__).warning("Skipping table %s: %s", table, e)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if attached:
            try:
                conn.execute("DETACH DATABASE src")
            except Exception:
                pass
        conn.close()


def _merge_crons(src_path: Path, dst_path: Path) -> None:
    src = json.loads(src_path.read_text())
    dst = json.loads(dst_path.read_text())
    existing = {j.get("name") for j in dst.get("jobs", [])}
    imported = 0
    for job in src.get("jobs", []):
        name = job.get("name")
        if not name or name in existing:
            continue
        job["id"] = hashlib.md5(f"{name}-imported".encode(), usedforsecurity=False).hexdigest()[:8]
        dst.setdefault("jobs", []).append(job)
        imported += 1
    dst_path.write_text(json.dumps(dst, indent=2))
    total = len(src.get("jobs", []))
    print(f"  Cron jobs imported: {imported} (skipped {total - imported} duplicates)")


def _merge_notifications(src_path: Path, dst_path: Path) -> None:
    existing: set[str] = set()
    with open(dst_path) as f:
        for line in f:
            try:
                existing.add(json.loads(line).get("ts") or line.strip())
            except (ValueError, TypeError):
                pass
    imported = 0
    with open(dst_path, "a") as out, open(src_path) as f:
        for line in f:
            try:
                key = json.loads(line).get("ts") or line.strip()
                if key not in existing:
                    out.write(line)
                    existing.add(key)
                    imported += 1
            except (ValueError, TypeError):
                pass
    print(f"  Notifications imported: {imported}")


def _backup_and_copy(mc: Path, backup: Path, snap: Path, component: str) -> None:
    for f in CORE_FILES.get(component, ()):
        if (mc / f).is_file():
            if os.path.islink(mc / f):
                print(f"⚠️  Skipping symlinked core file during backup: {mc / f}")
                continue
            shutil.move(str(mc / f), str(backup / f))
        if (snap / f).is_file():
            if os.path.islink(snap / f):
                print(f"⚠️  Skipping symlinked file from snapshot: {snap / f}")
                continue
            shutil.copy2(str(snap / f), str(mc / f))
            if component == "security":
                # restrict_to_owner (fail-loud), NOT chmod_safe (swallows OSError):
                # security files include sel_hmac.key. Mirrors the create path's
                # deliberate fail-loud lockdown — better to abort than silently
                # land a restored secret group/world-readable. POSIX applies
                # chmod 0o600; Windows applies an owner-only DACL via icacls.
                # Unlink the freshly
                # copied file on failure so the "abort" the comment promises
                # actually removes the exposed artifact — otherwise the
                # restored secret would sit under the destination-inherited
                # DACL after the OSError propagates out of _do_replace.
                try:
                    platform_compat.restrict_to_owner(str(mc / f))
                except OSError:
                    (mc / f).unlink(missing_ok=True)
                    raise


def _do_replace(snap: Path, mc: Path, components: list[str] | None) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = mc / f"pre-restore-{ts}"
    backup.mkdir(exist_ok=True)
    print("🔄 Replace mode — backing up current state...")

    for comp in ("memory", "crons", "config", "notifications", "security"):
        if _want(components, comp):
            _backup_and_copy(mc, backup, snap, comp)
            print(f"  ✅ {comp}")

    if _want(components, "workspace"):
        for dirname in ("workspace", "plan_memory"):
            d = mc / dirname
            if d.is_dir():
                _copytree_safe(d, backup / dirname, dirs_exist_ok=True)
            sd = snap / dirname
            if sd.is_dir():
                if d.is_dir():
                    shutil.rmtree(str(d))
                _copytree_safe(sd, d)
        print("  ✅ workspace")

    if _want(components, "skills"):
        sk = mc / "skills"
        if sk.is_dir():
            _copytree_safe(sk, backup / "skills", dirs_exist_ok=True)
        snap_sk = snap / "skills"
        if snap_sk.is_dir():
            if sk.is_dir():
                shutil.rmtree(str(sk))
            _copytree_safe(snap_sk, sk)
        print("  ✅ skills")

    try:
        backup.rmdir()
    except OSError:
        print(f"  Previous state saved to: {backup}/")
    print("✅ Replace complete.")


def _do_merge(snap: Path, mc: Path, components: list[str] | None) -> None:
    print("🔀 Merge mode — importing...")

    if _want(components, "memory") and (snap / "memory.db").is_file():
        if not (mc / "memory.db").is_file():
            shutil.copy2(str(snap / "memory.db"), str(mc / "memory.db"))
            if (snap / "memory_index.db").is_file():
                shutil.copy2(str(snap / "memory_index.db"), str(mc / "memory_index.db"))
            print("  Memory: copied (no existing memory.db)")
        else:
            _merge_memory(snap / "memory.db", mc / "memory.db")
        print("  ✅ memory")

    if _want(components, "crons"):
        sc, dc = snap / "crons.json", mc / "crons.json"
        if sc.is_file():
            if dc.is_file():
                _merge_crons(sc, dc)
            else:
                shutil.copy2(str(sc), str(dc))
                print("  Crons: copied (no existing crons)")
        print("  ✅ crons")

    if _want(components, "config"):
        for f in CORE_FILES["config"]:
            s, d = snap / f, mc / f
            if s.is_file() and not d.is_file():
                shutil.copy2(str(s), str(d))
                print(f"  {f}: restored (was missing)")
        print("  ✅ config")

    if _want(components, "notifications"):
        sn, dn = snap / "notifications.jsonl", mc / "notifications.jsonl"
        if sn.is_file():
            if dn.is_file():
                _merge_notifications(sn, dn)
            else:
                shutil.copy2(str(sn), str(dn))
                print("  Notifications: copied")
        print("  ✅ notifications")

    if _want(components, "security"):
        for f in CORE_FILES["security"]:
            s, d = snap / f, mc / f
            if s.is_file() and not d.is_file():
                shutil.copy2(str(s), str(d))
                # restrict_to_owner (fail-loud), NOT chmod_safe — security
                # files include sel_hmac.key; mirror the create path. Windows
                # applies an owner-only DACL via icacls. Unlink the freshly
                # copied file on
                # failure so an icacls error doesn't leave a restored secret
                # under the destination-inherited DACL.
                try:
                    platform_compat.restrict_to_owner(str(d))
                except OSError:
                    d.unlink(missing_ok=True)
                    raise
                print(f"  {f}: restored (was missing)")
        print("  ✅ security")

    if _want(components, "workspace"):
        for dirname in ("workspace", "plan_memory"):
            sd = snap / dirname
            if sd.is_dir():
                dd = mc / dirname
                dd.mkdir(parents=True, exist_ok=True)
                _copy_tree_no_overwrite(sd, dd)
        print("  ✅ workspace")

    if _want(components, "skills"):
        if (snap / "skills").is_dir():
            (mc / "skills").mkdir(parents=True, exist_ok=True)
            _copy_tree_no_overwrite(snap / "skills", mc / "skills")
        print("  ✅ skills")

    print("✅ Merge complete.")


def _is_gateway_running() -> bool:
    """Check if the KiroCrew gateway is listening on its dashboard port."""
    # Deterministic override (used by tests / scripted restores) — avoids a real
    # socket probe whose result is environment-dependent.
    override = os.environ.get("KIROCREW_ASSUME_GATEWAY_RUNNING")
    if override is not None:
        return override.strip().lower() not in ("", "0", "false", "no")
    port = _DASHBOARD_PORT
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def restore_main(argv: list[str] | None = None, *, parsed: argparse.Namespace | None = None) -> int:
    if parsed is None:
        p = argparse.ArgumentParser(
            prog="kirocrew-restore", description="Restore KiroCrew state from a snapshot."
        )
        p.add_argument("snapshot", nargs="?")
        p.add_argument("--mode", choices=("replace", "merge"))
        p.add_argument("--dry-run", action="store_true")
        p.add_argument(
            "--force", action="store_true", help="Allow restore even if gateway is running"
        )
        p.add_argument("--components")
        p.add_argument("--list-components", action="store_true")
        parsed = p.parse_args(argv)
    args = parsed

    if args.list_components:
        _list_components()
        return 0

    if not args.snapshot:
        print("❌ snapshot file is required (unless --list-components is given)")
        return 1

    force = getattr(args, "force", False)
    if not force and _is_gateway_running():
        _audit("state_restore_rejected", "reason=gateway_running")
        print("❌ Gateway is running. Stop it first (kirocrew stop) or use --force.")
        return 1

    snap_path = Path(args.snapshot)
    if not snap_path.is_file():
        print(f"❌ File not found: {snap_path}")
        return 1

    # Parse components
    components: list[str] | None = None
    if args.components:
        components = [c.strip() for c in args.components.split(",")]
        for c in components:
            if c not in VALID_COMPONENTS:
                print(f"❌ Unknown component: {c}\n")
                _list_components()
                return 1

    mc = _mc_dir()
    mode = args.mode or ("merge" if (mc / "memory.db").is_file() else "replace")

    with tempfile.TemporaryDirectory() as work_str:
        work = Path(work_str)

        # Security checks are enforced inside _data_filter (no TOCTOU gap)
        with tarfile.open(str(snap_path), "r:gz") as tar:
            try:
                tar.extractall(work, filter=_data_filter)
            except TypeError:
                # Python < 3.11.4: filter param not supported, apply manually
                members = [m for m in tar.getmembers() if _data_filter(m) is not None]
                tar.extractall(work, members=members)

        snap_dirs = [
            d for d in work.iterdir() if d.is_dir() and d.name.startswith("kirocrew-snapshot-")
        ]
        if not snap_dirs:
            print("❌ Invalid snapshot format")
            return 1
        snap = snap_dirs[0]

        _print_manifest(snap)
        if components:
            print(f"🔧 Components: {','.join(components)}")

        if args.dry_run:
            print(f"\n🔍 Dry run — would restore to {mc} in {mode} mode")
            print("Files in snapshot:")
            for f in sorted(snap.rglob("*")):
                if f.is_file():
                    print(f"  {f.relative_to(snap)}")
            return 0

        mc.mkdir(parents=True, exist_ok=True)
        # Whether session_map.json can arrive from the snapshot this run:
        # replace mode overwrites it; merge mode copies it only when missing.
        # Reconciliation additionally requires the snapshot to actually carry
        # a sessions tree — a pre-sessions snapshot ships a map but no
        # transcripts, and reconciling against that would delete every
        # restored mapping instead of leaving history intact.
        map_from_snapshot = _want(components, "config") and (
            mode == "replace" or not (mc / "session_map.json").is_file()
        )
        if mode == "replace":
            _do_replace(snap, mc, components)
        else:
            _do_merge(snap, mc, components)
        # Sessions restore is invoked ONLY from this CLI entrypoint, never
        # from the shared _do_replace/_do_merge helpers: portability's
        # dashboard ZIP import reuses those helpers, and its documented
        # exclusion of conversation data must hold even for a ZIP that
        # happens to contain a sessions/ directory.
        sessions_restored = False
        if _want(components, "sessions"):
            sessions_restored = _restore_sessions(snap, mc, components)
        # Reconciliation additionally requires sessions to have ACTUALLY been
        # restored this run: with --components config the transcripts
        # deliberately do not arrive, and a refused restore (e.g. linked local
        # sessions root) never copied them — reconciling against either gap
        # would permanently drop valid mappings.
        if map_from_snapshot and sessions_restored:
            _reconcile_session_map(mc)

    # Integrity check
    if _want(components, "memory") and (mc / "memory.db").is_file():
        try:
            with sqlite3.connect(str(mc / "memory.db")) as conn:
                result = conn.execute("PRAGMA integrity_check;").fetchone()[0]
        except Exception as e:
            result = str(e)
        if result == "ok":
            print("🔍 memory.db integrity: OK")
        else:
            print(f"⚠️  memory.db integrity check failed: {result}")
            _audit("state_restore_rejected", f"reason=integrity_check_failed from={snap_path.name}")
            return 1
        if not (mc / "memory_index.db").is_file():
            print(
                "⚠️  memory_index.db is missing — full-text search may not "
                "work until the FTS index is rebuilt."
            )

    comp_str = ",".join(components) if components else "all"
    _audit("state_restored", f"mode={mode} components={comp_str} from={snap_path.name}")

    print("\n⚠️  Restart kirocrew gateway to pick up changes: kirocrew restart")
    return 0

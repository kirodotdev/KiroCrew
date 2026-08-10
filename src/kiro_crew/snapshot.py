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
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from kiro_crew import platform_compat

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
    "artifacts",
    "uploads",
)

# Files that must always have 0o600 permissions in snapshots and on restore.
SECURITY_SENSITIVE_FILES: frozenset = frozenset({"sel_hmac.key", "telemetry_salt"})

# Files that must NEVER ride a snapshot: sel_hmac.key is regenerated on restore
# so audit-log HMACs stay bound to the host that wrote them.
#
# This set is matched by BASENAME inside `_data_filter`, which runs over the
# ENTIRE tar — including the staged workspace/, plan_memory/ and skills/ trees.
# So any name added here also silently drops a USER file that happens to share
# it. Keep the set minimal for that reason.
#
# The beacon's per-install identity (beacon_install_id / beacon_last_sent) is
# deliberately NOT here: snapshot staging copies an explicit per-component file
# list (CORE_FILES) plus those three directories, and no component lists a beacon
# file, so a root beacon file is never staged in the first place. The
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


def _refusal_reason(e: BaseException) -> str:
    """Audit tag for a refusal at the snapshot/restore CLI boundary:
    RuntimeError = a deliberate safety refusal (hardlinked user file);
    OSError = a filesystem failure (unreadable source, full destination)."""
    return "hardlink_refused" if isinstance(e, RuntimeError) else "io_error"


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
    "artifacts": "artifacts/ directory (versioned artifact library; additive-only on restore — existing slugs are never overwritten)",
    "uploads": "uploads/ directory (user-uploaded files; additive-only on restore — existing filenames are never overwritten)",
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


def _pinned_copy_file(s: str, d: str, *, on_hardlink: str = "abort") -> None:
    """Copy one file's bytes from a descriptor pinned to a validated inode.

    The staging walks copy user-writable trees, so the copy itself must not
    trust the path name: the listing-time link/hardlink screen checks an
    entry by name, and a ``shutil.copy2`` that then reopens the same NAME
    dereferences whatever the entry points at NOW — an agent that swaps a
    checked artifact or upload for a hardlink/symlink to a credential (e.g.
    ``~/.aws/credentials``) between the check and the copy gets the
    credential bytes staged as innocent-looking regular content that the tar
    pass's hardlink rejection (``_data_filter``) never sees. Open first, then
    validate the DESCRIPTOR: ``O_NOFOLLOW`` where the platform has it, then
    ``fstat`` the open fd — the inode that is validated is exactly the inode
    whose bytes are copied, so no check-to-use window remains. Mode and
    timestamps are applied from the same ``fstat`` result rather than a
    fresh by-name stat.

    This is the same invariant as ``_pinned_copy_file`` in the sessions
    snapshot path (PR: include agent session transcripts in snapshots) —
    keep the two implementations aligned so the PRs merge-compose cleanly.

    A symlink swapped in after the listing screen surfaces as ``ELOOP`` and
    is skipped with a warning (the screen would have skipped it the same
    way). A hardlinked or non-regular source follows ``on_hardlink``:
    ``"abort"`` raises the module's standard refusal (snapshot creation of
    user trees must not silently omit data), ``"skip"`` drops it with a
    warning. ``FileNotFoundError`` propagates for the callers' vanish
    tolerance; every other ``OSError`` propagates so real failures abort.
    """
    # O_NONBLOCK: an O_RDONLY open on a FIFO planted in the tree would block
    # until a writer appears; nonblocking open succeeds immediately and the
    # fstat below then refuses the non-regular file. Regular-file reads are
    # unaffected by the flag.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        fd = os.open(s, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            print(f"⚠️  Skipping symlink in source tree: {s}")
            return
        raise
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
            if on_hardlink == "abort":
                raise RuntimeError(
                    f"Refusing snapshot: {s} is hardlinked (link count > 1) "
                    "or not a regular file. Copying would either silently "
                    "omit it or bypass the tar hardlink rejection. Replace "
                    "the hardlink with a regular copy and retry."
                )
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


def _copytree_safe(src: Path, dst: Path, *, on_hardlink: str = "skip", **kwargs) -> None:
    """copytree that skips symlinks, junctions AND hardlinked files.

    Links are skipped to prevent sensitive-file leakage. Hardlinks need the
    same treatment at COPY time: ``shutil`` dereferences them into ordinary
    files, so the tar pass's hardlink rejection (``_data_filter``) never sees
    a link to reject — a hardlink to a credential would otherwise enter the
    snapshot as innocent-looking regular bytes.

    ``on_hardlink="abort"`` raises instead of skipping: snapshot CREATION of
    user trees must not report success while silently omitting a legitimate
    hardlinked file — the user discovers the loss only when a restore
    permanently lacks it. Skip remains the default for the paths where a
    hardlink is impossible (post-tar-filter snapshot content) or already
    pre-screened (``_refuse_hardlinked_files``).
    """
    outer_ignore = kwargs.pop("ignore", None)

    def _entry_unsafe(directory: str, name: str) -> bool:
        full = os.path.join(directory, name)
        if platform_compat.is_link_or_junction(full):
            return True
        try:
            st = os.lstat(full)
        except OSError:
            return True  # unstatable: cannot vouch for it, skip
        if _stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
            if on_hardlink == "abort":
                raise RuntimeError(
                    f"Refusing snapshot: {full} is hardlinked (link count > 1). "
                    "Copying would either silently omit it or bypass the tar "
                    "hardlink rejection. Replace the hardlink with a regular "
                    "copy and retry."
                )
            return True
        return False

    def _ignore_symlinks(directory, contents):
        skipped = {name for name in contents if _entry_unsafe(directory, name)}
        for name in skipped:
            print(f"⚠️  Skipping symlink in source tree: {os.path.join(directory, name)}")
        if outer_ignore:
            skipped |= set(outer_ignore(directory, contents))
        return skipped

    if on_hardlink == "abort":
        # Staging of user-writable trees: the ignore-callback screen above
        # checks entries by NAME, so it alone leaves a check-to-use window.
        # Copy every file through the descriptor-pinned gate so the inode
        # that was screened is the inode whose bytes ride the snapshot.

        def _pinned(s, d):
            _pinned_copy_file(s, d, on_hardlink=on_hardlink)

        shutil.copytree(
            str(src), str(dst), ignore=_ignore_symlinks, copy_function=_pinned, **kwargs
        )
        return

    shutil.copytree(str(src), str(dst), ignore=_ignore_symlinks, **kwargs)


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


_GUARDED_DIR_FD_OK = (
    os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
)


def _copy_tree_no_overwrite_guarded(src: Path, dst: Path) -> None:
    """Additive copy that additionally never writes through a linked target.

    ``_copy_tree_no_overwrite`` guards the SOURCE against symlinks but trusts
    the destination. Restoring user files cannot: a symlink or junction at
    any destination component would redirect restored files outside the data
    home. Validating the destination path by name and then re-traversing it
    by name for the write leaves a check-to-use window — an agent that swaps
    a destination ancestor (``uploads/`` itself, or any subdirectory) for a
    link between the check and the ``os.open`` gets the restored bytes
    written through the replaced component. So where the platform supports
    ``dir_fd``, every destination component is opened ``O_NOFOLLOW`` relative
    to its PARENT's descriptor and files are created exclusively via
    ``dir_fd``: the directory that was validated is exactly the directory
    written into, with no by-name re-traversal. Platforms without ``dir_fd``
    (Windows) fall back to the by-name link screen in
    ``_copy_tree_no_overwrite_guarded_by_name``.
    """
    if not _GUARDED_DIR_FD_OK:  # pragma: no cover - Windows fallback
        _copy_tree_no_overwrite_guarded_by_name(src, dst)
        return

    o_dir = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)

    def _open_dir_pinned(parent_fd: int, name: str, rel: Path) -> int | None:
        """Open (creating if absent) directory ``name`` relative to
        ``parent_fd``. ``None`` = skip the subtree: a link swapped in at this
        component (ELOOP) or a local FILE squatting on the name (ENOTDIR) —
        the local side owns the name, restore stays additive."""
        for _attempt in range(2):
            try:
                return os.open(name, o_dir, dir_fd=parent_fd)
            except FileNotFoundError:
                try:
                    # 0o700: the restored directory is the effective
                    # permission boundary (the tar data filter normalizes
                    # file modes), matching ``_make_restore_dir_owner_only``.
                    os.mkdir(name, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass  # raced with another writer: retry the open
                continue
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    print(f"  ⚠️  skipping {rel} (linked restore destination)")
                    return None
                if exc.errno == errno.ENOTDIR:
                    break
                raise
        print(f"  ⚠️  skipping {rel} (path type conflict)")
        return None

    try:
        root_fd = os.open(str(dst), o_dir)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT):
            print(f"  ⚠️  skipping restore into {dst} (linked restore destination)")
            return
        raise
    try:
        for item in sorted(src.rglob("*")):
            if item.is_symlink():
                continue
            rel = item.relative_to(src)
            parts = rel.parts
            fds: list[int] = []
            try:
                parent_fd = root_fd
                skip = False
                for depth, comp in enumerate(parts[:-1]):
                    fd = _open_dir_pinned(parent_fd, comp, Path(*parts[: depth + 1]))
                    if fd is None:
                        skip = True
                        break
                    fds.append(fd)
                    parent_fd = fd
                if skip:
                    continue
                if item.is_dir():
                    fd = _open_dir_pinned(parent_fd, parts[-1], rel)
                    if fd is not None:
                        os.close(fd)
                    continue
                if not item.is_file():
                    continue
                # Exclusive create closes the exists()-then-copy race: any
                # entry already at the name (a concurrently restored file, a
                # dangling symlink planted at the incoming filename) raises
                # FileExistsError — someone else owns it, restore is
                # additive. O_BINARY (Windows CRT) keeps the descriptor
                # byte-exact: without it a text-mode fd expands 0x0A to
                # 0x0D 0x0A, corrupting every restored binary upload
                # (mirrors ``_read_meta_bounded``).
                try:
                    fdesc = os.open(
                        parts[-1],
                        os.O_CREAT
                        | os.O_EXCL
                        | os.O_WRONLY
                        | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_BINARY", 0),
                        0o600,
                        dir_fd=parent_fd,
                    )
                except FileExistsError:
                    continue
                try:
                    # Copy and apply metadata THROUGH the exclusively created
                    # descriptor — a by-name copystat here would reopen the
                    # very window the pinned traversal just closed.
                    with open(item, "rb") as src_f, os.fdopen(fdesc, "wb") as dst_f:
                        shutil.copyfileobj(src_f, dst_f)
                        st = os.stat(item)
                        os.fchmod(dst_f.fileno(), _stat.S_IMODE(st.st_mode))
                        os.utime(dst_f.fileno(), ns=(st.st_atime_ns, st.st_mtime_ns))
                except BaseException:
                    try:
                        os.unlink(parts[-1], dir_fd=parent_fd)
                    except OSError:
                        pass
                    raise
            finally:
                for fd in fds:
                    os.close(fd)
    finally:
        os.close(root_fd)


def _copy_tree_no_overwrite_guarded_by_name(src: Path, dst: Path) -> None:
    """By-name fallback for platforms without ``dir_fd`` (Windows).

    Screens every destination component for links/junctions before writing
    and creates files exclusively. Unlike the pinned variant this leaves a
    check-to-use window between the screen and the write; junction detection
    lives in ``platform_compat``.
    """

    def _path_is_linked(p: Path) -> bool:
        while True:
            if platform_compat.is_link_or_junction(p):
                return True
            if p == dst:
                return False
            parent = p.parent
            if parent == p:  # filesystem root — out of scope, refuse
                return True
            p = parent

    for item in sorted(src.rglob("*")):
        if item.is_symlink():
            continue
        target = dst / item.relative_to(src)
        if _path_is_linked(target):
            print(f"  ⚠️  skipping {item.relative_to(src)} (linked restore destination)")
            continue
        if item.is_dir():
            try:
                target.mkdir(parents=True, exist_ok=True)
            except (FileExistsError, NotADirectoryError):
                # A FILE already occupies this directory path locally: the
                # local side owns the name — skip the subtree additively
                # instead of aborting a restore that already changed earlier
                # components.
                print(f"  ⚠️  skipping {item.relative_to(src)} (path type conflict)")
                continue
        elif item.is_file():
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except (FileExistsError, NotADirectoryError):
                print(f"  ⚠️  skipping {item.relative_to(src)} (path type conflict)")
                continue
            try:
                fdesc = os.open(
                    str(target),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
                )
            except FileExistsError:
                continue
            try:
                with open(item, "rb") as src_f, os.fdopen(fdesc, "wb") as dst_f:
                    shutil.copyfileobj(src_f, dst_f)
                shutil.copystat(str(item), str(target))
            except BaseException:
                target.unlink(missing_ok=True)
                raise


_META_MAX_BYTES = 1 << 20  # generous cap; a real meta.json is a few KB
_FOLDERS_MAX_BYTES = 1 << 20  # generous cap; a real artifact_folders.json is a few KB
_FOLDERS_MAX_RECORDS = 10_000  # far beyond any real folder tree


def _read_meta_bounded(slug_dir: Path) -> bytes | None:
    """Descriptor-pinned, bounded read of a slug's ``meta.json``.

    A bare ``read_bytes()`` follows a symlink planted at ``meta.json`` — a
    link to an endless source (a device node, a FIFO) turns the stability
    probe into an unbounded read that hangs or OOMs the snapshot. Open with
    ``O_NOFOLLOW`` (plus a link pre-check for platforms where the flag
    degrades to 0), verify ON THE OPENED DESCRIPTOR (``fstat``) that the
    target is a regular, non-hardlinked file within the size cap, and read
    through that same descriptor so the verified inode is the one whose
    bytes are returned. ``None`` for a link, non-regular, or oversized
    meta.json, and for one that DISAPPEARED (slug mid-create/mid-delete) —
    the slug's stability check then fails and the slug is skipped; the raw
    bytes are never read. Any other read failure (EACCES, EIO, ...)
    propagates so snapshot creation fails instead of silently omitting the
    slug.
    """
    meta = slug_dir / "meta.json"
    if platform_compat.is_link_or_junction(meta):
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(str(meta), flags)
    except OSError as exc:
        # Only DISAPPEARANCE (slug mid-create/mid-delete) and a link swapped
        # in after the screen keep the skip semantics. Anything else (EACCES,
        # EIO, ...) propagates so snapshot creation FAILS: treating an
        # unreadable meta.json as transient would silently omit the whole
        # slug from a backup that then reports success.
        if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
            return None
        raise
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode) or st.st_nlink > 1 or st.st_size > _META_MAX_BYTES:
            return None
        chunks: list[bytes] = []
        remaining = _META_MAX_BYTES + 1  # one past the cap: detects mid-read growth
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            remaining -= len(chunk)
        return None  # grew past the cap while being read
    finally:
        os.close(fd)


def _slug_state(slug_dir: Path) -> tuple | None:
    """Point-in-time fingerprint of a slug directory's FULL file state.

    ``meta.json`` bytes alone cannot prove a copy coherent: an updater that
    writes content/version files after (or without) touching ``meta.json``
    produces a torn copy whose meta-only probe still passes. Fingerprint
    every entry instead — (relpath, kind, size, mtime_ns) via ``lstat`` so
    links fingerprint as themselves — mirroring the (size, mtime_ns)
    stability check ``_stage_uploads_stable`` already applies per file.
    ``*.tmp`` atomic-write staging files are excluded: they never ride the
    copy, and a slow in-flight writer holding one open would otherwise defeat
    the bounded retries even when the real files are stable. ``None`` when
    the tree cannot be walked (slug vanishing mid-probe).
    """
    entries: list[tuple[str, str, int, int]] = []
    try:
        for dirpath, dirnames, filenames in os.walk(slug_dir):
            dirnames[:] = [
                d
                for d in dirnames
                if not platform_compat.is_link_or_junction(os.path.join(dirpath, d))
            ]
            rel_base = os.path.relpath(dirpath, str(slug_dir))
            for d in dirnames:
                entries.append((os.path.join(rel_base, d), "d", 0, 0))
            for name in filenames:
                if name.endswith(".tmp"):
                    continue
                try:
                    st = os.lstat(os.path.join(dirpath, name))
                except OSError:
                    return None
                entries.append(
                    (os.path.join(rel_base, name), "f", st.st_size, st.st_mtime_ns)
                )
    except OSError:
        return None
    # A same-size rewrite can land within one mtime_ns tick on coarse-clock
    # filesystems, making the stat fingerprint blind to it. meta.json is the
    # store's version record and small by contract — include its BYTES so a
    # version bump is always caught (this is exactly the sensitivity the
    # previous meta-only probe had).
    return (tuple(sorted(entries)), _read_meta_bounded(slug_dir))


def _stage_artifact_slugs(src: Path, dst: Path) -> None:
    """Stage the artifact library slug-by-slug with metadata stability checks.

    A whole-tree copy racing a live ``ArtifactStore.update`` can capture a
    slug whose ``meta.json`` and version files come from different writes. A
    slug is the store's unit of consistency, so each is copied individually:
    the slug's FULL file state (every entry's path, size, mtime) is
    fingerprinted before and after the copy, and the slug is re-copied
    (bounded retries) when they differ. If the state is still moving after the
    retries, the slug is OMITTED from this generation and the skip is reported
    to the operator: a retry-exhausted copy is by construction one whose
    consistency probe failed on every attempt, and silently shipping an
    unproven copy would be worse than deferring the slug to the next scheduled
    snapshot. ``*.tmp`` atomic-write staging files never ride.
    """
    dst.mkdir(parents=True, exist_ok=True)
    tmp_ignore = shutil.ignore_patterns("*.tmp")

    for entry in sorted(src.iterdir()):
        if platform_compat.is_link_or_junction(entry):
            continue
        target = dst / entry.name
        if entry.is_file():
            if not entry.name.endswith(".tmp"):
                # Descriptor-pinned copy in abort mode: refuses — not
                # silently thins out — a hardlinked user file, and closes the
                # window between the listing screen and the copy (mirrors the
                # workspace/skills components' contract).
                _pinned_copy_file(str(entry), str(target), on_hardlink="abort")
            continue
        if not entry.is_dir():
            continue
        staged_ok = False
        for _attempt in range(3):
            if _read_meta_bounded(entry) is None:
                # A slug with unreadable/absent metadata is either mid-create
                # or mid-delete — it cannot be verified consistent, so it
                # never rides this snapshot generation.
                break
            before = _slug_state(entry)
            if before is None:
                break
            if target.exists():
                shutil.rmtree(str(target))
            try:
                _copytree_safe(entry, target, on_hardlink="abort", ignore=tmp_ignore)
            except (FileNotFoundError, NotADirectoryError):
                # Source vanished mid-copy (live deletion): drop the partial
                # stage copy and move on rather than aborting the snapshot.
                # Only disappearance is tolerated — any other copy failure
                # (EACCES, ENOSPC, ...) re-raises below so the snapshot fails
                # loudly instead of reporting success minus an artifact.
                break
            if _slug_state(entry) == before:
                staged_ok = True
                break
        if not staged_ok:
            if target.exists():
                shutil.rmtree(str(target), ignore_errors=True)
            print(f"⚠️  artifact changing during snapshot; skipped this generation: {entry.name}")


def _stage_uploads_stable(src: Path, dst: Path) -> None:
    """Stage uploads/ with per-file stability checks.

    The upload handler streams multipart bodies directly to their final path
    (no atomic rename), so a snapshot racing an in-flight upload could capture
    a truncated file that then looks valid forever. Each file's (size,
    mtime_ns) is compared before and after its copy; on mismatch the copy is
    retried, and a file still moving after the retries is dropped from THIS
    snapshot generation with a warning — the next scheduled snapshot picks up
    the completed upload.
    """
    dst.mkdir(parents=True, exist_ok=True)
    # Top-down walk with explicit pruning: rglob would TRAVERSE a linked
    # directory before any per-item check could reject it, so a nested
    # junction's target files would already be in the iteration. os.walk with
    # in-place dirnames filtering never descends into pruned entries.
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [
            d for d in dirnames if not platform_compat.is_link_or_junction(Path(dirpath) / d)
        ]
        rel = Path(dirpath).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for fname in sorted(filenames):
            item = Path(dirpath) / fname
            if platform_compat.is_link_or_junction(item):
                continue
            target = dst / rel / fname
            stable = False
            for _attempt in range(3):
                try:
                    st1 = item.stat()
                    # Descriptor-pinned copy in abort mode: refuses — not
                    # silently thins out — a hardlinked user file (mirroring
                    # the workspace/skills components), and closes the window
                    # between the link screen above and the copy.
                    _pinned_copy_file(str(item), str(target), on_hardlink="abort")
                    st2 = item.stat()
                except (FileNotFoundError, NotADirectoryError):
                    # Vanished mid-copy (e.g. rejected upload cleanup). Only
                    # disappearance is tolerated — any other copy failure
                    # (EACCES, ENOSPC, ...) re-raises so the snapshot fails
                    # loudly instead of reporting success minus an upload.
                    break
                if (st1.st_size, st1.st_mtime_ns) == (st2.st_size, st2.st_mtime_ns):
                    stable = True
                    break
            if not stable:
                target.unlink(missing_ok=True)
                print(f"⚠️  upload still being written; skipped this snapshot: {fname}")


def _copy_artifacts_no_overwrite(src: Path, dst: Path) -> tuple[int, int]:
    """Copy artifact entries from ``src`` into ``dst``, whole slug at a time.

    A slug directory is the unit of consistency — its ``meta.json`` indexes the
    version files beside it — so a slug is copied in full when absent on the
    target and skipped entirely when it already exists. Mixing snapshot version
    files into a live slug could desync ``meta.json`` from the versions it
    describes, and an existing slug may hold newer work than the snapshot.
    Returns ``(imported, skipped)`` counts.
    """
    imported = skipped = 0
    for entry in sorted(src.iterdir()):
        if entry.is_symlink():
            continue
        target = dst / entry.name
        if target.exists():
            skipped += 1
            continue
        # Atomically reserve ownership of the destination before copying:
        # FileExistsError here means another writer created the entry after
        # the exists() probe (e.g. two --force restores racing), and that
        # entry is THEIRS — treat it as existing and never touch it. The
        # cleanup below may only ever delete a destination this loop created.
        if entry.is_dir():
            try:
                target.mkdir()
            except FileExistsError:
                skipped += 1
                continue
            try:
                _copytree_safe(entry, target, dirs_exist_ok=True)
            except BaseException:
                # A partial slug copy must not survive: a later retry would
                # see the target as existing, skip it, and strand the artifact
                # in a half-copied state forever. BaseException so Ctrl-C /
                # SystemExit mid-copy also cleans up. Owned by us — see above.
                shutil.rmtree(str(target), ignore_errors=True)
                raise
        elif entry.is_file():
            try:
                fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                skipped += 1
                continue
            os.close(fd)
            try:
                shutil.copy2(str(entry), str(target))
            except BaseException:
                # Same contract as the directory branch: an interrupt must not
                # strand a zero-byte/partial file that later restores skip.
                target.unlink(missing_ok=True)
                raise
        else:
            continue
        imported += 1
    return imported, skipped


def _merge_artifact_folders(src_path: Path, dst_path: Path) -> None:
    """Merge the artifact folder tree by folder id; target records win.

    Folder ids are opaque hex handles, so an id present on both sides is the
    same folder (shared lineage) and the target's copy is kept unchanged; ids
    only in the snapshot are appended. A ``parent_id`` whose folder was not
    imported is tolerated: ``ArtifactFolderStore`` treats a dangling parent as
    root, so a partial import degrades to flat folders rather than data loss.
    Written atomically (tmp + rename), mirroring the store's own persistence.
    """
    try:
        # Bounded like ``_read_meta_bounded``: a snapshot produced elsewhere
        # can carry a pathological artifact_folders.json, and an unbounded
        # read-plus-parse would balloon restore memory before any record
        # validation runs. Reject past the caps instead of parsing.
        if src_path.stat().st_size > _FOLDERS_MAX_BYTES:
            print("  ⚠️  Snapshot artifact_folders.json exceeds size cap; skipping folder merge")
            return
        snap_raw = json.loads(src_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        print("  ⚠️  Could not read snapshot artifact_folders.json; skipping folder merge")
        return
    if not isinstance(snap_raw, list):
        return
    if len(snap_raw) > _FOLDERS_MAX_RECORDS:
        print("  ⚠️  Snapshot artifact_folders.json exceeds record cap; skipping folder merge")
        return
    existing: list = []
    if dst_path.exists():
        try:
            raw = json.loads(dst_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            print("  ⚠️  Could not read target artifact_folders.json; keeping target as-is")
            return
        if not isinstance(raw, list):
            # Valid JSON but not the store's shape: treating it as empty would
            # atomically replace (and so discard) the target's contents below,
            # violating target-wins. Fail closed like the unreadable case.
            print("  ⚠️  Target artifact_folders.json is not a list; keeping target as-is")
            return
        existing = raw
    have = {
        f["id"]
        for f in existing
        if isinstance(f, dict) and isinstance(f.get("id"), str) and f["id"]
    }
    added = 0
    for rec in snap_raw:
        # Records are sanitized to the store's exact shape, never appended
        # verbatim: a snapshot produced elsewhere can carry malformed fields
        # (an unhashable id crashes the set lookup here; a non-numeric
        # ``order`` persists and then crashes the folder listing's int()
        # coercion server-side). Only validated ids merge; every other field
        # is coerced or defaulted to a value the store is known to handle.
        if not (isinstance(rec, dict) and isinstance(rec.get("id"), str) and rec["id"]):
            continue
        if rec["id"] in have:
            continue
        clean: dict = {
            "id": rec["id"],
            "name": str(rec.get("name") or "")[:100],
            "order": rec["order"] if isinstance(rec.get("order"), int) else len(existing),
            "parent_id": rec["parent_id"] if isinstance(rec.get("parent_id"), str) else "",
        }
        if isinstance(rec.get("icon"), str):
            clean["icon"] = rec["icon"]
        if isinstance(rec.get("color"), str):
            clean["color"] = rec["color"]
        existing.append(clean)
        have.add(clean["id"])
        added += 1
    if added:
        try:
            payload = json.dumps(existing, indent=2, sort_keys=True).encode()
        except (RecursionError, ValueError):
            # A snapshot record that survived id-validation can still be a
            # deeply nested structure json can't serialize without blowing
            # the stack — skip the merge rather than abort the restore.
            print("  ⚠️  Could not serialize merged artifact_folders.json; skipping folder merge")
            return
        fd, tmp = tempfile.mkstemp(dir=str(dst_path.parent), suffix=".tmp")
        try:
            try:
                # os.write may write fewer bytes than given (e.g. a nearly
                # full filesystem) — a single unchecked call could atomically
                # replace valid folder metadata with truncated JSON. Loop
                # until every byte lands; a zero-byte write is an error.
                view = memoryview(payload)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("short write while persisting artifact_folders.json")
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, str(dst_path))
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    print(f"  Artifact folders imported: {added}")


def _make_restore_dir_owner_only(dd: Path) -> bool:
    """Create *dd* owner-only for a restore; True when the lockdown held.

    ``platform_compat.make_owner_only_dir`` is deliberately best-effort (it
    warns and continues), which is the wrong contract here: restored artifacts
    and uploads are private user content, so if the directory cannot be locked
    down the restore of that component must not proceed under inherited
    readable permissions. The link check runs FIRST — the lockdown helper
    follows links, so calling it on a linked root would chmod/re-DACL an
    unrelated directory before the refusal. POSIX verifies the resulting
    mode; Windows re-runs the fail-loud DACL restriction directly.
    """
    if platform_compat.is_link_or_junction(dd):
        # A linked local destination root would both redirect restored files
        # outside the data home AND get its TARGET's permissions rewritten by
        # the lockdown below. Refuse before touching anything.
        return False
    try:
        # A REGULAR FILE squatting on the component name would make the
        # mkdir inside the lockdown helper raise and abort the whole restore
        # mid-way; treat it as a refusal for this component instead.
        platform_compat.make_owner_only_dir(dd)
        if not dd.is_dir():
            return False
        if platform_compat.IS_WINDOWS:
            platform_compat.restrict_to_owner(dd)
        elif dd.stat().st_mode & 0o077:
            return False
    except (OSError, FileExistsError):
        return False
    return True


def _acquire_gateway_lock(mc: Path) -> int | None:
    """Acquire and HOLD the exclusive ``gateway.lock`` flock; fd or ``None``.

    Port-probing ``_is_gateway_running`` misses a gateway serving on a
    non-default port (``dashboard.url``); the lock file is port-independent —
    the gateway holds an exclusive flock on it for its whole lifetime. A
    probe-then-release check is not enough: a gateway starting AFTER the
    probe loads the pre-merge folder list and its next mutation rewrites the
    file wholesale, silently erasing the merged records. So the lock is
    ACQUIRED here (non-blocking) and held by the caller across the merge —
    while held, no gateway can start. ``None`` means the lock is held
    elsewhere or unusable: positive evidence the merge must be skipped. The
    caller must ``os.close()`` the returned fd, which releases the lock.
    """
    lock_path = mc / "gateway.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(lock_path), flags, 0o600)
    except OSError:
        # Fail closed: a lock file that cannot be opened gives no evidence
        # the gateway is absent — merging anyway would let a live gateway's
        # next folder save erase the merged records.
        return None
    try:
        if platform_compat.try_acquire_lock(fd, exclusive=True):
            return fd
    except OSError:
        pass
    os.close(fd)
    return None


def _restore_artifacts(snap: Path, mc: Path) -> None:
    """Restore the artifact library; never overwrites an existing slug.

    Used by both replace and merge modes: the artifact library is versioned
    user work keyed by slug, so restore is additive-only in either mode — a
    slug already on the target wins over the snapshot copy unconditionally.
    The folder tree (``artifact_folders.json``) is merged by folder id so
    restored artifacts keep resolving their ``folder_id`` references.
    """
    sd = snap / "artifacts"
    if sd.is_dir():
        dd = mc / "artifacts"
        # Owner-only, fail closed: restored artifacts are user content and
        # must not be copied into a group/world-traversable directory.
        if not _make_restore_dir_owner_only(dd):
            print("  ⚠️  artifacts not restored (could not restrict artifacts/ to owner-only)")
            return
        imported, skipped = _copy_artifacts_no_overwrite(sd, dd)
        print(f"  Artifacts imported: {imported} (skipped {skipped} existing)")
    sf = snap / "artifact_folders.json"
    if sf.is_file():
        # The gateway's folder store keeps the WHOLE folder list in memory
        # and rewrites the file wholesale on its next mutation — unlike
        # per-slug artifact files, an on-disk merge under a live gateway is
        # guaranteed to be silently erased by that next save. This applies
        # even under --force. Ownership is decided by the port-independent,
        # data-home-scoped ``gateway.lock`` flock alone: a port probe would
        # conflate an UNRELATED listener on the default port (another data
        # home's gateway, any other process) with a gateway owning THIS home,
        # skipping the merge for no reason. The flock is acquired and HELD
        # across the merge, so a gateway starting mid-merge cannot slip past.
        lock_fd = _acquire_gateway_lock(mc)
        if lock_fd is None:
            print(
                "  ⚠️  artifact folder tree not merged (gateway running; "
                "rerun with the gateway stopped to import folders)"
            )
        else:
            try:
                _merge_artifact_folders(sf, mc / "artifact_folders.json")
            finally:
                os.close(lock_fd)
    print("  ✅ artifacts")


def _restore_uploads(snap: Path, mc: Path) -> None:
    """Restore user uploads; never overwrites an existing filename.

    Used by both replace and merge modes: uploads are referenced by transcripts
    on the target host, so restore is additive-only in either mode — an
    existing filename wins over the snapshot copy unconditionally.
    """
    sd = snap / "uploads"
    if sd.is_dir():
        dd = mc / "uploads"
        # Owner-only, fail closed: the tar data filter normalizes file modes
        # on extraction, so the directory is the effective permission boundary
        # for a restored private upload.
        if not _make_restore_dir_owner_only(dd):
            print("  ⚠️  uploads not restored (could not restrict uploads/ to owner-only)")
            return
        _copy_tree_no_overwrite_guarded(sd, dd)
    print("  ✅ uploads")


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
        for d in ("workspace", "skills", "plan_memory", "artifacts", "uploads"):
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

        # Workspace (exclude hygiene_data, insert_facts*.py), plan memory and
        # skills: abort — not silently thin out — the snapshot on a hardlinked
        # user file. TemporaryDirectory cleans up the partial stage.
        try:
            if (mc / "workspace").is_dir():
                _copytree_safe(
                    mc / "workspace",
                    stage / "workspace",
                    on_hardlink="abort",
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("hygiene_data", "insert_facts*.py"),
                )
            if (mc / "plan_memory").is_dir():
                _copytree_safe(
                    mc / "plan_memory",
                    stage / "plan_memory",
                    on_hardlink="abort",
                    dirs_exist_ok=True,
                )
            if (mc / "skills").is_dir():
                _copytree_safe(
                    mc / "skills", stage / "skills", on_hardlink="abort", dirs_exist_ok=True
                )
        except (RuntimeError, OSError) as e:
            # OSError: staging hit a filesystem failure (unreadable source,
            # full destination) — report it like any other refusal instead
            # of letting a traceback escape the CLI.
            print(f"❌ {e}")
            _audit("snapshot_rejected", f"reason={_refusal_reason(e)}")
            return 1

        # Artifacts (versioned artifact library, one directory per slug), plus
        # the folder-tree metadata that artifact ``folder_id`` fields point at —
        # without it a restored library loses its folder organization.
        # Linked component roots are refused outright (a symlinked/junctioned
        # artifacts/ or uploads/ would re-target the walk to an arbitrary
        # tree, e.g. a credentials directory). ``*.tmp`` is the store's
        # atomic-write staging suffix (tmp + rename in ``_write_text``):
        # excluding it means a snapshot taken mid-write can never capture a
        # torn temp file, only the previous complete rename.
        art_root = mc / "artifacts"
        up_root = mc / "uploads"
        try:
            if art_root.is_dir():
                if platform_compat.is_link_or_junction(art_root):
                    print("⚠️  artifacts not snapshotted (artifacts/ is a link or junction)")
                else:
                    _stage_artifact_slugs(art_root, stage / "artifacts")
            folders_src = mc / "artifact_folders.json"
            if folders_src.is_file() and not os.path.islink(folders_src):
                # Abort — never silently omit — on a hardlinked folder file:
                # a fresh restore would otherwise lose the library's folder
                # organization with no signal at snapshot time. The copy goes
                # through the descriptor-pinned gate so the inode that is
                # checked is the inode staged.
                _pinned_copy_file(
                    str(folders_src),
                    str(stage / "artifact_folders.json"),
                    on_hardlink="abort",
                )

            # Uploads (user files referenced by chat transcripts)
            if up_root.is_dir():
                if platform_compat.is_link_or_junction(up_root):
                    print("⚠️  uploads not snapshotted (uploads/ is a link or junction)")
                else:
                    _stage_uploads_stable(up_root, stage / "uploads")
        except (RuntimeError, OSError) as e:
            # OSError: staging hit a filesystem failure (unreadable source,
            # full destination) — report it like any other refusal instead
            # of letting a traceback escape the CLI.
            print(f"❌ {e}")
            _audit("snapshot_rejected", f"reason={_refusal_reason(e)}")
            return 1

        # Manifest
        ws_files = sum(1 for _ in (stage / "workspace").rglob("*") if _.is_file())
        pm_files = sum(1 for _ in (stage / "plan_memory").rglob("*") if _.is_file())
        sk_count = sum(1 for _ in (stage / "skills").iterdir() if _.is_dir())
        art_count = sum(1 for _ in (stage / "artifacts").iterdir() if _.is_dir())
        up_files = sum(1 for _ in (stage / "uploads").rglob("*") if _.is_file())
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
                "artifact_count": art_count,
                "upload_files": up_files,
            },
        }
        (stage / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))

        # Tarball — write to temp file and rename atomically to avoid corrupt partials
        out.mkdir(parents=True, exist_ok=True)
        outfile = out / f"{name}.tar.gz"
        tmp_tar = outfile.with_suffix(".tar.gz.tmp")
        try:
            with tarfile.open(str(tmp_tar), "w:gz") as tar:
                tar.add(str(stage), arcname=name, filter=_data_filter)
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
        # Only present in snapshots created after these components were added;
        # omit the lines (rather than printing 0) for older snapshots.
        if "artifact_count" in c:
            print(f"  Artifacts: {c['artifact_count']}")
        if "upload_files" in c:
            print(f"  Upload files: {c['upload_files']}")
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


def _refuse_hardlinked_files(mc: Path, components: list[str] | None) -> None:
    """Abort replace-mode restore if a tree slated for wholesale replacement
    contains a hardlinked regular file.

    ``_copytree_safe`` deliberately skips hardlinks (they must never enter a
    portable snapshot), but the pre-restore BACKUP reuses it: a hardlinked
    file would be silently absent from the backup and then deleted by the
    ``rmtree`` that follows — unrecoverable data loss. Refuse up front,
    before any mutation, so the user can materialize the file first.
    """
    dirnames: list[str] = []
    if _want(components, "workspace"):
        dirnames += ["workspace", "plan_memory"]
    if _want(components, "skills"):
        dirnames.append("skills")
    for dirname in dirnames:
        d = mc / dirname
        if not d.is_dir():
            continue
        for path in d.rglob("*"):
            try:
                st = path.lstat()
            except OSError:
                continue
            if _stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
                raise RuntimeError(
                    f"Refusing replace-mode restore: {path} is hardlinked "
                    "(link count > 1). The pre-restore backup skips hardlinks, "
                    "so replacing this tree would lose the file. Replace the "
                    "hardlink with a regular copy and retry."
                )


def _do_replace(snap: Path, mc: Path, components: list[str] | None) -> None:
    _refuse_hardlinked_files(mc, components)
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

    # Artifacts and uploads are deliberately additive-only even in replace
    # mode (no backup + wholesale-replace like workspace/skills): both hold
    # user data keyed by stable names (artifact slugs, upload filenames), so an
    # entry already on the target may be newer than the snapshot copy and is
    # never overwritten or deleted.
    if _want(components, "artifacts"):
        _restore_artifacts(snap, mc)

    if _want(components, "uploads"):
        _restore_uploads(snap, mc)

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

    if _want(components, "artifacts"):
        _restore_artifacts(snap, mc)

    if _want(components, "uploads"):
        _restore_uploads(snap, mc)

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
        try:
            if mode == "replace":
                _do_replace(snap, mc, components)
            else:
                _do_merge(snap, mc, components)
        except (RuntimeError, OSError) as e:
            # OSError: a filesystem failure mid-restore (unreadable snapshot
            # content, full destination) — report and return failure instead
            # of letting a traceback escape after a partial restoration.
            print(f"❌ {e}")
            _audit(
                "state_restore_rejected",
                f"reason={_refusal_reason(e)} from={snap_path.name}",
            )
            return 1

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

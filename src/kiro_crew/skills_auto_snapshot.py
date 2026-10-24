"""Immutable skill-tree generations: the bounded snapshot of a live or candidate tree read
through authenticated no-follow opens, its generation hash, the private materialization
a promotion publishes from, the publication scratch paths, and the staging of a
candidate under the pinned pending parent.

Each function here whose first parameter is ``self`` is a ``SkillsLoader`` method: the class
binds it by name.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
from pathlib import Path

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.atomic_write import atomic_write, fsync_dir
from kiro_crew.platform_compat import is_link_or_junction
from kiro_crew.skills_auto_model import (
    _SKILL_SNAPSHOT_MAX_DEPTH,
    _SKILL_SNAPSHOT_MAX_ENTRIES,
    _SKILL_SNAPSHOT_MAX_FILE_BYTES,
    _SKILL_SNAPSHOT_MAX_TOTAL_BYTES,
    AUTO_SKILL_NAMESPACE,
    AutoSkillProvenance,
    _build_auto_skill_content,
    _ClaimSnapshot,
    _emit_pending_staged,
    _PinnedSkillParent,
    _SkillTreeSnapshot,
    _TaggedFileIdentity,
    _ValidatedCandidateSnapshot,
)
from kiro_crew.skills_auto_pinning import (
    _opened_path_matches,
    _pinned_parent_matches,
    _stat_pinned_child,
    _tag_opened_identity,
)

#: The facade's logger, so a filter or a capture on ``kiro_crew.skills`` sees these records.
logger = logging.getLogger("kiro_crew.skills")


def _auto_apply_candidate_binding(
    skill_bytes: bytes,
    *,
    target: object,
    base_version: object,
    base_content_hash: object,
) -> str:
    """Bind unattended apply to exact staged bytes and live-base identity."""
    fields = json.dumps(
        [target, base_version, base_content_hash],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256()
    digest.update(len(skill_bytes).to_bytes(8, "big"))
    digest.update(skill_bytes)
    digest.update(fields)
    return digest.hexdigest()


def _stage_candidate_under_pending_parent(
    self,
    pending_parent: _PinnedSkillParent,
    slug: str,
    *,
    description: str,
    triggers: str,
    procedure_md: str,
    provenance: AutoSkillProvenance,
    scripts: list[dict] | None,
    source: str,
    kind: str,
    target: str | None,
    base_version: int | None,
    notify: bool,
    base_content_hash: str | None,
) -> tuple[str, bytes, list[str]]:
    """Write one complete candidate beneath the retained pending parent."""
    content = _build_auto_skill_content(
        slug=slug,
        description=description,
        triggers=triggers,
        procedure_md=procedure_md,
        provenance=provenance,
    )
    content_bytes = content.encode("utf-8")
    candidate_path = pending_parent.path / slug
    candidate_identity: _TaggedFileIdentity | None = None
    script_names: list[str] = []
    try:
        with self._create_pinned_child_exclusive(pending_parent, slug) as candidate:
            candidate_identity = candidate.native_identity
            self._write_pinned_new_file(candidate, "SKILL.md", content_bytes)
            clean_scripts = [entry for entry in (scripts or []) if isinstance(entry, dict)]
            if clean_scripts:
                with self._create_pinned_child_exclusive(candidate, "scripts") as script_parent:
                    for entry in clean_scripts:
                        filename = str(entry.get("filename", "")).strip()
                        if not filename or "/" in filename or "\\" in filename or ".." in filename:
                            continue
                        self._write_pinned_new_file(
                            script_parent,
                            filename,
                            str(entry.get("content", "")).encode("utf-8"),
                        )
                        script_names.append(filename)
                    self._sync_pinned_parent(script_parent)
            metadata: dict[str, object] = {
                "slug": slug,
                "name": f"{AUTO_SKILL_NAMESPACE}/{slug}",
                "source": source,
                "created_at": provenance.created_at or AutoSkillProvenance.now_iso(),
                "description": description,
                "triggers": triggers,
                "has_scripts": bool(script_names),
                "scripts": script_names,
                "kind": kind or "new",
                "notify_suppressed": not notify,
            }
            if target is not None:
                metadata["target"] = target
            if base_version is not None:
                metadata["base_version"] = base_version
            if base_content_hash is not None:
                metadata["base_content_hash"] = base_content_hash
            self._write_pinned_new_file(
                candidate,
                ".meta.json",
                json.dumps(metadata, indent=2).encode("utf-8"),
            )
            self._sync_pinned_parent(candidate)
            if not self._pinned_parent_matches(pending_parent):
                raise OSError("pending parent changed during candidate staging")
            self._sync_pinned_parent(pending_parent)
            if not self._pinned_parent_matches(candidate):
                raise OSError("staged candidate changed before commit")
    except BaseException:
        if candidate_identity is not None and not self._remove_private_tree(
            candidate_path,
            what="partial pending skill candidate",
            expected_identity=candidate_identity,
        ):
            logger.warning(
                "Could not remove refused staged candidate %s without touching a replacement",
                candidate_path,
            )
        raise
    return f"{AUTO_SKILL_NAMESPACE}/{slug}", content_bytes, script_names


def _capture_claim_snapshot(self, candidate_dir: Path) -> _ClaimSnapshot:
    """Capture immutable candidate authority without reopening its metadata."""
    tree = self._skill_tree_snapshot(candidate_dir)
    return _ClaimSnapshot(
        generation_hash=tree.generation_hash if tree is not None else None,
        metadata_bytes=(tree.files.get(Path(".meta.json")) if tree is not None else None),
        tree=tree,
    )


def _emit_pending_staged_metadata(slug: str, meta: dict) -> None:
    """Emit one staged event from metadata the caller already captured."""
    _emit_pending_staged(
        {
            "name": meta.get("name", f"{AUTO_SKILL_NAMESPACE}/{slug}"),
            "slug": slug,
            "kind": meta.get("kind", "new"),
            "target": meta.get("target"),
            "source": meta.get("source", ""),
            "has_scripts": meta.get("has_scripts") is True,
            "description": meta.get("description", ""),
            "triggers": meta.get("triggers", ""),
        }
    )


def emit_pending_staged(self, slug: str) -> None:
    """Emit a review notification from one captured pending generation."""
    if not self._is_pending_slug_safe(slug):
        return
    candidate = self._pending_root() / slug
    captured = self._capture_claim_snapshot(candidate)
    if captured.generation_hash is None:
        return
    meta = self._candidate_metadata_from_bytes(captured.metadata_bytes, redact=True)
    self._emit_pending_staged_metadata(slug, meta)


def _lone_regular_file_hash(path: Path) -> str | None:
    """Hash a lone regular file without following a link or junction."""
    if is_link_or_junction(path):
        return None
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _skill_tree_entries_hash(entries: list[tuple[str, str, int, bytes]]) -> str:
    """Hash one captured tree manifest with the publication digest format."""
    digest = hashlib.sha256()
    for kind, relative, mode, payload in sorted(entries):
        path_bytes = relative.encode("utf-8")
        digest.update(kind.encode("ascii"))
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(mode.to_bytes(4, "big"))
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _lone_regular_file_hash_at(
    self,
    parent: _PinnedSkillParent,
    name: str,
) -> str | None:
    """Hash one stable lone file relative to an authenticated parent."""
    fd: int | None = None
    try:
        before = self._stat_pinned_child(parent, name)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            return None
        if platform_compat.IS_WINDOWS:
            fd = platform_compat.open_file_no_reparse(parent.path / name)
        else:
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(name, flags, dir_fd=parent.fd)
        opened = os.fstat(fd)
        if (
            not self._opened_path_matches(fd, parent.path / name, before)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
        ):
            return None
        captured = self._stable_file_payload(
            fd,
            opened,
            max_bytes=_SKILL_SNAPSHOT_MAX_FILE_BYTES,
        )
        if captured is None or not self._pinned_parent_matches(parent):
            return None
        return hashlib.sha256(captured[0]).hexdigest()
    except OSError:
        return None
    finally:
        if fd is not None:
            os.close(fd)


def _stable_file_payload(
    fd: int,
    opened: os.stat_result,
    *,
    max_bytes: int,
) -> tuple[bytes, os.stat_result] | None:
    """Read one bounded regular opened inode and prove it stayed unchanged."""
    if opened.st_size < 0 or opened.st_size > max_bytes:
        return None
    payload = bytearray(opened.st_size)
    view = memoryview(payload)
    offset = 0
    while offset < opened.st_size:
        chunk = os.read(fd, min(opened.st_size - offset, 1024 * 1024))
        if not chunk:
            return None
        view[offset : offset + len(chunk)] = chunk
        offset += len(chunk)
    if os.read(fd, 1):
        return None
    after = os.fstat(fd)
    if (
        not stat.S_ISREG(after.st_mode)
        or after.st_nlink != 1
        or (
            not platform_compat.IS_WINDOWS
            and (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
        )
        or opened.st_size != after.st_size
        or opened.st_mtime_ns != after.st_mtime_ns
        or stat.S_IMODE(opened.st_mode) != stat.S_IMODE(after.st_mode)
    ):
        return None
    return bytes(payload), after


def _skill_tree_snapshot_pinned(
    root: Path,
    *,
    parent: _PinnedSkillParent | None = None,
    name: str | None = None,
) -> _SkillTreeSnapshot | None:
    """Capture a tree through one pinned root and descriptor-relative descendants."""
    root_fd: int | None = None
    cache: dict[tuple[str, ...], int] = {}
    try:
        if parent is None:
            root_fd = pinned_fs.open_dir_pinned(root, what="live skill tree", refusal=OSError)
        else:
            if name is None or not _pinned_parent_matches(parent):
                return None
            before = _stat_pinned_child(parent, name)
            if not stat.S_ISDIR(before.st_mode) or is_link_or_junction(root):
                return None
            root_fd = os.open(name, pinned_fs.dir_flags(), dir_fd=parent.fd)
            if not platform_compat.IS_WINDOWS and not os.path.samestat(before, os.fstat(root_fd)):
                return None
        if root_fd is None:  # defensive typing; open_dir_pinned returns int or raises
            return None
        root_info = os.fstat(root_fd)
        if not stat.S_ISDIR(root_info.st_mode):
            return None
        device = root_info.st_dev
        tree = pinned_fs.scan_tree_pinned(
            root_fd,
            device=device,
            max_entries=_SKILL_SNAPSHOT_MAX_ENTRIES,
            max_depth=_SKILL_SNAPSHOT_MAX_DEPTH,
        )
        if tree.links:
            return None

        files: dict[Path, bytes] = {}
        file_modes: dict[Path, int] = {}
        dir_modes: dict[Path, int] = {Path("."): stat.S_IMODE(root_info.st_mode)}
        entries: list[tuple[str, str, int, bytes]] = [
            ("d", ".", stat.S_IMODE(root_info.st_mode), b"")
        ]
        total_bytes = 0
        for parts in sorted(tree.dirs, key=lambda item: (len(item), item)):
            fd = pinned_fs.open_verified_chain(
                root_fd,
                parts,
                cache=cache,
                dirs=tree.dirs,
                device=device,
            )
            info = os.fstat(fd)
            relative = Path(*parts)
            mode = stat.S_IMODE(info.st_mode)
            dir_modes[relative] = mode
            entries.append(("d", relative.as_posix(), mode, b""))

        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        for parts, expected_inode in sorted(tree.files.items()):
            parent_fd = pinned_fs.open_verified_chain(
                root_fd,
                parts[:-1],
                cache=cache,
                dirs=tree.dirs,
                device=device,
            )
            file_before = pinned_fs.stat_at(parent_fd, parts[-1])
            remaining_budget = _SKILL_SNAPSHOT_MAX_TOTAL_BYTES - total_bytes
            read_limit = min(_SKILL_SNAPSHOT_MAX_FILE_BYTES, remaining_budget)
            if (
                file_before is None
                or not stat.S_ISREG(file_before.st_mode)
                or file_before.st_nlink != 1
                or (file_before.st_dev, file_before.st_ino) != (device, expected_inode)
                or file_before.st_size < 0
                or file_before.st_size > read_limit
            ):
                return None
            fd = os.open(parts[-1], flags, dir_fd=parent_fd)
            try:
                opened = os.fstat(fd)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino) != (device, expected_inode)
                    or opened.st_size < 0
                    or opened.st_size > read_limit
                ):
                    return None
                captured = _stable_file_payload(
                    fd,
                    opened,
                    max_bytes=read_limit,
                )
            finally:
                os.close(fd)
            if captured is None:
                return None
            payload, after = captured
            total_bytes += len(payload)
            relative = Path(*parts)
            mode = stat.S_IMODE(after.st_mode)
            files[relative] = payload
            file_modes[relative] = mode
            entries.append(("f", relative.as_posix(), mode, payload))
        root_identity = _tag_opened_identity(root_fd)
        if root_identity is None:
            return None
        snapshot = _SkillTreeSnapshot(
            files=files,
            file_modes=file_modes,
            dir_modes=dir_modes,
            generation_hash=_skill_tree_entries_hash(entries),
            root_identity=root_identity,
        )
        if parent is not None and not _pinned_parent_matches(parent):
            return None
        return snapshot
    except (OSError, ValueError):
        return None
    finally:
        pinned_fs.drain_verified_chain(cache)
        if root_fd is not None:
            os.close(root_fd)


def _snapshot_path_matches(fd: int, expected: str | Path) -> bool:
    """Authenticate an opened snapshot entry against its expected location.

    Windows compares native IDs from two no-reparse handles, so 8.3 and long
    path spellings of one object agree without reopening through
    ``os.path.samefile``. POSIX keeps the descriptor-derived path
    comparison: directory handles there do not prevent renames, so a
    by-name identity reopen would create a new race.
    """
    if platform_compat.IS_WINDOWS:
        return platform_compat.opened_path_identity_matches(fd, expected)
    opened = pinned_fs.fd_real_path(fd)
    if opened is None:
        return False
    return os.path.normcase(os.path.normpath(opened)) == os.path.normcase(
        os.path.normpath(expected)
    )


def _skill_tree_snapshot_by_name(root: Path) -> _SkillTreeSnapshot | None:
    """Fallback: hold directories and authenticate each opened file by location."""
    if is_link_or_junction(root):
        return None
    held_dirs: list[int] = []
    try:
        expected_root = os.path.realpath(root)
        root_fd = platform_compat.pin_directory(root)
        held_dirs.append(root_fd)
        root_info = os.fstat(root_fd)
        real_root = pinned_fs.fd_real_path(root_fd)
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or (not platform_compat.IS_WINDOWS and real_root is None)
            or not _snapshot_path_matches(root_fd, expected_root)
        ):
            return None
        identity_root = expected_root if platform_compat.IS_WINDOWS else real_root
        if identity_root is None:  # narrowed above for POSIX; defensive for typing
            return None

        files: dict[Path, bytes] = {}
        file_modes: dict[Path, int] = {}
        dir_modes: dict[Path, int] = {Path("."): stat.S_IMODE(root_info.st_mode)}
        entries: list[tuple[str, str, int, bytes]] = [
            ("d", ".", stat.S_IMODE(root_info.st_mode), b"")
        ]
        stack: list[tuple[Path, tuple[str, ...]]] = [(root, ())]
        entry_count = 0
        total_bytes = 0

        def opened_at(fd: int, parts: tuple[str, ...]) -> bool:
            expected = os.path.join(identity_root, *parts)
            return _snapshot_path_matches(fd, expected)

        while stack:
            current_path, parent_parts = stack.pop()
            with os.scandir(current_path) as listing:
                for entry in listing:
                    parts = parent_parts + (entry.name,)
                    entry_count += 1
                    if entry_count > _SKILL_SNAPSHOT_MAX_ENTRIES:
                        return None
                    if len(parts) > _SKILL_SNAPSHOT_MAX_DEPTH:
                        return None
                    entry_path = Path(entry.path)
                    if entry.is_symlink() or is_link_or_junction(entry_path):
                        return None
                    before = entry.stat(follow_symlinks=False)
                    relative = Path(*parts)
                    if stat.S_ISDIR(before.st_mode):
                        child_fd = platform_compat.pin_directory(entry_path)
                        held_dirs.append(child_fd)
                        opened = os.fstat(child_fd)
                        if (
                            not stat.S_ISDIR(opened.st_mode)
                            or not _opened_path_matches(
                                child_fd,
                                entry_path,
                                before,
                            )
                            or not opened_at(child_fd, parts)
                        ):
                            return None
                        mode = stat.S_IMODE(opened.st_mode)
                        dir_modes[relative] = mode
                        entries.append(("d", relative.as_posix(), mode, b""))
                        stack.append((entry_path, parts))
                        continue
                    # ``os.DirEntry.stat()`` sets st_ino, st_dev and
                    # st_nlink to zero on Windows (the directory-enumeration
                    # data carries no link count), so a pre-open
                    # ``before.st_nlink != 1`` would refuse EVERY regular
                    # file there and fail-close the whole fallback snapshot.
                    # The single-link requirement is still enforced below on
                    # the OPENED descriptor, whose ``os.fstat`` reads the
                    # real ``nNumberOfLinks`` on Windows -- that is the
                    # authoritative check the spec names ("the opened
                    # inode's ... checks still run before bytes are
                    # accepted"). POSIX keeps the pre-open gate as an early
                    # reject.
                    if not stat.S_ISREG(before.st_mode) or (
                        not platform_compat.IS_WINDOWS and before.st_nlink != 1
                    ):
                        return None
                    remaining_budget = _SKILL_SNAPSHOT_MAX_TOTAL_BYTES - total_bytes
                    read_limit = min(_SKILL_SNAPSHOT_MAX_FILE_BYTES, remaining_budget)
                    if before.st_size < 0 or before.st_size > read_limit:
                        return None
                    fd = platform_compat.open_file_no_reparse(
                        entry_path,
                        nonblocking=True,
                    )
                    try:
                        opened = os.fstat(fd)
                        if (
                            not stat.S_ISREG(opened.st_mode)
                            or opened.st_nlink != 1
                            or not _opened_path_matches(
                                fd,
                                entry_path,
                                before,
                            )
                            or not opened_at(fd, parts)
                            or opened.st_size < 0
                            or opened.st_size > read_limit
                        ):
                            return None
                        captured = _stable_file_payload(
                            fd,
                            opened,
                            max_bytes=read_limit,
                        )
                    finally:
                        os.close(fd)
                    if captured is None:
                        return None
                    payload, after = captured
                    total_bytes += len(payload)
                    mode = stat.S_IMODE(after.st_mode)
                    files[relative] = payload
                    file_modes[relative] = mode
                    entries.append(("f", relative.as_posix(), mode, payload))
        if not _snapshot_path_matches(root_fd, identity_root):
            return None
        root_identity = _tag_opened_identity(root_fd)
        if root_identity is None:
            return None
        return _SkillTreeSnapshot(
            files=files,
            file_modes=file_modes,
            dir_modes=dir_modes,
            generation_hash=_skill_tree_entries_hash(entries),
            root_identity=root_identity,
        )
    except (OSError, ValueError):
        return None
    finally:
        pinned_fs.close_all(held_dirs)


def _skill_tree_snapshot(root: Path) -> _SkillTreeSnapshot | None:
    if not platform_compat.IS_WINDOWS and pinned_fs.supports_pinned_tree_walk():
        return _skill_tree_snapshot_pinned(root)
    return _skill_tree_snapshot_by_name(root)


def _skill_tree_hash(root: Path) -> str | None:
    """Hash one exact generation without following a file or ancestor link."""
    snapshot = _skill_tree_snapshot(root)
    return snapshot.generation_hash if snapshot is not None else None


def _skill_tree_snapshot_child(
    self,
    parent: _PinnedSkillParent,
    name: str,
) -> _SkillTreeSnapshot | None:
    """Capture one authority child without abandoning its retained parent."""
    root = parent.path / name
    if platform_compat.IS_POSIX and not platform_compat.IS_WINDOWS:
        if not pinned_fs.supports_pinned_tree_walk():
            return None
        return self._skill_tree_snapshot_pinned(root, parent=parent, name=name)
    if not self._pinned_parent_matches(parent):
        return None
    snapshot = self._skill_tree_snapshot_by_name(root)
    if not self._pinned_parent_matches(parent):
        return None
    return snapshot


def _skill_tree_hash_child(
    self,
    parent: _PinnedSkillParent,
    name: str,
) -> str | None:
    snapshot = self._skill_tree_snapshot_child(parent, name)
    return snapshot.generation_hash if snapshot is not None else None


def _sync_skill_tree(root: Path) -> None:
    """Durably flush one authenticated staged generation before journaling.

    Windows ``FlushFileBuffers`` requires a writable handle, so its files
    are opened ``O_RDWR``.  Every platform still opens with ``O_NOFOLLOW``
    where available and compares the descriptor with the lstat identity;
    fixing Windows durability must not turn the flush into a link-following
    read of an attacker-swapped entry.
    """
    directories: list[Path] = []
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        if is_link_or_junction(current_path):
            raise OSError("linked directory in staged skill tree")
        directories.append(current_path)
        for name in files:
            entry = current_path / name
            if is_link_or_junction(entry):
                raise OSError("linked file in staged skill tree")
            before = os.lstat(entry)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise OSError("unsafe file in staged skill tree")
            access = os.O_RDWR if platform_compat.IS_WINDOWS else os.O_RDONLY
            flags = access | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(str(entry), flags)
            try:
                opened = os.fstat(fd)
                opened_identity = _tag_opened_identity(fd)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or opened_identity is None
                    or not _opened_path_matches(fd, entry, before)
                ):
                    raise OSError("staged skill file changed during durable open")
                os.fsync(fd)
                after = os.fstat(fd)
                after_identity = _tag_opened_identity(fd)
                if (
                    not stat.S_ISREG(after.st_mode)
                    or after.st_nlink != 1
                    or after_identity != opened_identity
                    or (
                        not platform_compat.IS_WINDOWS
                        and (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
                    )
                ):
                    raise OSError("staged skill file changed during flush")
            finally:
                os.close(fd)
        for name in dirs:
            entry = current_path / name
            if is_link_or_junction(entry):
                raise OSError("linked directory in staged skill tree")
    for directory in reversed(directories):
        fsync_dir(directory)


def _publication_paths(self, claim_name: str) -> tuple[Path, Path]:
    private = self._private_root()
    return (
        private / f".publish-{claim_name}",
        self._live_quarantine_root() / claim_name,
    )


def _claim_materialization_path(self, claim_name: str) -> Path:
    token = hashlib.sha256(claim_name.encode("utf-8")).hexdigest()
    return self._private_root() / f".claim-{token}"


def _restoration_path(self, claim_name: str) -> Path:
    """Return the private claims staging path for one immutable restoration."""
    token = hashlib.sha256(claim_name.encode("utf-8")).hexdigest()
    return self._claims_root() / f".restore-{token}"


def _cleanup_publication_artifacts(self, claim_name: str) -> bool:
    """Remove only fresh private stage, materialization, and restoration trees."""
    clean = True
    stage, _public_old_live = self._publication_paths(claim_name)
    paths = [
        stage,
        self._claim_materialization_path(claim_name),
        self._restoration_path(claim_name),
    ]
    for path in paths:
        if is_link_or_junction(path):
            clean = False
            continue
        if os.path.lexists(path) and not self._remove_private_tree(
            path,
            what="skill publication artifact",
        ):
            clean = False
    return clean


def _materialize_candidate_snapshot(
    snapshot: _ValidatedCandidateSnapshot, destination: Path
) -> None:
    """Write an immutable candidate snapshot into a fresh private directory."""
    destination.mkdir(parents=True, exist_ok=False)
    for relative, payload in sorted(snapshot.files.items(), key=lambda item: str(item[0])):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = snapshot.modes.get(relative, 0o600)
        if relative.parts and relative.parts[0] == "scripts":
            mode |= 0o111
        atomic_write(target, payload, mode=mode, fsync=True)
        platform_compat.chmod_safe(target, mode)


def _materialize_skill_tree_snapshot(snapshot: _SkillTreeSnapshot, destination: Path) -> None:
    """Materialize raw live bytes and modes without reopening the live tree."""
    root_mode = snapshot.dir_modes.get(Path("."), 0o700)
    destination.mkdir(mode=root_mode, parents=False, exist_ok=False)
    platform_compat.chmod_safe(destination, root_mode)
    for relative, mode in sorted(
        snapshot.dir_modes.items(), key=lambda item: (len(item[0].parts), str(item[0]))
    ):
        if relative == Path("."):
            continue
        target = destination / relative
        target.mkdir(mode=mode, parents=False, exist_ok=False)
        platform_compat.chmod_safe(target, mode)
    for relative, payload in sorted(snapshot.files.items(), key=lambda item: str(item[0])):
        target = destination / relative
        mode = snapshot.file_modes.get(relative, 0o600)
        atomic_write(target, payload, mode=mode, fsync=True)
        platform_compat.chmod_safe(target, mode)

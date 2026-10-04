"""Race-resistant filesystem writes shared by direct ACP adapters."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.atomic_write import (
    atomic_write,
    open_access_control_source,
    pinned_parent_replace_supported,
)
from kiro_crew.pinned_fs import (
    create_and_open_dir_pinned,
    lstat_by_name,
    open_dir_pinned,
    stat_at,
    supports_pinned_walk,
)
from kiro_crew.security import is_sensitive_path, is_sensitive_write_path


def resolve_tool_path(path: str, cwd: str | None = None) -> Path:
    """Resolve an agent tool path against its session cwd without following it."""
    try:
        target = Path(path).expanduser()
    except RuntimeError as exc:
        # ``Path.expanduser`` raises for an unknown ``~user``.  Tool paths are
        # model input, so turn that into an ordinary tool refusal instead of
        # letting it escape the adapter and terminate the ACP process.
        raise OSError(f"cannot expand ACP tool path: {path!r}") from exc
    if not target.is_absolute():
        try:
            base = Path(cwd or os.getcwd()).expanduser()
        except RuntimeError as exc:
            raise OSError(f"cannot expand ACP tool working directory: {cwd!r}") from exc
        if not base.is_absolute():
            base = Path.cwd() / base
        target = base / target
    return target


def normalize_tool_input_paths(
    name: str, raw_input: dict[str, object], *, cwd: str | None = None
) -> dict[str, object]:
    """Return permission/UI input with file paths based exactly like execution."""
    normalized = dict(raw_input)
    if name in {"read_file", "write_file"}:
        raw_path = normalized.get("path")
        if isinstance(raw_path, str) and raw_path:
            try:
                normalized["path"] = str(resolve_tool_path(raw_path, cwd))
            except OSError:
                # Permission presentation is best-effort. Keep invalid model
                # input intact so the execution path can return its ordinary
                # tool error instead of terminating the direct ACP adapter.
                pass
    return normalized


def _refuse_unpinned_parent(target: Path) -> None:
    """Refuse a Windows parent chain that cannot safely host a by-name write."""
    linked = platform_compat.first_linked_ancestor(target.parent)
    if linked is not None or platform_compat.is_link_or_junction(target.parent):
        raise OSError(f"refusing linked ACP tool destination: {target}")


def _create_parent_chain_pinned(path: Path) -> int:
    """Create missing parent components one at a time through pinned parents."""
    missing: list[Path] = []
    cursor = path
    while lstat_by_name(cursor) is None:
        parent = cursor.parent
        if parent == cursor:
            raise OSError(f"no existing ancestor for ACP tool destination: {path}")
        missing.append(cursor)
        cursor = parent

    existing = lstat_by_name(cursor)
    if existing is None or not stat.S_ISDIR(existing.st_mode):
        raise OSError(f"ACP tool destination ancestor is not a directory: {cursor}")

    for directory in reversed(missing):
        directory_fd = create_and_open_dir_pinned(
            directory, what="ACP tool destination", refusal=OSError
        )
        os.close(directory_fd)
    return create_and_open_dir_pinned(path, what="ACP tool destination", refusal=OSError)


def read_text_pinned(path: str, *, cwd: str | None = None, limit: int = 200_000) -> str:
    """Read one regular file through a pinned parent and no-follow leaf open."""
    if not path:
        raise OSError("empty path")
    target = resolve_tool_path(path, cwd)
    if is_sensitive_path(str(target)):
        raise PermissionError(f"blocked sensitive ACP tool source: {str(target)!r}")
    parent_fd = -1
    file_fd = -1
    try:
        if supports_pinned_walk():
            parent_fd = open_dir_pinned(target.parent, what="ACP tool source", refusal=OSError)
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            file_fd = os.open(target.name, flags, dir_fd=parent_fd)
        elif platform_compat.IS_WINDOWS:
            _refuse_unpinned_parent(target)
            parent_fd = platform_compat.pin_directory(target.parent)
            _refuse_unpinned_parent(target)
            file_fd = platform_compat.open_file_no_reparse(target, nonblocking=True)
        else:
            raise OSError("safe pinned ACP file reads are unavailable on this platform")

        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(f"refusing to read non-regular file: {target}")
        if opened.st_nlink > 1:
            raise OSError(f"refusing to read multiply-linked file: {target}")
        with os.fdopen(file_fd, "r", encoding="utf-8", errors="replace") as source:
            file_fd = -1
            return source.read(limit)
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def write_text_pinned(path: str, content: str, *, cwd: str | None = None) -> None:
    """Atomically replace a regular file without following planted links.

    Existing mode bits and access-control xattrs are copied from an open source
    descriptor. A symlink/non-regular leaf is refused instead of redirected.
    On platforms with descriptor-relative traversal, the parent chain remains
    pinned through publication; elsewhere the same no-follow leaf policy is the
    documented floor.
    """
    if not path:
        raise OSError("empty path")
    target = resolve_tool_path(path, cwd)
    if is_sensitive_write_path(str(target)):
        raise PermissionError(f"blocked sensitive ACP tool destination: {str(target)!r}")
    parent_fd = -1
    fallback_parent_fd = -1
    source_fd = -1
    try:
        pinned = supports_pinned_walk() and pinned_parent_replace_supported()
        if pinned:
            parent_fd = _create_parent_chain_pinned(target.parent)
            existing = stat_at(parent_fd, target.name)
        else:
            if not platform_compat.IS_WINDOWS:
                raise OSError("safe pinned ACP file writes are unavailable on this platform")
            # Refuse linked ancestors before even probing the parent: on
            # Windows, a path-following metadata call can otherwise contact a
            # remote UNC/SMB target and leak the caller's network credential.
            _refuse_unpinned_parent(target)
            if not target.parent.is_dir():
                raise OSError(f"ACP tool destination parent does not exist: {target.parent}")
            fallback_parent_fd = platform_compat.pin_directory(target.parent)
            # Check again after acquiring the non-delete-sharing Windows handle.
            # Once this succeeds, the parent and its ancestors cannot be renamed
            # until the write finishes.
            _refuse_unpinned_parent(target)
            existing = lstat_by_name(target)

        if existing is not None:
            if stat.S_ISLNK(existing.st_mode):
                raise OSError(f"refusing to write symbolic link: {target}")
            if not stat.S_ISREG(existing.st_mode):
                raise OSError(f"refusing to write non-regular file: {target}")
            if hasattr(os, "geteuid") and existing.st_uid != os.geteuid():
                raise OSError(f"refusing to replace file owned by another user: {target}")
            opened = open_access_control_source(
                target.name if pinned else target,
                dir_fd=parent_fd if pinned else None,
            )
            source_fd = -1 if opened is None else opened
            source_stat = os.fstat(source_fd) if source_fd >= 0 else existing
            if (source_stat.st_dev, source_stat.st_ino) != (existing.st_dev, existing.st_ino):
                raise OSError(f"refusing to replace a file changed during validation: {target}")
            mode = stat.S_IMODE(source_stat.st_mode)
        else:
            mode = None

        atomic_write(
            target.name if pinned else target,
            content,
            fsync=True,
            mode=mode,
            preserve_access_control_from=source_fd if source_fd >= 0 else None,
            parent_dir_fd=parent_fd if pinned else None,
        )
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if parent_fd >= 0:
            os.close(parent_fd)
        if fallback_parent_fd >= 0:
            os.close(fallback_parent_fd)

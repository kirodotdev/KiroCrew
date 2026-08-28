"""No-link reads and atomic writes for project-local adapter settings."""

from __future__ import annotations

from pathlib import Path

from kiro_crew.atomic_write import _refuse_linked_parent, atomic_write
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
from kiro_crew.platform_compat import is_link_or_junction


def read_text(path: Path) -> str:
    """Read a settings file only when its path contains no link or junction.

    These files are operator-owned input used to establish the permission route.
    Following a project-controlled link would let the project select an external
    file as both the source of that decision and the destination of a seed write.
    """
    _refuse_linked_parent(path)
    if is_link_or_junction(path):
        raise OSError(f"refusing to read linked adapter settings file {path}")
    if not path.exists():
        raise FileNotFoundError(path)
    try:
        raw = safe_read_file_bytes_nolink(str(path))
    except FileTooLargeError as exc:
        raise OSError(f"adapter settings file exceeds the safety cap: {path}") from exc
    if raw is None:
        raise OSError(f"refusing to read adapter settings file {path}")
    return raw.decode("utf-8")


def write_text(path: Path, content: str) -> None:
    """Publish adapter settings atomically without following linked parents."""
    if is_link_or_junction(path):
        raise OSError(f"refusing to replace linked adapter settings file {path}")
    # Owner-only mode also activates atomic_write's cross-platform parent-chain
    # symlink/junction refusal before mkdir and again before publication.
    atomic_write(path, content, restrict_to_owner=True)


__all__ = ["read_text", "write_text"]

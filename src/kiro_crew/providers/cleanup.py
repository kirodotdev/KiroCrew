"""Shared cleanup utilities for LLM provider session files.

Provides path safety validation used by providers before deleting session
files on disk.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _is_safe_path(target: Path, expected_root: Path) -> bool:
    """Validate target is strictly under expected_root (no traversal).

    Returns True only if the resolved target path is a proper child of
    the resolved expected_root (never equal to it).  Deleting the root
    directory itself is never correct during session cleanup.

    The check compares path components, not string prefixes, so a root
    that already ends in a separator (``/`` or a drive root such as
    ``R:\\``) still accepts its children, and a sibling that only shares a
    string prefix (``/data/ab`` against root ``/data/a``) is rejected.
    The components must match exactly. ``is_relative_to`` ignores case on
    Windows, and a case-sensitive directory there can hold ``cli`` and
    ``CLI`` side by side.

    Returns False on any resolution error (broken symlinks, permission
    issues, etc.).
    """
    try:
        resolved = target.resolve().parts
        root = expected_root.resolve().parts
        return len(resolved) > len(root) and resolved[: len(root)] == root
    except (OSError, ValueError):
        return False

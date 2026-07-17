"""Shared cleanup utilities for LLM provider session files.

Provides path safety validation used by providers before deleting session
files on disk.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _is_safe_path(target: Path, expected_root: Path) -> bool:
    """Validate target is strictly under expected_root (no traversal).

    Returns True only if the resolved target path is a proper child of
    the resolved expected_root (never equal to it).  Deleting the root
    directory itself is never correct during session cleanup.

    Returns False on any resolution error (broken symlinks, permission
    issues, etc.).
    """
    try:
        resolved = target.resolve()
        root = expected_root.resolve()
        return str(resolved).startswith(str(root) + os.sep)
    except (OSError, ValueError):
        return False

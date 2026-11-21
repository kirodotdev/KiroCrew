"""Whether ``dashboard.default_project`` is the folder a new dashboard chat starts in.

A chat whose session folder names no project starts here when this answers a
folder, else in ``default_project_dir()``'s. Slot creation applies that rule and
the setup wizard names its answer, so both read the configured half from here.
"""

from __future__ import annotations

import os

from kiro_crew.sandbox import voice_runtime_workspace_conflict
from kiro_crew.security import is_sensitive_canonical_path


def usable_default_project(configured: object) -> str:
    """``dashboard.default_project`` resolved, when a new chat would start there; else ``""``.

    Touches the disk, so an async caller runs it off the loop.
    """
    if not isinstance(configured, str) or not configured:
        return ""
    resolved = os.path.realpath(os.path.expanduser(configured))
    # *resolved* is canonical, so the canonical gate judges it: it answers
    # inline off the loop, where a resolver stall cannot read as a match.
    # A configured default that overlaps the data home would be refused
    # at spawn anyway, so it is skipped here like a sensitive path.
    if (
        os.path.isdir(resolved)
        and not is_sensitive_canonical_path(resolved)
        and voice_runtime_workspace_conflict(resolved) is None
    ):
        return resolved
    return ""

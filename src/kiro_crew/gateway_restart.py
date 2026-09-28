"""Gateway restart target selection through the composed lifecycle provider."""

from __future__ import annotations

import os
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.platform.context import current_context


def resolve_restart_launcher() -> str | None:
    """Validate the edition launcher before draining any live sessions.

    Imported by restart consumers before an update can retire their import tree.
    None alone opts into the core's existing Python/managed-venv resolver. A bad
    explicit target or a provider error refuses restart, never falls back to A.
    This is an availability check, not a new authorization boundary: the trusted
    composition root supplies the provider, not request/config/environment data.
    """
    launcher = current_context().gateway_lifecycle.restart_launcher()
    if launcher is None:
        return None
    if not isinstance(launcher, str) or not launcher or "\0" in launcher:
        raise ValueError("Cannot restart: invalid gateway launcher path")
    path = Path(launcher)
    if not path.is_absolute() or not path.is_file() or not os.access(launcher, os.X_OK):
        raise ValueError("Cannot restart: gateway launcher must be an absolute executable file")
    if platform_compat.IS_WINDOWS and path.suffix.lower() != ".exe":
        raise ValueError("Cannot restart: gateway launcher must be a native Windows executable")
    # Do not resolve symlinks: dispatchers can select the app from this basename.
    return launcher

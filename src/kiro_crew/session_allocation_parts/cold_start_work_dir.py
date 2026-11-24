"""The work directory a cold start on a session key would run in, read without starting it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kiro_crew.config.loader_locations import provider_work_dir


def recorded_resume_cwd(session_map: Any, key: str) -> str | None:
    """The cwd *key*'s saved session recorded, when that directory still exists.

    A cold start that resumes or forks the saved conversation, and names no cwd of
    its own, starts there.
    """
    stored = session_map.get_cwd(key)
    return stored if stored and Path(stored).is_dir() else None


def cold_start_work_dir(session_map: Any, key: str, cwd: str | None) -> Path:
    """Where a cold start of *key* asked for *cwd* would open its workspace ``cli.json``.

    The caller's own *cwd* wins; with none, a key that resumes or forks a saved
    conversation takes its recorded cwd; otherwise the provider factory's
    per-session directory. Nothing is created or started.

    shortcut: a recorded cwd inside an earlier process's hidden scratch tree, and a
    crew template's own project, are resolved later in ``get_or_create``; a cold
    start through either opens a different folder than this names. Upgrade when a
    caller needs those starts exact.
    """
    effective = cwd
    if not effective and (session_map.get(key) or session_map.get_native_fork(key)):
        effective = recorded_resume_cwd(session_map, key)
    return provider_work_dir(key, effective)

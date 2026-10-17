"""Find the Kiro Crew project root (the repo holding ``skills/`` and ``src/kiro_crew/``).

Stdlib-only so the slim ``mcp-*`` entry (:mod:`kiro_crew.mcp_entry`) can run the
same detection ``kirocrew`` does without importing :mod:`kiro_crew.cli`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

# Markers that uniquely identify the Kiro Crew repo root for project-dir
# auto-detection. ``skills/`` + ``src/kiro_crew/`` is the stable signature:
# ``skills/`` is editable-at-root and ``src/kiro_crew/`` pins this to the
# Kiro Crew package repo (not just any directory that happens to contain a
# ``skills/`` folder).
PROJECT_MARKERS = ("skills", "src/kiro_crew")


def detect_project_dir(saved_file: Callable[[], Path]) -> str | None:
    """Find the project root containing the project markers.

    Search order:
    1. Walk up from CWD
    2. Read the saved path from ``saved_file()`` (``config_dir()/project_dir``)

    *saved_file* is called only when the walk finds nothing: resolving it
    creates the data home, which a found checkout must not need.
    """
    cur = Path.cwd().resolve()
    for d in (cur, *cur.parents):
        if all((d / m).is_dir() for m in PROJECT_MARKERS):
            return str(d)
    pdf = saved_file()
    if pdf.is_file():
        saved = pdf.read_text(encoding="utf-8").strip()
        p = Path(saved)
        if p.is_dir() and all((p / m).is_dir() for m in PROJECT_MARKERS):
            return saved
    return None

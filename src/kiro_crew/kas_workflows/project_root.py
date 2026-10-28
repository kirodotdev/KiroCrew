"""Which project directories a Kiro Workflows request may name.

A KAS project directory arrives from a request (the Workflows tab, the Task
Runner) or from a chat's own project. Before any filesystem call it is confined
to an allowed root: the user's home directory, the gateway's own project
directory (``KIROCREW_PROJECT_DIR``) or a directory the user already picked
through the project picker (``recent_projects.json``). Every root is server-held;
nothing in the root list comes from the request.

The check runs in two passes. The first is pure string work on the normalized
path, so a path outside every root is refused without touching the filesystem.
The second resolves symlinks with ``realpath`` and checks again, so a link
inside a root cannot lead outside it. Only then is the directory itself checked.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Sequence
from pathlib import Path

#: The recent-projects file under the config directory (written by the project picker).
_RECENT_PROJECTS_FILE = "recent_projects.json"
#: Upper bound on the recent-projects entries read as roots.
_MAX_RECENT_ROOTS = 50


def _home() -> str:
    return str(Path.home())


def _recent_project_dirs() -> list[str]:
    from kiro_crew.config.paths import config_dir

    fp = config_dir() / _RECENT_PROJECTS_FILE
    try:
        data = json.loads(fp.read_text(encoding="utf-8")) if fp.is_file() else []
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data[:_MAX_RECENT_ROOTS] if isinstance(d, str) and d]


def allowed_project_roots(extra: Iterable[str] = ()) -> list[str]:
    """The server-held roots a KAS project directory must sit under.

    Each root is given in both its normalized and its resolved form, so a home
    directory reached through a symlink (``/home/x`` -> ``/local/home/x``)
    matches either spelling. *extra* adds server-held directories (a chat's own
    project); a caller must never pass request data here.
    """
    candidates: list[str] = [_home()]
    env_project = os.environ.get("KIROCREW_PROJECT_DIR", "")
    if env_project:
        candidates.append(env_project)
    candidates.extend(_recent_project_dirs())
    candidates.extend(extra)
    roots: list[str] = []
    for candidate in candidates:
        if not candidate or not os.path.isabs(candidate):
            continue
        for form in (os.path.normpath(candidate), os.path.realpath(candidate)):
            if form not in roots:
                roots.append(form)
    return roots


def _prefix(root: str) -> str:
    """*root* with exactly one trailing separator, so ``/a`` never admits ``/ab``."""
    return root if root.endswith(os.sep) else root + os.sep


def _existing_project_dir(resolved: str) -> str | None:
    from kiro_crew.security import is_sensitive_path

    if not os.path.isdir(resolved) or is_sensitive_path(resolved):
        return None
    return resolved


def confine_project_dir(raw: object, roots: Sequence[str]) -> str | None:
    """*raw* resolved and confined to *roots*, or None when it is not allowed.

    None when *raw* is not an absolute path string, lies outside every root
    (before or after resolving symlinks), is not an existing directory, or is a
    sensitive path. The returned value is the resolved path. The directory is
    only looked at inside the branch where the resolved path has been shown to
    start with a root.
    """
    if not isinstance(raw, str) or not raw or "\x00" in raw or not os.path.isabs(raw):
        return None
    normalized = os.path.normpath(raw)
    if not any(normalized == r or normalized.startswith(_prefix(r)) for r in roots):
        # Pure string refusal: nothing outside every root is resolved or stat'd.
        return None
    resolved = os.path.realpath(normalized)
    for root in roots:
        if resolved == root or resolved.startswith(_prefix(root)):
            return _existing_project_dir(resolved)
    return None

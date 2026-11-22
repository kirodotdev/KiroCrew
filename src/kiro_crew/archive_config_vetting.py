"""What an archive's ``config.json`` may carry before a restore or import installs it.

A restore or import installs the archive's config document whole. Three things in it are
later turned into paths: a roster key, a row's ``member_id`` and a workspace entry's
``dir``. The one check here is shared by both archive paths so they refuse the same
documents for the same reason.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from kiro_crew.members import is_single_path_component

#: Longest piece of an archive-supplied name echoed in a refusal.
_SHOWN_CHARS = 40


def _shown(value: str) -> str:
    """The name for a message: cut short, with control characters escaped by ``repr``."""
    cut = value[:_SHOWN_CHARS]
    return repr(cut + "..." if len(value) > _SHOWN_CHARS else cut)


def _workspace_roots() -> list[str]:
    """The folders a relative workspace folder that uses ``..`` may still lie under."""
    from kiro_crew.config.loader_locations import workspace_root
    from kiro_crew.config.paths import config_dir
    from kiro_crew.config.sections_agent_fields import DEFAULT_CWD_ALLOWED_ROOTS

    roots = [str(workspace_root(create=False)), str(config_dir())]
    roots += [os.path.expanduser(r) for r in DEFAULT_CWD_ALLOWED_ROOTS]
    return roots + [os.path.realpath(r) for r in roots]


def _dir_outside_roots(raw: str, roots: list[str]) -> bool:
    """Whether relative workspace folder *raw* climbs out of every allowed root with ``..``.

    An absolute folder is not judged here: it is a placement the product supports, and
    the sensitive-path check applies where it is used. A relative folder without ``..``
    anchors under the data home and is always inside.
    """
    from kiro_crew.sandbox_plan import path_within

    # A NUL cannot be a real path component; refuse it the same on every OS.
    if "\x00" in raw:
        return True

    try:
        path = os.path.expanduser(raw)
        try:
            Path(raw).expanduser()
        except RuntimeError:
            return True  # an unknown "~user": the runtime fails on it, even behind a "./"
        windows_path = PureWindowsPath(path)  # splits on / and \
        parts = windows_path.parts
        # "D:x" or "C:..\x" has a drive but no root: a Windows runtime anchors it to that
        # drive's current folder, outside the data home. A segment of only dots and spaces
        # ("... ", ".. ") is trimmed by Windows, so it may be a ".." in disguise.
        if (windows_path.drive and not windows_path.root) or any(
            p not in (".", "..") and not p.strip(" .") for p in parts
        ):
            return True
        if ".." not in parts and ".." not in PurePosixPath(path).parts:
            return False  # "\\/../.." is one UNC root to Windows but two climbs to POSIX
        # A backslash is a name character on POSIX and a separator on Windows, and an archive
        # can be restored on either, so each reading that is relative must stay inside a root.
        # A reading that is absolute is not judged: "/x" anywhere (even where the OS calls it
        # relative, Windows with Python 3.13), and on Windows also "\x", "C:\x" and UNC roots.
        native_absolute = (
            os.path.isabs(path)
            or path.startswith("/")
            or (os.name == "nt" and bool(windows_path.root))
        )
        candidates = []
        if not native_absolute:
            candidates.append(os.path.join(roots[1], path))
        if not windows_path.root:
            candidates.append(os.path.join(roots[1], path.replace("\\", "/")))
        for candidate in candidates:
            normal = os.path.normcase(os.path.realpath(candidate))
            if not any(
                path_within(normal, os.path.normcase(os.path.realpath(root))) for root in roots
            ):
                return True
        return False
    except (ValueError, OSError):
        # An unresolvable path cannot be shown to be inside, so refuse it.
        return True


def _is_data_home(raw: str) -> bool:
    """Whether workspace folder *raw* is an absolute path that is exactly the data home.

    A relative folder is kept, as ``"workspace/.."`` is: it anchors under the data home, and
    the runtime refuses the data home itself when a run or a channel turn would use it.
    """
    from kiro_crew.config.paths import config_dir

    home = os.path.realpath(config_dir())
    try:
        expanded = os.path.expanduser(raw)
        if not os.path.isabs(expanded):
            return False
        folder = os.path.realpath(expanded)
    except ValueError:  # an embedded NUL: not resolvable, so not the data home
        return False
    return os.path.normcase(folder) == os.path.normcase(home)


def config_document_refusal(data: Any) -> str | None:
    """Why the parsed archive ``config.json`` *data* must not be installed, or ``None``.

    The message names the offending key or workspace name and nothing else of the
    document, so it can be shown without echoing archive text.
    """
    if not isinstance(data, dict):
        return None
    agents = data.get("agents")
    if isinstance(agents, dict):
        for key, row in agents.items():
            if not is_single_path_component(key):
                return f"the agent name {_shown(str(key))} is not a single file name"
            member_id = row.get("member_id") if isinstance(row, dict) else None
            if isinstance(member_id, str) and member_id and not is_single_path_component(member_id):
                return f"the member_id of agent {_shown(key)} is not a single file name"
    workspaces = data.get("workspaces")
    if isinstance(workspaces, dict):
        roots = _workspace_roots()
        for name, entry in workspaces.items():
            raw = entry.get("dir") if isinstance(entry, dict) else entry
            if isinstance(raw, str) and raw and _dir_outside_roots(raw, roots):
                return f"workspace {_shown(str(name))} places its folder outside the workspace root"
            if isinstance(raw, str) and raw and _is_data_home(raw):
                return f"workspace {_shown(str(name))} places its folder on the data home itself"
    return None

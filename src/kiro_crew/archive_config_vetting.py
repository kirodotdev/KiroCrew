"""What an archive's ``config.json`` may carry before a restore or import installs it.

A restore or import installs the archive's config document whole. Three things in it are
later turned into paths: a roster key, a row's ``member_id`` and a workspace entry's
``dir``. The one check here is shared by both archive paths so they refuse the same
documents for the same reason.
"""

from __future__ import annotations

import os
from pathlib import PureWindowsPath
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

    path = os.path.expanduser(raw)
    windows_path = PureWindowsPath(path)  # splits on / and \
    # A path with a root but no drive ("/x", "\x") is absolute here, whatever this OS says:
    # joined under the data home it would still discard the data home.
    if os.path.isabs(path) or windows_path.root or ".." not in windows_path.parts:
        return False
    # A backslash is a separator in an archive written on Windows, so a ``..\\x`` that
    # would climb there must also climb here, where normpath keeps it as a name.
    normal = os.path.normcase(os.path.normpath(os.path.join(roots[1], path).replace("\\", "/")))
    return not any(path_within(normal, os.path.normcase(root)) for root in roots)


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
    return None

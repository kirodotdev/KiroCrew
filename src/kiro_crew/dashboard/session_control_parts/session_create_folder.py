"""The project directory a session ``session_create`` opens takes from its folder.

A folder the caller NAMED supplies its nearest declared project directory, the way
dashboard-native creation does. A folder the session INHERITED from its caller (the
MCP resolver files a create that named no folder beside its caller) only files it:
the child keeps the caller's project, so a conductor that switched to a worktree
still sends its workers there.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from kiro_crew.dashboard.chat_folders import _folder_declared_project, _resolve_folder_project_dir
from kiro_crew.dashboard.session_control_parts.caller_access import SessionControlError
from kiro_crew.dashboard.state import _safe_folder_tree

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState


def inherited_project_folder(inherited_folder: str, folder_id: str, project_folder_id: str) -> str:
    """The folder the child takes its project directory from, after any re-filing.

    *inherited_folder* is the folder the create inherited (``""`` for a named or
    absent one), and *folder_id* / *project_folder_id* are the filing and project
    folders once a Bot conductor's home filing has run. An inherited folder kept as
    the filing folder gives no project. One the Bot rule replaced with its home
    gives the home's, which is what the same create with no folder got before
    folders were inherited.
    """
    if not inherited_folder:
        return project_folder_id
    return "" if folder_id == inherited_folder else folder_id


async def folder_project_for_create(
    state: "DashboardState", folder_id: str, project_folder_id: str
) -> tuple[str | None, str]:
    """``(declared_raw, project_dir)`` for a create filed in *folder_id*.

    Raises ``folder_not_found`` when either folder is missing (an empty
    *project_folder_id* names no folder), and ``folder_project_invalid`` when the
    project folder's nearest declared project is not a usable directory.
    ``declared_raw`` is the unvalidated declaration the create's late re-check
    compares against, ``None`` when no folder in the chain declares one.
    """
    folder_snapshot = await state.read_folders(
        lambda folders: [dict(folder) for folder in _safe_folder_tree(folders)]
    )
    known = {str(f.get("id") or "") for f in folder_snapshot}
    if not {folder_id, project_folder_id} - {""} <= known:
        raise SessionControlError("folder not found", code="folder_not_found")
    raw, error = _folder_declared_project(folder_snapshot, project_folder_id)
    if error:
        raise SessionControlError(f"invalid folder project: {error}", code="folder_project_invalid")
    project, error = await asyncio.to_thread(
        _resolve_folder_project_dir, folder_snapshot, project_folder_id
    )
    if error:
        raise SessionControlError(f"invalid folder project: {error}", code="folder_project_invalid")
    return raw, project

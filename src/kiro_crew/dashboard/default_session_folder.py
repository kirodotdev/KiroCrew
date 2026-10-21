"""The sidebar folder new dashboard chats are filed into (``dashboard.default_session_folder``).

The setting stores a folder ID, so renaming the folder or moving it under
another parent keeps the choice. It is a fallback only: the create route applies
it when the dashboard asks for it (``default_folder: true``) and the request
names no folder of its own, so every explicit placement still wins.

A default that does not resolve to a folder the person owns is ignored at
create time and the chat lands unfiled. That covers a deleted folder, a
hand-edited config value and a folder an app or crew member owns; none of them
can block a new chat.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.folder_ownership import _folder_owner_app

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState


def _person_folder(folders: list[dict[str, Any]], folder_id: str) -> dict[str, Any] | None:
    """The person-owned folder called *folder_id*, or ``None``.

    An app's or member's folder is never a default: the person's own New chat
    would otherwise file their conversation into a row another principal owns.
    """
    if not folder_id:
        return None
    for folder in folders:
        if str(folder.get("id") or "") == folder_id:
            return folder if not _folder_owner_app(folder) else None
    return None


def nearest_default_agent(folders: list[dict[str, Any]], folder_id: str) -> str:
    """The ``default_agent`` of *folder_id* or its closest ancestor that sets one.

    Mirrors the client's ``resolveFolderAgent`` walk, so a chat that lands in the
    default folder starts with the agent New chat in that folder would pick. An
    empty value on a subfolder means "inherit". Cycle-guarded, because
    ``parent_id`` is user-editable.
    """
    by_id = {str(f.get("id") or ""): f for f in folders}
    seen: set[str] = set()
    current = by_id.get(folder_id)
    while current is not None:
        fid = str(current.get("id") or "")
        if fid in seen:
            break
        seen.add(fid)
        agent = current.get("default_agent")
        if isinstance(agent, str) and agent.strip():
            return agent.strip()
        current = by_id.get(str(current.get("parent_id") or ""))
    return ""


def resolve_default_session_folder(
    folders: list[dict[str, Any]], configured: str
) -> tuple[str, str]:
    """``(folder_id, default_agent)`` for a new chat, or ``("", "")`` for unfiled.

    *configured* is ``dashboard.default_session_folder``. A value that names no
    person-owned folder answers unfiled.
    """
    folder = _person_folder(folders, configured)
    if folder is None:
        return "", ""
    return configured, nearest_default_agent(folders, configured)


async def default_session_folder_choosable(state: "DashboardState", folder_id: str) -> bool:
    """Whether *folder_id* may be saved as the default: an existing person-owned folder."""
    return await state.read_folders(lambda folders: _person_folder(folders, folder_id) is not None)

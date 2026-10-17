"""Folder-ownership rules on plain folder dicts.

Pure functions, kept out of :mod:`kiro_crew.dashboard.chat_folders` so the
``mcp-dashboard`` stdio server can apply them without importing the HTTP
handler tree (aiohttp, the STT stack, boto3, slack_sdk) that module carries.
``chat_folders`` re-exports all three names, so its callers are unchanged.
"""

from __future__ import annotations

from typing import Any


def _folder_owner_app(folder: dict[str, Any]) -> str:
    """The principal that owns *folder*, or ``""`` when the person owns it.

    The single place the storage rule is expressed. A folder created by a
    non-person principal carries that principal in ``owner_app``:

    * an APP -> its bare app name (unchanged; every folder written before crew
      members reached this surface keeps its meaning, so this stayed a field
      addition rather than a migration); and
    * an admitted crew MEMBER -> ``"member:<store>"`` (see
      ``token_auth.folder_principal``). App names are validated identifiers that
      never begin ``member:``, so the two principal spaces cannot collide.

    An absent or empty key reads as the person's, exactly as before.

    Ownership decides only the tree-shaping verbs (create-into, rename,
    reparent, delete). Reads stay whole for apps (an app sees the person's
    folders); a member's reads are scoped to its own folders and sessions (see
    ``api_chat_folders``).
    """
    return str(folder.get("owner_app") or "")


def _subtree_holds_foreign_folder(
    folders: list[dict[str, Any]], *, root_id: str, request_app: str
) -> bool:
    """True if anything under *root_id* belongs to someone other than *request_app*.

    Reparenting a folder relocates everything beneath it -- that is what "the
    folder moves with everything in it" means -- so the blast radius of a move is
    the whole SUBTREE, not the row being written. An app moving a folder it owns
    would otherwise relocate a folder the person nested inside it, which is the
    same violation as editing that folder directly, reached one level down.

    Used by the reparent path only. Delete asks a stricter question instead --
    whether the folder is EMPTY -- because a delete has more kinds of content to
    account for (sessions, and archived sessions a live scan cannot see), and
    emptiness answers all of them without an ownership test per content type.

    Scoped to the descendants and not the root: the caller's authority over the
    root itself is a separate question, answered separately.

    Uses the cycle-guarded walk so a pre-existing corrupt parent chain in
    folders.json cannot hang the request.
    """
    for f in folders:
        fid = str(f.get("id") or "")
        if fid == root_id:
            continue
        if _folder_owner_app(f) == request_app:
            continue
        if _is_descendant(folders, ancestor_id=root_id, folder_id=fid):
            return True
    return False


def _is_descendant(folders: list[dict], *, ancestor_id: str, folder_id: str) -> bool:
    """True if `folder_id` is `ancestor_id` or lies anywhere under it.

    Walks parent_id links upward from `folder_id` with a visited-set guard
    so pre-existing corrupt cycles in folders.json can't hang the request.
    """
    by_id = {f["id"]: f for f in folders}
    seen: set[str] = set()
    cur: str | None = folder_id
    while cur and cur not in seen:
        if cur == ancestor_id:
            return True
        seen.add(cur)
        node = by_id.get(cur)
        cur = str(node.get("parent_id") or "") if node else None
    return False

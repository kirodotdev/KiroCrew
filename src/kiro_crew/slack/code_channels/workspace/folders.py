"""The channel's own sidebar folder, and the sessions working in it.

Every code channel with a worktree gets a folder of its own, named for the
channel and nested under the folder its session was filed in (the Code channels
default). The folder's ``project_dir`` IS the channel's worktree, so a session
filed anywhere under it — the conductor's workers, which the conductor files
under ``<its folder>/<agent>`` — starts in that one tree; the folder project is
inherited from the nearest ancestor that declares one.

The folder is created through the dashboard's single create path
(:func:`kiro_crew.dashboard.chat_folders.create_folder_record`): the same path
validation, data-home overlap refusal and locked store write the folder API and
project scaffolding use. Nothing here writes a folder field any other way, and
every create is SEL-recorded.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable

from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: ``(state, parent_id, name, project_dir) -> folder id``; injectable for tests.
FolderCreator = Callable[[Any, str, str, str], Awaitable[str]]

_MAX_NAME = 100


def _audit(outcome: str, resources: str, error: str = "") -> None:
    try:
        sel().log_api_access(
            caller="slack",
            operation="code_channel_folder",
            outcome=outcome,
            source="slack",
            resources=resources,
            error=error,
        )
    except Exception:  # pragma: no cover - the audit must never break a bind
        logger.debug("code channel folder SEL write failed", exc_info=True)


def folder_name(channel_name: str, title: str, channel_id: str) -> str:
    """The channel folder's display name: the channel name, else its title."""
    name = (channel_name or "").strip().lstrip("#") or (title or "").strip() or channel_id
    return name[:_MAX_NAME]


def _same_project(raw: object, project: str) -> bool:
    """Blocking: whether a folder's ``project_dir`` resolves to ``project``."""
    return isinstance(raw, str) and bool(raw) and os.path.realpath(raw) == os.path.realpath(project)


def _sibling_with_project(folders: list[Any], parent_id: str, name: str, project: str) -> str:
    """Blocking: a folder under ``parent_id`` named ``name`` whose project is ``project``.

    ``folders`` is a snapshot taken on the loop; the path resolution here can
    stall on a slow mount, so callers run it in a thread.
    """
    for folder in folders:
        if not isinstance(folder, dict) or (folder.get("parent_id") or "") != parent_id:
            continue
        if str(folder.get("name", "")).strip().casefold() != name.strip().casefold():
            continue
        if _same_project(folder.get("project_dir"), project):
            return str(folder.get("id") or "")
    return ""


async def create_channel_folder(state: Any, parent_id: str, name: str, project_dir: str) -> str:
    """Create (or reuse) the channel's folder; returns its id, ``""`` when refused.

    Reuses a sibling of the same name already pointing at this worktree (a
    restart, a rebind). A different folder already holding the name gets a
    suffixed sibling instead of being repointed: an existing folder's project is
    never changed from here.
    """
    from kiro_crew.dashboard.chat_folders import (
        FolderCreateError,
        FolderNameExistsError,
        create_folder_record,
    )

    snapshot = list(getattr(state, "_folders", None) or ())
    existing = await asyncio.to_thread(
        _sibling_with_project, snapshot, parent_id, name, project_dir
    )
    if existing:
        return existing
    where = f"parent={parent_id or '-'} name={name} project={project_dir}"
    for candidate in (name, f"{name} ({os.path.basename(project_dir.rstrip(os.sep))})"):
        try:
            folder = await create_folder_record(
                state,
                name=candidate[:_MAX_NAME],
                parent_id=parent_id,
                project_dir=project_dir,
                refuse_duplicate_name=True,
            )
        except FolderNameExistsError:
            continue
        except FolderCreateError as exc:
            _audit("denied", where, str(exc)[:200])
            logger.warning("code channel folder not created: %s", exc)
            return ""
        # The create path hands back a same-name twin it would not duplicate;
        # one whose project is another directory is a collision, not this folder.
        if folder.get("reused") and not await asyncio.to_thread(
            _same_project, folder.get("project_dir"), project_dir
        ):
            continue
        push = getattr(state, "push_slots_update", None)
        if callable(push):
            push()
        _audit("allowed", f"{where} folder={folder.get('id', '')}")
        return str(folder.get("id") or "")
    _audit("denied", where, "name taken")
    return ""


def subtree_ids(state: Any, folder_id: str) -> frozenset[str]:
    """``folder_id`` and every folder nested under it (empty when unknown)."""
    from kiro_crew.dashboard.chat_folders import chat_folder_subtree_ids

    try:
        return chat_folder_subtree_ids(state, folder_id)
    except Exception:
        logger.debug("code channel folder subtree unreadable", exc_info=True)
        return frozenset()


def sessions_in(state: Any, folder_ids: frozenset[str]) -> dict[str, tuple[bool, int]]:
    """``slot key -> (turn running, turn generation)`` for every live session in ``folder_ids``.

    The generation counts the turns a slot has started, so a turn that began and
    ended between two reads still shows as a change.
    """
    if not folder_ids:
        return {}
    out: dict[str, tuple[bool, int]] = {}
    for key, slot in list((getattr(state, "_slots", None) or {}).items()):
        if (getattr(slot, "folder_id", "") or "") in folder_ids:
            generation = getattr(slot, "_turn_generation", 0)
            out[str(key)] = (
                bool(getattr(slot, "turn_running", False)),
                generation if isinstance(generation, int) else 0,
            )
    return out

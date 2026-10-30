"""One sidebar folder per mate, holding every chat and folder that mate creates.

A mate's home folder is a top-level folder owned by the mate's principal
(``owner_app == "member:<store>"``, the same principal the folder fence already
uses) and marked :data:`MATE_HOME_KEY`. It is created the first time the mate
opens a chat or a folder, named with the mate's display name, and renamed when
the mate's display name changes. The link is the memory store, which a rename
does not change; the name is only a label.

The rules this module serves:

* a chat a mate's own conductor opens is filed in the mate's home folder, or in
  a folder inside it the mate named;
* a folder a mate creates lands inside its home folder: a top-level create, or
  one under a folder outside the home subtree, is re-parented to the home.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from kiro_crew.dashboard.chat_folders_records import MAX_CHAT_FOLDERS

logger = logging.getLogger(__name__)

#: Folder field marking a mate's home folder. Set on exactly one top-level folder
#: per mate principal.
MATE_HOME_KEY = "mate_home"

#: The prefix ``folder_principal`` gives a crew member caller.
MEMBER_PRINCIPAL_PREFIX = "member:"


class MateFolderError(RuntimeError):
    """The mate's home folder could not be created (the folder cap is reached)."""


class MateHomeLockedError(RuntimeError):
    """A caller other than the owning mate tried to file a session in a mate's home."""


def mate_principal(store_id: str) -> str:
    """The folder principal of the mate whose memory store is *store_id*."""
    return f"{MEMBER_PRINCIPAL_PREFIX}{store_id}" if store_id else ""


def is_mate_principal(principal: str) -> bool:
    """Whether *principal* is a crew member's (a mate's) folder principal."""
    return principal.startswith(MEMBER_PRINCIPAL_PREFIX) and len(principal) > len(
        MEMBER_PRINCIPAL_PREFIX
    )


def mate_home_id(folders: list[dict[str, Any]], principal: str) -> str:
    """The id of *principal*'s home folder in *folders*, or ``""``."""
    for folder in folders:
        if (
            folder.get(MATE_HOME_KEY)
            and str(folder.get("owner_app") or "") == principal
            and not folder.get("parent_id")
        ):
            return str(folder.get("id") or "")
    return ""


def in_mate_home(folders: list[dict[str, Any]], folder_id: str, principal: str) -> bool:
    """Whether *folder_id* is *principal*'s home folder or sits anywhere inside it."""
    home = mate_home_id(folders, principal)
    if not home or not folder_id:
        return False
    by_id = {str(f.get("id") or ""): f for f in folders}
    seen: set[str] = set()
    cursor = folder_id
    while cursor and cursor in by_id and cursor not in seen:
        if cursor == home:
            return True
        seen.add(cursor)
        cursor = str(by_id[cursor].get("parent_id") or "")
    return False


def is_mate_home(folder: dict[str, Any] | None) -> bool:
    """Whether *folder* is a mate's home folder."""
    return bool(folder and folder.get(MATE_HOME_KEY) and not folder.get("parent_id"))


def home_owner_of(folders: list[dict[str, Any]], folder_id: str) -> str:
    """The mate principal whose home holds *folder_id* (the home itself included), or ``""``."""
    by_id = {str(f.get("id") or ""): f for f in folders}
    seen: set[str] = set()
    cursor = folder_id
    while cursor and cursor in by_id and cursor not in seen:
        row = by_id[cursor]
        if is_mate_home(row):
            return str(row.get("owner_app") or "")
        seen.add(cursor)
        cursor = str(row.get("parent_id") or "")
    return ""


def refuses_write_into(folders: list[dict[str, Any]], folder_id: str, principal: str) -> bool:
    """Whether *principal* may NOT put a chat or folder into *folder_id*.

    A mate's home and everything under it are the mate's alone: the person, an
    app or another mate cannot add to it.
    """
    owner = home_owner_of(folders, folder_id)
    return bool(owner) and owner != principal


def refuses_refile(
    folders: list[dict[str, Any]], current_folder_id: str, new_folder_id: str, principal: str
) -> bool:
    """Whether *principal* may NOT move a chat filed in *current_folder_id* to *new_folder_id*.

    Both ends count: a chat goes into a mate's home only from that mate, and
    leaves one only by that mate. Staying in the same folder checks only the
    destination, so an unchanged placement is never a move out.
    """
    return refuses_write_into(folders, new_folder_id, principal) or (
        current_folder_id != new_folder_id
        and refuses_write_into(folders, current_folder_id, principal)
    )


def inherited_folder(folders: list[dict[str, Any]], folder_id: str, principal: str) -> str:
    """The folder a session *principal* makes inherits from one filed in *folder_id*.

    A copy of a session (a fork) keeps its source's folder, unless that folder is
    in a mate's home *principal* may not write into: the copy is then unfiled
    (``""``), so nothing lands in a mate's home without the mate putting it there.
    """
    return "" if refuses_write_into(folders, folder_id, principal) else folder_id


def mate_home_subtree_ids(folders: list[dict[str, Any]]) -> set[str]:
    """Ids of every mate's home folder and of every folder inside one.

    The set :func:`home_owner_of` answers non-empty for, built in one pass over
    the tree, so a writer that scans every folder does not walk each one's
    ancestry again. A ``parent_id`` cycle reaches no home and is not included.
    """
    by_id = {str(f.get("id") or ""): f for f in folders}
    locked: dict[str, bool] = {}
    for start in by_id:
        if not start:
            continue
        path: list[str] = []
        cursor = start
        verdict = False
        while cursor and cursor in by_id and cursor not in path:
            if cursor in locked:
                verdict = locked[cursor]
                break
            if is_mate_home(by_id[cursor]):
                verdict = True
                path.append(cursor)
                break
            path.append(cursor)
            cursor = str(by_id[cursor].get("parent_id") or "")
        for fid in path:
            locked[fid] = verdict
    return {fid for fid, inside in locked.items() if inside}


def execution_principal(caller_execution: Any) -> str:
    """The folder principal a session-control caller files sessions as.

    A session on a mate's store (its conductor or one of its workers) files as
    that mate; any other caller, or one whose execution record is unknown, as
    nobody's (``""``), which every mate's home refuses.
    """
    if caller_execution is None or getattr(caller_execution, "member_id", None) is None:
        return ""
    return mate_principal(str(caller_execution.store.store_id or ""))


async def refuse_foreign_home_filing(
    state: Any, caller_history_key: str, folder_id: str, current_folder_id: str = ""
) -> None:
    """Raise :class:`MateHomeLockedError` when the caller may not file into *folder_id*.

    For the session-control verbs that file a session into a folder the caller
    names (fork, revive): the same rule :func:`mate_home_filing` applies to a
    create. *current_folder_id* is where the session sits now, for a verb that
    refiles an existing session: leaving a mate's home is refused too (see
    :func:`refuses_refile`). The caller's execution record is read only when
    either folder is in a mate's home, so the common case costs one folder read.
    """
    if not folder_id:
        return

    def _touches_home(folders: list[dict[str, Any]]) -> bool:
        return bool(home_owner_of(folders, folder_id)) or (
            current_folder_id != folder_id and bool(home_owner_of(folders, current_folder_id))
        )

    if not await state.read_folders(_touches_home):
        return
    principal = await session_folder_principal(caller_history_key)
    if await state.read_folders(
        lambda folders: refuses_refile(folders, current_folder_id, folder_id, principal)
    ):
        raise MateHomeLockedError(folder_id)


async def session_folder_principal(caller_history_key: str) -> str:
    """:func:`execution_principal` of the session at *caller_history_key*, read off the loop.

    An unreadable execution record answers ``""`` (nobody's), which every
    mate's home refuses.
    """
    from kiro_crew.execution_context import read_session_execution

    try:
        execution = await asyncio.to_thread(read_session_execution, caller_history_key)
    except (ValueError, OSError):
        execution = None
    return execution_principal(execution)


def _clean_name(display_name: str, fallback: str) -> str:
    name = (display_name or "").strip()[:100]
    return name or fallback.strip()[:100] or "Mate"


def display_name_for_principal(principal: str) -> str:
    """The display name of the mate behind *principal*, or ``""``. Blocking (config read)."""
    from kiro_crew.config import KiroCrewConfig

    if not is_mate_principal(principal):
        return ""
    store = principal[len(MEMBER_PRINCIPAL_PREFIX) :]
    for name, agent in KiroCrewConfig.load().agents.items():
        if str(getattr(agent, "memory_store", "") or "") == store:
            return str(getattr(agent, "display_name", "") or "") or str(name)
    return ""


async def home_parent_for_create(state: Any, principal: str, parent_id: str) -> str:
    """The parent a folder *principal* creates under *parent_id* is filed under.

    *parent_id* is kept when it is already inside the mate's home folder;
    anything else (the top level, a person's folder, a folder the mate made
    outside its home before this rule) becomes the home folder itself.
    """
    in_home = await state.read_folders(lambda folders: in_mate_home(folders, parent_id, principal))
    if in_home:
        return parent_id
    display = await asyncio.to_thread(display_name_for_principal, principal)
    return await ensure_mate_home(state, principal, display)


async def home_create_target(
    state: Any, principal: str, parent_id: str, project_dir: str
) -> tuple[str, str]:
    """``(parent_id, project_dir)`` for a folder *principal* creates under *parent_id*.

    The parent is :func:`home_parent_for_create`'s. A create filed home out of an
    existing person's folder, with no ``project_dir`` of its own, takes the
    project that folder inherits, so it keeps working in the folder's repository
    the way a conductor re-filed home does (:func:`mate_home_filing`).
    """
    home = await home_parent_for_create(state, principal, parent_id)
    if home == parent_id or project_dir.strip():
        return home, project_dir

    # Function-local: the path module pulls in the hook and sandbox layers.
    from kiro_crew.dashboard.chat_folders_paths import _folder_declared_project

    def _kept_project(folders: list[dict[str, Any]]) -> str:
        if not _person_folder(folders, parent_id):
            return ""
        raw_project, error = _folder_declared_project(folders, parent_id)
        return "" if raw_project is None or error else raw_project

    return home, await state.read_folders(_kept_project)


async def ensure_mate_home(state: Any, principal: str, display_name: str) -> str:
    """The id of *principal*'s home folder, created (or renamed) under the folder lock.

    One folder per principal: the lookup and the append run in the same locked
    mutation, so two chats the mate opens at once cannot each create one. An
    existing home whose name differs from *display_name* is renamed in the
    same write, which keeps the label current without a second writer.
    """
    if not is_mate_principal(principal):
        raise ValueError("not a mate principal")
    name = _clean_name(display_name, principal[len(MEMBER_PRINCIPAL_PREFIX) :])

    def _mutate(folders: list[dict[str, Any]]) -> tuple[bool, tuple[str, str]]:
        home = mate_home_id(folders, principal)
        if home:
            row = next(f for f in folders if f.get("id") == home)
            if str(row.get("name") or "") == name:
                return False, (home, "")
            row["name"] = name
            return True, (home, "")
        if len(folders) >= MAX_CHAT_FOLDERS:
            return False, ("", "folder_cap_reached")
        folder_id = uuid.uuid4().hex[:12]
        folders.append(
            {
                "id": folder_id,
                "name": name,
                "order": len(folders),
                "collapsed": False,
                "hidden": False,
                "parent_id": "",
                "project_dir": "",
                "default_agent": "",
                "default_destination_crew": "",
                "created_at": time.time(),
                "owner_app": principal,
                MATE_HOME_KEY: True,
            }
        )
        return True, (folder_id, "")

    folder_id, err = await state.mutate_folders(_mutate)
    if err:
        raise MateFolderError(err)
    return str(folder_id)


async def rename_mate_home(state: Any, principal: str, display_name: str) -> None:
    """Rename *principal*'s home folder to *display_name*, if it has one."""
    if not is_mate_principal(principal):
        return
    name = _clean_name(display_name, principal[len(MEMBER_PRINCIPAL_PREFIX) :])

    def _mutate(folders: list[dict[str, Any]]) -> tuple[bool, None]:
        home = mate_home_id(folders, principal)
        row = next((f for f in folders if f.get("id") == home), None) if home else None
        if row is None or str(row.get("name") or "") == name:
            return False, None
        row["name"] = name
        return True, None

    await state.mutate_folders(_mutate)


async def release_mate_home(state: Any, principal: str) -> bool:
    """Turn *principal*'s home folder into an ordinary folder; whether there was one.

    Called once the mate is deleted: without the marker the home and its
    subtree are unlocked, so the person may rename, move or delete them.
    """
    if not is_mate_principal(principal):
        return False

    def _mutate(folders: list[dict[str, Any]]) -> tuple[bool, bool]:
        home = mate_home_id(folders, principal)
        row = next((f for f in folders if f.get("id") == home), None) if home else None
        if row is None:
            return False, False
        row.pop(MATE_HOME_KEY, None)
        return True, True

    return bool(await state.mutate_folders(_mutate))


async def follow_display_name(state: Any, memory_store: str, display_name: str) -> None:
    """Rename the mate's home folder after a saved display-name change.

    Best-effort: the rename is already saved, and the next chat the mate opens
    renames the folder anyway (:func:`ensure_mate_home`).
    """
    if state is None:
        return
    try:
        await rename_mate_home(state, mate_principal(memory_store), display_name)
    except Exception:  # noqa: BLE001 - a folder-store failure must not fail the save
        logger.warning("mate home folder rename failed", exc_info=True)
    else:
        state.push_slots_update()


async def hand_back_home(state: Any, memory_store: str) -> None:
    """Hand a deleted mate's home folder back to the person as an ordinary folder.

    Best-effort: the mate is already deleted, and a home left marked only stays
    locked; it never fails the delete.
    """
    if state is None:
        return
    try:
        released = await release_mate_home(state, mate_principal(memory_store))
    except Exception:  # noqa: BLE001 - a folder-store failure must not fail the delete
        logger.warning("mate home folder release failed", exc_info=True)
    else:
        if released:
            state.push_slots_update()


def _person_folder(folders: list[dict[str, Any]], folder_id: str) -> bool:
    """Whether *folder_id* exists and sits outside every mate's home."""
    exists = any(str(f.get("id") or "") == folder_id for f in folders)
    return exists and not home_owner_of(folders, folder_id)


async def mate_home_filing(
    state: Any, caller_execution: Any, folder_id: str, *, conductor: bool
) -> tuple[str, str]:
    """``(filing_folder, project_folder)`` for a session opened by *caller_execution*.

    *conductor* says whether the caller is a mate's own conductor chat. The
    caller decides it off the event loop, by the rule it already dispatches the
    mate's worker by: answering it here, on the loop, would schedule a rescan
    of the installed agents for any template name the snapshot does not hold.

    A mate's own conductor chat files in *folder_id* when it sits inside the
    mate's home folder, else in the home folder itself, created on first use;
    every other caller keeps *folder_id*. The project folder is the one the
    child takes its project directory from: the filing folder, except that a
    conductor re-filed home out of an existing person's folder keeps that
    folder's project, so its worker still runs in the folder's repository.
    Raises :class:`MateFolderError` at the cap, and :class:`MateHomeLockedError`
    when the folder filed in is inside a mate's home and the caller is not that
    mate (its conductor or a worker on its store). Only a member identity
    (:func:`execution_principal`) has a home; a legacy selection has none.
    """
    # Only a member identity (``member_id``) owns a home: a legacy selection
    # without one files where it asks, as nobody's, and never gets a home.
    principal = execution_principal(caller_execution)
    conductor = conductor and bool(principal)
    named = folder_id
    project_from_named = False
    if conductor and principal:
        if not folder_id or not await state.read_folders(
            lambda folders: in_mate_home(folders, folder_id, principal)
        ):
            project_from_named = bool(named) and await state.read_folders(
                lambda folders: _person_folder(folders, named)
            )
            display = await asyncio.to_thread(display_name_for_principal, principal)
            folder_id = await ensure_mate_home(state, principal, display)
    if folder_id and await state.read_folders(
        lambda folders: refuses_write_into(folders, folder_id, principal)
    ):
        raise MateHomeLockedError(folder_id)
    return folder_id, named if project_from_named else folder_id

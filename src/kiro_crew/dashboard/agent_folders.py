"""At the end of a turn, announce a file the turn changed that the agent catalog reads.

The catalog lists the spec files in the user agents folder and each chat's project
(``list_agents``), reads the Mate agents folder for its crewmates' rows, and hides a
crewmate whose template covers it unless it has notes of its own (``briefing.md``, in
its agent-writable folder). Nothing announces a file a turn writes there with a shell
or a file tool, and a ``turn_end`` refresh does not re-read the catalog, so the end
of each turn compares those files with what it saw last and pushes one ``agents``
refresh when one moved. Everything is stat-only; no file is opened.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Hashable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew import agent_discovery, members
from kiro_crew.config import paths as paths_mod
from kiro_crew.dashboard.handlers._shared_workspace import _slot_project
from kiro_crew.executors import discovery_executor

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: The value a project the scope guard refused keeps, so it is never asked again.
_REFUSED = "refused"


def _briefing_signature(slug: str) -> Hashable:
    try:
        st = os.stat(members.member_briefing_path(slug), follow_symlinks=False)
    except (OSError, ValueError):
        return ()
    return (st.st_mtime_ns, st.st_size)


def _signatures(
    projects: set[Path], slugs: set[str], seen: Mapping[tuple[str, str], Hashable]
) -> dict[tuple[str, str], Hashable]:
    """Each file's signature now. Blocking: a ``scandir`` or a ``stat`` each."""
    current: dict[tuple[str, str], Hashable] = {}
    for folder in (agent_discovery._kiro_agents_dir(), paths_mod.mate_agents_dir()):
        current[("dir", str(folder))] = agent_discovery._dir_signature(folder)
    for project in projects:
        key = ("project", str(project))
        if seen.get(key) == _REFUSED or agent_discovery._project_scope_denied(
            project, operation="agent_folders_turn_end", source="dashboard"
        ):
            current[key] = _REFUSED
        else:
            current[key] = agent_discovery._project_signature(project)
    for slug in slugs:
        current[("briefing", slug)] = _briefing_signature(slug)
    return current


async def announce_moved(state: "DashboardState") -> None:
    """Push one ``agents`` refresh when an agents folder moved since it was last seen.

    A folder seen for the first time counts as moved. The new signatures replace the
    old ones on the loop before the push, so two turns that end together announce one
    move once, and the record holds only the folders of the chats open now.
    """
    slots = list(state._slots.items())
    projects = {p for _key, slot in slots if (p := _slot_project(slot))}
    slugs = {s for key, _slot in slots if (s := members.slug_from_dm_slot_key(key))}
    seen = state._agent_folder_signatures
    try:
        current = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), _signatures, projects, slugs, seen
        )
    except Exception:
        logger.debug("agents folder check at turn end failed", exc_info=True)
        return
    seen = state._agent_folder_signatures
    moved = any(key not in seen or seen[key] != value for key, value in current.items())
    state._agent_folder_signatures = current
    if moved:
        state.push_refresh("agents")

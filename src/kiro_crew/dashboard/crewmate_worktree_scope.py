"""Point a crewmate's worker at the crewmate's own worktree, at the start of its turn.

A worker session a crewmate opened (a ``template`` record on
``kirocrew-mate-worker`` carrying the crewmate's ``member_id``) gets its
``slot.project`` -- its cwd and project scope -- set to the crewmate's own git
worktree of the repository the turn is about (:mod:`kiro_crew.crewmate_worktree`).
The repository is read from the paths this turn's message names, then from the
slot's current project; the first one inside a git repository wins. A path in
the worktree the worker already runs in (or in that repository's own checkout)
leaves it where it is, so its session is not reset for nothing.

Called by turn admission (``chat_turn.turn_admission``) right after the
crewmate's Profile Permissions are seeded and before the provider is claimed, so
a first turn, a restored slot and a resumed one alike start in the worktree. A
change of project on a session that already runs arms the same deferred reset
``set_project`` arms, so the next spawn takes the new cwd. Git failing leaves the
turn on its current project.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _worker_crewmate(config: Any, execution: Any) -> str:
    """The crewmate a WORKER session runs for, else ``""`` (a conductor answers ``""``)."""
    from kiro_crew.dashboard.crewmate_approval import crewmate_name_for
    from kiro_crew.mate_worker import MATE_WORKER_AGENT

    if (
        getattr(execution, "selection_kind", "") != "template"
        or getattr(execution, "template_id", "") != MATE_WORKER_AGENT
    ):
        return ""
    return crewmate_name_for(config, execution)


def resolve_worker_project(config: Any, name: str, project: str, message: str) -> "Any | None":
    """The crewmate worktree this worker turn should run in, else ``None``. Blocking."""
    from kiro_crew import crewmate_worktree as cw
    from kiro_crew.config.loader import workspace_dir_for
    from kiro_crew.members import member_slug

    record = config.agents.get(name)
    if record is None:
        return None
    slug = member_slug(name, config)
    ws_dir = str(workspace_dir_for(getattr(record, "workspace", "") or None))
    candidates = [*cw.paths_in(message), *([project] if project else [])]
    for candidate in candidates:
        try:
            found = cw.ensure(slug, ws_dir, candidate)
        except cw.WorktreeRefused as exc:
            logger.warning("crewmate %s: no worktree for %s: %s", name, candidate, exc)
            continue
        except Exception as exc:  # noqa: BLE001 - this candidate only; try the next
            if type(exc).__name__ == "SandboxUnavailable":
                # No sandbox for git at all: no candidate can have a worktree.
                logger.warning("crewmate %s: worktree lookup failed", name, exc_info=True)
                return None
            logger.warning("crewmate %s: no worktree for %s", name, candidate, exc_info=True)
            continue
        if found is None:
            continue
        real_project = os.path.realpath(project) if project else ""
        if real_project and cw._contains(found.path, real_project):
            # Already in this worktree: a path of the same repository moves nothing.
            return cw.CrewmateWorktree(found.repo, found.path, found.branch, project)
        return found
    return None


async def scope_worker_turn(
    state: Any, slot: Any, config: Any, execution: Any, message: str
) -> str:
    """Move *slot*'s project to its crewmate's worktree; return the new project or ``""``."""
    name = _worker_crewmate(config, execution)
    if not name:
        return ""
    project = getattr(slot, "project", "") or ""
    found = await asyncio.to_thread(resolve_worker_project, config, name, project, message or "")
    if found is None or found.cwd == project:
        return ""
    from kiro_crew.dashboard.chat_utils import effective_session_key

    slot.project = found.cwd
    key = effective_session_key(slot)
    sessions = getattr(state, "sessions", None)
    if sessions is not None and sessions.get_provider(key) is not None:
        slot._pending_reset_history_key = key
    slot._dirty = True
    push = getattr(state, "push_slots_update", None)
    if push is not None:
        try:
            push()
        except Exception:  # noqa: BLE001 - a missed push is repaired by the next one
            logger.debug("push_slots_update failed", exc_info=True)
    logger.info("crewmate %s worker %s runs in %s (%s)", name, slot.key, found.cwd, found.branch)
    return found.cwd

"""Point a crewmate's worker at the crewmate's own worktree, at the start of its turn.

A worker session a crewmate opened (a ``template`` record on
``kirocrew-mate-worker`` carrying the crewmate's ``member_id``) gets its
``slot.project`` -- its cwd and project scope -- set to the crewmate's own git
worktree of the repository the turn is about (:mod:`kiro_crew.crewmate_worktree`).
The repository is read, in order, from the paths this turn's message names, the
slot's current project, and the crewmate's goals; the first one inside a git
repository wins. The shared checkout is therefore never the worker's cwd: a
project pointed at it (by the session's creation, by ``set_project``, or by a
brief that names it) is moved to the crewmate's worktree before the provider is
claimed.

Called by turn admission (``chat_turn.turn_admission``) right after the crewmate's Profile Permissions are
seeded, so a first turn, a restored slot and a resumed one alike start in the
worktree. Every worker turn in a worktree counts, read only, how far the
repository's default branch has moved ahead of its branch
(:func:`kiro_crew.crewmate_worktree.note_behind_base`) and tells the worker in
its rail; nothing is ever rebased for it. A change of project on a session that already runs arms the same
deferred reset ``set_project`` arms, so the next spawn takes the new cwd.
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


def _goal_text(slug: str, name: str) -> str:
    from kiro_crew import member_goals

    try:
        goals = member_goals.read_goals(slug, name).get("goals") or []
    except Exception:  # noqa: BLE001 - an unreadable goal names no repository
        return ""
    return "\n".join(str(g.get("text") or "") for g in goals if isinstance(g, dict))


def _current(project: str) -> "Any | None":
    """The crewmate worktree *project* already sits in, from its index, else ``None``."""
    from kiro_crew import crewmate_worktree as cw

    found = cw.describe(project)
    if found is None:
        return None
    path, repo, branch = found
    return cw.CrewmateWorktree(repo=repo, path=path, branch=branch, cwd=project)


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

    def _own(path: str) -> bool:
        # Membership in the crewmate's own index, wherever its worktrees sit.
        return cw.owns(ws_dir, path, slug)

    here = cw.describe(project) if project else None

    def _elsewhere(path: str) -> bool:
        # A path in the worktree the turn already runs in, or in the shared
        # checkout of that same repository, names nothing new; one in another
        # repository (another of the crewmate's own worktrees included) is
        # where the turn is sent.
        if here is None:
            return True
        if not _own(path):
            try:
                root = cw.repo_root(path)
            except Exception:  # noqa: BLE001 - unreadable: let ensure decide
                return True
            return root is None or os.path.realpath(root) != os.path.realpath(here[1])
        there = cw.describe(path)
        return there is None or there[0] != here[0]

    named = [p for p in cw.paths_in(message) if _elsewhere(p)]
    if not named and project and _own(project):
        # Already in one of its worktrees and the turn names no other repository.
        return _current(project)
    candidates = [*named]
    if project:
        candidates.append(project)
    candidates.extend(cw.paths_in(_goal_text(slug, name)))
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
        if found is not None:
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
    if found is None:
        return ""
    from kiro_crew import crewmate_worktree as cw

    try:
        # Read only: how far the default branch has moved ahead, for the rail.
        await asyncio.to_thread(cw.note_behind_base, found)
    except Exception:  # noqa: BLE001 - git unavailable: the turn runs with no note
        logger.warning("crewmate %s: base count failed for %s", name, found.path, exc_info=True)
    if found.cwd == project:
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

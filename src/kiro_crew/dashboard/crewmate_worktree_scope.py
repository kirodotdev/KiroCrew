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

Called by ``chat_runner`` right after the crewmate's Profile Permissions are
seeded, so a first turn, a restored slot and a resumed one alike start in the
worktree. Every worker turn in a worktree first brings its branch up to the
repository's default branch (:func:`kiro_crew.crewmate_worktree.sync_with_base`):
a clean worktree is rebased, a dirty one is left and the worker told. A change of project on a session that already runs arms the same
deferred reset ``set_project`` arms, so the next spawn takes the new cwd.
"""

from __future__ import annotations

import asyncio
import logging
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
        return cw.owns(ws_dir, path)

    named = [p for p in cw.paths_in(message) if not _own(p)]
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
        except Exception:  # noqa: BLE001 - git unavailable: the turn runs where it was
            logger.warning("crewmate %s: worktree lookup failed", name, exc_info=True)
            return None
        if found is not None:
            return found
    return None


async def scope_worker_turn(
    state: Any, slot: Any, config: Any, execution: Any, message: str
) -> str:
    """Move *slot*'s project to its crewmate's worktree; return the new project or ``""``.

    Never while Crewmate 2.0 is off (``kiro_crew.crewmate_v2``).
    """
    from kiro_crew import crewmate_v2

    if not crewmate_v2.enabled():
        return ""
    name = _worker_crewmate(config, execution)
    if not name:
        return ""
    project = getattr(slot, "project", "") or ""
    found = await asyncio.to_thread(resolve_worker_project, config, name, project, message or "")
    if found is None:
        return ""
    from kiro_crew import crewmate_worktree as cw

    try:
        # The repository's default branch may have moved since the last turn.
        await asyncio.to_thread(cw.sync_with_base, found)
    except Exception:  # noqa: BLE001 - git unavailable: the turn runs on the branch as it is
        logger.warning("crewmate %s: base sync failed for %s", name, found.path, exc_info=True)
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

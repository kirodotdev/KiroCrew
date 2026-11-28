"""Point a crewmate's worker at the crewmate's own worktree, at the start of its turn.

A worker session a crewmate opened (a ``template`` record on
``kirocrew-mate-worker`` carrying the crewmate's ``member_id``) gets its
``slot.project`` -- its cwd and project scope -- set to the crewmate's own git
worktree of the repository the turn is about (:mod:`kiro_crew.crewmate_worktree`).
The repository is read from the paths this turn's message names, then from the
slot's current project; the first one inside a git repository wins. A path in
the worktree the worker already runs in (or in that repository's own checkout)
leaves it where it is, so its session is not reset for nothing. A project a person
picked for the session (``slot._project_picked``, held on the transcript binding
and set by ``api_chat_slot_project``) is kept as it is.

Called by turn admission (``chat_turn.turn_admission``) right after the
crewmate's Profile Permissions are seeded and before the provider is claimed, so
a first turn, a restored slot and a resumed one alike start in the worktree. A
change of project on a session that already runs arms the same deferred reset
``set_project`` arms, so the next spawn takes the new cwd. Git failing leaves the
turn on its current project.

A worker whose session is closed loses the worktrees that were its own (a
repository the edition lays out per worker): :func:`release_closed_worker`,
called by ``chat_api.slot_lifecycle.close_slot`` once the close commits.

The cleanup and everything that starts work in a worker's folder hold the
worker's key in turn (:func:`_holding_worker_key`), so they never interleave: the
cleanup from its admission at the close to the hand-over of the project it moved,
a worker turn from its wait to taking its project, and a start that reads the
slot's project ahead of a turn (:func:`settle_worker_project`: the eager spawn, a
thread reply, a side chat) while it reads it. A worker reopened while its cleanup
runs therefore waits for it and then starts on the project it left; the next
turn's lookup makes the worktree again on the worker's branch if it was removed.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
from collections.abc import AsyncIterator, Callable
from typing import Any

logger = logging.getLogger(__name__)

#: Most project moves kept for workers whose next turn has not come yet.
_MAX_KEPT_MOVES = 256


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


def resolve_worker_project(
    config: Any,
    name: str,
    project: str,
    message: str,
    worker: str = "",
    moved_from_repo: str = "",
) -> "Any | None":
    """The crewmate worktree this worker turn should run in, else ``None``. Blocking.

    *worker* is the worker's slot key: a repository the edition lays out per
    worker gets this worker's own worktree. *moved_from_repo* names the
    repository a close moved *project* into the checkout of: the worktree is
    then found from that repository and *project*'s path in it, never from what
    the checkout now holds there (a folder gone, a file, a link elsewhere, another
    repository), so the worker resumes in its own worktree's same folder.
    """
    from kiro_crew import crewmate_worktree as cw
    from kiro_crew.config.loader import workspace_dir_for
    from kiro_crew.members import member_slug

    record = config.agents.get(name)
    if record is None:
        return None
    slug = member_slug(name, config)
    ws_dir = str(workspace_dir_for(getattr(record, "workspace", "") or None))
    prefix = config.agent.member_branch_prefix
    moved_rel = _moved_relpath(moved_from_repo, project)
    candidates = [
        *cw.paths_in(message),
        *([moved_from_repo] if moved_rel is not None else [project] if project else []),
    ]
    for candidate in candidates:
        try:
            found = cw.ensure(slug, ws_dir, candidate, prefix, worker)
        except cw.WorktreeRefused as exc:
            logger.warning("Bot %s: no worktree for %s: %s", name, candidate, exc)
            continue
        except Exception as exc:  # noqa: BLE001 - this candidate only; try the next
            if type(exc).__name__ == "SandboxUnavailable":
                # No sandbox for git at all: no candidate can have a worktree.
                logger.warning("Bot %s: worktree lookup failed", name, exc_info=True)
                return None
            logger.warning("Bot %s: no worktree for %s", name, candidate, exc_info=True)
            continue
        if found is None:
            continue
        if moved_rel is not None and candidate == moved_from_repo:
            return cw.CrewmateWorktree(
                found.repo, found.path, found.branch, _folder_in(found.path, moved_rel)
            )
        real_project = os.path.realpath(project) if project else ""
        if real_project and cw._contains(found.path, real_project):
            # Already in this worktree: a path of the same repository moves nothing.
            return cw.CrewmateWorktree(found.repo, found.path, found.branch, project)
        return found
    return None


def _moved_relpath(repo: str, project: str) -> str | None:
    """*project*'s plain path inside *repo*, read from the path alone; ``None`` when it is not one."""
    if not repo or not project:
        return None
    from kiro_crew import crewmate_worktree as cw

    path, root = os.path.normpath(project), os.path.normpath(repo)
    if not cw._contains(root, path):
        return None
    rel = os.path.relpath(path, root)
    return "" if rel == "." else rel if cw._plain(rel) else None


def _folder_in(worktree: str, rel: str) -> str:
    """The folder *rel* of *worktree* when it is a real folder in it, else the worktree's root."""
    from kiro_crew import crewmate_worktree as cw

    if not rel:
        return worktree
    folder = os.path.join(worktree, rel)
    if os.path.isdir(folder) and cw._contains(os.path.realpath(worktree), os.path.realpath(folder)):
        return folder
    return worktree


async def scope_worker_turn(
    state: Any, slot: Any, config: Any, execution: Any, message: str
) -> str:
    """Move *slot*'s project to its crewmate's worktree; return the new project or ``""``."""
    name = _worker_crewmate(config, execution)
    if not name:
        return ""
    before = getattr(slot, "project", "") or ""
    # Even a picked project waits: a cleanup still removing must not run under the turn.
    async with _holding_worker_key(state, slot.key) as hold:
        _take_moved_project(state, slot)
        await hold.in_thread(
            _rebuild_in_place,
            config,
            name,
            slot.key,
            getattr(slot, "project", "") or "",
            getattr(state, "conversation_log", None),
            _history_key(slot),
        )
        if getattr(slot, "_project_picked", False):
            # A project a person picked for this session is kept as it is.
            return ""
        project = getattr(slot, "project", "") or ""
        # The cleanup this turn waited for may have moved the project; the caller
        # refreshes its binding only for a project this returns.
        settled = _take_project(state, slot, project) if project != before else ""

        def lookup() -> Any:
            from kiro_crew.dashboard.chat_utils import slot_history_key

            moved_from = _moved_checkout_repo(
                getattr(state, "conversation_log", None), slot_history_key(slot), project
            )
            return resolve_worker_project(
                config, name, project, message or "", slot.key, moved_from
            )

        found = await hold.in_thread(lookup)
        if found is None or found.cwd == project:
            return settled
        if getattr(slot, "_project_picked", False):
            # A pick landed during the lookup: it stands.
            return settled
        _take_project(state, slot, found.cwd)
    logger.info("Bot %s worker %s runs in %s (%s)", name, slot.key, found.cwd, found.branch)
    return found.cwd


def _history_key(slot: Any) -> str:
    from kiro_crew.dashboard.chat_utils import slot_history_key

    return slot_history_key(slot)


def _rebuild_in_place(
    config: Any, name: str, worker: str, project: str, log: Any, history_key: str
) -> None:
    """Make the worker's removed worktree again when its project names a folder of it. Blocking.

    Any writer may have saved the old worktree path over a close's move to the
    checkout (a slot save, a person's pick), and a restart resumes on whatever was
    saved. The repository the move recorded (``worker_checkout_repo``) is carried
    by every save, so a missing project in the worker's own folder is rebuilt at
    the same path on the worker's branch and names its folder again, a pick
    included. One outside that folder, by its path or through a link, is refused
    and logged; nothing is created for it.
    """
    from kiro_crew import crewmate_worktree as cw
    from kiro_crew.config.loader import workspace_dir_for
    from kiro_crew.members import member_slug

    if not project or log is None or not history_key:
        return
    own = cw.worker_folder_for(name, config, worker)
    if own is None or not cw._contains(own, os.path.normpath(project)):
        return  # not a folder of the worker's own: the lookup handles it
    try:
        repo = (log.get_metadata(history_key) or {}).get(_MOVED_FROM_REPO)
    except Exception:  # noqa: BLE001 - nothing to rebuild from
        logger.debug("worker metadata for %s unreadable", history_key, exc_info=True)
        return
    if not isinstance(repo, str) or not repo:
        return
    record = config.agents.get(name)
    workspace = str(workspace_dir_for(getattr(record, "workspace", "") or None))
    try:
        cw.rebuild_worker_worktree(
            member_slug(name, config),
            workspace,
            repo,
            project,
            worker,
            config.agent.member_branch_prefix,
        )
    except cw.WorktreeRefused as exc:
        logger.warning("worker %s: %s is not rebuilt: %s", worker, project, exc)
    except Exception:  # noqa: BLE001 - the turn keeps its project; the lookup runs next
        logger.warning("worker %s: %s could not be rebuilt", worker, project, exc_info=True)


def _take_project(state: Any, slot: Any, project: str) -> str:
    """Put *slot* on *project*, resetting a provider started elsewhere; return *project*."""
    from kiro_crew.dashboard.chat_utils import effective_session_key

    slot.project = project
    key = effective_session_key(slot)
    sessions = getattr(state, "sessions", None)
    if sessions is not None and sessions.get_provider(key) is not None:
        from kiro_crew.dashboard.transcript_binding import arm_project_reset

        arm_project_reset(state, slot, key)
    slot._dirty = True
    push = getattr(state, "push_slots_update", None)
    if push is not None:
        try:
            push()
        except Exception:  # noqa: BLE001 - a missed push is repaired by the next one
            logger.debug("push_slots_update failed", exc_info=True)
    return project


class _KeyHold:
    """A held worker key; :meth:`in_thread` runs blocking work the hold outlasts."""

    def __init__(self) -> None:
        self.thread: asyncio.Future | None = None

    async def in_thread(self, fn: Callable[..., Any], *args: Any) -> Any:
        """``fn(*args)`` in a thread; a cancelled caller keeps the key until it returns."""
        self.thread = asyncio.ensure_future(asyncio.to_thread(fn, *args))
        return await asyncio.shield(self.thread)


def _join_queue(state: Any, worker: str) -> tuple[asyncio.Future | None, asyncio.Future]:
    """Take the next place in line for *worker*'s key, at once; ``(ahead, mine)``.

    No await, so a close's cleanup holds its place before anything that runs
    after the close can ask for the key. *mine* is done once this place is given up.
    """
    holders: dict[str, asyncio.Future] = _table(state, "_worker_key_holders")
    ahead = holders.get(worker)
    mine = asyncio.get_running_loop().create_future()
    holders[worker] = mine
    return ahead, mine


def _leave_queue(state: Any, worker: str, mine: asyncio.Future) -> None:
    """Give up a held place in *worker*'s line; the one behind it goes next."""
    if not mine.done():
        mine.set_result(None)
    holders = _table(state, "_worker_key_holders")
    if holders.get(worker) is mine:
        del holders[worker]


def _give_up_place(
    state: Any, worker: str, place: tuple[asyncio.Future | None, asyncio.Future]
) -> None:
    """Give up a place not yet reached: it passes on once the holder ahead is done."""
    ahead, mine = place
    if ahead is not None and not ahead.done():
        ahead.add_done_callback(lambda _done: _leave_queue(state, worker, mine))
    else:
        _leave_queue(state, worker, mine)


@contextlib.asynccontextmanager
async def _holding_worker_key(
    state: Any,
    worker: str,
    place: tuple[asyncio.Future | None, asyncio.Future] | None = None,
) -> AsyncIterator[_KeyHold]:
    """Hold *worker*'s key, in the order the places were taken (*place*, else a new one).

    A holder awaits nothing but its own threads, so a wait here always ends. A
    waiter cancelled in line passes its place on once the holder ahead is done, and
    a holder cancelled while its thread runs keeps the key until the thread returns.
    """
    ahead, mine = place if place is not None else _join_queue(state, worker)
    if ahead is not None and not ahead.done():
        try:
            await asyncio.shield(ahead)
        except asyncio.CancelledError:
            _give_up_place(state, worker, (ahead, mine))
            raise
    hold = _KeyHold()
    try:
        yield hold
    finally:
        thread = hold.thread
        if thread is not None and not thread.done():
            thread.add_done_callback(lambda _done: _leave_queue(state, worker, mine))
        else:
            _leave_queue(state, worker, mine)


def _take_moved_project(state: Any, slot: Any) -> None:
    """Put *slot* on the project its worker's cleanup moved, while it still runs on the old one."""
    moves = getattr(state, "_worker_moves", None)
    moved = moves.pop(slot.key, None) if moves else None
    if moved and _on_moved_project(slot, moved):
        slot.project = moved[1]
        slot._dirty = True


async def settle_worker_project(state: Any, slot: Any) -> None:
    """Wait out a close cleanup of a Bot worker's key and put the slot back in its worktree.

    For a start that reads ``slot.project`` ahead of the slot's next turn (the
    eager spawn, a thread reply, a side chat): read it right after this returns,
    with no await between. A project naming a removed worktree of the worker is
    made again first (:func:`_rebuild_in_place`), so no start runs in, or creates,
    an empty folder at its place. A project a close moved into the checkout is put
    back in the worker's worktree, as its next turn would, so the start reads the
    worker's files and not the checkout's; a picked project stays. Returns at once
    for a slot that is not a worker.
    """
    from kiro_crew.mate_worker import MATE_WORKER_AGENT

    worker = getattr(slot, "key", "") or ""
    if not worker or str(getattr(slot, "agent", "") or "") != MATE_WORKER_AGENT:
        return
    async with _holding_worker_key(state, worker) as hold:
        _take_moved_project(state, slot)
        project = getattr(slot, "project", "") or ""
        back = await hold.in_thread(
            _settle_for_slot,
            worker,
            project,
            bool(getattr(slot, "_project_picked", False)),
            _session_key(slot),
            getattr(state, "conversation_log", None),
            _history_key(slot),
        )
        if back and not getattr(slot, "_project_picked", False) and slot.project == project:
            _take_project(state, slot, back)


def _session_key(slot: Any) -> str:
    from kiro_crew.dashboard.chat_utils import effective_session_key

    return effective_session_key(slot)


def _settle_for_slot(
    worker: str, project: str, picked: bool, session_key: str, log: Any, history_key: str
) -> str:
    """The worktree folder a worker slot outside a turn goes back to, else ``""``. Blocking.

    Its Bot is read here. :func:`_rebuild_in_place` runs first; then a *project* a
    close moved into the checkout, and not *picked*, is looked up in the worker's
    worktree the way its next turn's lookup does.
    """
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.execution_context import read_session_execution

    try:
        config = KiroCrewConfig.load()
        name = _worker_crewmate(config, read_session_execution(session_key))
    except Exception:  # noqa: BLE001 - the start keeps its project; the turn rebuilds
        logger.warning("worker %s: its Bot was not read", worker, exc_info=True)
        return ""
    if not name:
        return ""
    _rebuild_in_place(config, name, worker, project, log, history_key)
    moved_from = "" if picked else _moved_checkout_repo(log, history_key, project)
    if not moved_from:
        return ""
    found = resolve_worker_project(config, name, project, "", worker, moved_from)
    return found.cwd if found is not None and found.cwd != project else ""


def _release_worker(
    log: Any,
    worker: str,
    session_key: str,
    history_key: str,
    saved: str,
) -> tuple[str, str] | None:
    """Remove *worker*'s own worktrees, when its session is a crewmate's worker. Blocking.

    *saved* is the project the closed session was saved with. The session's
    execution record is read here, durable record included, so a worker whose
    carrier is not in memory (a persistent session, or one restored after a restart
    that has run no turn) is recognised too.

    When the project sits in one of the worker's worktrees, the saved project is
    rewritten to the same folder in the repository's checkout BEFORE anything is
    removed. A restart at any point after that resumes on the checkout path, where
    the next turn's lookup reuses the worktree if it is still there and rebuilds it
    on the worker's own branch if it is gone. The move is returned as
    ``(saved, checkout)``. It runs holding the worker's key, so no turn of the
    worker runs meanwhile (:func:`release_closed_worker`).
    """
    from kiro_crew import crewmate_worktree as cw
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.execution_context import read_session_execution

    try:
        execution = read_session_execution(session_key)
        config = KiroCrewConfig.load()
        name = _worker_crewmate(config, execution)
        own = cw.worker_folder_for(name, config, worker) if name else None
        in_own = bool(saved and own and cw._contains(own, os.path.realpath(saved)))
        # Only a project in the worker's own folder is moved: the Bot's shared
        # worktree is never removed on a close, so its path stays valid.
        source = cw.source_path(saved) if in_own else ""
    except Exception:  # noqa: BLE001 - a close never fails on this
        logger.warning("worker %s: its worktrees were not looked at", worker, exc_info=True)
        return None
    if not name:
        return None
    move = (saved, source) if source and source != saved else None
    if move is not None and not _is_checkout_folder(source):
        # A folder only the worker's branch has would resume one level up, and a
        # symlink on the way would resume outside the repository: either way the
        # worktree holding it stays.
        logger.warning(
            "worker %s: %s is not in the checkout; its worktrees are kept", worker, saved
        )
        return None
    if in_own and move is None:
        # The saved project names a worktree of this worker that maps to no
        # checkout (a layout other than the one the edition gives now): removing it would
        # leave the saved project on a deleted folder with nothing to rebuild from.
        logger.warning("worker %s: %s maps to no checkout; its worktrees are kept", worker, saved)
        return None
    if move is not None:
        try:
            repo = cw.repo_root(saved) or ""
            if not repo or not cw._contains(repo, source):
                logger.warning(
                    "worker %s: %s maps outside its repository; its worktrees are kept",
                    worker,
                    saved,
                )
                return None
            written = log.update_metadata_if(
                history_key,
                {"project": source, _MOVED_FROM_REPO: repo},
                lambda meta: meta.get("project") == saved and not meta.get("project_picked"),
            )
        except Exception:  # noqa: BLE001 - keep the worktree the saved project names
            logger.warning("worker %s: its saved project was not moved", worker, exc_info=True)
            return None
        if not written:
            # A later save names another project, or a person picked it since the
            # close: that close's own cleanup handles the first, and a pick is never
            # moved, so the worktree it names stays.
            return None
    cw.remove_for_worker(name, config, worker)
    if move is not None and os.path.isdir(saved):
        # Its worktree was kept (it holds work), so the saved path still leads
        # there; a live slot on it stays put.
        return None
    return move


#: Saved-project metadata naming the repository a worker's project was moved
#: into its checkout of, so its next turn finds the worktree from the repository
#: and the folder's path, whatever the checkout holds at that path by then.
_MOVED_FROM_REPO = "worker_checkout_repo"


def _moved_checkout_repo(log: Any, history_key: str, project: str) -> str:
    """The repository *project* was moved into the checkout of on a close, else ``""``.

    Only while the saved project is still that move's path: a later save names
    another project, which the ordinary lookup handles.
    """
    if log is None or not project or not history_key:
        return ""
    try:
        meta = log.get_metadata(history_key) or {}
    except Exception:  # noqa: BLE001 - without it the ordinary lookup runs
        logger.debug("worker metadata for %s unreadable", history_key, exc_info=True)
        return ""
    repo = meta.get(_MOVED_FROM_REPO)
    if not isinstance(repo, str) or meta.get("project") != project:
        return ""
    return repo


def _is_checkout_folder(source: str) -> bool:
    """Whether *source* is a real folder of the checkout :func:`source_path` named.

    That path is the checkout's real path joined with the worktree-relative
    subfolder, so it equals its own real path unless a component on the way is
    a symlink, which could lead out of the repository or into another one.
    """
    return os.path.isdir(source) and os.path.realpath(source) == os.path.normpath(source)


def _on_moved_project(slot: Any, move: tuple[str, str]) -> bool:
    """Whether *slot* still runs in the project *move* came from, and no person picked it."""
    return not getattr(slot, "_project_picked", False) and getattr(slot, "project", "") == move[0]


def _table(state: Any, name: str) -> dict:
    """The per-worker table *name* on *state*, made once; never replaced, even when empty."""
    table = getattr(state, name, None)
    if table is None:
        table = {}
        setattr(state, name, table)
    return table


def _remember_move(state: Any, worker: str, move: tuple[str, str]) -> None:
    """Point a live slot of *worker* still on the moved project at its new path, else keep it.

    A kept move waits in ``state._worker_moves`` for the worker's next turn (a resume
    still building its slot is not in the map yet). The table keeps at most
    :data:`_MAX_KEPT_MOVES` entries, dropping the oldest first; one dropped only
    loses the in-memory shortcut, since the saved project was rewritten already.
    """
    live = getattr(state, "_slots", {}).get(worker)
    if live is not None and getattr(live, "_project_picked", False):
        return  # a person's pick stands over any cleanup's move
    if live is not None and _on_moved_project(live, move):
        live.project = move[1]
        live._dirty = True
        return
    moves: dict[str, tuple[str, str]] = _table(state, "_worker_moves")
    moves.pop(worker, None)
    moves[worker] = move
    while len(moves) > _MAX_KEPT_MOVES:
        moves.pop(next(iter(moves)))


#: A close cleanup's place in its worker's line, taken by :func:`reserve_worker_cleanup`.
CleanupPlace = tuple[asyncio.Future | None, asyncio.Future]


def reserve_worker_cleanup(state: Any, slot: Any) -> CleanupPlace | None:
    """Take *slot*'s close cleanup's place in its worker's line; ``None`` when none will run.

    Called with no await between it and the removal of *slot* from the slot map,
    so the cleanup is in line before anything can reopen the worker: a resume and
    its turn then wait for the cleanup whatever the rest of the close awaits. The
    place goes to :func:`release_closed_worker` once the close commits, or to
    :func:`cancel_worker_cleanup` when it does not. Only a crewmate's worker whose
    project no person picked gets one, since no worktree is applied over a pick.
    """
    from kiro_crew.mate_worker import MATE_WORKER_AGENT

    if str(getattr(slot, "agent", "") or "") != MATE_WORKER_AGENT or getattr(
        slot, "_project_picked", False
    ):
        return None
    return _join_queue(state, slot.key)


def cancel_worker_cleanup(state: Any, slot: Any, place: CleanupPlace | None) -> None:
    """Give up a reserved cleanup place for a close that did not commit."""
    if place is not None:
        _give_up_place(state, slot.key, place)


def release_closed_worker(
    state: Any, slot: Any, session_key: str, place: CleanupPlace | None = None
) -> None:
    """Remove a closed worker's own worktrees in the background; a crewmate's workers only.

    Called once *slot*'s close has committed, with the session key it ran under
    and the place :func:`reserve_worker_cleanup` took at the removal (taken here
    when the caller took none). Nothing runs when a person picked its project. The
    cleanup holds the worker's key until the project it moved is handed over
    (:func:`_holding_worker_key`); two closes in quick succession clean up one
    after the other. The Bot's shared worktrees are left alone: they go with the Bot.
    """
    from kiro_crew.dashboard.chat_utils import slot_history_key

    worker = slot.key
    if place is None:
        place = reserve_worker_cleanup(state, slot)
    if place is None:
        return
    release = functools.partial(
        _release_worker,
        state.conversation_log,
        worker,
        session_key,
        slot_history_key(slot),
        getattr(slot, "project", "") or "",
    )
    task = asyncio.create_task(_clean_up(state, worker, place, release))
    background = getattr(state, "_background_tasks", None)
    if background is not None:
        background.add(task)
        task.add_done_callback(background.discard)


async def _clean_up(
    state: Any,
    worker: str,
    place: tuple[asyncio.Future | None, asyncio.Future],
    release: Callable[[], tuple[str, str] | None],
) -> None:
    """Run *release* holding *worker*'s key at *place*, then hand its move over."""
    async with _holding_worker_key(state, worker, place) as hold:
        try:
            move = await hold.in_thread(release)
        except Exception:  # noqa: BLE001 - a failed cleanup moves nothing
            logger.warning("worker %s: its close cleanup failed", worker, exc_info=True)
            return
        if move:
            _remember_move(state, worker, move)

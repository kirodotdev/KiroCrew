"""Asking the skill catalog for a chat's scope before that chat's first message.

A session start reads the skill list inside its prompt build. The loader serves a
scope's list from memory for ``skills_discovery._ITER_CACHE_TTL_SECS``. That lifetime
is the bound on how long a change made outside the app (a sync, a manual copy) can go
unseen, because such a change sends no signal. The first read after it queues a re-walk
on the catalog worker and is answered from the list the loader has. When that first
read is a session start's own, the re-walk runs beside the prompt build and competes
with it for the path gate, after the person has sent the message.

The dashboard announces a slot when its composer gets focus or a key press
(``slot_focused``, at most one frame per ten seconds per slot). On that frame the
gateway asks the loader for the slot's scope on a worker thread, so a re-walk that is
due starts while the person is still typing. A turn that starts its own session asks
the same as its session claim begins, which covers a message no frame came before and
a list that expired after the last frame's read.
Nothing is kept here and the list's lifetime is unchanged: the read is the loader's
own, a fresh list is returned as it is, and an expired one is refreshed by the loader
exactly as a session start's read would refresh it. Only the skill catalog is read.
No memory, agent spec, steering file or hook is touched.

All state here is read and written on the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from kiro_crew.dashboard.chat_utils import effective_session_key

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: One read per scope at most this often. It matches the dashboard's own throttle on
#: the frame, so a person typing asks about once per frame and tab flipping asks no
#: more than that. It is well under the list's lifetime on purpose: a read of a
#: fresh list refreshes nothing, so a floor as long as the lifetime could let the
#: one read that matters, the first after expiry, wait a whole lifetime.
MIN_INTERVAL_SECS = 10.0
#: Scopes remembered at once; the oldest goes first. A forgotten scope is only read
#: again sooner than the floor.
_MAX_SCOPES = 64

#: scope (project directory, "" for none) -> monotonic time its last read started.
_started: dict[str, float] = {}
#: Scopes with a read on a worker thread right now.
_in_flight: set[str] = set()


def read_ahead(state: "DashboardState", slot_key: object) -> "asyncio.Task[None] | None":
    """Start a read of *slot_key*'s skill scope, unless one is not due. Loop only.

    Read only for a slot whose next turn builds a session-start context: one with
    no live session, or with a started session no turn has claimed. A slot that is
    answering, or whose session has had a turn, reads nothing, because its next
    build makes no session-start skill read. Returns the task, or ``None``.
    """
    slot = state.get_slot(slot_key) if isinstance(slot_key, str) and slot_key else None
    sessions = getattr(state, "sessions", None)
    if slot is None or slot.running or sessions is None:
        return None
    session_key = effective_session_key(slot)
    if sessions.has_session(session_key) and not sessions.fresh_start_unclaimed(session_key):
        return None
    return _start(state, slot.project or "")


def read_beside_claim(state: "DashboardState", project: str) -> "asyncio.Task[None] | None":
    """Start a read of *project*'s skill scope for a turn that starts its session. Loop only.

    A turn nobody announced (a message sent to a chat with no frame before it: an
    API client, a pasted message sent at once) reaches its prompt build only after
    the session claim, which lasts seconds and, with many MCP servers, tens of
    seconds. Called as that claim starts, so a due re-walk runs beside the claim,
    while the build is still far off, instead of beside the build, when the
    session's servers are starting too. Same single read in flight as
    :func:`read_ahead`, but not its floor: a frame may have read a list seconds
    before it expired, and this is the last moment before the build. A turn
    starts a session once, so nothing here can repeat fast.
    """
    return _start(state, project or "", floor=False)


def _start(
    state: "DashboardState", scope: str, *, floor: bool = True
) -> "asyncio.Task[None] | None":
    """Start one read of *scope* unless one is in flight or, with *floor*, one began too recently.

    Never raises: a state with no context builder, or a builder with no loader,
    starts nothing.
    """
    skills = getattr(getattr(state, "context_builder", None), "skills", None)
    if skills is None:
        return None
    now = time.monotonic()
    last = _started.get(scope)
    if scope in _in_flight:
        return None
    if floor and last is not None and now - last < MIN_INTERVAL_SECS:
        return None
    _started.pop(scope, None)
    while len(_started) >= _MAX_SCOPES:
        del _started[next(iter(_started))]
    _started[scope] = now
    _in_flight.add(scope)
    task = asyncio.create_task(_read(skills, scope))
    tracked = getattr(state, "_background_tasks", None)
    if tracked is not None:
        tracked.add(task)
        task.add_done_callback(tracked.discard)
    return task


def read_scope(skills: object, scope: str) -> None:
    """Ask the loader for *scope*'s list, as a session start's own read does. Blocking."""
    skills.scoped_skills(project_dir=scope or None, only=None)  # type: ignore[attr-defined]


async def _read(skills: object, scope: str) -> None:
    """Run :func:`read_scope` on a worker thread; a failure is dropped."""
    try:
        await asyncio.to_thread(read_scope, skills, scope)
    except Exception:  # noqa: BLE001 - a head start must never fail a frame
        logger.debug("Skill read ahead of a first message failed", exc_info=True)
    finally:
        _in_flight.discard(scope)

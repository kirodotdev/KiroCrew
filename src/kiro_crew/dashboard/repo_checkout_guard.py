"""Per-repository checkout reservations that order a branch switch against agent work.

A branch switch rewrites the working tree under every session whose project lies
in the repository. The switch handler refuses while any of those sessions has a
turn running or sub-agents pending, but neither turn admission nor sub-agent
admission takes the switch's lock, so work admitted between that check and the
checkout would read files on one branch and write them back on the other.

The two sides order themselves through this registry, on the event loop:

* the switch calls :func:`reserve` synchronously, BEFORE it awaits its busy
  check, and the reservation is released only when the checkout worker itself
  has finished (:func:`release_when_done`), so a cancelled switch request cannot
  drop it while git is still rewriting files;
* every turn calls :func:`wait_for_checkout` at its entry, before it reads any
  project file, and every sub-agent run awaits
  :func:`wait_for_subagent_checkout` just before it executes;
* a turn also marks itself unplaced and awaits :func:`wait_for_any_checkout`
  before it claims its session, since where that session runs is the
  allocator's choice, then records the folder the session reports and waits
  out any checkout of it. :func:`slot_folders` is what the busy check reads;
* a workflow step, which opens its own session outside chat slots and the
  sub-agent manager, does the same through :func:`tracked_work`, and the busy
  check reads :func:`tracked_work_folders`.

Both kinds of work are published before their first step: a turn's task is on
its slot, and a sub-agent run is counted as its parent's pending work. So work
whose entry ran before the reservation is seen by the busy check, which refuses
the switch, and work whose entry runs after it waits until the checkout has
finished. The wait has no deadline: every git call the checkout makes is
bounded, so the worker always finishes, and work that started beside a live
checkout would reopen the race this registry closes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import Any

logger = logging.getLogger(__name__)

# How often a waiter logs that it is still waiting on a checkout.
WAIT_LOG_SECS = 120.0

_reserved: dict[str, asyncio.Event] = {}


def reserve(root: str) -> bool:
    """Reserve ``root`` (a resolved repository root) for one checkout.

    Returns False when another checkout already holds it.
    """
    if root in _reserved:
        return False
    _reserved[root] = asyncio.Event()
    return True


def release(root: str) -> None:
    """Drop the reservation on ``root`` and wake everything waiting on it."""
    event = _reserved.pop(root, None)
    if event is not None:
        event.set()


def release_when_done(root: str, worker: asyncio.Future) -> None:
    """Release ``root`` once ``worker`` finishes, however its awaiter ends."""

    def _done(future: asyncio.Future) -> None:
        if not future.cancelled():
            # Retrieved so an exception nobody awaited (the switch request was
            # cancelled) is not reported as never retrieved.
            future.exception()
        release(root)

    worker.add_done_callback(_done)


def is_reserved(root: str) -> bool:
    return root in _reserved


def _covering_root(project: str, roots: Iterable[str]) -> str | None:
    """The reserved root that contains ``project`` or that ``project`` contains."""
    resolved = os.path.realpath(project)
    for root in roots:
        try:
            common = os.path.commonpath([resolved, root])
        except ValueError:  # different drives on Windows
            continue
        if common in (resolved, root):
            return root
    return None


async def wait_for_checkout(project: str | None, *, waiter: str = "") -> None:
    """Wait while a checkout holds the repository that ``project`` lies in.

    ``waiter`` names the work that is waiting (a session key or run id) for the
    periodic log line, so a checkout that never finishes can be traced.
    """
    if not project or not _reserved:
        return
    while _reserved:
        root = await asyncio.to_thread(_covering_root, project, tuple(_reserved))
        if root is None:
            return
        event = _reserved.get(root)
        if event is None:
            continue
        try:
            await asyncio.wait_for(event.wait(), WAIT_LOG_SECS)
        except asyncio.TimeoutError:
            logger.warning(
                "%s still waiting on a checkout of %s before starting work",
                waiter or "work",
                root,
            )


async def wait_for_any_checkout(*, waiter: str = "") -> None:
    """Wait until no checkout is in flight anywhere.

    For work whose folder cannot be known yet, so no single repository's
    reservation can be matched against it.
    """
    while _reserved:
        root, event = next(iter(_reserved.items()))
        try:
            await asyncio.wait_for(event.wait(), WAIT_LOG_SECS)
        except asyncio.TimeoutError:
            logger.warning("%s still waiting on a checkout of %s", waiter or "work", root)


#: Ends a step's session for good: called with ``escalate`` false on the first
#: attempt and true on each retry, and answers whether the session's process tree
#: is confirmed gone. Raising counts as not yet.
SessionStop = Callable[[bool], Awaitable[bool]]


class TrackedWork:
    """Session work that runs outside chat slots and the sub-agent manager.

    A workflow step opens its own session through the session manager, so
    neither the slot scan nor the sub-agent registry sees it. It registers
    here instead (:func:`tracked_work`): unplaced while its session is being
    acquired, since where the session lands is the allocator's choice, then
    placed on the folder that session reports (:meth:`place`). A step whose
    session reports no folder stays unplaced for its whole run.
    """

    __slots__ = ("ended_cleanly", "folders", "placed", "provider", "stop", "waiter")

    def __init__(self, waiter: str) -> None:
        self.folders: tuple[str, ...] = ()
        self.placed = False
        self.provider: Any = None
        self.stop: SessionStop | None = None
        self.ended_cleanly = False
        self.waiter = waiter

    async def place(self, provider: Any, *also: object, stop: SessionStop | None = None) -> None:
        """Record where the work runs and wait out a checkout of any of it.

        ``provider`` is the session the work drives: the folder it reports is
        where its native turn runs. ``also`` names any other folder the work
        reads, such as the project its prompt is built against, which can
        differ from a reused session's folder. ``stop`` ends that session for
        good; work that runs a turn passes it, and unless the backend itself
        reported that turn finished (:meth:`turn_ended`) the block's exit
        awaits it before the record goes (:func:`tracked_work`). Everything is recorded with no await
        before it, so a busy check that runs after this call sees it and one
        that ran before it saw the work unplaced. Only the provider's own
        report places the work.
        """
        self.provider = provider
        if stop is not None:
            self.stop = stop
            self.ended_cleanly = False
        session_folder = str(getattr(provider, "cwd", "") or "")
        folders = [f for f in (session_folder, *(str(x or "") for x in also)) if f]
        self.folders = (*self.folders, *folders)
        if session_folder:
            self.placed = True
        for folder in folders:
            await wait_for_checkout(folder, waiter=self.waiter)

    def turn_ended(self, event: Any) -> None:
        """Hear the turn's completion event (``stream_and_collect``'s ``on_complete``).

        Only an end of turn the backend itself reported counts: one of the
        protocol's own stop reasons (:data:`BACKEND_STOP_REASONS`) on a
        completion that is not synthetic. The session layer also ends turns on
        its own -- a stalled tool, a cancel the backend never acknowledged, a
        failed compaction, a stale turn it gave up on -- and the collector
        returns early at its tool-call ceiling with no completion at all; in
        each of those the backend may still be running the turn's tools, so the
        step's session is ended. Positive evidence rather than a list of bad
        exits, so an exit added later fails closed.
        """
        self.ended_cleanly = (
            getattr(event, "stop_reason", None) in BACKEND_STOP_REASONS
            and getattr(event, "synthetic_completion", False) is not True
        )


#: The ACP protocol's stop reasons: each is the backend answering the prompt, so
#: its turn is over. An absent reason is a backend that does not fill the field
#: (``classify_stop_reason`` reads it as a normal end too); the session layer
#: always names its own terminals (``timeout``, ``stale_recover`` and the
#: ``error:`` family), and none of them is here.
BACKEND_STOP_REASONS = frozenset(
    {"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled", "", None}
)


_tracked: dict[int, TrackedWork] = {}

#: How often a step's session teardown is retried while it cannot finish.
TEARDOWN_RETRY_SECS = 2.0


def process_tree_gone(provider: Any) -> bool:
    """Whether ``provider``'s runtime confirmed its whole process tree exited.

    The runtime's own teardown evidence, set only when the root and every
    descendant it tracked are gone; a session's removal from the registry, or a
    destroy that returned, is not that.
    """
    return getattr(provider, "process_tree_confirmed_dead", None) is True


async def end_managed_session(
    sessions: Any, key: str, provider: Any, *, escalate: bool = False
) -> bool:
    """End ``provider``'s session for good and report whether its process is gone.

    Destroying the session ends its process, which is the only way to stop a
    native turn its cancelled task left running: cancelling the task closes the
    stream but tells the backend nothing until the session's next prompt. The
    destroy is fenced on the provider, re-checked under the registry lock, so a
    session someone else has since put under ``key`` is never destroyed; a
    session a turn holds (a sibling step on a shared named session) is skipped
    and retried. When ``key`` does not hold ``provider`` -- another
    teardown removed it, perhaps failing part-way -- a retry (``escalate``)
    shuts the provider down itself.

    Done only on the runtime's confirmation that the process tree exited
    (:func:`process_tree_gone`).
    """
    if process_tree_gone(provider):
        return True
    if sessions.get_provider(key) is provider:
        generation = sessions.session_generation(key)
        await sessions.destroy_if(key, generation, lambda: sessions.get_provider(key) is provider)
    elif escalate:
        await provider.shutdown()
    return process_tree_gone(provider)


async def _attempt_stop(work: TrackedWork, *, retry: bool) -> bool:
    """One try at ``work.stop``: whether the session's process is now gone.

    A failure is logged by exception type only, and only on the first try:
    session error text can carry conversation content, and a teardown that keeps
    failing is retried every few seconds.
    """
    if work.stop is None:
        return True
    try:
        return await work.stop(retry) is True
    except Exception as exc:  # noqa: BLE001 - retried; the record stays meanwhile
        if not retry:
            logger.warning(
                "%s: session teardown failed (%s); retrying", work.waiter, type(exc).__name__
            )
        return False


async def _retry_stop(work: TrackedWork) -> None:
    """Keep trying ``work.stop`` until the session's process is gone, then drop
    the record.

    Until then the busy check keeps refusing: dropping the record on a teardown
    that did not finish would let a switch run under a process that may still
    be running the turn's tools, so one that never finishes fails closed. A
    retry that is itself cancelled (the gateway shutting down) leaves the
    record.
    """
    warned = False
    while True:
        await asyncio.sleep(TEARDOWN_RETRY_SECS)
        if await _attempt_stop(work, retry=True):
            _tracked.pop(id(work), None)
            return
        if not warned:
            logger.warning("%s: session process not confirmed gone; still busy", work.waiter)
            warned = True


async def _end_session(work: TrackedWork) -> None:
    """End ``work``'s session once, inline; if that does not finish, retry it
    in the background with the record still registered (:func:`_retry_stop`)."""
    if await _attempt_stop(work, retry=False):
        _tracked.pop(id(work), None)
    else:
        _keep_task(asyncio.ensure_future(_retry_stop(work)))


def _keep_task(task: asyncio.Future) -> asyncio.Future:
    """Hold a reference to a teardown until it finishes."""
    _ending.add(task)
    task.add_done_callback(_ending.discard)
    return task


@contextlib.asynccontextmanager
async def tracked_work(*, waiter: str = "") -> AsyncIterator[TrackedWork]:
    """Register session work for the busy check for the length of the block.

    Registered unplaced before the first await, then waits out every checkout
    in flight, so a switch either sees the work (and refuses) or finishes
    before the work acquires its session.

    A block that placed a provider with a ``stop`` ends that session unless it
    finished normally after a turn the backend reported finished
    (:meth:`TrackedWork.turn_ended`) -- so a cancel, an exception, a turn the
    session layer ended itself and a collector that stopped at its tool-call
    ceiling all end it before the record goes
    (:func:`_end_session`): its native turn may still be running tools, and only
    ending the process stops it. The block waits for the first attempt, so a
    pooled worker's session is gone before the pool can hand the worker to
    another step; an attempt that does not finish is retried in the background
    with the record kept. The teardown runs shielded: a second cancel stops the
    wait, not the teardown, and the record stays until the teardown finishes.
    """
    work = TrackedWork(waiter or "work")
    _tracked[id(work)] = work
    finished = False
    try:
        await wait_for_any_checkout(waiter=work.waiter)
        yield work
        finished = True
    finally:
        if work.stop is None or (finished and work.ended_cleanly):
            _tracked.pop(id(work), None)
        else:
            await asyncio.shield(_keep_task(asyncio.ensure_future(_end_session(work))))


# Teardowns of work that ended abnormally, referenced until they finish.
_ending: set[asyncio.Future] = set()


def tracked_work_folders() -> tuple[list[str], bool]:
    """Folders tracked work runs in, and whether every piece of it is placed."""
    works = list(_tracked.values())
    folders = [folder for work in works for folder in work.folders if folder]
    return folders, all(work.placed for work in works)


def slot_folders(state: Any, slot: Any) -> tuple[list[str], bool]:
    """The folders a chat slot's work can touch, and whether all of them are known.

    An idle slot is judged by its ``project``. A running one also works where its
    turn does, which ``project`` does not track: a project picked mid-turn, the
    ``set_project`` tool and a member rebinding all write ``project`` while the
    turn's session keeps running where it started, and a session kept alive across
    a deferred reset, or resumed from a stored folder, runs in a folder no
    ``project`` value named. So a running slot adds the folders its turn recorded
    (``_ChatSlot._checkout_turn_projects``: the project at entry, the project its
    session was allocated for, the folder that session reported) and the folder
    the live session for its key reports now.

    The second value is False while the running turn is between starting to
    allocate its session and that session reporting its folder, since where it
    lands is the allocator's choice then, and when the session registry cannot be
    read. Callers treat such a slot as working in every repository.

    Read on the event loop: the turn updates these fields without awaiting, so a
    read here sees a consistent set where a read from a worker thread might not.
    """
    folders = [str(getattr(slot, "project", "") or "")]
    if not getattr(slot, "running", False):
        return [folder for folder in folders if folder], True
    folders.extend(str(f or "") for f in getattr(slot, "_checkout_turn_projects", ()) or ())
    placed = not getattr(slot, "_checkout_turn_unplaced", False)
    sessions = getattr(state, "sessions", None)
    if sessions is not None:
        from kiro_crew.dashboard.chat_utils import effective_session_key

        try:
            provider = sessions.get_provider(effective_session_key(slot))
            if provider is not None:
                folders.append(str(getattr(provider, "cwd", "") or ""))
        except Exception:  # noqa: BLE001 - an unreadable registry leaves the folder unknown
            placed = False
    return [folder for folder in folders if folder], placed


def _slot_projects(state: Any, session_key: str) -> list[str]:
    from kiro_crew.dashboard.chat_utils import effective_session_key

    found: list[str] = []
    slots = getattr(state, "_slots", None) or {}
    for slot in list(getattr(slots, "values", list)()):
        try:
            if effective_session_key(slot) != session_key:
                continue
        except Exception:  # noqa: BLE001 - a slot that cannot be keyed is not the parent
            continue
        # A root chat still placing its own session adds what is known; the
        # chat itself is judged as a slot, so its unknown folder is not the run's.
        found.extend(slot_folders(state, slot)[0])
    return found


class LineageUnknown(Exception):
    """A run's lineage names a parent run whose record is gone (its card was dismissed)."""


def runs_by_key(state: Any) -> dict[str, Any]:
    """The manager's runs keyed by the session key their children name as parent."""
    manager = getattr(state, "subagents", None)
    by_key: dict[str, Any] = {}
    for run in list(getattr(manager, "all_agents", None) or []):
        key = str(getattr(run, "conversation_key", "") or f"subagent:{getattr(run, 'id', '')}")
        by_key[key] = run
    return by_key


def _own_folders(node: Any) -> list[str]:
    """The folders a run itself works in, once they can be known.

    ``cwd`` is the spawn's override and ``_session_cwd`` the folder its provider
    session actually started in, recorded once the session is acquired. A run
    with neither has not been placed yet, and where it lands is the allocator's
    choice (a member's workspace, a resumed conversation's stored folder, the
    pool's or a per-session work folder), so :class:`LineageUnknown` is raised
    for it rather than guessing.
    """
    own = [
        str(getattr(node, "cwd", "") or ""),
        str(getattr(node, "_session_cwd", "") or ""),
    ]
    if any(own):
        return own
    raise LineageUnknown(f"subagent:{getattr(node, 'id', '')}")


def subagent_folders(state: Any, info: Any, by_key: dict[str, Any] | None = None) -> list[str]:
    """Every folder a sub-agent run works in: its own, its ancestors', its root chat's.

    Each run contributes the folders :func:`_own_folders` finds for it, and the
    lineage is walked up through the manager's runs to the dashboard session at
    its top, since a grandchild's parent is another run rather than a chat.
    Raises :class:`LineageUnknown` when a ``subagent:`` parent has no record in
    the manager, or when any run in the lineage has not been placed yet, since
    the folders either would have contributed cannot be checked.
    """
    if by_key is None:
        by_key = runs_by_key(state)
    folders: list[str] = []
    seen: set[int] = set()
    node = info
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        folders.extend(_own_folders(node))
        parent = str(getattr(node, "parent_session_key", "") or "")
        if not parent:
            break
        node = by_key.get(parent)
        if node is None:
            if parent.startswith("subagent:"):
                raise LineageUnknown(parent)
            folders.extend(_slot_projects(state, parent))
    return [folder for folder in folders if folder]


def folders_overlap(folders: Iterable[str], root: str) -> bool:
    """Whether any of ``folders`` lies in ``root`` or contains it."""
    return any(_covering_root(folder, (root,)) is not None for folder in folders)


async def wait_for_subagent_checkout(state: Any, info: Any) -> None:
    """Wait out a checkout on any folder the sub-agent run works in.

    A run whose lineage cannot be resolved waits out every checkout in flight.
    """
    if not _reserved:
        return
    waiter = f"sub-agent {getattr(info, 'id', '')}"
    try:
        folders = subagent_folders(state, info)
    except LineageUnknown:
        await wait_for_any_checkout(waiter=waiter)
        return
    for folder in folders:
        await wait_for_checkout(folder, waiter=waiter)

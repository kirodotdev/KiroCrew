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
  :func:`wait_for_subagent_checkout` just before it executes.

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
import logging
import os
from collections.abc import Iterable
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
        found.append(str(getattr(slot, "project", "") or ""))
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


def subagent_folders(state: Any, info: Any, by_key: dict[str, Any] | None = None) -> list[str]:
    """Every folder a sub-agent run works in: its own, its ancestors', its root chat's.

    A run without a ``cwd`` override works on its parent's folder by path, and a
    grandchild's parent is another run rather than a chat, so the lineage is
    walked up through the manager's runs to the dashboard session at its top.
    Raises :class:`LineageUnknown` when a ``subagent:`` parent has no record in
    the manager, since the folders it would have contributed cannot be known.
    """
    if by_key is None:
        by_key = runs_by_key(state)
    folders: list[str] = []
    seen: set[int] = set()
    node = info
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        folders.append(str(getattr(node, "cwd", "") or ""))
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
        while _reserved:
            root, event = next(iter(_reserved.items()))
            try:
                await asyncio.wait_for(event.wait(), WAIT_LOG_SECS)
            except asyncio.TimeoutError:
                logger.warning("%s still waiting on a checkout of %s", waiter, root)
        return
    for folder in folders:
        await wait_for_checkout(folder, waiter=waiter)

"""The dashboard's readers for the checkout guard (:mod:`kiro_crew.repo_checkout_guard`).

The registry itself (reservations, the waits, tracked work, prompts in flight)
lives below the dashboard so the code that registers with it -- workflow steps,
the ACP transports -- imports no dashboard code. What stays here reads dashboard
objects: the folders a chat slot's work can touch (:func:`slot_folders`), whether
a turn runs on a slot's session (:func:`slot_turn_running`), and
the folders a sub-agent run works in, walked up its lineage to the chat at its
top (:func:`subagent_folders`, :func:`live_subagent_runs`).
"""

from __future__ import annotations

import re
from typing import Any

from kiro_crew import repo_checkout_guard as _core
from kiro_crew.repo_checkout_guard import wait_for_any_checkout, wait_for_checkout


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
    sessions = getattr(state, "sessions", None)
    if not getattr(slot, "running", False):
        return _idle_slot_folders(slot, sessions, folders)
    folders.extend(str(f or "") for f in getattr(slot, "_checkout_turn_projects", ()) or ())
    placed = not getattr(slot, "_checkout_turn_unplaced", False)
    if sessions is not None:
        from kiro_crew.dashboard.chat_utils import effective_session_key

        try:
            provider = sessions.get_provider(effective_session_key(slot))
            if provider is not None:
                folders.append(str(getattr(provider, "cwd", "") or ""))
        except Exception:  # noqa: BLE001 - an unreadable registry leaves the folder unknown
            placed = False
    return [folder for folder in folders if folder], placed


def slot_turn_running(state: Any, slot: Any) -> bool:
    """Whether a turn runs on ``slot``'s session, from the slot or a linked channel.

    Deliberately wider than ``_ChatSlot.linked_turn_running``, which answers a
    display question: that one reads only a channel session key and fails open,
    so a stale projection at worst shows "interrupted". Here a wrong False lets
    git rewrite files under a live turn, so any key whose lease is held counts
    (a cron or workflow run on the slot's key included) and an unreadable
    registry counts as a running turn.
    """
    if getattr(slot, "running", False):
        return True
    sessions = getattr(state, "sessions", None)
    is_busy = getattr(sessions, "is_busy", None)
    if not callable(is_busy):
        return False
    from kiro_crew.dashboard.chat_utils import effective_session_key

    try:
        return is_busy(effective_session_key(slot)) is True
    except Exception:  # noqa: BLE001 - an unreadable registry is treated as busy
        return True


def _idle_slot_folders(slot: Any, sessions: Any, folders: list[str]) -> tuple[list[str], bool]:
    """An idle slot's folders, unless another surface runs a turn on its session.

    A Slack thread or another channel linked to this chat runs its turns on the
    slot's session key while the slot itself reads idle. Such a turn holds the
    key's turn lease (``SessionManager.is_busy``), so a held lease counts the
    slot as running in the folder its session reports, and as unplaced while
    that session has not reported one yet.
    """
    is_busy = getattr(sessions, "is_busy", None)
    if not callable(is_busy):
        return [folder for folder in folders if folder], True
    from kiro_crew.dashboard.chat_utils import effective_session_key

    try:
        key = effective_session_key(slot)
        if is_busy(key) is not True:
            return [folder for folder in folders if folder], True
        provider = sessions.get_provider(key)
    except Exception:  # noqa: BLE001 - an unreadable registry leaves the folder unknown
        return [folder for folder in folders if folder], False
    session_folder = str(getattr(provider, "cwd", "") or "") if provider is not None else ""
    folders.append(session_folder)
    return [folder for folder in folders if folder], bool(session_folder)


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


#: A sub-agent run id as the manager mints it (lowercase hex). A ``_tasks`` key
#: of this shape, or one ending ``:recovery``, is a run's task; other keys
#: (``reject-<id>``, ``lost-<id>``, ``flush-<id>``) are one-shot announcements.
_RUN_ID_RE = re.compile(r"\A[0-9a-f]{8,}\Z")


def live_subagent_runs(manager: Any) -> tuple[list[Any], bool]:
    """Every sub-agent run whose native work may still be going, and whether all are known.

    ``running`` drops a run the moment ``done`` flips, but the run's ``finally``
    still caps its result and tears its session down after that, and a reap's
    kill can outlive the run task. So a run counts until nothing of it is left:
    not done, its task (or its cancel-recovery task) still live, its teardown
    gate still registered, or its kill handles still retained. An id those
    registries hold with no run record (a dashboard clear popped it
    mid-teardown) cannot be placed, so the second value is False and the caller
    counts every repository busy. A kill that FAILED leaves none of these
    behind: the record and the tombstone name the leaked process, which the
    next start ends; no in-memory signal says when such a process exits, so
    that gap is left open here. A manager
    without ``_agents`` (a test double) is read through ``running``.
    """
    agents = getattr(manager, "_agents", None)
    if not isinstance(agents, dict):
        return list(getattr(manager, "running", None) or []), True
    live_ids: set[str] = set()
    known = True
    for key, task in list((getattr(manager, "_tasks", None) or {}).items()):
        if task.done():
            continue
        run_id = str(key).removesuffix(":recovery")
        if run_id in agents:
            live_ids.add(run_id)
        elif _RUN_ID_RE.match(run_id):
            known = False
    for registry in ("_teardown_gates", "_process_handles"):
        for key in list(getattr(manager, registry, None) or {}):
            if str(key) in agents:
                live_ids.add(str(key))
            else:
                known = False
    runs = [
        run
        for run_id, run in list(agents.items())
        if not getattr(run, "done", False) or run_id in live_ids
    ]
    return runs, known


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


async def wait_for_subagent_checkout(state: Any, info: Any) -> None:
    """Wait out a checkout on any folder the sub-agent run works in.

    A run whose lineage cannot be resolved waits out every checkout in flight.
    """
    if not _core.is_any_reserved():
        return
    waiter = f"sub-agent {getattr(info, 'id', '')}"
    try:
        folders = subagent_folders(state, info)
    except LineageUnknown:
        await wait_for_any_checkout(waiter=waiter)
        return
    for folder in folders:
        await wait_for_checkout(folder, waiter=waiter)

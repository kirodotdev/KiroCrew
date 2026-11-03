"""The goal loop of a doc or board session, armed by the gateway.

A heading in a doc, or a section on a board, can carry a goal (``docGoals.ts``,
``boardModel.ts``). Every goal on a page is checked by ONE loop on the page's
conductor, at the page's one rate. The loop belongs to the session, so it runs with
the page closed; what used to need the page open was arming it. This module does
that on the server: a save of the doc's file (and a read of it, which covers a doc
written before this existed) brings the session's loop in line with the file.

* goals in the file, no loop: arm one and run its first check now;
* a loop whose message or rate differs from the file's: rewrite it;
* a paused loop of ours: revive it and run a check;
* no goals left: delete the loop.

The loop is ungated. A work-ledger watch is gated and skips every tick on which no
worker reported, which would skip the goal checks too; each check reads the ledger
itself instead. Arming it displaces the gateway's default conductor patrol, which
gives way to an explicit arm. A loop the conductor armed itself is left alone and
the arm is refused, so the page's goals then do not run: that is logged, not hidden.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from kiro_crew.dashboard import doc_files

logger = logging.getLogger(__name__)

#: The banner every goal wake shows in the transcript, and how a loop of ours is told
#: from anyone else's. The page tells a goal check from a person's ask by it too.
GOALS_BANNER = "[Goals] Checking the goal sections"

GOAL_INTERVALS = (5, 10, 30, 60)
DEFAULT_GOAL_EVERY = 10

_FRONT = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)
_GOAL_EVERY = re.compile(r"^goal-every:\s*(\d+)\s*$", re.MULTILINE)
# `<!-- goal <id>: text -->`; an older marker had no id, or carried its own `every=N`.
_ANY_MARKER = re.compile(r"^<!-- goal(?: g[a-z0-9]{3,16})?(?: every=\d+)?: \S.* -->$", re.MULTILINE)


@dataclass(frozen=True)
class GoalScan:
    """What a doc's file says about its goals."""

    active: bool
    #: Minutes between checks, for every goal on the page.
    every: int = DEFAULT_GOAL_EVERY


def scan_goals(doc_id: str, content: str) -> GoalScan:
    """The goals a doc or board file holds. Codebase and dashboard pages hold none."""
    kind = doc_files.doc_kind(doc_id)
    if kind == "board":
        try:
            data = json.loads(content)
        except ValueError:
            return GoalScan(False)
        if not isinstance(data, dict):
            return GoalScan(False)
        shapes = data.get("shapes")
        has = isinstance(shapes, list) and any(
            isinstance(s, dict)
            and s.get("kind") == "section"
            and isinstance(s.get("goal"), dict)
            and isinstance(s["goal"].get("text"), str)
            and s["goal"]["text"].strip()
            for s in shapes
        )
        every = data.get("goalEvery")
        return GoalScan(
            bool(has),
            every if every in GOAL_INTERVALS and type(every) is int else DEFAULT_GOAL_EVERY,
        )
    if kind != "doc":
        return GoalScan(False)
    every_value = DEFAULT_GOAL_EVERY
    body = content
    front = _FRONT.match(content)
    if front:
        found = _GOAL_EVERY.search(front.group(1))
        if found and int(found.group(1)) in GOAL_INTERVALS:
            every_value = int(found.group(1))
        body = content[front.end() :]
    return GoalScan(_ANY_MARKER.search(body) is not None, every_value)


def goal_instruction(kind: str, doc_id: str, title: str) -> str:
    """What a goal wake asks of the conductor: short, and enough on its own, since a
    wake carries none of an ask's context."""
    name = title.strip() or ("Untitled doc" if kind == "doc" else "Untitled board")
    file = str(doc_files.doc_path(doc_id))
    if kind == "doc":
        where = (
            f"Read the doc file {file}. Each heading followed by a line "
            "<!-- goal <id>: ... --> has a goal with that id; it covers that heading's "
            "section, down to the next heading of the same or a higher level."
        )
        anchor = 'data-anchor="<the heading\'s exact text>" data-place="margin"'
        write = (
            "Write results only inside that section, with a doc-insert block after an "
            "exact line from inside it."
        )
    else:
        where = (
            f'Read the board file {file} (JSON). Each shape of kind "section" with a '
            '"goal" has one; its id is the section\'s id, and it covers the shapes '
            "inside that section."
        )
        anchor = 'data-anchor="shape:<section id>" data-place="corner"'
        write = (
            'Change only shapes inside that section (board-edit, or board-add with "in": '
            "that section's id)."
        )
    return (
        "<<goals>>\n"
        f'Goal check for the {kind} "{name}". {where} For each goal, look at its section '
        "as it is now and do what the goal asks: start or steer workers for real work "
        "and keep them on your work ledger, never a second worker for something one of "
        f"yours already has. {write} Never touch anything outside a goal's section. Keep "
        f"one item per goal in your status overlay artifact ({anchor}): what it is doing "
        "now, moving while work runs (ov-running) and still once settled.\n"
        "Reply with one block per goal, each opened by a line of its own, [goal <id>], "
        "then a short report of what you found and did. When there is nothing to report, "
        'the block is exactly "Nothing new." and nothing else. The person reads each '
        "block on that goal's pin, so write it for them.\n"
        "Also read your work ledger and act on what your workers reported since the last "
        "check. This loop is what wakes you while the page has goals, so arm or change no "
        "loop of your own (no monitor_start, no monitor_update).\n"
        "<</goals>>"
    )


def is_goal_loop(loop: Any) -> bool:
    """Whether *loop* is the one this module owns: ungated, unstructured, our banner."""
    return (
        loop is not None
        and not getattr(loop, "gate", False)
        and getattr(loop, "monitor", None) is None
        and getattr(loop, "banner", "") == GOALS_BANNER
    )


# ---- the three operations on a loop, as seams ---------------------------------------


async def _arm(
    svc: Any, state: Any, slot_key: str, message: str, every: int
) -> tuple[Any, str | None]:
    from kiro_crew.autonudge_authz import authorize_and_add_nudge

    loop, error, _status = await authorize_and_add_nudge(
        svc=svc,
        state=state,
        slot_key=slot_key,
        message=message,
        idle_secs=every * 60,
        max_cycles=0,
        max_runtime_secs=0,
        banner=GOALS_BANNER,
        source="dashboard",
        caller="doc-goals",
        gate=False,
        replace_existing=False,
    )
    return loop, error


async def _patch(svc: Any, loop_id: str, message: str, every: int, revive: bool) -> str | None:
    from kiro_crew.autonudge_authz import authorize_and_update_nudge

    _loop, error, _status = await authorize_and_update_nudge(
        svc=svc,
        loop_id=loop_id,
        message=message,
        idle_secs=every * 60,
        active=True if revive else None,
        fresh_run=revive,
        source="dashboard",
        caller="doc-goals",
    )
    return error


async def _disarm(svc: Any, loop: Any) -> str | None:
    from kiro_crew.autonudge import NudgeAdmissionRefused
    from kiro_crew.sel import sel

    try:
        await svc.remove(loop.id, stop_reason="doc_goals_cleared")
    except NudgeAdmissionRefused as exc:
        return str(exc)
    try:
        await asyncio.to_thread(
            lambda: sel().log_tool_invocation(
                session_key=loop.slot_key,
                source="dashboard",
                tool_name="autonudge_delete",
                outcome="success",
                metadata={"loop_id": loop.id, "caller": "doc-goals"},
            )
        )
    except Exception:  # noqa: BLE001 - the audit follows the delete; it must not undo it
        logger.warning("doc goals: delete audit failed", exc_info=True)
    return None


async def _fire(svc: Any, loop_id: str) -> None:
    """Run the first check now. A turn already in flight refuses it, and the loop's own
    timer covers that."""
    _loop, error, _status = await svc.fire_now(loop_id)
    if error:
        logger.info("doc goals: first check not run now (%s)", error)


# ---- sync ----------------------------------------------------------------------------

_LOCKS: dict[str, asyncio.Lock] = {}
#: What each slot's loop was last brought to, so a save that changes nothing about the
#: goals does not rewrite the loop (each rewrite is a new config generation).
_APPLIED: dict[str, tuple[str, str, int]] = {}


def _slot_for_doc(state: Any, doc_id: str) -> Any | None:
    for slot in list(getattr(state, "_slots", {}).values()):
        if getattr(slot, "_doc", "") == doc_id:
            return slot
    return None


async def sync_goal_loop(state: Any, doc_id: str, content: str) -> str:
    """Bring the doc session's loop in line with *content*; returns what was done:
    ``armed``, ``updated``, ``removed``, ``unchanged``, or ``refused: <why>``."""
    from kiro_crew.autonudge import get_instance

    kind = doc_files.doc_kind(doc_id)
    if kind not in ("doc", "board"):
        return "unchanged"
    svc = get_instance()
    slot = _slot_for_doc(state, doc_id)
    if svc is None or slot is None:
        return "unchanged"
    key = slot.key
    scan = scan_goals(doc_id, content)
    async with _LOCKS.setdefault(key, asyncio.Lock()):
        loop = svc.get_by_slot(key)
        ours = is_goal_loop(loop)
        if not scan.active:
            if not ours:
                return "unchanged"
            error = await _disarm(svc, loop)
            _APPLIED.pop(key, None)
            return f"refused: {error}" if error else "removed"
        message = goal_instruction(kind, doc_id, getattr(slot, "title", "") or "")
        if loop is None or not ours:
            made, error = await _arm(svc, state, key, message, scan.every)
            if error:
                logger.warning("doc goals: could not arm the loop for %s: %s", key, error)
                return f"refused: {error}"
            _APPLIED[key] = (made.id, message, scan.every)
            await _fire(svc, made.id)
            return "armed"
        revive = not getattr(loop, "active", True)
        if not revive and _APPLIED.get(key) == (loop.id, message, scan.every):
            return "unchanged"
        error = await _patch(svc, loop.id, message, scan.every, revive)
        if error:
            logger.warning("doc goals: could not update the loop for %s: %s", key, error)
            return f"refused: {error}"
        _APPLIED[key] = (loop.id, message, scan.every)
        if revive:
            await _fire(svc, loop.id)
        return "updated"


def schedule_goal_sync(state: Any, doc_id: str, content: str) -> None:
    """Run :func:`sync_goal_loop` off the request, so a save is never slower for it.
    A failure is logged; the next save tries again."""
    if state is None or doc_files.doc_kind(doc_id) not in ("doc", "board"):
        return

    async def _run() -> None:
        try:
            await sync_goal_loop(state, doc_id, content)
        except Exception:  # noqa: BLE001 - background work: log it, never raise into the loop
            logger.warning("doc goals: sync failed for %s", doc_id, exc_info=True)

    task = asyncio.get_running_loop().create_task(_run())
    tasks = getattr(state, "_background_tasks", None)
    if isinstance(tasks, set):
        tasks.add(task)
        task.add_done_callback(tasks.discard)

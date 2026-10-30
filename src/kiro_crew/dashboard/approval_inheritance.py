"""A creator's later approval-mode change reaches the sessions it created.

``session_create`` and ``session_fork`` copy the creator's posture (``_trust`` /
``_trust_reads``) once, at birth. A conductor a person turns to Trust after it
dispatched its workers must not leave each worker asking on prompts the conductor
itself would auto-approve. This walks the creation tree below a slot whose own mode
just changed and works out what each descendant should hold.

The rules:

* Only a descendant still holding INHERITED posture (``_posture_inherited``) follows.
  A person who set a worker's own mode made an explicit choice, and the creator's
  change leaves it alone; that worker's own descendants follow it, not the root.
* A descendant never ends above its creator: it takes exactly the creator's carried
  posture (``trust_carries``), so a card-scoped trust on the creator hands down
  nothing more than ``_trust_reads``.
* Raising needs the child to be the only slot on its session, so every slot that
  addresses a session agrees on its posture.
* Raising follows only a gateway-authored creation edge: ``_lineage_minted`` with a
  live ``_created_by`` set at mint, or the crew log's creation record
  (``creator_of``). ``created_by`` alone is read back from an agent-editable file
  after a restart, so a forged line must not be a way to collect trust. Lowering
  follows any recorded edge, since asking more often is the safe direction.
* The walk stops at a descendant it does not change for that reason (an explicit
  mode, or a raise it may not make): nothing below such a node moves.
* A crewmate's own worker (``kirocrew-mate-worker``) is skipped: its mode comes from
  the crewmate's Profile Permissions (``crewmate_approval``), on every path.

Pure: it reads slots and returns the plan; the caller writes it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from kiro_crew.dashboard.approval_posture import trust_carries

Posture = Literal["trust", "trust_reads", "normal"]

_RANK: dict[str, int] = {"normal": 0, "trust_reads": 1, "trust": 2}


@dataclass(frozen=True)
class PostureChange:
    """One descendant and the posture it should now hold."""

    slot: Any
    posture: Posture


def carried_posture(slot: Any) -> Posture:
    """The posture *slot* hands to the sessions it creates."""
    if trust_carries(slot):
        return "trust"
    if getattr(slot, "_trust_reads", False):
        return "trust_reads"
    return "normal"


def held_posture(slot: Any) -> Posture:
    """The posture *slot* holds for its own tools (a card-scoped trust counts)."""
    if getattr(slot, "_trust", False):
        return "trust"
    if getattr(slot, "_trust_reads", False):
        return "trust_reads"
    return "normal"


def inherit_posture(child: Any, creator: Any) -> None:
    """Give a just-made *child* what *creator* carries, marked as inherited."""
    child._trust = trust_carries(creator)
    child._trust_card_scoped = False
    child._trust_reads = bool(getattr(creator, "_trust_reads", False))
    child._posture_inherited = True


def _verified_child(child: Any, parent_key: str) -> bool:
    """Whether a gateway-authored record says *parent_key* created *child*.

    The record names the slot, so it vouches only for the session the slot's own
    key addresses. A ``linked_session_key`` points the slot's turns at another
    session, and that link is read back from an editable transcript after a
    restart, so a linked child is never raised.
    """
    if getattr(child, "linked_session_key", ""):
        return False
    if getattr(child, "_lineage_minted", False) and child._created_by == parent_key:
        return True
    try:
        from kiro_crew.crew_log.session_tree_projection import creator_of

        return creator_of(str(child.key)) == parent_key
    except Exception:  # noqa: BLE001 - an unreadable tree verifies nothing
        return False


def is_profile_worker(slot: Any) -> bool:
    """Whether *slot* is a crewmate's own worker, whose mode its Profile decides."""
    from kiro_crew.mate_worker import MATE_WORKER_AGENT

    return str(getattr(slot, "agent", "") or "") == MATE_WORKER_AGENT


def plan_descendant_postures(
    slots: Iterable[Any], root: Any, *, may_raise: bool
) -> list[PostureChange]:
    """What each inheriting descendant of *root* should hold after *root*'s change.

    *root* already carries its new mode. *may_raise* is False for a caller that may
    set only *root* itself (an app or a cron); then the plan only ever lowers.
    """
    live = [s for s in slots if s is not root]
    by_creator: dict[str, list[Any]] = {}
    for s in live:
        creator = str(getattr(s, "_created_by", "") or "")
        if creator:
            by_creator.setdefault(creator, []).append(s)
    from kiro_crew.dashboard.chat_utils_slots import effective_session_key

    sessions: dict[str, int] = {}
    for s in [root, *live]:
        key = effective_session_key(s)
        sessions[key] = sessions.get(key, 0) + 1
    plan: list[PostureChange] = []
    seen: set[int] = {id(root)}
    # (creator key, the posture that creator now carries)
    frontier: list[tuple[str, Posture]] = [(str(root.key), carried_posture(root))]
    while frontier:
        parent_key, target = frontier.pop()
        for child in by_creator.get(parent_key, []):
            if id(child) in seen or is_profile_worker(child):
                continue
            seen.add(id(child))
            current = held_posture(child)
            if not getattr(child, "_posture_inherited", False):
                # An explicit choice: this child and everything below it stay. Its
                # children follow it when ITS mode changes, never past it from here,
                # so a walk from above cannot re-hand a grant someone else set on it.
                continue
            raising = _RANK[target] > _RANK[current]
            # A raise also needs the child to be the only slot on its session: the
            # session's stored policy is written once per sharing slot, and a slot
            # that shares it through a link vouches for nothing.
            shared = sessions.get(effective_session_key(child), 0) > 1
            if raising and (shared or not (may_raise and _verified_child(child, parent_key))):
                # Not raised, so nothing below it is raised by this walk either.
                continue
            if target != current:
                plan.append(PostureChange(child, target))
            # An inherited child carries what it was just given.
            frontier.append((str(child.key), target))
    return plan

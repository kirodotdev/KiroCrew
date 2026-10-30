"""Which revocations withdraw a running sub-agent's ``parent_policy=auto`` grant.

A child reads its parent's stored ``approval_policy`` once, when its run
starts. When that value is ``"auto"``, the run records the parent's session key
with the parent's revocation count at that moment (the store bumps the count
whenever a key's policy leaves ``"auto"``). A child of a child also inherits
its parent's record, because the root's revocation clears only the root's
stored value and never the ``"auto"`` the child's own session was created with.

On each tool request the grant re-reads every recorded count. Any count that
moved, or that cannot be read, withdraws the grant.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

#: ``(session key, revocation count)`` pairs, root first.
ApprovalLineage = tuple[tuple[str, int], ...]

_SUBAGENT_PREFIX = "subagent:"


def _generation(sessions: Any, key: str) -> int | None:
    read = getattr(sessions, "approval_revocation_generation", None)
    if read is None:
        return None
    try:
        value = read(key)
    except Exception:
        return None
    # ``bool`` is an ``int``; neither it nor a test double's return is a count.
    return value if type(value) is int else None


def approval_lineage(manager: Any, info: Any, parent_policy: str) -> ApprovalLineage:
    """The record a run whose stored parent policy is *parent_policy* starts with.

    Empty when the stored value is not ``"auto"`` (an ``"auto"`` that came from
    a fallback such as yolo or the config default is not a parent's trust) or
    when the store keeps no revocation count.
    """
    parent_key = info.parent_session_key
    if parent_policy != "auto" or not parent_key:
        return ()
    generation = _generation(manager._sessions, parent_key)
    if generation is None:
        return ()
    inherited: ApprovalLineage = ()
    if parent_key.startswith(_SUBAGENT_PREFIX):
        parent = _parent_run(manager, parent_key)
        if parent is None:
            # No run owns the parent conversation, so its ancestry cannot be
            # read: a count no store ever holds withdraws the grant at once.
            return ((parent_key, -1),)
        inherited = getattr(parent, "_approval_lineage", ())
    return (*inherited, (parent_key, generation))


def _parent_run(manager: Any, parent_key: str) -> Any:
    """The run executing on conversation *parent_key*.

    A ``spawn_continue`` run has its own id but runs on the original run's
    conversation key, and the original record may be evicted or may still be
    there. So every run on the key is a candidate: a live one first, then the
    newest, since the newest run is the one whose turn queued the spawn even
    when that turn ended before the child started.
    """
    runs = [
        run
        for run in list(manager._agents.values())
        if (run.conversation_key or f"{_SUBAGENT_PREFIX}{run.id}") == parent_key
    ]
    if not runs:
        return None
    return max(runs, key=lambda run: (not run.done, run.started))


def lineage_revoked(sessions: Any, lineage: ApprovalLineage) -> Callable[[], bool] | None:
    """A check for ``ParentPolicyAuto``: true once any recorded count has moved."""
    if not lineage:
        return None

    def _revoked() -> bool:
        return any(_generation(sessions, key) != seen for key, seen in lineage)

    return _revoked

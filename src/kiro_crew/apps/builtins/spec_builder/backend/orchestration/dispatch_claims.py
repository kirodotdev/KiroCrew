"""Process-owned generations that authorize a turn to publish.

Every dispatch passes its identity checks, awaits, and only then publishes a
task. Between those points the agent-writable index can move the directory and
slot of a creation, so ownership is held here, in memory on the event loop,
rather than in anything the agent can rewrite. Stop and Delete revoke matching
generations through ``_execution_stop_barrier`` before they wait for the
directory lock, and a revocation stays provisional until they commit.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from ..parsers import _decision_key
from ..repository import (
    _mutate_index,
    _observed_slot_keys_for_dir,
    _unindexed_observed_slot_keys,
)
from .execution_state import (
    _exec_loop_active_for_slot,
    _exec_loop_id_for_slot,
    _matching_execution_loops,
    _remove_nudge_loop_for_slot,
)

logger = logging.getLogger("kirocrew.app.spec-builder")

#: Process-owned ownership for handoffs from their durable ``executing`` claim
#: through the published turn. The index is agent-writable, so neither its status
#: nor its timestamps can authenticate which request owns that generation. These
#: registries are touched only on the gateway event loop.
_EXECUTION_CLAIMS: dict[str, tuple[str, str, str, asyncio.Task[Any] | None, Any | None]] = {}

_EXECUTION_STOPS: dict[str, int] = {}

_REVOKED_EXECUTION_CLAIMS: set[str] = set()

_REVOKED_PENDING_DISPATCH_CLAIMS: set[str] = set()

_STOP_ROLLBACK_TASKS: set[asyncio.Task[Any]] = set()

#: Short-lived ownership for every other turn that has passed its initial identity
#: check but has not published its task yet. Pending and execution claims exclude any
#: matching directory, slot, or name because the agent-writable index can move all but
#: the name while a final off-thread scan is running. Ownership transfers from the
#: request task to the published slot turn and survives queued successors until idle.
_PENDING_DISPATCH_CLAIMS: dict[str, tuple[str, str, str, asyncio.Task[Any] | None, Any | None]] = {}


def _prune_finished_pending_dispatch_claims() -> None:
    """Remove abandoned ordinary claims and follow a live queued successor."""
    for key, claim in list(_PENDING_DISPATCH_CLAIMS.items()):
        if key in _REVOKED_PENDING_DISPATCH_CLAIMS:
            continue
        owner, published_slot = claim[3], claim[4]
        if owner is None or not owner.done():
            continue
        if published_slot is not None:
            successor = getattr(published_slot, "task", None)
            if successor is not None and successor is not owner and not successor.done():
                _bind_pending_dispatch_to_turn(key, published_slot, successor)
                continue
        _PENDING_DISPATCH_CLAIMS.pop(key, None)


def _prune_finished_dispatch_claims() -> None:
    """Remove abandoned ordinary and autonomous dispatch claims."""
    _prune_finished_pending_dispatch_claims()
    for key, claim in list(_EXECUTION_CLAIMS.items()):
        token, slot_key, _name, owner, published_slot = claim
        # Stop/Delete owns the disposition of a provisionally revoked claim.
        # A done callback or conflict check running while teardown awaits must
        # not turn a rollback-capable revocation into a permanent one.
        if token in _REVOKED_EXECUTION_CLAIMS:
            continue
        if owner is None or not owner.done():
            continue
        if published_slot is None:
            _EXECUTION_CLAIMS.pop(key, None)
            continue
        slot_task = getattr(published_slot, "task", None)
        if (
            bool(getattr(published_slot, "running", False))
            or (slot_task is not None and not slot_task.done())
            or _exec_loop_active_for_slot(slot_key)
        ):
            continue
        _EXECUTION_CLAIMS.pop(key, None)


def _dispatch_claim_conflicts(
    dir_key: str,
    slot_key: str,
    name: str,
    *,
    allow_published_exact: bool = False,
) -> bool:
    """Whether another generation owns any stable view of this creation."""
    _prune_finished_dispatch_claims()
    normalized_dir = _decision_key(dir_key)

    # Process claims disappear on restart, but autonomous loops do not. Treat the
    # durable loop as the same exclusive generation so a restored idle timer cannot
    # race a new message or handoff over the same name, directory, or slot.
    if _matching_execution_loops(name, normalized_dir, {slot_key}, include_orphans=True):
        return True

    def _conflicts(
        existing_dir: str,
        existing_slot_key: str,
        existing_name: str,
        published_slot: Any | None,
    ) -> bool:
        overlaps = (
            existing_dir == normalized_dir
            or (bool(slot_key) and existing_slot_key == slot_key)
            or existing_name == name
        )
        exact = (
            existing_dir == normalized_dir
            and existing_slot_key == slot_key
            and existing_name == name
        )
        return overlaps and not (allow_published_exact and published_slot is not None and exact)

    return any(
        _conflicts(existing_dir, existing_slot_key, existing_name, published_slot)
        for existing_dir, existing_slot_key, existing_name, _owner, published_slot in (
            _PENDING_DISPATCH_CLAIMS.values()
        )
    ) or any(
        _conflicts(existing_dir, existing_slot_key, existing_name, published_slot)
        for existing_dir, (
            _token,
            existing_slot_key,
            existing_name,
            _owner,
            published_slot,
        ) in _EXECUTION_CLAIMS.items()
    )


def _reserve_pending_dispatch(dir_key: str, slot_key: str, name: str) -> str:
    """Return an exclusive revocable pre-publication token, or ``""`` if busy."""
    if _EXECUTION_STOPS.get(name, 0):
        return ""
    if _dispatch_claim_conflicts(dir_key, slot_key, name, allow_published_exact=True):
        return ""
    token = uuid.uuid4().hex
    _PENDING_DISPATCH_CLAIMS[token] = (
        _decision_key(dir_key),
        slot_key,
        name,
        asyncio.current_task(),
        None,
    )
    return token


def _pending_dispatch_is_current(token: str) -> bool:
    """Whether *token* still owns permission to publish its turn."""
    return (
        bool(token)
        and token not in _REVOKED_PENDING_DISPATCH_CLAIMS
        and token in _PENDING_DISPATCH_CLAIMS
    )


def _drop_pending_dispatch(token: str) -> None:
    """Release one pre-publication token without affecting a newer request."""
    _PENDING_DISPATCH_CLAIMS.pop(token, None)


def _drop_pending_dispatch_if_owner(token: str, owner: asyncio.Task[Any]) -> None:
    """Release a token only while *owner* still owns its current generation."""
    if token in _REVOKED_PENDING_DISPATCH_CLAIMS:
        return
    current = _PENDING_DISPATCH_CLAIMS.get(token)
    if current is not None and current[3] is owner:
        _PENDING_DISPATCH_CLAIMS.pop(token, None)


def _release_pending_dispatch_when_done(token: str) -> None:
    """Bound a token to the current request task as a defensive cleanup floor."""
    task = asyncio.current_task()
    if task is not None:
        task.add_done_callback(lambda done: _drop_pending_dispatch_if_owner(token, done))


def _bind_pending_dispatch_to_turn(token: str, slot: Any, turn: asyncio.Task[Any] | None) -> None:
    """Keep a published creation claim until its slot becomes idle."""
    owner = turn or getattr(slot, "task", None)
    current = _PENDING_DISPATCH_CLAIMS.get(token)
    if current is None or owner is None:
        _drop_pending_dispatch(token)
        return
    dir_key, slot_key, name, _old_owner, _old_slot = current
    _PENDING_DISPATCH_CLAIMS[token] = (dir_key, slot_key, name, owner, slot)

    def _release_or_follow(done: asyncio.Task[Any]) -> None:
        current_claim = _PENDING_DISPATCH_CLAIMS.get(token)
        if current_claim is None or current_claim[3] is not done:
            return
        successor = getattr(slot, "task", None)
        if successor is not None and successor is not done and not successor.done():
            _bind_pending_dispatch_to_turn(token, slot, successor)
            return
        _drop_pending_dispatch_if_owner(token, done)

    owner.add_done_callback(_release_or_follow)


def _reserve_execution_claim(dir_key: str, slot_key: str, name: str) -> tuple[str, str]:
    """Reserve one process-owned handoff generation, or return its refusal reason."""
    if _EXECUTION_STOPS.get(name, 0):
        return "", "stopping"
    normalized_dir = _decision_key(dir_key)
    if _dispatch_claim_conflicts(normalized_dir, slot_key, name):
        return "", "taken"
    token = uuid.uuid4().hex
    _EXECUTION_CLAIMS[normalized_dir] = (
        token,
        slot_key,
        name,
        asyncio.current_task(),
        None,
    )
    return token, ""


def _execution_claim_is_current(dir_key: str, token: str) -> bool:
    """Whether *token* still owns this directory's pre-dispatch handoff."""
    current = _EXECUTION_CLAIMS.get(_decision_key(dir_key))
    return (
        bool(token)
        and token not in _REVOKED_EXECUTION_CLAIMS
        and current is not None
        and current[0] == token
    )


def _drop_execution_claim(dir_key: str, token: str) -> bool:
    """Release only the generation owned by this request."""
    if not _execution_claim_is_current(dir_key, token):
        return False
    _EXECUTION_CLAIMS.pop(_decision_key(dir_key), None)
    return True


def _drop_execution_claim_if_owner(dir_key: str, token: str, owner: asyncio.Task[Any]) -> None:
    """Release an execution claim only before ownership transfers to its turn."""
    if token in _REVOKED_EXECUTION_CLAIMS:
        return
    current = _EXECUTION_CLAIMS.get(_decision_key(dir_key))
    if current is not None and current[0] == token and current[3] is owner:
        _EXECUTION_CLAIMS.pop(_decision_key(dir_key), None)


def _bind_execution_claim_to_turn(
    dir_key: str, token: str, slot: Any, turn: asyncio.Task[Any] | None
) -> None:
    """Keep a handoff claim while its turn chain or autonomous loop is live."""
    normalized_dir = _decision_key(dir_key)
    current = _EXECUTION_CLAIMS.get(normalized_dir)
    owner = turn or getattr(slot, "task", None)
    if current is None or current[0] != token or owner is None:
        _drop_execution_claim(normalized_dir, token)
        return
    _token, slot_key, name, _old_owner, _old_slot = current
    _EXECUTION_CLAIMS[normalized_dir] = (token, slot_key, name, owner, slot)

    def _release_or_follow(done: asyncio.Task[Any]) -> None:
        live = _EXECUTION_CLAIMS.get(normalized_dir)
        if live is None or live[0] != token or live[3] is not done:
            return
        successor = getattr(slot, "task", None)
        if successor is not None and successor is not done and not successor.done():
            _bind_execution_claim_to_turn(normalized_dir, token, slot, successor)
            return
        if _exec_loop_active_for_slot(slot_key):
            # Auto-nudge loops are deliberately idle between cycles. The finished
            # turn remains the claim owner until a later conflict check observes
            # both the loop and slot idle, or Stop/Delete revokes the generation.
            return
        _drop_execution_claim_if_owner(normalized_dir, token, done)

    owner.add_done_callback(_release_or_follow)


class _ExecutionStopCapture(dict[str, str | None]):
    """Runtime identities revoked by a Stop/Delete until teardown commits."""

    def __init__(
        self,
        slots: dict[str, str | None],
    ) -> None:
        super().__init__(slots)
        self.committed = False

    def commit(self) -> None:
        self.committed = True

    def runtime_slots(self, state: Any, own_slot: Any) -> list[Any]:
        """Every live slot this revocation captured, then *own_slot*, each once."""
        slots: list[Any] = []
        if state is not None:
            for slot_key in self:
                slot = state.get_slot(slot_key)
                if slot is not None and slot not in slots:
                    slots.append(slot)
        if own_slot is not None and own_slot not in slots:
            slots.append(own_slot)
        return slots

    async def remove_other_loops(
        self, own_slot_key: str, own_loop_id: str | None, *, stop_reason: str = ""
    ) -> None:
        """Remove each captured loop except the one the caller already removed.

        Failures propagate: a loop that survives a reported Stop or Delete can still
        nudge the creation it was captured for.

        ``stop_reason`` names the caller's teardown in each loop's stop log line.
        """
        for slot_key, loop_id in self.items():
            if slot_key == own_slot_key and loop_id == own_loop_id:
                continue
            await _remove_nudge_loop_for_slot(
                slot_key, only_loop_id=loop_id, stop_reason=stop_reason
            )


async def _settle_rolled_back_execution_claim(
    claim_dir: str,
    claim: tuple[str, str, str, asyncio.Task[Any] | None, Any | None],
) -> None:
    """Repair a handoff that unwound while a later teardown rolled back."""
    token, slot_key, name, owner, published_slot = claim
    if owner is not None:
        try:
            await owner
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    current = _EXECUTION_CLAIMS.get(claim_dir)
    if current is None or current[0] != token:
        return
    slot_task = getattr(published_slot, "task", None)
    if (
        bool(getattr(published_slot, "running", False))
        or (slot_task is not None and not slot_task.done())
        or _exec_loop_active_for_slot(slot_key)
    ):
        return

    def _settle(index: dict) -> bool:
        meta = index.get(name)
        if (
            meta is None
            or str(meta.get("slot_key", "")) != slot_key
            or _decision_key(str(meta.get("spec_dir", ""))) != claim_dir
            or str(meta.get("status", "")) != "executing"
        ):
            return False
        meta["status"] = "planning"
        meta["exec_started_at"] = 0.0
        meta["exec_arming_at"] = 0.0
        meta["updated_at"] = time.time()
        return True

    try:
        await _mutate_index(_settle)
    except Exception:
        logger.warning("could not settle a handoff after Stop rollback", exc_info=True)
        return
    current = _EXECUTION_CLAIMS.get(claim_dir)
    if current is not None and current[0] == token:
        _EXECUTION_CLAIMS.pop(claim_dir, None)


def _watch_rolled_back_execution_claim(
    claim_dir: str,
    claim: tuple[str, str, str, asyncio.Task[Any] | None, Any | None],
) -> None:
    if claim[3] is asyncio.current_task():
        return
    task = asyncio.create_task(_settle_rolled_back_execution_claim(claim_dir, claim))
    _STOP_ROLLBACK_TASKS.add(task)
    task.add_done_callback(_STOP_ROLLBACK_TASKS.discard)


@asynccontextmanager
async def _execution_stop_barrier(
    dir_key: str, slot_key: str, name: str
) -> AsyncIterator[_ExecutionStopCapture]:
    """Revoke this creation's handoff and refuse restarts until Stop completes."""
    _EXECUTION_STOPS[name] = _EXECUTION_STOPS.get(name, 0) + 1
    claimed_slots: dict[str, str | None] = {}
    claimed_executions: dict[str, tuple[str, str, str, asyncio.Task[Any] | None, Any | None]] = {}
    claimed_pending: dict[str, tuple[str, str, str, asyncio.Task[Any] | None, Any | None]] = {}
    normalized_dir = _decision_key(dir_key)
    # The directory spelling is agent-writable. Revoke by the immutable creation
    # identity and verified name so rewriting A to B cannot move Stop onto a
    # different claim key. The pre-barrier client check keeps stale Stops out.
    for claim_dir, claim in list(_EXECUTION_CLAIMS.items()):
        token, claim_slot_key, claim_name, _owner, _slot = claim
        if claim_dir == normalized_dir or claim_slot_key == slot_key or claim_name == name:
            claimed_slots[claim_slot_key] = _exec_loop_id_for_slot(claim_slot_key)
            claimed_executions[claim_dir] = claim
            _REVOKED_EXECUTION_CLAIMS.add(token)
    for token, (claim_dir, claim_slot_key, claim_name, _owner, _slot) in list(
        _PENDING_DISPATCH_CLAIMS.items()
    ):
        if claim_dir == normalized_dir or claim_slot_key == slot_key or claim_name == name:
            claimed_slots.setdefault(claim_slot_key, _exec_loop_id_for_slot(claim_slot_key))
            claimed_pending[token] = _PENDING_DISPATCH_CLAIMS[token]
            _REVOKED_PENDING_DISPATCH_CLAIMS.add(token)
    for observed_slot_key in _observed_slot_keys_for_dir(normalized_dir):
        claimed_slots.setdefault(observed_slot_key, _exec_loop_id_for_slot(observed_slot_key))
    # A direct embedded-chat turn has no Spec Builder dispatch claim. If the
    # agent rewrites name, directory, and slot together, its monotonic creation
    # witness is the only remaining way to reach that worker. Such an unindexed
    # creation has no control endpoint of its own, so any authenticated teardown
    # also cleans it up rather than reporting success while it keeps editing.
    for orphaned_slot_key in _unindexed_observed_slot_keys():
        claimed_slots.setdefault(orphaned_slot_key, _exec_loop_id_for_slot(orphaned_slot_key))
    claimed_slots.update(
        _matching_execution_loops(
            name,
            normalized_dir,
            {slot_key, *claimed_slots.keys()},
            include_orphans=True,
            include_inactive_direct=True,
        )
    )
    capture = _ExecutionStopCapture(claimed_slots)
    try:
        yield capture
    finally:
        if capture.committed:
            for claim_dir, claim in claimed_executions.items():
                current = _EXECUTION_CLAIMS.get(claim_dir)
                if current is not None and current[0] == claim[0]:
                    _EXECUTION_CLAIMS.pop(claim_dir, None)
            for token in claimed_pending:
                _PENDING_DISPATCH_CLAIMS.pop(token, None)
        for claim in claimed_executions.values():
            _REVOKED_EXECUTION_CLAIMS.discard(claim[0])
        if not capture.committed:
            for claim_dir, claim in claimed_executions.items():
                _watch_rolled_back_execution_claim(claim_dir, claim)
        for token in claimed_pending:
            _REVOKED_PENDING_DISPATCH_CLAIMS.discard(token)
        if not capture.committed:
            # A published turn can finish while its callback is deliberately
            # suppressed by provisional revocation. Once rollback restores the
            # claim, reconcile that missed edge immediately so a completed turn
            # cannot retain the directory forever.
            _prune_finished_pending_dispatch_claims()
        remaining = _EXECUTION_STOPS.get(name, 1) - 1
        if remaining > 0:
            _EXECUTION_STOPS[name] = remaining
        else:
            _EXECUTION_STOPS.pop(name, None)

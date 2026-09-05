"""Spec-agent execution and turn lifecycle services."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async

from . import repository as _repository
from .decisions import (
    _CLAIM_TAKEN,
    _abandon_pending_decision,
    _finalize_decision,
    _mark_decision_relayed,
    _pending_decision_is_current,
    _pending_decisions,
    _restore_decision_pending,
)
from .parsers import (
    _SLOT_KEY_RE,
    _decision_key,
    _known_status,
    _owns_slot_key,
    _redact,
    _redact_and_truncate,
    _same_spec_dir,
    _usable_name,
)
from .repository import (
    _DELETING,
    _DUPLICATING,
    _INDEX_LOCK,
    APP_NAME,
    _aload_index,
    _audit,
    _forget_observed_slot_identity,
    _load_index,
    _load_settings,
    _mutate_index,
    _observed_slot_keys_for_dir,
    _pin_legacy_slot_identity,
    _safe_dir,
    _slot_key,
    _touch_spec,
    _unindexed_observed_slot_keys,
    _write_stop_sentinel_for_spec,
)

try:
    from kiro_crew.constants import CHAT_TURN_TIMEOUT
except Exception:  # pragma: no cover - constant always present in prod
    CHAT_TURN_TIMEOUT = 1800  # type: ignore[assignment]

try:
    from kiro_crew.autonudge import AutoNudgeService as _AutoNudgeService
    from kiro_crew.autonudge import get_instance as _autonudge_instance
    from kiro_crew.autonudge_authz import authorize_and_add_nudge
except Exception:  # pragma: no cover - autonudge always present in prod
    _AutoNudgeService = None  # type: ignore[assignment,misc]
    _autonudge_instance = None  # type: ignore[assignment]
    authorize_and_add_nudge = None  # type: ignore[assignment]

# dashboard.server imports builtin route modules during startup. These imports
# stay deferred inside dispatch, transcript, and teardown helpers to avoid
# closing that import cycle.

logger = logging.getLogger("kirocrew.app.spec-builder")
# The autonomous nudge loop is capped rather than infinite. There is no trust
# TTL any more because this app no longer grants trust — see the create handler.
_EXEC_MAX_CYCLES = 60

# Bound recovery on a fire callback whose history storage or provider ignores
# cancellation.  The inactive durable loop remains the retry marker on timeout.
_ORPHAN_QUIESCE_TIMEOUT_SECS = 2.0

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


async def _restore_worker_transcript(state: Any, name: str, *, adopt_closed: bool) -> None:
    """Bring this spec's persisted conversation back into a cold worker slot.

    Slots are in-memory: a gateway restart or idle cleanup drops the worker's chat
    while its transcript remains on disk. Read endpoints must rehydrate before
    materializing an empty slot because core's resume returns early once a slot
    exists.

    ``adopt_closed`` is the CALLER's decision, not a constant. For a spec already
    in the index it is True: the worker is not a tab the user closed, its lifecycle
    belongs to the spec, and idle-slot cleanup marks it closed on idleness alone.
    For a spec being CREATED it must be False -- a delete leaves the archived
    transcript on disk under a key derived from the name, so creating a new spec
    with a previously used name would hand the fresh agent the deleted spec's
    conversation.

    Best-effort by design. A missing, malformed or foreign transcript must leave
    the app working: the caller falls through to creating a fresh slot, and the
    ownership check it applies afterwards is what keeps a foreign transcript from
    being adopted.
    """
    try:
        restored = await rehydrate_slot_from_history_async(
            state, _slot_key(name), adopt_closed=adopt_closed
        )
    except Exception:
        logger.warning("spec %s: restoring the worker transcript failed", name, exc_info=True)
        return
    if restored is not None:
        _audit("spec_transcript_restored", name)


def _slot_identity_moved(name: str, slot_key: str) -> bool:
    """True when ``name`` no longer resolves to the key this request captured.

    ``_slot_key`` reads the module-global ``_SLOT_KEYS``, which a delete +
    same-name recreate rewrites to a fresh per-creation key. Any resolution taken
    AFTER an await can therefore name a different spec than the one the request
    began with, so the captured key is the identity and this is the check that it
    still holds. A moved mapping means our spec was replaced while we waited: the
    request must touch nothing rather than adopt the replacement's slot and stamp
    its own project onto it.
    """
    if _slot_key(name) == slot_key:
        return False
    _audit("spec_slot_replaced_midflight", name, outcome="denied")
    logger.warning(
        "spec %s was replaced while its slot was being acquired — refusing the stale request",
        name,
    )
    return True


async def _ensure_worker_slot(
    state: Any, name: str, meta: dict, *, adopt_closed: bool = True
) -> Any:
    """Materialize this spec's worker slot, SCOPED, and return it.

    The single place a spec slot comes into existence. It exists because
    ``get_or_create_slot`` only stamps ``app`` on NEWLY created slots, and
    because a slot created by any OTHER path is unscoped: a spec discovered on
    disk (created by the Kiro CLI/IDE) has no slot until something makes one,
    and if the embedded chat's ``POST /api/chat`` got there first the slot came
    up with no ``_app`` (so it surfaced in the main sidebar) and no ``project``
    (so approved tools ran from the gateway's own working directory instead of
    the user's project). Creating it HERE, from the indexed metadata, means the
    first thing that touches a spec's slot always scopes it.

    Refuses a slot that ANOTHER app already owns. ``get_or_create_slot`` keys off
    the name, so a foreign app holding ``spec-builder-<name>`` would otherwise be
    silently re-owned here -- its ``_app`` overwritten and its ``project``
    repointed at our spec's directory, taking the slot (and its transcript) away
    from the app that created it. Mirrors the ownership check
    ``_teardown_worker_slot`` already applies before deleting a slot.
    """
    if state is None:
        return None
    # The NAME is untrusted here for the same reason the indexed working_dir is:
    # handlers reach this with a key read back from index.json, which is app state
    # on disk that the agent this app runs can be talked into rewriting. From here
    # the name becomes a slot key and then a history key. Re-assert the same
    # grammar and redaction-stability predicate creation and discovery enforce.
    if not _usable_name(name):
        _audit("spec_slot_name_denied", _redact_and_truncate(name, 64), outcome="denied")
        logger.warning("refusing a spec slot for a name that fails the grammar")
        return None
    # Resolve once before any await so a same-name recreation cannot swap the
    # identity mid-flight.
    slot_key = _slot_key(name)
    existing = state.get_slot(slot_key) if hasattr(state, "get_slot") else None
    if existing is None:
        # Pull the transcript back before anything creates an empty slot under
        # this key. A restored slot lands in
        # state._slots, so the ownership check below governs it exactly as it
        # governs a live one: a transcript whose metadata says another app owns
        # it is refused, not adopted.
        await _restore_worker_transcript(state, name, adopt_closed=adopt_closed)
        if _slot_identity_moved(name, slot_key):
            return None
        existing = state.get_slot(slot_key) if hasattr(state, "get_slot") else None
    if existing is not None:
        owner = getattr(existing, "_app", None)
        # Only a slot ALREADY owned by this app may be adopted. An UNSCOPED slot
        # under our key is somebody else's conversation -- a main-chat session
        # that happens to be named `spec-builder-<x>` -- and adopting it
        # rewrote its ownership, repointed its project and pulled its transcript
        # into this app. The embedded chat mounts only after this endpoint has
        # created and scoped the slot, so nothing legitimate arrives unscoped.
        if owner != APP_NAME:
            _audit(
                "spec_slot_foreign_denied",
                f"{name}: owned by {owner or 'nobody'}",
                outcome="denied",
            )
            logger.warning(
                "spec slot %s is owned by %s — refusing to take it over", name, owner or "nobody"
            )
            return None
        slot = existing
        created = False
    else:
        slot = state.get_or_create_slot(name=slot_key, app=APP_NAME)
        created = True
    # The indexed working_dir is NOT trusted input. It is app state on disk, and
    # the agent this app runs can be talked into rewriting files -- so a rewritten
    # index entry would become the worker's cwd on the next message, and relative
    # reads from a credential directory would sidestep every per-path check this
    # app makes. Re-validate through the same chokepoint every caller-supplied
    # directory passes, off the event loop, and REFUSE the slot if it no longer
    # holds: a spec whose working dir is unusable must not run at all.
    #
    # ABSENT counts as unusable, which is why this is not gated on `wd` being
    # truthy. `create` rejects an empty or relative working_dir with a 400 and
    # discovery always stamps the root it scanned, so no legitimate entry reaches
    # here without one -- but deleting the key is exactly the edit the agent can
    # make, and skipping the check for it left the slot with no project at all.
    # An unscoped slot is worse than a mis-scoped one: chat_runner passes
    # cwd=slot.project, so the worker's CLI would inherit the GATEWAY's working
    # directory and run every approved relative tool from there.
    wd = str(meta.get("working_dir", ""))
    safe_wd = await asyncio.to_thread(_safe_dir, wd) if wd else None
    if safe_wd is None:
        _audit("spec_working_dir_denied", f"{name}: {_redact(wd)}", outcome="denied")
        logger.warning("spec %s has no usable indexed working_dir — refusing", name)
        return None
    # The app-wide default model, read only for a slot this call CREATED and
    # that has no explicit pick: a per-slot model set through the chat API stays
    # authoritative, and an existing slot restored across a gateway restart must
    # keep running exactly as it was -- the help copy promises a changed default
    # applies to spec sessions started AFTER the change, so re-stamping an
    # adopted slot here would contradict it. Off the loop like every other file
    # read on this path; the identity re-check below covers this await window as
    # well as _safe_dir's.
    default_model = ""
    if created and not str(getattr(slot, "model", "") or ""):
        default_model = str((await asyncio.to_thread(_load_settings)).get("model", "") or "")
    # Second window: _safe_dir ran off-loop, so re-assert the identity before
    # stamping ownership and the project onto the slot. Without this a stale
    # request repointed a replacement spec's worker at ITS OWN directory.
    if _slot_identity_moved(name, slot_key):
        return None
    try:
        slot._app = APP_NAME
        # cwd for the worker's CLI process (chat_runner: cwd=slot.project).
        # Without it the agent must `cd <project>` before every command, which
        # turns every tool pill in the chat into identical cd-noise -- and for a
        # discovered spec it would edit files outside the project entirely.
        if safe_wd is not None:
            slot.project = str(safe_wd)
        # '' = inherit: the session layer's resolution chain applies unchanged.
        # A concrete pick rides slot.model, which chat_runner already resolves
        # first — and if the pick stops being served, its withhold keeps the pin
        # and runs the turn on the backend default with a notice.
        if default_model and not str(getattr(slot, "model", "") or ""):
            slot.model = default_model
        if not getattr(slot, "_titled", False):
            slot.title = f"Spec: {name}"
            slot._titled = True
            if hasattr(state, "push_slot_title"):
                state.push_slot_title(slot.key, slot.title)
    except Exception:
        logger.debug("slot scoping failed for %s", name, exc_info=True)
    return slot


#: Distinguishes "caller did not capture an identity" (legacy, unpinned) from
#: "caller captured NOTHING, so there is nothing of ours to act on". Passing
#: ``None`` for a pin must not silently degrade to unpinned.
_UNPINNED: Any = object()


def _exec_loop_id_for_slot(slot_key: str) -> str | None:
    """The id of the live autonudge loop on *slot_key*, or ``None``.

    Captured by stop/delete BEFORE they await, so the removal can be pinned to
    the loop that existed when the request arrived.
    """
    if _autonudge_instance is None:
        return None
    try:
        svc = _autonudge_instance()
        if svc is None:
            return None
        loop = svc.get_by_slot(slot_key)
        return str(getattr(loop, "id", "")) or None if loop else None
    except Exception:
        logger.debug("autonudge lookup failed for slot %s", slot_key, exc_info=True)
        return None


def _exec_loop_id(name: str) -> str | None:
    """The id of this spec's live autonudge loop, or ``None``."""
    return _exec_loop_id_for_slot(_slot_key(name))


_EXECUTION_HANDOFF_PREFIX = "EXECUTION HANDOFF for spec '"


def _matching_execution_loops(
    name: str,
    dir_key: str,
    slot_keys: set[str],
    *,
    include_orphans: bool = False,
    include_inactive_direct: bool = False,
    service: Any = _UNPINNED,
) -> dict[str, str | None]:
    """Find durable Spec Builder loops after process claims are lost on restart.

    An orphan has no remaining name, directory, or slot binding in the current
    index. It cannot safely be attributed to one replacement entry, so dispatch
    fails closed while teardown removes it.
    """
    try:
        if service is _UNPINNED:
            if _autonudge_instance is None:
                return {}
            svc = _autonudge_instance()
        else:
            svc = service
        if svc is None:
            return {}
        loops: list[Any]
        if hasattr(svc, "list_all"):
            loops = svc.list_all()
        else:
            loops = [svc.get_by_slot(key) for key in slot_keys]
    except Exception:
        logger.debug("autonudge execution-loop scan failed", exc_info=True)
        return {}
    matched: dict[str, str | None] = {}
    for loop in loops:
        if loop is None:
            continue
        active = bool(getattr(loop, "active", True))
        loop_slot_key = str(getattr(loop, "slot_key", "") or "")
        loop_message = str(getattr(loop, "message", "") or "")
        sentinel = str(getattr(loop, "stop_sentinel_path", "") or "")
        sentinel_dir = _decision_key(str(Path(sentinel).parent)) if sentinel else ""
        direct_match = bool(name or dir_key or slot_keys) and (
            loop_slot_key in slot_keys
            or (bool(name) and _owns_slot_key(name, loop_slot_key))
            or (bool(dir_key) and bool(sentinel_dir) and sentinel_dir == dir_key)
        )
        belongs_to_index = (
            any(
                loop_slot_key == indexed_slot_key
                or _owns_slot_key(indexed_name, loop_slot_key)
                or (bool(sentinel_dir) and sentinel_dir == indexed_dir)
                for indexed_name, indexed_dir, indexed_slot_key in _repository._INDEXED_SPEC_IDENTITIES
            )
            or any(
                _owns_slot_key(indexed_name, loop_slot_key)
                for indexed_name in _repository._INDEXED_SPEC_NAMES
            )
            or (bool(sentinel_dir) and sentinel_dir in _repository._INDEXED_SPEC_DIRS)
        )
        orphan = bool(
            include_orphans
            and loop_slot_key
            and _SLOT_KEY_RE.match(loop_slot_key)
            and sentinel
            and loop_message.startswith(_EXECUTION_HANDOFF_PREFIX)
            and not belongs_to_index
        )
        if (direct_match and (active or include_inactive_direct)) or orphan:
            matched[loop_slot_key] = str(getattr(loop, "id", "") or "") or None
    return matched


async def _remove_orphaned_executions(state: Any) -> set[str]:
    """Archive endpoint-less workers in one service-owned store transaction."""
    if _AutoNudgeService is None:
        raise RuntimeError("AutoNudge service unavailable during orphan cleanup")
    async with _AutoNudgeService.maintenance_service() as service:
        return await _remove_orphaned_executions_with_service(state, service)


async def _remove_orphaned_executions_with_service(state: Any, service: Any) -> set[str]:
    """Archive orphan workers while startup and peer maintenance are excluded."""
    orphaned_loops = _matching_execution_loops("", "", set(), include_orphans=True, service=service)
    orphaned = set(orphaned_loops) | _unindexed_observed_slot_keys()
    if not orphaned:
        return set()
    if state is None:
        raise RuntimeError("gateway state unavailable during orphan cleanup")

    # Persistently pause every timer but retain its durable identity until the
    # worker transcript is safely archived.  A firing timer may publish a slot
    # during this await, so slot capture intentionally happens afterwards.
    for loop_id in orphaned_loops.values():
        if not loop_id:
            raise RuntimeError("orphaned loop has no stable identity")
        quiesced = await asyncio.wait_for(
            service.deactivate_and_wait(loop_id),
            timeout=_ORPHAN_QUIESCE_TIMEOUT_SECS,
        )
        if not quiesced:
            raise RuntimeError("orphaned loop disappeared during cleanup")

    captured_slots: dict[str, Any] = {}
    for slot_key in orphaned:
        slot = state.get_slot(slot_key)
        if slot is not None and getattr(slot, "_app", None) != APP_NAME:
            raise RuntimeError("orphaned slot is no longer owned by Spec Builder")
        captured_slots[slot_key] = slot

    for slot_key, slot in captured_slots.items():
        if slot is None:
            continue
        captured_task = getattr(slot, "task", None)
        # Before the teardown await below, for the reason its docstring gives.
        folder_committed_before_teardown = _parked_folder_membership(state, slot)
        observed_name = next(
            (
                name
                for name, observed_key in _repository._OBSERVED_SLOT_KEYS.items()
                if observed_key == slot_key
            ),
            "orphaned",
        )
        archived = await _teardown_worker_slot(
            state,
            observed_name,
            only_slot=slot,
            require_archive=True,
        )
        if captured_task is not None and not captured_task.done():
            # The bounded teardown can time out on a provider that suppresses
            # cancellation. Keep the slot addressable for another recovery
            # attempt and refuse Create while that task can still edit files.
            try:
                _revalidate_parked_vocabulary(state, slot, folder_committed_before_teardown)
                state._slots[slot_key] = slot
            except Exception:
                logger.warning("could not restore a still-running orphan slot %s", slot_key)
            raise RuntimeError("orphaned worker is still running")
        if not archived or state.get_slot(slot_key) is not None:
            raise RuntimeError("orphaned worker could not be archived")

    # Removal comes last.  Until every worker is archived, the inactive loop is
    # the restart-durable recovery marker that makes a retry find this creation.
    for loop_id in orphaned_loops.values():
        await service.remove(loop_id)

    # This app-owned recovery is the authoritative end of those creations. A
    # same-name Create must be able to mint its new K2 identity instead of being
    # pinned back to a K1 worker that was just archived.
    await _aload_index()
    _forget_observed_slot_identity("", *(orphaned & _unindexed_observed_slot_keys()))
    return orphaned


def _exec_loop_active_for_slot(slot_key: str) -> bool:
    """True while an autonudge loop bound to *slot_key* is still live.

    Registry lookup only -- no filesystem, no index read -- so a caller already holding a
    slot key can ask this ON the event loop. ``_exec_loop_active`` is the by-name wrapper
    for callers that have a name instead.

    The loop is CAPPED (``_EXEC_MAX_CYCLES``): when it runs out of cycles the
    service deactivates it on its own, without telling this app. So the index's
    ``status`` cannot be trusted by itself -- the live loop is the authority.
    """
    if _autonudge_instance is None or not slot_key:
        return False
    try:
        svc = _autonudge_instance()
        if svc is None:
            return False
        loop = svc.get_by_slot(slot_key)
        return bool(loop) and bool(getattr(loop, "active", True))
    except Exception:
        logger.debug("autonudge lookup failed for slot %s", slot_key, exc_info=True)
        return False


def _exec_loop_active(name: str) -> bool:
    """True while this spec's autonudge loop is still live.

    BLOCKING-ish: ``_slot_key`` reads the index to prefer the key persisted at creation, so
    this form must not be called from a hot on-loop path. Callers that already hold a slot
    key use ``_exec_loop_active_for_slot`` instead.
    """
    return _exec_loop_active_for_slot(_slot_key(name))


_CLAIM_OK = ""
_CLAIM_GONE = "gone"


async def _claim_execution(
    name: str,
    *,
    expect_spec_dir: str,
    expect_slot_key: str,
    live_running: bool,
) -> tuple[str, dict]:
    """Compare-and-set ``planning`` -> ``executing`` for one spec, atomically.

    Reading the status and then committing it in a separate step is not a guard:
    two concurrent execute requests both read ``planning``, both pass, and both
    dispatch -- so Pause cancels one prompt while the other drains and keeps
    editing the user's files. The decision and the write have to be the SAME index
    mutation, which is what this does: ``_mutate_index`` re-reads under its lock,
    so exactly one caller can observe ``planning`` and claim it.

    Identity is checked in the same breath, for the same reason: a delete plus a
    re-import at the same name and path is a different creation, and the claim must
    not land on it.
    """
    outcome = {"reason": _CLAIM_GONE}
    entry: dict = {}

    def _apply(index: dict) -> bool:
        meta = index.get(name)
        if (
            meta is None
            or meta.get(_DELETING)
            or meta.get(_DUPLICATING)
            or str(meta.get("spec_dir", "")) != expect_spec_dir
        ):
            return False
        actual_key = str(meta.get("slot_key", ""))
        if expect_slot_key and actual_key and actual_key != expect_slot_key:
            return False
        # Three signals, because any one of them can be the live one: the recorded
        # status, the nudge loop, and the slot's own running flag.
        if str(meta.get("status", "")) == "executing" or live_running or _exec_loop_active(name):
            outcome["reason"] = _CLAIM_TAKEN
            return False
        now = time.time()
        meta["status"] = "executing"
        meta["exec_started_at"] = now
        # Marks the pre-arm window so a concurrent poll does not reconcile the
        # state away before the loop exists. Cleared once the loop is armed.
        meta["exec_arming_at"] = now
        meta["updated_at"] = now
        entry.update(meta)
        outcome["reason"] = _CLAIM_OK
        return True

    await _mutate_index(_apply)
    return outcome["reason"], entry


#: How long a spec may sit in the pre-arm window before the reconciler stops
#: believing it. Arming is one authorization call plus one index write; a minute is
#: far beyond that, and bounding it matters because a process that dies mid-arm
#: would otherwise mask the reconciliation forever.
_ARMING_GRACE_SECS = 60.0


async def _effective_status(name: str, meta: dict, slot: Any) -> str:
    """The spec's status, reconciled against the live nudge loop.

    Without this, an execution that reached the cycle cap left ``executing``
    persisted forever: the UI showed "building" and offered Pause on a run that
    had already finished, and there was no way back to planning short of a
    restart. Reconciles ONCE and persists, identity-pinned so a recreated spec is
    not stamped by a stale request.
    """
    status = _known_status(meta.get("status"))
    if status != "executing":
        return status
    spec_dir = _decision_key(str(meta.get("spec_dir", "")))
    slot_keys = {
        str(meta.get("slot_key", "")),
        _slot_key(name),
    }
    if (
        _exec_loop_active(name)
        or _matching_execution_loops(name, spec_dir, slot_keys)
        or bool(getattr(slot, "running", False))
    ):
        return "executing"
    # The handoff records "executing" BEFORE it arms the loop (see the ordering
    # note in _handle_handoff), so between those two steps there is legitimately
    # no loop and no running turn. ``exec_arming_at`` distinguishes that window
    # from a finished run until the loop is armed.
    try:
        arming_at = float(meta.get("exec_arming_at", 0.0) or 0.0)
    except (TypeError, ValueError):
        arming_at = 0.0
    if arming_at and (time.time() - arming_at) < _ARMING_GRACE_SECS:
        return "executing"
    # BOTH pins, from the same snapshot the caller validated. spec_dir alone
    # cannot tell our spec from a replacement: a delete + re-import at the same
    # name AND path leaves it identical (the rule _unwind_create states).
    #
    # The three guards above do NOT close this. A replacement mid-ARMING has
    # written status=executing but not yet armed its loop, so _exec_loop_active
    # is False and no turn is running -- and the arming grace cannot save it,
    # because `arming_at` is read from the STALE `meta` (this caller's snapshot
    # of the original spec), not from the replacement's fresh entry. Without the
    # slot_key pin the stamp lands on the replacement and hides Pause for the
    # whole run that follows -- exactly the symptom the grace window exists for.
    await _touch_spec(
        name,
        expect_spec_dir=str(meta.get("spec_dir", "")),
        expect_slot_key=str(meta.get("slot_key", "")) or None,
        status="planning",
    )
    _audit("spec_execution_settled", f"{name}: nudge loop no longer active")
    return "planning"


async def _remove_nudge_loop(name: str, *, only_loop_id: Any = _UNPINNED) -> None:
    """Remove this spec's autonudge loop, if any. Single site for the lookup so
    halt / delete / handoff-abort cannot drift apart.

    ``only_loop_id`` pins it to a loop the caller CAPTURED: the lookup is by slot
    key, which is derived from the name, so an unpinned removal on an abort path
    would cancel the loop belonging to a same-name spec created in the meantime.
    """
    await _remove_nudge_loop_for_slot(_slot_key(name), only_loop_id=only_loop_id)


async def _remove_nudge_loop_for_slot(slot_key: str, *, only_loop_id: Any = _UNPINNED) -> None:
    """Remove the pinned autonudge loop bound to an already-captured slot key."""
    if _autonudge_instance is None:  # pragma: no cover - present in prod
        return
    if only_loop_id is None:
        return  # pinned, but nothing was captured -> nothing of ours to remove
    # Failures propagate so a persisted loop cannot survive a reported delete and
    # rearm against a same-name spec after restart. Best-effort unwind callers catch
    # the failure explicitly.
    svc = _autonudge_instance()
    if svc is None:
        return
    loop = svc.get_by_slot(slot_key)
    if loop and (only_loop_id is _UNPINNED or getattr(loop, "id", None) == only_loop_id):
        await svc.remove(loop.id)


#: One asyncio lock per spec, held across "is a turn running? -> claim pending ->
#: relay -> finalize", and across a DELETE's whole destructive sequence. Every
#: handler that can start or destroy a turn takes it, so the spec cannot be deleted
#: while an answer moves through the outbox.
#:
#: A decision answer must never be QUEUED: Pause clears the queue, so the answer may
#: never arrive. The lock makes the idle check authoritative for Spec Builder entry
#: points, while the pending status makes a process exit before relay recoverable
#: without a compensating delete that could itself fail.
#:
#: The LOOP is stored alongside each lock and compared on every lookup. An
#: ``asyncio.Lock`` binds to the loop that first awaits it, so a module-level
#: registry that outlived a loop would hand back a lock bound to the dead one and
#: raise "is bound to a different event loop" on acquisition -- which is what a
#: second gateway loop in one process (and the test suite) does.
# Keyed by CANONICAL SPEC DIRECTORY (see _turn_lock), never by name.
_TURN_LOCKS: dict[str, tuple[Any, asyncio.Lock]] = {}
_CASE_FOLD_TURN_KEYS = sys.platform == "darwin"


def _turn_key(spec_dir: str) -> str:
    """Stable lexical key used only to serialize directory operations.

    Darwin normally preserves case while resolving paths even when its volume treats
    case variants as one directory. Folding there may serialize two distinct directories
    on a case-sensitive Darwin volume, which is safe; the index collision check uses
    ``samefile`` and still admits them. The conservative lock prevents two filesystem-
    equivalent spellings from racing create against create or delete cleanup.
    """
    key = _decision_key(spec_dir)
    return key.casefold() if _CASE_FOLD_TURN_KEYS else key


def _turn_lock(spec_dir: str) -> asyncio.Lock:
    """The turn-start lock for a spec DIRECTORY on the RUNNING loop, created on first use.

    NORMALIZES its own argument through ``_turn_key`` rather than trusting the caller
    to have done it. That is not defensive habit: ``_decision_key`` applies ``normcase``,
    which lowercases on Windows, and ``_turn_key`` additionally folds Darwin paths so
    the default case-insensitive volume cannot mint two locks for one directory. A raw
    path and a pre-normalized path therefore reach the same dictionary entry. Both
    helpers are pure and lexical, so this remains safe on the event loop.

    Keyed on the directory, not the name, for the same reason the decision ledger is: the
    index can hold several names for one directory, and a per-name lock let two of them
    start turns on the same documents concurrently -- each seeing only its own idle slot,
    so both dispatched. One directory is one turn.

    Safe to build lazily without its own mutex: every caller runs on the event
    loop, and the get-or-create below contains no await.
    """
    loop = asyncio.get_running_loop()
    dir_key = _turn_key(spec_dir)
    entry = _TURN_LOCKS.get(dir_key)
    if entry is not None and entry[0] is loop:
        return entry[1]
    lock = asyncio.Lock()
    _TURN_LOCKS[dir_key] = (loop, lock)
    return lock


def _alias_slots_locked(
    dir_key: str, *, own_slot_key: str, own_name: str = ""
) -> dict[str | None, str]:
    """slot_key -> name for every OTHER indexed spec on this directory.

    ``None`` is an alias whose persisted slot identity is not ownership-valid.
    Such an alias is occupied: its worker may still be running under the
    per-creation key that the agent-writable index no longer reveals.

    BLOCKING -- call via ``asyncio.to_thread`` (``_alias_slots`` is the only caller). It
    reads the index and resolves each entry's directory, both filesystem work, which is
    exactly why it does not belong on the loop.

    Excludes the caller's own slot: that one is the same session, where an ordinary
    message is legitimately QUEUED rather than refused. Another name is a different
    session over the same documents, so a turn running under it is a concurrent editor.

    Each alias's key is RESOLVED, never read raw from its entry. index.json is
    agent-writable, so ``meta["slot_key"]`` is attacker-controlled, and trusting it gave
    an alias two ways to make itself invisible to the busy scan: delete the field and the
    entry was skipped for having no key, or copy the caller's key and it was skipped as
    "our own slot". Either way a live concurrent editor read as absent and both agents
    wrote the same spec files. ``_slot_key`` answers from the ownership-validated map
    instead -- a key only survives ``_owns_slot_key`` if it structurally encodes its own
    indexed name -- and falls back to the name-derived form otherwise, which is the same
    key ``_ensure_worker_slot`` would have run that alias under. That fallback is only
    authoritative for a legacy entry, though: if a per-creation key was removed while
    its worker was active, the fallback names a DIFFERENT slot. An ownership-invalid
    entry therefore refuses dispatch instead of guessing which worker owns the files.

    Resolution happens HERE, once and off the loop, rather than in ``_busy_alias``: that
    one runs on the event loop, and one validated resolution per alias is also cheaper
    than re-deriving keys per question asked.
    """
    out: dict[str | None, str] = {}
    own_entry_found = not own_name
    with _INDEX_LOCK:
        index = _load_index()
    for other, meta in index.items():
        if not isinstance(meta, dict):
            continue
        other_dir = str(meta.get("spec_dir", ""))
        persisted = meta.get("slot_key")
        valid_slot = isinstance(persisted, str) and _owns_slot_key(other, persisted)
        slot_key = _slot_key(other) if valid_slot else ""
        if own_name and other == own_name:
            own_entry_found = True
            # The current entry itself may be rewritten while a dispatch awaits this
            # scan. Stop would then derive a different lexical lock key. The process
            # barrier revokes by slot even across that move, and the scan also refuses
            # the stale dispatch regardless of whether both paths still alias.
            if (
                not valid_slot
                or slot_key != own_slot_key
                or _decision_key(other_dir) != _decision_key(dir_key)
            ):
                out[None] = other
            continue
        if not own_name and valid_slot and slot_key == own_slot_key:
            continue
        if not _same_spec_dir(other_dir, dir_key):
            continue
        if not valid_slot:
            out[None] = other
            continue
        out[slot_key] = other
    if not own_entry_found:
        out[None] = "current spec"
    return out


async def _alias_slots(
    dir_key: str, *, own_slot_key: str, own_name: str = ""
) -> dict[str | None, str]:
    """``_alias_slots_locked`` off the event loop."""
    return await asyncio.to_thread(
        _alias_slots_locked,
        dir_key,
        own_slot_key=own_slot_key,
        own_name=own_name,
    )


def _busy_alias(state: Any, aliases: dict[str | None, str]) -> str:
    """The name of an alias that is mid-turn OR holding an armed execution loop, or "".

    A running turn is not the only way an alias occupies these documents. An autonudge
    execution loop (a handoff/build) sits IDLE between its nudge cycles, so asking only
    whether the slot is running right now let an alias with a live loop read as free: the
    other name dispatched, then the loop's timer fired, and two agents wrote the same spec
    files. The loop is as much an occupant as the turn it periodically starts.

    Deliberately on the loop and deliberately in-memory only: the slot registry and the
    nudge registry both live here, so reading them from a worker thread would race the very
    state this is trying to observe. Both questions are answered by slot KEY, and the keys
    arrive already resolved and ownership-validated from ``_alias_slots_locked`` -- so this
    function derives nothing and touches no file. Keeping derivation out of here is the
    point: the by-name ``_exec_loop_active`` would re-derive a key per call, and one
    resolver, off the loop, is what makes every alias key validated the same way.
    """
    if state is None:
        return ""
    for slot_key, other in aliases.items():
        if slot_key is None:
            return other
        slot = state.get_slot(slot_key) if hasattr(state, "get_slot") else None
        if slot is not None and getattr(slot, "running", False):
            return other
        if _exec_loop_active_for_slot(slot_key):
            return other
    return ""


def _busy_observed_directory_slot(state: Any, dir_key: str, own_slot_key: str) -> str:
    """Return an older authenticated slot still working on this directory."""
    if state is None:
        return ""
    observed_keys = _observed_slot_keys_for_dir(dir_key) | _unindexed_observed_slot_keys()
    for slot_key in observed_keys:
        if slot_key == own_slot_key:
            continue
        slot = state.get_slot(slot_key) if hasattr(state, "get_slot") else None
        slot_task = getattr(slot, "task", None) if slot is not None else None
        if (
            bool(getattr(slot, "running", False))
            or (slot_task is not None and not slot_task.done())
            or _exec_loop_active_for_slot(slot_key)
        ):
            return slot_key
    return ""


def _alias_turn_snapshot(
    state: Any, aliases: dict[str | None, str]
) -> dict[str, tuple[Any, Any, int]]:
    """Capture each live alias slot and its monotonic turn history on the loop."""
    if state is None:
        return {}
    out: dict[str, tuple[Any, Any, int]] = {}
    for slot_key in aliases:
        if slot_key is None:
            continue
        alias_slot = state.get_slot(slot_key) if hasattr(state, "get_slot") else None
        out[slot_key] = (
            alias_slot,
            getattr(alias_slot, "task", None),
            int(getattr(alias_slot, "_turn_generation", 0)),
        )
    return out


def _alias_turn_started_since(
    state: Any,
    aliases: dict[str | None, str],
    snapshot: dict[str, tuple[Any, Any, int]],
) -> bool:
    """True when any alias published a turn after the serialized busy scan.

    Task identity detects a turn that both started and finished while this request
    awaited filesystem work; checking only ``running`` loses that whole interval.
    A newly discovered alias with any task is likewise ambiguous and fails closed.
    """
    if state is None:
        return False
    for slot_key in aliases:
        if slot_key is None:
            return True
        alias_slot = state.get_slot(slot_key) if hasattr(state, "get_slot") else None
        prior = snapshot.get(slot_key)
        if prior is None:
            if alias_slot is not None and (
                getattr(alias_slot, "task", None) is not None
                or int(getattr(alias_slot, "_turn_generation", 0)) > 0
            ):
                return True
            continue
        prior_slot, prior_task, prior_generation = prior
        if (
            alias_slot is not prior_slot
            or getattr(alias_slot, "task", None) is not prior_task
            or int(getattr(alias_slot, "_turn_generation", 0)) != prior_generation
        ):
            return True
    return False


async def _final_alias_conflict(
    state: Any,
    dir_key: str,
    own_slot_key: str,
    initial_aliases: dict[str | None, str],
    snapshot: dict[str, tuple[Any, Any, int]],
    *,
    own_name: str = "",
) -> str:
    """Return an alias that invalidated a dispatch window, or ``""``.

    This must be the last await on every successful dispatch path. The caller checks
    its own slot and publishes the task synchronously after this returns.
    """
    fresh_aliases = await _alias_slots(dir_key, own_slot_key=own_slot_key, own_name=own_name)
    all_aliases = {**initial_aliases, **fresh_aliases}
    if busy_slot := _busy_observed_directory_slot(state, dir_key, own_slot_key):
        return busy_slot
    if busy_under := _busy_alias(state, all_aliases):
        return busy_under
    if _alias_turn_started_since(state, all_aliases, snapshot):
        return next(iter(all_aliases.values()), "another view")
    return ""


def _discard_queued_work(slot: Any) -> None:
    """Drop everything that would start a SUCCESSOR turn on this slot.

    Ending a turn is not the same as stopping the work. ``_run_chat`` swallows
    its ``CancelledError`` instead of re-raising, so its end-of-turn block runs
    on a cancel exactly as it does on a clean finish -- and that block requeues
    unconsumed steers, then starts the next queued message, and otherwise hands
    a pending synthesis to ``_run_pending_synthesis``. So a Pause or a Delete
    that only stopped the turn handed the agent its next prompt: it kept editing
    the user's spec files after the click, and for Delete it kept writing into a
    directory the request was about to archive.

    Three sources can each relaunch, so all three are dropped:
    ``_queue`` (queued messages), ``_pending_steers`` (requeued to the HEAD of
    the queue by the end-of-turn block, so they become queue items) and
    ``_pending_synthesis`` (a subagent-synthesis turn).

    Call this BEFORE any stop -- cooperative or cancel. A cooperative
    ``stop_turn`` ends the turn too, so clearing after it races the successor.

    Attribute-tolerant on purpose: a foreign or partially-built slot may not
    carry these, and failing to discard must never be what breaks teardown.
    """
    for attr in ("_queue", "_pending_steers"):
        seq = getattr(slot, attr, None)
        if seq is None:
            continue
        try:
            seq.clear()
        except Exception:
            logger.debug("could not clear %s during stop", attr, exc_info=True)
    try:
        slot._pending_synthesis = False
    except Exception:
        logger.debug("could not clear _pending_synthesis during stop", exc_info=True)


def _parked_folder_membership(state: Any, slot: Any) -> bool | None:
    """Committed membership of *slot*'s folder, read BEFORE a teardown await.

    A slot popped for teardown is unreachable from BOTH passes of a folder or tag
    delete sweep: the captured list predates the pop and the live view no longer holds
    it. So the restore below is the last place a deleted id can be caught, and this
    reading is the evidence that separates "absent because deleted" from "absent
    because never committed". It is only true if taken before the await.

    ``None`` is UNKNOWN, which the validators treat as fail-open, so a state that
    publishes no committed vocabulary proves nothing and prunes nothing.
    """
    reader = getattr(state, "committed_folder_membership", None)
    if not callable(reader):
        return None
    try:
        return reader(getattr(slot, "folder_id", ""))
    except Exception:
        logger.debug("could not read committed folder membership", exc_info=True)
        return None


def _revalidate_parked_vocabulary(state: Any, slot: Any, folder_committed: bool | None) -> None:
    """Re-check *slot*'s ids before it re-enters the live registry.

    The restore-side half of the protocol in :func:`_parked_folder_membership`, routing
    through the same two validators every other adopter uses so a vocabulary deleted
    while this slot was parked cannot come back durably attached to it.

    A validator failure leaves the value untouched rather than propagating: the caller
    restores this slot to keep the user's transcript reachable, and losing that to a
    metadata check would trade a phantom folder for a lost conversation.
    """
    validate_folder = getattr(state, "folder_id_for_restore", None)
    if callable(validate_folder):
        try:
            slot.folder_id = validate_folder(
                getattr(slot, "folder_id", ""), was_committed=folder_committed
            )
        except Exception:
            logger.debug("could not revalidate a parked folder id", exc_info=True)
    validate_tags = getattr(state, "tag_ids_for_restore", None)
    if callable(validate_tags):
        try:
            slot.tags = validate_tags(list(getattr(slot, "tags", None) or []))
        except Exception:
            logger.debug("could not revalidate parked tag ids", exc_info=True)


async def _teardown_worker_slot(
    state: Any, name: str, *, only_slot: Any = _UNPINNED, require_archive: bool = False
) -> bool:
    """Remove this spec's worker slot, cancelling any in-flight turn.

    Mirrors the gateway's own slot-delete sequence: pop from the registry BEFORE
    any await (so nothing can re-enter it mid-teardown), then cancel the running
    task and await it with a bounded shield, then persist the slot as closed.

    Only ever touches a slot this app owns (``slot._app == APP_NAME``) — a
    foreign or unscoped slot is left alone rather than deleted by name collision.

    ``only_slot`` pins it to the exact slot OBJECT the caller captured. The
    registry is keyed by name, so an abort path that tears down "by name" would
    destroy the slot of a same-name spec created while the request was in flight.

    Returns False ONLY when ``require_archive`` was asked for and persisting the
    conversation failed. Every refusal path returns True: there is no transcript of
    OURS at risk (no slot, a replacement, or a foreign owner), so a caller must not
    treat it as data loss and abort.
    """
    if state is None:
        return True
    if only_slot is None:
        return True  # pinned, but nothing was captured -> nothing of ours to tear down
    # The captured slot's own key wins when the caller pinned one: recomputing from
    # the name would look up a DIFFERENT slot once keys are per-creation.
    slot_key = getattr(only_slot, "key", None) or _slot_key(name)
    if not isinstance(slot_key, str) or not _SLOT_KEY_RE.match(slot_key):
        slot_key = _slot_key(name)
    try:
        slot = state.get_slot(slot_key)
    except Exception:
        slot = None
    if slot is None:
        return True
    if only_slot is not _UNPINNED and slot is not only_slot:
        logger.warning("refusing to tear down slot %s: replaced since capture", slot_key)
        return True
    if getattr(slot, "_app", None) != APP_NAME:
        logger.warning("refusing to tear down slot %s: not owned by %s", slot_key, APP_NAME)
        return True
    # Before the cancel below: _run_chat's end-of-turn block would otherwise
    # start the next queued prompt, so the agent would keep writing into a spec
    # directory this request is about to archive.
    _discard_queued_work(slot)
    try:
        state._slots.pop(slot_key, None)
    except Exception:
        logger.debug("slot registry pop failed for %s", slot_key, exc_info=True)
    # Read HERE, before any post-pop await: that await is the window a delete commits
    # in, and a parked slot is reachable from neither sweep pass.
    folder_committed_before_save = _parked_folder_membership(state, slot)
    task = getattr(slot, "task", None)
    if getattr(slot, "running", False) and task is not None:
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:
            logger.debug("worker task raised during teardown of %s", slot_key, exc_info=True)
    # circular import (see module header): dashboard.server imports this module.
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

    try:
        await save_slot_off_loop(state, slot, closed=True, best_effort=not require_archive)
    except Exception:
        # The transcript is the user's data. A caller that is about to drop the
        # spec from the index (delete) asks for require_archive, because reporting
        # success here would discard a conversation that was never written. The
        # slot is put back so the caller can restore the entry and the user can
        # retry; callers that do not require the archive keep the old
        # best-effort behaviour (an abort path has already lost the race).
        logger.warning("closing save failed for %s", slot_key, exc_info=True)
        if require_archive:
            try:
                _revalidate_parked_vocabulary(state, slot, folder_committed_before_save)
                state._slots[slot_key] = slot
            except Exception:
                logger.warning("could not restore slot %s after a failed archive", slot_key)
            _audit("spec_slot_archive_failed", name, outcome="denied")
            return False
    _audit("spec_slot_teardown", name)
    return True


async def _halt_execution(
    state: Any,
    name: str,
    spec_dir: Path,
    *,
    reason: str,
    only_loop_id: Any = _UNPINNED,
    only_slot: Any = _UNPINNED,
    expect_slot_key: str = "",
) -> None:
    """Stop an autonomous run: sentinel the loop, then remove it.

    Deliberately does NOT touch ``slot._trust``. This app no longer grants
    trust, so there is nothing of ours to revoke — and if the USER trusted the
    session from the approval card, Stop must not silently undo their decision.
    """
    # Off-loop: the sentinel write is six filesystem syscalls, and a spec dir on
    # unresponsive network storage would otherwise freeze the gateway loop for
    # the duration of a Stop click. The identity travels WITH the write rather
    # than being checked by the caller beforehand: the caller's check and this
    # write are separated by a thread hop, which is exactly the window a same-name
    # delete plus re-import needs to redirect the STOP onto a replacement.
    if not await asyncio.to_thread(_write_stop_sentinel_for_spec, spec_dir, name, expect_slot_key):
        # Not fatal: the two stops below are what actually end the run. Logged so an
        # operator can tell "no sentinel" from "sentinel ignored".
        logger.warning("spec %s: no stop sentinel written; halting by loop + turn", name)
    await _remove_nudge_loop(name, only_loop_id=only_loop_id)
    # ...and stop the turn that is running RIGHT NOW. The sentinel and the loop
    # removal only prevent FUTURE nudges: the in-flight _run_chat kept going, so
    # Pause flipped the status to "planning" and returned ok while the agent
    # carried on editing the user's files. Cooperative stop first (the gateway's
    # own stop_turn), then a bounded cancel of the slot task as the fallback.
    await _halt_active_turn(state, name, only_slot=only_slot)
    _audit("spec_execution_halted", f"{name}: {reason}")


async def _halt_active_turn(state: Any, name: str, *, only_slot: Any = _UNPINNED) -> bool:
    """Stop the spec slot's in-flight turn, keeping the slot and its transcript.

    Unlike ``_teardown_worker_slot`` (used by DELETE) this does not remove the
    slot -- Pause must leave the conversation intact so the user can resume.
    Returns True when a running turn was stopped.
    """
    if only_slot is None:
        return False  # pinned, but nothing was captured
    slot_key = getattr(only_slot, "key", None) or _slot_key(name)
    slot = state.get_slot(slot_key) if state is not None else None
    if slot is None or not getattr(slot, "running", False):
        return False
    if only_slot is not _UNPINNED and slot is not only_slot:
        logger.warning("refusing to stop slot %s: replaced since capture", slot_key)
        return False
    # Ownership must be EXACT, as it is in _ensure_worker_slot and
    # _teardown_worker_slot. Tolerating an unscoped owner here meant a plain
    # `POST /api/chat` on slot `spec-builder-<name>` -- somebody else's
    # conversation that merely shares the key -- could be cancelled mid-turn by
    # this app's Stop button, losing that turn's response.
    if getattr(slot, "_app", None) != APP_NAME:
        return False
    # Before BOTH stops below. The cooperative stop_turn also ends the turn, so
    # clearing after it would race _run_chat's end-of-turn block into starting
    # the next queued prompt -- Pause would return ok while the agent carried on.
    _discard_queued_work(slot)
    try:
        # circular import (see module header): dashboard.server imports us.
        from kiro_crew.dashboard.chat_utils import _history_key_for

        await state.sessions.stop_turn(_history_key_for(slot.key), force=False)
    except Exception:
        logger.debug("cooperative stop failed for %s", name, exc_info=True)
    task = getattr(slot, "task", None)
    if task is not None and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:
            logger.debug("worker task raised while pausing %s", name, exc_info=True)
    return True


def _exec_prompt(name: str, spec_dir: Path, working_dir: str) -> str:
    return (
        f"{_EXECUTION_HANDOFF_PREFIX}{name}'. The plan is approved. Read "
        f"{spec_dir / 'tasks.md'} and work through each unchecked task IN ORDER, "
        f"operating inside {working_dir} (your shell already starts there — no cd needed). After each task: "
        f"mark its checkbox [x] in tasks.md, run the relevant build/tests to verify, "
        f"then continue. Stop when all tasks are checked or you hit a blocker that needs "
        f"me, and summarize what was done and what remains."
    )


# ── slot turn relay (embedded chat) ──────────────────────────────────────────


def _dispatch_turn(
    state: Any,
    slot: Any,
    message: str,
    *,
    message_meta: dict[str, str] | None = None,
    append_user: bool = True,
    directive_user_origin: bool = False,
    on_consumed: Callable[[bool], None] | None = None,
    on_irreversibly_consumed: Callable[[], Awaitable[None] | None] | None = None,
) -> asyncio.Task[Any] | None:
    """Relay a turn into the spec's agent slot with its structural provenance."""
    if getattr(slot, "running", False):
        try:
            # Deferred to avoid the dashboard import cycle. A spec slot is
            # app-scoped, so an UNMARKED plain entry would fail the drain's
            # closed-world re-check.
            # The stamp records app=True at admission, which the drain treats as
            # designed behaviour rather than a containment change.
            from kiro_crew.dashboard.session_control import containment_meta

            slot.queue_append(
                message,
                meta=containment_meta(state, slot),
                directive_user_origin=directive_user_origin,
            )
        except Exception:
            logger.debug("queue_append failed", exc_info=True)
        try:
            # _redact, not the raw message: `queued` is NOT one of the roles
            # _ChatSlot.append suppresses the global SSE push for (only "chunk",
            # "done" and "user" are), so this text goes to every connected
            # dashboard client. The host sanitizes the stored value on its own
            # steer/queue paths for the same reason -- raw content must not reach
            # an external surface -- and _redact is this module's copy of that
            # chain, failing closed when the security module is unavailable.
            slot.append("queued", _redact(message))
        except Exception:
            pass
        state.push_slots_update()
        return None
    # circular import (see module header): dashboard.server imports this module.
    from kiro_crew.dashboard.chat_runner import _run_chat

    try:
        # Deferred like the other dashboard imports; the resolver follows a
        # raised agent.chat_turn_timeout_secs above the 2h default and runs
        # OFF the event loop (inside the task, via asyncio.to_thread).
        from kiro_crew.dashboard.turn_dispatch import bounded_chat_turn
    except Exception:  # pragma: no cover - resolver always present in prod
        bounded_chat_turn = None  # type: ignore[assignment]

    if append_user:
        if message_meta:
            slot.append("user", message, meta=message_meta)
        else:
            slot.append("user", message)
    run_chat = _run_chat(
        state,
        slot,
        message,
        _directive_user_origin=directive_user_origin,
        _on_consumed=on_consumed,
        _on_irreversibly_consumed=on_irreversibly_consumed,
    )
    if bounded_chat_turn is not None:
        task = asyncio.create_task(bounded_chat_turn(run_chat))
    else:
        task = asyncio.create_task(asyncio.wait_for(run_chat, timeout=float(CHAT_TURN_TIMEOUT)))
    slot.task = task
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    state.push_slots_update()
    return task


def _reserve_slot_turn(state: Any, slot: Any) -> asyncio.Task[Any] | None:
    """Make every ordinary turn starter observe this slot as busy across awaits.

    The request task is a temporary turn owner. A dashboard chat request that passed
    its first idle check before this reservation may still overwrite ``slot.task``;
    callers therefore pass the returned identity through to the final dispatch gate.
    The done callback only clears its own reservation and cannot erase such a turn.
    """
    if getattr(slot, "running", False):
        return None
    reservation = asyncio.current_task()
    if reservation is None:  # pragma: no cover - handlers always run in a task
        return None
    slot.task = reservation

    def _release(done: asyncio.Task[Any]) -> None:
        if getattr(slot, "task", None) is not done:
            return
        slot.task = None
        if not getattr(slot, "_queue", None):
            return
        # A generic chat message that arrived while validation was in flight was
        # legitimately queued behind the reservation. If validation refuses, no
        # decision turn exists to drain it, so hand the queue to the host runner.
        from kiro_crew.dashboard.chat_runner import _start_next_queued_turn

        drain = asyncio.create_task(_start_next_queued_turn(state, slot))
        slot.task = drain
        state._background_tasks.add(drain)
        drain.add_done_callback(state._background_tasks.discard)

    reservation.add_done_callback(_release)
    return reservation


async def _deliver_pending_decision(
    state: Any,
    slot: Any,
    spec_dir: str,
    pending: dict[str, str],
    *,
    turn_reservation: asyncio.Task[Any] | None = None,
    initial_aliases: dict[str | None, str] | None = None,
    alias_snapshot: dict[str, tuple[Any, Any, int]] | None = None,
    own_name: str = "",
    expected_slot_key: str = "",
    dispatch_claim: str = "",
) -> bool:
    """Dispatch one durable outbox entry and finalize it after model consumption.

    The delivery id is persisted both in the ledger and on the chat row. A restored
    row proves the user-facing append happened, but not that the model consumed the
    prompt. Recovery therefore re-runs that row without appending a duplicate and
    leaves the ledger pending until ``_run_chat`` reports consumption.
    """
    delivery_id = pending.get("delivery_id", "")
    if not delivery_id:
        return False
    inflight = getattr(state, "_spec_decision_deliveries_inflight", None)
    if not isinstance(inflight, set):
        inflight = set()
        state._spec_decision_deliveries_inflight = inflight
    consumed_claims = getattr(state, "_spec_decision_deliveries_consumed", None)
    if not isinstance(consumed_claims, set):
        consumed_claims = set()
        state._spec_decision_deliveries_consumed = consumed_claims
    inflight_key = (_decision_key(spec_dir), delivery_id)
    if inflight_key in inflight:
        # Consumption is irreversible even when the following ledger write fails.
        # Keep the process-local claim and let later detail polls retry only that
        # write; reopening dispatch would send the same answer to the model twice.
        if inflight_key in consumed_claims:
            try:
                finalized = await _finalize_decision(
                    spec_dir,
                    pending.get("decision_id", ""),
                    pending.get("fingerprint", ""),
                    delivery_id,
                )
            except Exception:
                logger.warning(
                    "could not retry consumed decision finalization for %s",
                    spec_dir,
                    exc_info=True,
                )
            else:
                if finalized:
                    consumed_claims.discard(inflight_key)
                    inflight.discard(inflight_key)
        return False
    # Claim this process-local dispatch before re-reading the durable row. A detail
    # poll may already hold a stale pending snapshot while the consuming turn's
    # settlement is saving ``final``. The marker closes that in-process window; the
    # fresh read closes the later case where settlement finished before this call.
    inflight.add(inflight_key)
    fresh_pending = next(
        (
            entry
            for entry in await _pending_decisions(spec_dir)
            if entry.get("decision_id") == pending.get("decision_id")
            and entry.get("fingerprint") == pending.get("fingerprint")
            and entry.get("delivery_id") == delivery_id
        ),
        None,
    )
    if fresh_pending is None:
        inflight.discard(inflight_key)
        return False
    pending = fresh_pending
    durable_relay_started = pending.get("status") == "relayed"
    already_relayed = False
    for row in getattr(slot, "messages", []) or []:
        if not isinstance(row, dict):
            continue
        meta = row.get("meta")
        if isinstance(meta, dict) and meta.get("spec_decision_delivery_id") == delivery_id:
            already_relayed = True
            break
    still_current = await _pending_decision_is_current(spec_dir, pending)
    if still_current is not True:
        if still_current is False and not (durable_relay_started or already_relayed):
            await _abandon_pending_decision(
                spec_dir,
                pending.get("decision_id", ""),
                pending.get("fingerprint", ""),
                delivery_id,
            )
        inflight.discard(inflight_key)
        return False
    if turn_reservation is None:
        occupied = getattr(slot, "running", False)
    else:
        # Identity, not merely ``running``: a generic dashboard request can pass
        # its idle check before our reservation and replace ``slot.task`` while the
        # state/ledger reads above are off-loop. Even if that fast turn has already
        # completed, its different task proves the validated snapshot is stale.
        occupied = getattr(slot, "task", None) is not turn_reservation
    if occupied:
        inflight.discard(inflight_key)
        return False
    relayed_here = False

    async def _refuse_before_dispatch() -> bool:
        if relayed_here:
            await _restore_decision_pending(
                spec_dir,
                pending.get("decision_id", ""),
                pending.get("fingerprint", ""),
                delivery_id,
            )
        inflight.discard(inflight_key)
        return False

    if not durable_relay_started:
        if not await _mark_decision_relayed(
            spec_dir,
            pending.get("decision_id", ""),
            pending.get("fingerprint", ""),
            delivery_id,
        ):
            inflight.discard(inflight_key)
            return False
        pending["status"] = "relayed"
        relayed_here = True
        # The durable transition above is an await. A generic request that passed
        # its own idle check before our reservation may have published a different
        # task during it; never dispatch from the older validated snapshot.
        if turn_reservation is None:
            occupied = getattr(slot, "running", False)
        else:
            occupied = getattr(slot, "task", None) is not turn_reservation
        if occupied:
            return await _refuse_before_dispatch()

    # The directory lock serializes Spec Builder endpoints, but a dashboard chat
    # can start any app-owned alias slot directly. Re-read the agent-writable alias
    # index after the LAST delivery await, then synchronously check both running
    # state and task identity before the synchronous dispatch below. The snapshot
    # catches a fast alias turn that started and finished during an earlier await;
    # the fresh scan catches an alias added during that turn.
    initial_aliases = initial_aliases or {}
    alias_snapshot = alias_snapshot or {}
    alias_conflict = await _final_alias_conflict(
        state,
        _decision_key(spec_dir),
        expected_slot_key or str(getattr(slot, "key", "")),
        initial_aliases,
        alias_snapshot,
        own_name=own_name,
    )
    if alias_conflict:
        return await _refuse_before_dispatch()
    if turn_reservation is None:
        occupied = getattr(slot, "running", False)
    else:
        occupied = getattr(slot, "task", None) is not turn_reservation
    if occupied:
        return await _refuse_before_dispatch()
    # Stop publishes this revocation before it waits for the directory lock. The
    # final alias read happens off-thread and the agent can rewrite its own entry
    # after that worker captured it, so the mutable snapshot alone cannot prove a
    # Stop did not finish on a new path/slot while this request was suspended.
    if dispatch_claim and not _pending_dispatch_is_current(dispatch_claim):
        return await _refuse_before_dispatch()

    settlement_started = False
    consumption_by_turn: dict[asyncio.Task[Any], bool] = {}
    watched_turns: set[asyncio.Task[Any]] = set()

    async def _finalize_consumed_decision() -> None:
        try:
            finalized = await _finalize_decision(
                spec_dir,
                pending.get("decision_id", ""),
                pending.get("fingerprint", ""),
                delivery_id,
            )
        except Exception:
            logger.warning(
                "could not finalize consumed decision for %s",
                spec_dir,
                exc_info=True,
            )
            consumed_claims.add(inflight_key)
        else:
            if not finalized:
                consumed_claims.add(inflight_key)
                return
            consumed_claims.discard(inflight_key)
            inflight.discard(inflight_key)

    def _track_settlement(settlement: asyncio.Task[None]) -> None:
        state._background_tasks.add(settlement)
        settlement.add_done_callback(state._background_tasks.discard)

    async def _on_irreversibly_consumed() -> None:
        nonlocal settlement_started
        if settlement_started:
            return
        settlement_started = True
        await _finalize_consumed_decision()

    def _on_consumed(consumed: bool = True) -> None:
        turn = asyncio.current_task()
        if turn is None or settlement_started:
            return
        consumption_by_turn[turn] = consumed
        if not consumed or turn in watched_turns:
            return
        watched_turns.add(turn)

        async def _settle_after_turn() -> None:
            nonlocal settlement_started
            try:
                await turn
            except asyncio.CancelledError:
                # Distinguish the watched turn being cancelled (it is done, and a
                # prior True report still proves consumption) from this watcher
                # being cancelled during shutdown while the turn remains live.
                if not turn.done():
                    raise
            except Exception:
                # Cancellation or a handled provider failure does not undo a prompt
                # that already reached the model. The consumption report, including
                # a same-turn False retraction, remains the authority.
                pass
            consumed_at_end = consumption_by_turn.pop(turn, False)
            watched_turns.discard(turn)
            if not consumed_at_end or settlement_started:
                return
            settlement_started = True
            await _finalize_consumed_decision()

        _track_settlement(asyncio.create_task(_settle_after_turn()))

    if turn_reservation is not None:
        # No await between releasing the reservation and publishing the real task,
        # so an ordinary turn starter cannot observe an idle slot in this handoff.
        slot.task = None
    turn = _dispatch_turn(
        state,
        slot,
        pending.get("message", ""),
        message_meta={"spec_decision_delivery_id": delivery_id},
        append_user=not already_relayed,
        directive_user_origin=True,
        on_consumed=_on_consumed,
        on_irreversibly_consumed=_on_irreversibly_consumed,
    )
    if dispatch_claim:
        _bind_pending_dispatch_to_turn(dispatch_claim, slot, turn)
    if turn is not None:

        async def _release_if_turn_chain_ends_unconsumed() -> None:
            """Keep the claim across automatic retries, then reopen if none consume."""
            current = turn
            while True:
                try:
                    await current
                except asyncio.CancelledError:
                    if not current.done():
                        raise
                except Exception:
                    pass
                # The queue drain runs in the turn's ``finally`` before the task is
                # done. Follow its successor so a pre-consumption provider retry does
                # not briefly look idle and admit a duplicate replay.
                successor = getattr(slot, "task", None)
                if successor is not None and successor is not current:
                    current = successor
                    continue
                # ``bounded_chat_turn`` may wrap ``_run_chat`` in a different task,
                # so the report maps can be keyed by the inner task rather than
                # ``current``. Any live True watcher owns the marker until it either
                # observes a False retraction or completes the durable finalization.
                if settlement_started or watched_turns or any(consumption_by_turn.values()):
                    return
                inflight.discard(inflight_key)
                return

        release = asyncio.create_task(_release_if_turn_chain_ends_unconsumed())
        state._background_tasks.add(release)
        release.add_done_callback(state._background_tasks.discard)
    elif not watched_turns:
        # Test doubles and defensive dispatch failures may not return a task. A
        # synchronous consumption report owns cleanup through its watcher; without
        # one there is no live delivery to protect.
        inflight.discard(inflight_key)
    return True


async def _replay_pending_decision(state: Any, slot: Any, name: str, meta: dict[str, Any]) -> bool:
    """Replay at most one crash-interrupted answer during a recovery POST.

    One pending entry is the normal maximum because a decision answer is refused while
    its slot is running. Processing one also keeps the polling endpoint bounded if an
    interrupted development build left malformed residue.
    """
    pinned = await _pin_legacy_slot_identity(name, meta)
    if pinned is None:
        return False
    meta = pinned
    spec_dir = str(meta.get("spec_dir", ""))
    pending_entries = await _pending_decisions(spec_dir)
    if not pending_entries:
        return False
    pending = None
    for entry in pending_entries:
        # A relayed row whose question is provably gone is retained as an
        # ambiguity marker: the model may have consumed it before the crash. It
        # cannot be dispatched or deleted, but it also must not permanently
        # starve a newer current answer behind it. Unknown state still fails
        # closed by preserving first-in-order recovery.
        if (
            entry.get("status") == "relayed"
            and (await _pending_decision_is_current(spec_dir, entry)) is False
        ):
            continue
        pending = entry
        break
    if pending is None:
        return False
    dir_key = _decision_key(spec_dir)
    expected_slot_key = str(meta.get("slot_key", ""))
    async with _turn_lock(dir_key):
        dispatch_claim = _reserve_pending_dispatch(dir_key, expected_slot_key, name)
        if not dispatch_claim:
            return False
        _release_pending_dispatch_when_done(dispatch_claim)
        aliases = await _alias_slots(
            dir_key,
            own_slot_key=expected_slot_key or str(getattr(slot, "key", "")),
        )
        if _busy_alias(state, aliases):
            return False
        alias_snapshot = _alias_turn_snapshot(state, aliases)
        turn_reservation = _reserve_slot_turn(state, slot)
        if turn_reservation is None:
            return False
        if (
            await _touch_spec(
                name,
                expect_spec_dir=spec_dir,
                expect_slot_key=expected_slot_key or None,
            )
            is None
        ):
            return False
        return await _deliver_pending_decision(
            state,
            slot,
            spec_dir,
            pending,
            turn_reservation=turn_reservation,
            initial_aliases=aliases,
            alias_snapshot=alias_snapshot,
            own_name=name,
            expected_slot_key=expected_slot_key,
            dispatch_claim=dispatch_claim,
        )


async def _serialize_messages(state: Any, slot_key: str) -> list[dict]:
    """Return the spec slot's transcript for the embedded chat view. Prefers the
    live in-memory slot (includes in-progress turns); falls back to the persisted
    session log. Content is redacted before leaving the backend.

    ASYNC because the fallback reads the persisted transcript: a whole JSONL file
    off disk, which is exactly the case that matters (a rehydrated session with no
    in-memory messages, i.e. right after a gateway restart, which is when the user
    opens the spec again). Doing that inline stalled the gateway event loop for
    the length of the file.
    """
    msgs: list[Any] = []
    slot = state.get_slot(slot_key)
    if slot is not None and getattr(slot, "messages", None):
        msgs = list(slot.messages)
    else:
        try:
            # circular import (see module header): dashboard.server imports us.
            from kiro_crew.dashboard.chat_utils import _history_key_for

            if getattr(state, "conversation_log", None) is not None:
                msgs = await asyncio.to_thread(
                    state.conversation_log.read_messages, _history_key_for(slot_key)
                )
        except Exception:
            logger.debug("read_messages failed for %s", slot_key, exc_info=True)
    out: list[dict] = []
    for m in msgs:
        if isinstance(m, dict):
            role, content, ts = m.get("role", ""), m.get("content", ""), m.get("ts", "")
        else:
            role = getattr(m, "role", "")
            content = getattr(m, "content", "")
            ts = getattr(m, "ts", "")
        if role == "system":
            continue
        if role == "tool":
            # Mirror the main chat: surface tool activity as a compact line
            # (first line, bounded) so the embedded chat shows the agent working.
            first = (content or "").strip().splitlines()[0] if content else ""
            out.append({"role": "tool", "content": _redact_and_truncate(first, 200), "ts": ts})
            continue
        out.append({"role": role, "content": _redact(content or ""), "ts": ts})
    return out

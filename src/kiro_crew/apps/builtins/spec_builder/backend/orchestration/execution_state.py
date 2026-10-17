"""Autonomous execution a handoff arms: its nudge loop and durable status.

The index's ``executing`` status is agent-writable and the nudge loop can end on
its own at its cycle cap, so the live loop registry is the authority for whether a
run is active. This module owns reading that registry, the atomic
``planning -> executing`` claim, reconciling a finished run back to planning,
halting a run, and archiving durable loops that no index entry owns.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from .. import repository as _repository
from ..decisions import _CLAIM_TAKEN
from ..parsers import _SLOT_KEY_RE, _decision_key, _known_status, _owns_slot_key
from ..repository import (
    _DELETING,
    _DUPLICATING,
    APP_NAME,
    _aload_index,
    _audit,
    _forget_observed_slot_identity,
    _mutate_index,
    _slot_key,
    _touch_spec,
    _unindexed_observed_slot_keys,
    _write_stop_sentinel_for_spec,
)
from ..runtime import _UNPINNED, _halt_active_turn, _teardown_worker_slot

try:
    from kiro_crew.autonudge import AutoNudgeService as _AutoNudgeService
    from kiro_crew.autonudge import get_instance as _autonudge_instance
    from kiro_crew.autonudge_authz import authorize_and_add_nudge
except Exception:  # pragma: no cover - autonudge always present in prod
    _AutoNudgeService = None  # type: ignore[assignment,misc]
    _autonudge_instance = None  # type: ignore[assignment]
    authorize_and_add_nudge = None  # type: ignore[assignment]

logger = logging.getLogger("kirocrew.app.spec-builder")

# The autonomous nudge loop is capped rather than infinite. There is no trust
# TTL because this app does not grant trust — see the create handler.
_EXEC_MAX_CYCLES = 60

# Bound recovery on a fire callback whose history storage or provider ignores
# cancellation.  The inactive durable loop remains the retry marker on timeout.
_ORPHAN_QUIESCE_TIMEOUT_SECS = 2.0


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
            service.deactivate_and_wait(loop_id, stopped_reason="orphaned_worker"),
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


async def _remove_nudge_loop(
    name: str, *, only_loop_id: Any = _UNPINNED, stop_reason: str = ""
) -> None:
    """Remove this spec's autonudge loop, if any. Single site for the lookup so
    halt / delete / handoff-abort cannot drift apart.

    ``only_loop_id`` pins it to a loop the caller CAPTURED: the lookup is by slot
    key, which is derived from the name, so an unpinned removal on an abort path
    would cancel the loop belonging to a same-name spec created in the meantime.
    ``stop_reason`` names the stop in the loop's WARNING stop line.
    """
    await _remove_nudge_loop_for_slot(
        _slot_key(name), only_loop_id=only_loop_id, stop_reason=stop_reason
    )


async def _remove_nudge_loop_for_slot(
    slot_key: str, *, only_loop_id: Any = _UNPINNED, stop_reason: str = ""
) -> None:
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
        await svc.remove(loop.id, stop_reason=stop_reason)


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

    Deliberately does NOT touch ``slot._trust``. This app does not grant
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
    await _remove_nudge_loop(name, only_loop_id=only_loop_id, stop_reason="spec_stopped")
    # ...and stop the turn that is running RIGHT NOW. The sentinel and the loop
    # removal only prevent FUTURE nudges: the in-flight _run_chat kept going, so
    # Pause flipped the status to "planning" and returned ok while the agent
    # carried on editing the user's files. Cooperative stop first (the gateway's
    # own stop_turn), then a bounded cancel of the slot task as the fallback.
    await _halt_active_turn(state, name, only_slot=only_slot)
    _audit("spec_execution_halted", f"{name}: {reason}")


def _exec_prompt(name: str, spec_dir: Path, working_dir: str) -> str:
    return (
        f"{_EXECUTION_HANDOFF_PREFIX}{name}'. The plan is approved. Read "
        f"{spec_dir / 'tasks.md'} and work through each unchecked task IN ORDER, "
        f"operating inside {working_dir} (your shell already starts there — no cd needed). After each task: "
        f"mark its checkbox [x] in tasks.md, run the relevant build/tests to verify, "
        f"then continue. Stop when all tasks are checked or you hit a blocker that needs "
        f"me, and summarize what was done and what remains."
    )

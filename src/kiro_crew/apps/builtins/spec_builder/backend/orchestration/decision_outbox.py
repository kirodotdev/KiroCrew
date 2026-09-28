"""Relay of durable decision answers into the spec agent's turn.

``decisions`` owns the protected ledger and its ``pending -> relayed -> final``
rows. This module owns moving one row through a real turn: re-reading it,
revalidating the question, crossing the durable relay boundary, dispatching
without a duplicate chat row, and finalizing only once the chat runner reports
that the model consumed the prompt. Replay is the crash-recovery entry to the
same path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..decisions import (
    _abandon_pending_decision,
    _finalize_decision,
    _mark_decision_relayed,
    _pending_decision_is_current,
    _pending_decisions,
    _restore_decision_pending,
)
from ..parsers import _decision_key
from ..repository import _pin_legacy_slot_identity, _touch_spec
from ..runtime import _dispatch_turn, _reserve_slot_turn
from .dispatch_claims import (
    _bind_pending_dispatch_to_turn,
    _pending_dispatch_is_current,
    _release_pending_dispatch_when_done,
    _reserve_pending_dispatch,
)
from .turn_guard import (
    _alias_slots,
    _alias_turn_snapshot,
    _busy_alias,
    _final_alias_conflict,
    _turn_lock,
)

logger = logging.getLogger("kirocrew.app.spec-builder")


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

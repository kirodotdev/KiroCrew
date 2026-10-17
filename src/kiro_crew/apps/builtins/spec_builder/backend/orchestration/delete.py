"""Destructive delete: revoke, reserve, archive, then remove.

The order is the contract. The name is reserved before the runtime is captured,
the conversation is archived before the index entry is removed, and the decision
record is cleared only after the entry is gone, so every failure leaves either a
live spec with its settled answers or a reservation a retry can finish.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from ..decisions import (
    _aload_index_with_decision_alias_status,
    _decision_alias_conflict_locked,
    _forget_decisions,
)
from ..parsers import _decision_key
from ..repository import (
    _aload_index_with_slot_identity,
    _audit,
    _commit_delete_teardown,
    _forget_deleted,
    _forget_observed_slot_identity,
    _mark_deleting,
    _mutate_index,
    _remember_deleted,
    _slot_key,
    _unmark_deleting,
)
from ..runtime import _teardown_worker_slot
from .dispatch_claims import _execution_stop_barrier
from .execution_state import _exec_loop_id, _remove_nudge_loop
from .request_identity import (
    _STALE_CLIENT_ERROR,
    _client_claim,
    _client_identity_mismatch,
    _require_auth,
)
from .turn_guard import _turn_lock

logger = logging.getLogger("kirocrew.app.spec-builder")


async def _handle_delete(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    name = request.match_info["name"]
    # Body first, then the index: see _handle_stop_execution. A body await
    # between the two would let a replacement spec be the thing torn down.
    claimed = await _client_claim(request)
    index, doomed_runtime_slot_key, doomed_observed_slot_key = (
        await _aload_index_with_slot_identity(name)
    )
    if name not in index:
        return web.json_response({"code": "not_found", "error": "not found"}, status=404)
    doomed_dir = str(index[name].get("spec_dir", ""))
    # The creation we verified, carried to the commit below so the entry that gets
    # dropped is the one this request checked.
    doomed_slot_key = str(index[name].get("slot_key", ""))
    # A tampered raw key can differ from the authenticated identity this process
    # has already observed. Successful deletion owns both spellings and must release
    # both, otherwise a same-name recreation remains pinned to the deleted worker.
    # A legacy row has no persisted key, but its name-derived runtime key is still
    # the creation identity captured by this request. Carry that fallback through
    # both index transactions so a same-path replacement cannot satisfy an empty pin.
    # Prefer a non-empty raw spelling so a deliberately malformed/tampered row remains
    # a reachable cleanup endpoint for the authenticated runtime identity.
    doomed_commit_slot_key = doomed_slot_key or doomed_runtime_slot_key
    if _client_identity_mismatch(claimed, doomed_dir, doomed_runtime_slot_key):
        return web.json_response({"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409)
    # Hold the same directory lock as message and handoff across the destructive
    # sequence. Those handlers must observe either a live spec or no spec, never a
    # teardown window in which they can start a turn or record an answer.
    doomed_key = _decision_key(doomed_dir)
    # Publish the same creation-scoped revocation as Stop before waiting for the
    # mutable directory lock. If the agent repointed this name while a message's
    # final scan was off-thread, deleting through the new spelling must still
    # prevent that stale request from publishing onto the old slot afterwards.
    async with (
        _execution_stop_barrier(doomed_key, doomed_runtime_slot_key, name) as claimed_slot_keys,
        _turn_lock(doomed_key),
    ):
        _fresh_index, decision_alias_conflict, _decision_store_usable = (
            await _aload_index_with_decision_alias_status(doomed_dir)
        )
        if decision_alias_conflict:
            return web.json_response(
                {
                    "code": "decision_directory_alias_conflict",
                    "error": "multiple spec names resolve to this directory; repair the spec index before continuing",
                },
                status=409,
            )
        # Publish the tombstone before the entry becomes hidden; otherwise discovery
        # could re-adopt the documents during teardown. Every non-delete exit clears it.
        await asyncio.to_thread(_remember_deleted, doomed_dir)
        # Reserve rather than drop the name during teardown. This keeps same-name
        # creation out and lets rollback restore the original per-creation slot key.
        if not await _mark_deleting(
            name, expect_spec_dir=doomed_dir, expect_slot_key=doomed_commit_slot_key
        ):
            await asyncio.to_thread(_forget_deleted, doomed_dir)
            return web.json_response({"code": "not_found", "error": "not found"}, status=404)
        # RESERVED -- only now capture the runtime. Capturing before the reservation left
        # a window where a message could materialize a NEW slot (or arm a new loop) that
        # this capture had already passed: the teardown below then cancelled a stale
        # handle while the freshly-created session kept running the agent against files
        # the user had just deleted. With the marker set first, _touch_spec refuses that
        # message, so nothing new can appear between here and the teardown.
        state = request.app.get("state")
        doomed_loop_id = _exec_loop_id(name)
        doomed_slot = state.get_slot(_slot_key(name)) if state is not None else None
        doomed_slots = claimed_slot_keys.runtime_slots(state, doomed_slot)
        if not doomed_slots:
            # Preserve the teardown boundary even when no runtime slot exists.
            # The helper treats a pinned None as a no-op, while callers still get
            # one archive/failure boundary before the final index transaction.
            doomed_slots.append(None)
        # Stop any execution loop; leave the .md files on disk (they are the user's
        # project files under .kiro/specs) — only drop app bookkeeping + the slot.
        try:
            await _remove_nudge_loop(name, only_loop_id=doomed_loop_id, stop_reason="spec_deleted")
            await claimed_slot_keys.remove_other_loops(
                doomed_runtime_slot_key, doomed_loop_id, stop_reason="spec_deleted"
            )
        except Exception:
            # Fail the delete rather than report success: the entry is still in the
            # index, so a retry is meaningful, and the persisted loop cannot rearm
            # against a same-name spec re-imported later. Release the reservation and
            # the tombstone too -- both were taken above, and leaving either behind
            # would hide a spec the user still has from their own list.
            logger.warning("spec %s: loop removal failed — delete aborted", name, exc_info=True)
            await _unmark_deleting(name, expect_spec_dir=doomed_dir)
            await asyncio.to_thread(_forget_deleted, doomed_dir)
            _audit("spec_delete_aborted", name, outcome="denied")
            return web.json_response(
                {
                    "code": "loop_removal_failed",
                    "error": "could not stop this spec's background loop; nothing was deleted",
                },
                status=503,
            )
        # Tear down the worker as well as its nudge loop so an in-flight turn cannot
        # keep editing after deletion. The order mirrors gateway slot deletion: pop
        # from the registry, cancel and await the task, then persist as closed.
        #
        # require_archive: the conversation is user data, so deletion cannot report
        # success unless it is durably archived. Before any teardown, failure releases
        # the reservation; after one slot succeeds, the durable reservation remains so
        # a retry can finish the partial delete without claiming its queue is restorable.
        try:
            teardown_reserved = await _commit_delete_teardown(
                name,
                expect_spec_dir=doomed_dir,
                expect_slot_key=doomed_commit_slot_key,
            )
        except Exception:
            logger.warning(
                "spec %s: destructive delete boundary could not be saved",
                name,
                exc_info=True,
            )
            teardown_reserved = False
        if not teardown_reserved:
            await _unmark_deleting(name, expect_spec_dir=doomed_dir)
            await asyncio.to_thread(_forget_deleted, doomed_dir)
            _audit("spec_delete_reservation_failed", name, outcome="denied")
            return web.json_response(
                {
                    "code": "delete_reservation_failed",
                    "error": (
                        "could not reserve this spec's destructive cleanup; retry " "the delete"
                    ),
                },
                status=503,
            )
        archive_succeeded = True
        teardown_committed = False
        for slot_to_remove in doomed_slots:
            if not await _teardown_worker_slot(
                state, name, only_slot=slot_to_remove, require_archive=True
            ):
                archive_succeeded = False
                break
            teardown_committed = True
        if not archive_succeeded:
            if teardown_committed:
                # At least one slot has already been archived and had its queued
                # work discarded. Re-exposing the spec would claim that no delete
                # occurred even though that session cannot be restored. Keep the
                # reservation and tombstone so the next DELETE completes the
                # remaining idempotent teardown instead.
                claimed_slot_keys.commit()
                return web.json_response(
                    {
                        "code": "archive_failed",
                        "error": (
                            "part of this spec's conversation was archived; retry "
                            "the delete to finish cleanup"
                        ),
                    },
                    status=503,
                )
            released = await _unmark_deleting(name, expect_spec_dir=doomed_dir)
            # The spec lives again, so the tombstone must go: leaving it would suppress
            # the documents from discovery for a spec that was never deleted.
            await asyncio.to_thread(_forget_deleted, doomed_dir)
            detail = (
                "nothing was deleted"
                if released
                else "nothing was deleted; the spec may need a reload to reappear"
            )
            return web.json_response(
                {
                    "code": "archive_failed",
                    "error": f"could not archive this spec's conversation; {detail}",
                },
                status=503,
            )

        pop_refusal = ""

        def _pop_if_same(idx: dict) -> bool:
            nonlocal pop_refusal
            # Identity-pinned: a same-name spec cannot exist here (the name was reserved),
            # but the entry is still re-read under the lock, so pin it anyway rather than
            # trusting the snapshot this handler loaded before the awaits.
            meta = idx.get(name)
            if meta is None or str(meta.get("spec_dir", "")) != doomed_dir:
                return False
            actual_key = str(meta.get("slot_key", ""))
            if doomed_commit_slot_key and actual_key and actual_key != doomed_commit_slot_key:
                return False
            # The slot teardown above awaited while the agent could still write its
            # index. Refuse inside this final transaction if it minted a second lexical
            # ledger spelling in that window; popping now would strand the settled row
            # under the removed spelling and let the survivor create a conflicting one.
            if _decision_alias_conflict_locked(idx, doomed_dir):
                pop_refusal = "directory_alias"
                return False
            del idx[name]
            return True

        # A raised write failure and a False mutation both leave the entry reserved
        # after its transcript teardown; the ledger remains intact until pop succeeds.
        released_slot_keys = tuple(
            {
                doomed_slot_key,
                doomed_observed_slot_key,
                doomed_runtime_slot_key,
                *claimed_slot_keys.keys(),
            }
        )
        try:
            popped = await _mutate_index(
                _pop_if_same,
                on_commit=lambda: _forget_observed_slot_identity(name, *released_slot_keys),
            )
        except Exception:
            logger.warning("spec %s: the index entry could not be removed", name, exc_info=True)
            popped = False
        if not popped:
            if pop_refusal == "directory_alias":
                # Every captured slot is already archived. Keep the destructive
                # boundary visible rather than resurrecting a partially torn-down
                # spec; after the alias is repaired, retrying DELETE can finish the
                # idempotent index removal.
                claimed_slot_keys.commit()
                _audit("spec_decision_directory_alias_conflict", name, outcome="denied")
                return web.json_response(
                    {
                        "code": "decision_directory_alias_conflict",
                        "error": (
                            "multiple spec names resolve to this directory; repair "
                            "the spec index, then retry the delete"
                        ),
                    },
                    status=409,
                )
            # The conversation is ALREADY archived, so un-deleting would be the lie the
            # ordering above exists to prevent. The reservation stays, which keeps the
            # spec hidden and makes a retry idempotent: it re-runs a no-op teardown and
            # removes the entry.
            #
            # The recorded answers are untouched, which is why there is nothing to put
            # back: the ledger is only cleared once the entry is actually gone. The spec
            # still exists, so its settled decisions stay settled.
            logger.warning("spec %s: archived but the index entry could not be removed", name)
            return web.json_response(
                {
                    "code": "index_write_failed",
                    "error": (
                        "this spec's conversation was archived but its record could not be "
                        "removed; retry the delete"
                    ),
                },
                status=503,
            )
        # The spec is gone from the index. NOW the ledger entry can go: until this point
        # a failure had to leave the answers intact, because a spec that survives with
        # its answers erased is a decision silently reopened. From here a cleanup failure
        # is housekeeping -- logged, not fatal, and not worth failing a delete that has
        # already happened. It is not harmless on its own, though: a DIFFERENT spec can
        # later be created at this same path, and create closes that by clearing an
        # orphaned record before it registers one.
        forgot, still_referenced = await _forget_decisions(doomed_dir)
        if not forgot:
            _audit("spec_decision_record_stale", name, outcome="denied")
            logger.warning("spec %s: deleted, but its decision record could not be cleared", name)
        if not still_referenced:
            # The lock deliberately STAYS registered. Evicting it looked safe when no
            # other name referenced the directory, but "no reference" was read before
            # this line and cannot be relied on at it: a create can register the same
            # directory in that window, and -- worse -- a handler that called
            # _turn_lock() before the eviction is already waiting on the OLD object.
            # The next arrival would then be handed a BRAND-NEW lock and the two would
            # serialize against nothing, running concurrent turns over the same files:
            # exactly the hole the directory-keyed lock exists to close, reintroduced by
            # its own cleanup. There is no reference count that fixes this, because a
            # waiter holds the object rather than an index entry, so the eviction is
            # simply dropped. What remains is one small asyncio.Lock per directory that
            # ever had a turn -- a bounded, harmless residue next to a correctness hole.
            logger.debug("spec %s: keeping the turn lock registered for %s", name, doomed_key)
        claimed_slot_keys.commit()
        _audit("spec_delete", name)
        return web.json_response({"ok": True})

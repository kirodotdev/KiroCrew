"""Tearing down what a deleted transcript left behind: its live slot and its crew logs.

:func:`_remove_slot_for_history_key` removes only the exact slot and manager generation the
delete claim captured before the unlink and releases the cron jobs its retired owner keys
still hold; :func:`_remove_session_crew_logs` removes the deleted conversation's crew-log
units that nothing live still serves. The delete routes call it after the unlink. New
post-delete cleanup goes here.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable

from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.dashboard.handlers.sessions_parts.delete_claim import (
    _capture_history_delete_claim,
    _history_delete_candidate_keys,
    _HistoryDeleteClaim,
    _live_slot_sid,
    _release_cron_ownership,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import transcript_stem, transcript_stems
from kiro_crew.messaging.link import canonical_key

# The family logs under the facade's name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.dashboard.handlers.sessions")


async def _remove_slot_for_history_key(
    state: DashboardState,
    key: str,
    *,
    delete_claim: _HistoryDeleteClaim | None = None,
) -> None:
    """Remove only the exact slot and manager generation captured before unlink."""
    claim = delete_claim or _capture_history_delete_claim(state, key)
    slot = None
    if (
        claim.slot is not None
        and claim.registry_key is not None
        and claim.complete
        and claim.path_match_verified
    ):
        current_slot = state._slots.get(claim.registry_key)
        ownership_current = False
        if current_slot is claim.slot and claim.session_key is not None:
            try:
                ownership_current = (
                    slot_history_key(current_slot) == claim.history_key
                    and effective_session_key(current_slot) == claim.session_key
                    and getattr(current_slot, "task", None) is claim.slot_task
                    and state.sessions.session_generation(claim.session_key)
                    == claim.session_generation
                )
            except Exception:
                logger.warning(
                    "History delete: captured slot ownership is unreadable for %s; "
                    "preserving the slot",
                    key,
                    exc_info=True,
                )
        if current_slot is claim.slot and ownership_current:
            popped = state._slots.pop(claim.registry_key, None)
            if popped is claim.slot:
                slot = claim.slot
        else:
            logger.info(
                "History delete: slot %s was replaced, rerouted, or reclaimed; preserving it",
                claim.registry_key,
            )

    target_session_key = claim.session_key if slot is not None else None
    target_session_generation = claim.session_generation

    def live_slot_owns_session(session_key: str) -> bool:
        """Fail safe when any current slot owns or cannot resolve *session_key*."""
        canonical_session_key = canonical_key(session_key)
        for current_slot in tuple(state._slots.values()):
            try:
                if canonical_key(effective_session_key(current_slot)) == canonical_session_key:
                    return True
            except Exception:
                logger.warning(
                    "History delete: current slot session owner unreadable for %s; "
                    "preserving destructive cleanup",
                    key,
                    exc_info=True,
                )
                return True
        return False

    def live_runtime_owns_session(session_key: str) -> bool:
        """Fail safe when a slot or SessionManager runtime still owns the key."""
        if live_slot_owns_session(session_key):
            return True
        canonical_session_key = canonical_key(session_key)
        try:
            manager_keys = state.sessions.session_keys()
            return any(
                canonical_key(manager_key) == canonical_session_key for manager_key in manager_keys
            )
        except Exception:
            logger.warning(
                "History delete: current SessionManager owners unreadable for %s; "
                "preserving cron ownership",
                key,
                exc_info=True,
            )
            return True

    if slot:
        cancelled = state.cancel_questions_for_slot(slot.key)
        if cancelled:
            logger.info(
                "History delete: cancelled %d pending question(s) on slot %s",
                cancelled,
                slot.key,
            )
    if slot:
        teardown_tasks = {task for task in (slot.task,) if task is not None and not task.done()}
        if teardown_tasks:
            for task in teardown_tasks:
                task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*teardown_tasks, return_exceptions=True),
                    timeout=2.0,
                )
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

    session_destroyed = False
    if slot and target_session_key is not None:
        try:
            destroyed = await state.sessions.destroy_if(
                target_session_key,
                target_session_generation,
                lambda: not live_slot_owns_session(target_session_key),
                preserve_autocompact_override=True,
            )
            if not destroyed:
                logger.info(
                    "History delete: session %s was replaced, busy, reserved, or has "
                    "a current slot owner; preserving it",
                    target_session_key,
                )
            session_destroyed = bool(destroyed)
        except Exception:
            logger.warning(
                "History delete: conditional session destroy failed for %s",
                target_session_key,
                exc_info=True,
            )

    # Pins, work ledgers, and autocompact overrides are independent state. A
    # transcript can be created or restored by another process after any owner
    # scan, so deleting those sidecars cannot be made atomic here. Preserve them:
    # stale state is reversible, while deleting a successor's state is not.

    # The append-only CREW LOG is not one of those sidecars, and the difference is
    # mechanical rather than a re-reading of the rule above. A work ledger's identity
    # is the SLOT KEY, which is recycled, so no check here can prove a successor is not
    # about to inherit it. A crew-log unit is keyed by the ACP SESSION ID, and its
    # header -- written once inside the fenced tree -- names the slot it ran on, so the
    # units of THIS conversation can be told apart from anyone else's.
    #
    # A conversation owns one unit per ACP id it ran under: a reset, an agent or model
    # switch and a provider swap each start a new one. Which of them this delete takes
    # depends on what the claim proved.
    #
    # * A delete that tore down a slot takes the units the locked transaction excluded
    #   before the unlink (generation-proved, so never a successor's), plus the
    #   conversation's own unit only when the teardown SUCCEEDED: ``destroy_if``
    #   refuses for a replaced, busy or still-owned session, and that session may be
    #   writing right now. The write lease is no substitute, because ownership ends
    #   BETWEEN turns by design.
    # * A row with no open slot takes none. Its claim proves no unit id, and the slot
    #   key its headers name is recyclable: another conversation that reused it
    #   leaves superseded units the session map does not name, which a header
    #   match alone would hand to this delete. They are left to retention.
    crew_log_slots = {
        key,
        transcript_stem(key),
        key.removeprefix("dashboard:"),
        *_history_delete_candidate_keys(key),
    }
    crew_log_units: frozenset[str] = frozenset()
    if slot is not None:
        crew_log_slots.update(
            spelling
            for spelling in (
                getattr(claim.slot, "key", None),
                claim.session_key,
            )
            if isinstance(spelling, str) and spelling
        )
        proved = set(claim.ledger_excluded_units)
        own = claim.acp_session_id or ""
        if session_destroyed and own:
            proved.add(own)
        else:
            proved.discard(own)
        crew_log_units = frozenset(proved)
    if crew_log_units:
        spellings = frozenset(crew_log_slots)

        def live_units() -> "frozenset[str]":
            return _live_session_units(state) | frozenset(
                sid for spelling in spellings if (sid := _live_slot_sid(state, spelling))
            )

        await asyncio.to_thread(
            _remove_session_crew_logs,
            state.sessions,
            key,
            spellings,
            crew_log_units,
            live_units(),
            live_units=live_units,
        )

    # Cron ownership is different from the independent sidecars above: every key
    # here came from the strict store scan or from linked_session_key while the
    # transcript lock was held. Re-check the live slot and SessionManager
    # registry after the teardown awaits, then compare-and-clear only those exact
    # retired owners in the cron store's own lock. A successor already presenting
    # one of the keys keeps it.
    proven_owner_keys = {owner for owner in claim.cron_owner_keys if owner}
    releasable_owner_keys = {
        owner for owner in proven_owner_keys if not live_runtime_owns_session(owner)
    }
    preserved_owner_keys = proven_owner_keys - releasable_owner_keys
    if preserved_owner_keys:
        logger.info(
            "History delete: current runtime owner(s) still present for %s; preserving cron "
            "ownership for %s",
            key,
            ", ".join(sorted(preserved_owner_keys)),
        )
    try:
        released = await _release_cron_ownership(
            getattr(state, "crons", None), releasable_owner_keys
        )
    except Exception:
        logger.warning("History delete: cron ownership release failed for %s", key, exc_info=True)
    else:
        if released:
            logger.info(
                "History delete: released %d cron job(s) for %s: %s",
                len(released),
                key,
                ", ".join(released),
            )


#: How long the delete funnel waits for the teardown entry to land before taking
#: the lease. Short on purpose: the flush is what lets the emitter release this
#: session's cached handle, and with it the write lease this removal must claim.
#: A timeout is not a failure -- the entry stays owed, so the unit becomes
#: collectable by the retention sweep even when this pass is refused.
_CREW_LOG_TEARDOWN_FLUSH_SECONDS = 2.0


def _live_session_units(state: DashboardState) -> "frozenset[str]":
    """The crew-log unit of every session the manager is running now. Never raises."""
    from kiro_crew.crew_log.resolve import unit_for_session_key

    units: set[str] = set()
    try:
        keys = tuple(state.sessions.session_keys())
    except Exception:
        logger.debug("History delete: running sessions unreadable", exc_info=True)
        return frozenset()
    for session_key in keys:
        try:
            unit = unit_for_session_key(state.sessions, session_key)
        except Exception:
            continue
        if isinstance(unit, str) and unit:
            units.add(unit)
    return frozenset(units)


def _remove_session_crew_logs(
    sessions: Any,
    history_key: str,
    slots: "frozenset[str]",
    proved: "frozenset[str]",
    protected: "frozenset[str]",
    *,
    live_units: "Callable[[], frozenset[str]] | None" = None,
) -> None:
    """Remove the crew-log units of a deleted conversation. Never raises.

    A unit is a candidate when its HEADER names one of *slots* -- the header is written
    once inside the fenced crew-log tree and never rewritten, so it does not move when
    ``session_map.json`` (agent-visible) does -- and it is one of the *proved* ids. It is then kept when:

    * *protected* names it: a live slot or a running session serves it now;
    * a session-map key of another conversation still maps it, or the map cannot be
      read: that holder can resume, and its log is still needed. A key is this
      conversation's when its transcript is the one this delete removed;
    * a writer holds its lease;
    * *live_units*, re-read under that lease just before the removal, names it. The
      *protected* snapshot is taken before the selection, and a same-key resume after
      it maps the unit back to this row's own key, which the map check accepts, while
      the lease is free between turns. Only a fresh read of what is running now tells
      that unit apart from a finished one. A read that fails keeps the unit.

    Every unit taken is excluded from its slot's ledger fold first, so a later session
    on the same recycled slot key cannot read the deleted conversation's state out of a
    unit whose removal failed. That exclusion is given back only for a unit kept because
    it is someone else's (the map, the trash hold or a live session); a unit of this
    conversation that could not be removed keeps it and is left to the retention sweep.

    Best-effort, like every other step of this teardown: the transcript row is already
    gone, so raising would turn a crew log that could not be collected into a failed
    delete the user has to retry against a row that is absent.
    """
    from kiro_crew import session_ledger

    try:
        from kiro_crew.crew_log import emit as crew_log_emit
        from kiro_crew.crew_log import store as crew_log_store
        from kiro_crew.crew_log.schema import KIND_SESSION

        # LET THE TEARDOWN ENTRY LAND FIRST: the emitter releases a session's cached
        # handle, and the write lease that handle carries, only once its close lands.
        # A timeout is not a failure; the unit is then ``owned`` and left to retention.
        crew_log_emit.flush(timeout=_CREW_LOG_TEARDOWN_FLUSH_SECONDS)
        by_slot = crew_log_store.session_units_by_slot()
    except Exception:
        logger.warning(
            "History delete: could not list the crew logs of %s", history_key, exc_info=True
        )
        return

    own_stems = set(transcript_stems(history_key)) | {transcript_stem(history_key)}

    def own(mapped: str) -> bool:
        return transcript_stem(mapped) in own_stems

    def mapped_elsewhere(unit: str) -> bool:
        try:
            first = sessions.find_key_by_sid(unit)
            if first is None:
                return False
            if not own(first):
                return True
            other = sessions.find_key_by_sid(unit, exclude=first)
        except Exception:
            return True
        return other is not None and not own(other)

    taken: dict[str, list[str]] = {}
    for slot_key in sorted(slots):
        for unit in by_slot.get(slot_key, ()):
            if unit not in proved:
                continue
            if unit in protected or mapped_elsewhere(unit):
                continue
            taken.setdefault(slot_key, []).append(unit)
    if not taken:
        return
    # What THIS transaction added, per slot: the only exclusions it may take back. A
    # unit a concurrent delete already excluded is that delete's to keep.
    added: dict[str, set[str]] = {}

    def give_back(slot_key: str, units: "set[str]") -> None:
        """Undo this transaction's exclusion of *units*: they stay, so they stay foldable."""
        mine = tuple(sorted(units & added.get(slot_key, set())))
        if not mine:
            return
        try:
            session_ledger.unexclude_units(slot_key, mine)
        except Exception:
            logger.error(
                "History delete: crew log(s) %s of %s were kept but stay excluded from slot "
                "%r's ledger record; restore them by hand in the slot's control directory",
                ", ".join(mine),
                history_key,
                slot_key,
                exc_info=True,
            )
            return
        added[slot_key] -= set(mine)

    try:
        for slot_key, units in taken.items():
            # The transcript is already unlinked, so nothing is left to refuse.
            recorded = session_ledger.exclude_units(slot_key, tuple(units), refusable=False)
            added[slot_key] = set(getattr(recorded, "added", ()) or ())
    except session_ledger.LedgerExclusionError:
        logger.error(
            "History delete: refusing to remove the crew logs of %s -- their slot's ledger "
            "exclusion could not be recorded",
            history_key,
        )
        for slot_key, recorded_units in list(added.items()):
            give_back(slot_key, set(recorded_units))
        return
    except Exception:
        logger.warning(
            "History delete: could not exclude the crew logs of %s", history_key, exc_info=True
        )
        for slot_key, recorded_units in list(added.items()):
            give_back(slot_key, set(recorded_units))
        return

    removed = 0
    for slot_key, units in taken.items():
        # Units this delete leaves in place because they are SOMEONE ELSE'S: mapped to
        # another conversation, held by the trash for a session awaiting restore, or
        # served by a session now. Only these get their exclusion back. A unit of this
        # conversation that could not be removed -- a writer's lease, a failed removal,
        # an error -- stays excluded, so a later session on the recycled slot key cannot
        # fold the deleted conversation's state out of it.
        kept: set[str] = set()
        for unit in units:
            # Re-proved here, after the exclusion: a key mapped onto this unit since the
            # selection makes it another conversation's again, and it must not be taken.
            if mapped_elsewhere(unit):
                kept.add(unit)
                continue
            refused: list[str] = []

            # The guard runs under the lease and asks two things: whether the trash
            # holds the unit -- it then belongs to a session still waiting to be
            # restored, whose slot key this row may reuse -- and whether a session or a
            # live slot serves it NOW, re-read here rather than taken from the snapshot
            # the selection used. Whether the unit is closed is not asked: the sole lease
            # refuses a unit a writer holds, and a gateway restart leaves a finished unit
            # with no close.
            def free(directory: Path, unit: str = unit, refused: list[str] = refused) -> bool:
                if crew_log_store.is_trash_held(directory):
                    refused.append("held by the trash")
                    return False
                if live_units is None:
                    return True
                try:
                    serving = unit in live_units()
                except Exception:
                    serving = True
                if serving:
                    refused.append("served by a session now")
                return not serving

            try:
                status = crew_log_store.remove_unit(KIND_SESSION, unit, guard=free)
            except Exception:
                logger.warning(
                    "History delete: could not remove crew log %r; it stays excluded from "
                    "slot %r's ledger record and is left to retention",
                    unit,
                    slot_key,
                    exc_info=True,
                )
                continue
            if status == crew_log_store.REMOVE_REMOVED:
                removed += 1
            elif refused:
                kept.add(unit)
                logger.info(
                    "History delete: crew log %r of %s kept (%s)", unit, history_key, refused[0]
                )
            elif status != crew_log_store.REMOVE_ABSENT:
                logger.info(
                    "History delete: crew log %r of %s not removed (%s); it stays excluded "
                    "from slot %r's ledger record and is left to retention",
                    unit,
                    history_key,
                    status,
                    slot_key,
                )
        give_back(slot_key, kept)
    if removed:
        logger.info("History delete: removed %d crew log(s) of %s", removed, history_key)

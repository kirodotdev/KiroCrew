"""The history delete routes: one session, the bulk clear, and the clearable count.

``DELETE /api/sessions/{key}`` and ``DELETE /api/sessions`` (closed sessions only) claim
their rows through :mod:`.delete_claim`, unlink, then tear the live slot down through
:mod:`.slot_teardown`; ``GET /api/sessions/clearable/count`` deletes nothing and resolves the
same set of rows the bulk clear removes. New delete routes go here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import replace
from typing import Any

from aiohttp import web

from kiro_crew.cron import CronStoreBusy, CronStoreUnreadable
from kiro_crew.dashboard.handlers._shared import guard_owner_surface_routes
from kiro_crew.dashboard.handlers.sessions_parts.app_scope import (
    _NOT_TRANSCRIPT_OWNER,
    _app_bulk_history_refusal,
    _app_not_found,
    _app_transcript_refusal,
    _audit_app_allow,
)
from kiro_crew.dashboard.handlers.sessions_parts.delete_claim import (
    CRON_OWNERSHIP_UNKNOWN_CODE,
    LEDGER_EXCLUSION_UNWRITABLE_CODE,
    _AppOwnershipLost,
    _capture_history_delete_claim,
    _cron_ownership_unknown_refusal,
    _cron_store_refusal,
    _delete_history_session,
    _exclude_slot_units,
    _history_delete_candidate_keys,
    _HistoryDeleteClaim,
    _ledger_rollback_refusal,
    _owner_keys_after_unlink,
    _owner_keys_bound_to_transcript,
    _OwnerKeyUnreadable,
    _rollback_ledger_exclusion,
    _unstrand_shared_ledger_unit,
)
from kiro_crew.dashboard.handlers.sessions_parts.history_reads import (
    _is_user_listed_row,
    _open_slot_transcript_keys,
)
from kiro_crew.dashboard.handlers.sessions_parts.slot_teardown import _remove_slot_for_history_key
from kiro_crew.dashboard.state import DashboardState

# The family logs under the facade's name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.dashboard.handlers.sessions")


async def api_session_delete(request: web.Request) -> web.Response:
    """DELETE /api/sessions/{key} — permanently delete a history session."""
    state: DashboardState = request.app["state"]
    key = request.match_info["key"]
    refusal = await _app_transcript_refusal(request, state, key, "session_delete")
    if refusal is not None:
        return refusal
    if not state.conversation_log:
        return web.json_response({"error": "no conversation log"}, status=400)
    # Freeze the slot/transcript/manager route before the first await. The strict
    # cron-store scan establishes exact owner keys independently of that route.
    request_app = str(request.get("app") or "")
    with contextlib.ExitStack() as delete_window:
        delete_claim = _claim_history_delete(delete_window, state, key)
        if (
            request_app
            and delete_claim.slot is not None
            and getattr(delete_claim.slot, "_app", "") != request_app
        ):
            # The transcript records this app, but the live slot the cleanup would
            # pop is not the app's: the slot is the server-side record, the metadata
            # line is not, so the slot decides. Same 404 as any other refusal.
            return _app_not_found(
                request_app, "session_delete", f"session={key}", "app does not own the live slot"
            )
        return await _delete_claimed_session(state, key, delete_claim, request_app)


def _claim_history_delete(
    windows: contextlib.ExitStack, state: DashboardState, key: str
) -> _HistoryDeleteClaim:
    """Open *key*'s delete-in-flight window on *windows*, then capture its claim.

    The one way a delete handler takes a slot claim. The claim names the slot the
    delete will remove afterwards, so the window has to be open from that instant
    until the cleanup ran: a resume that published before it is in the claim, and
    one that re-checks after it sees the window and refuses with a retryable
    ``resume_conflict``. Both steps are synchronous, so nothing can land
    between them. The caller closes *windows* after its slot cleanup.
    """
    assert state.conversation_log is not None
    windows.enter_context(state.conversation_log.delete_in_flight_window(key))
    return _capture_history_delete_claim(state, key)


async def _delete_claimed_session(
    state: DashboardState, key: str, delete_claim: _HistoryDeleteClaim, request_app: str
) -> web.Response:
    """The body of :func:`api_session_delete`, run inside its in-flight window."""
    assert state.conversation_log is not None
    crons = getattr(state, "crons", None)
    try:
        swept = await _owner_keys_bound_to_transcript(crons, (key,))
    except (CronStoreBusy, CronStoreUnreadable) as exc:
        return _cron_store_refusal(exc)

    # The ledger exclusion is written BEFORE the unlink, and a failure REFUSES the
    # delete with the row intact. It has to be this side of the unlink to be a
    # precondition at all: the crew-log removal below runs after the transcript is
    # already gone, so a failure there has nothing left to refuse, and the
    # surviving units of this slot would stay foldable by the next session on the
    # same recycled slot key. The claim's candidate slot is tied to this transcript
    # by its filename stems; if the delete then does not proceed, the exclusion is
    # rolled back, because those units belong to a session that still exists.
    from kiro_crew import session_ledger

    # Resolve ambiguous slot ownership and read linked_session_key inside the
    # same canonical-plus-legacy lock set, before the unlink destroys either
    # piece of evidence. An unreadable owner claim refuses with the row intact.
    try:
        ok, delete_claim = await asyncio.to_thread(
            _delete_history_session,
            state.conversation_log,
            key,
            delete_claim,
            exact_owner_keys=swept.get(key, ()),
            exclude=lambda _slot: _exclude_slot_units(state, session_ledger, delete_claim),
            owner_app=request_app,
        )
    except _AppOwnershipLost:
        return _app_not_found(
            request_app, "session_delete", f"session={key}", _NOT_TRANSCRIPT_OWNER
        )
    except _OwnerKeyUnreadable:
        return _cron_ownership_unknown_refusal(crons, swept.get(key, ()))
    except session_ledger.LedgerExclusionError:
        # Nothing was unlinked: the exclusion is written inside the same hold, before the
        # delete, so a refusal leaves the row intact.
        return web.json_response(
            {
                "error": (
                    "This session's ledger exclusions could not be recorded, so it was "
                    "not deleted. Deleting it now would leave its work state readable by "
                    "the next session in the same slot."
                ),
                "code": LEDGER_EXCLUSION_UNWRITABLE_CODE,
            },
            status=409,
        )

    if not ok:
        # Nothing was destroyed, so nothing may stay excluded: those units are a live
        # session's own record. A rollback that cannot be written answers retryable
        # rather than reporting a clean refusal over a record that now reads empty.
        try:
            await _rollback_ledger_exclusion(session_ledger, delete_claim, restore_carry=True)
        except session_ledger.LedgerExclusionError:
            return _ledger_rollback_refusal()

    if ok:
        if request_app:
            # Only a completed delete proves the in-hold ownership check ran.
            _audit_app_allow(request_app, "session_delete", f"session={key}")
        try:
            await _unstrand_shared_ledger_unit(state, session_ledger, delete_claim)
        except session_ledger.LedgerExclusionError:
            # The row IS gone: this runs after the unlink, so the answer must not say the
            # delete was refused. What is left is a surviving session whose record reads
            # empty, which is reported for what it is.
            logger.error(
                "History delete: %s was deleted, but a session sharing its crew log unit "
                "still has that unit excluded from its ledger record; restore it by hand "
                "in the slot's control directory",
                key,
            )
        # Catch a job created after the strict pre-scan but before the unlink.
        # The row is already gone, so a failed second scan is loud but cannot
        # refuse; the pre-unlink owner claim still proceeds to cleanup.
        after = await _owner_keys_after_unlink(crons, (key,))
        delete_claim = replace(
            delete_claim,
            cron_owner_keys=(delete_claim.cron_owner_keys | frozenset(after.get(key, ()))),
        )
        try:
            await _remove_slot_for_history_key(state, key, delete_claim=delete_claim)
        except Exception:
            logger.warning("cleanup failed for session %s", key, exc_info=True)
        state.release_deleted_slot_keys(_history_delete_candidate_keys(key))
        state.push_slots_update()
        state.push_refresh("history")
    return web.json_response({"ok": ok})


async def api_sessions_clear(request: web.Request) -> web.Response:
    """DELETE /api/sessions — permanently delete closed history sessions only.

    ``user_only=1`` narrows the clear to the rows ``GET /api/sessions?user_only=1``
    lists, which is what the Older Sessions "Delete all" sends. Without
    it every closed session is cleared, as before.
    """
    refusal = _app_bulk_history_refusal(request, "sessions_clear")
    if refusal is not None:
        return refusal
    state: DashboardState = request.app["state"]
    if not state.conversation_log:
        return web.json_response({"error": "no conversation log"}, status=400)

    log = state.conversation_log
    clearable, skipped, unreadable = await asyncio.to_thread(
        _clearable_history_keys, state, log, user_only=_user_only_query(request)
    )

    # One strict owner scan covers the whole selection. A known cron-store
    # failure refuses before any transcript is unlinked.
    crons = getattr(state, "crons", None)
    try:
        swept = await _owner_keys_bound_to_transcript(crons, clearable)
    except (CronStoreBusy, CronStoreUnreadable) as exc:
        return _cron_store_refusal(exc, cleared=0, skipped=skipped, failed=0)

    # Every row's window stays open until the batch's slot cleanup below has run,
    # because that is when the claim it took is acted on.
    with contextlib.ExitStack() as delete_windows:
        return await _clear_history_rows(
            state, log, clearable, skipped, unreadable, swept, crons, delete_windows
        )


async def _clear_history_rows(
    state: DashboardState,
    log: Any,
    clearable: list[str],
    skipped: int,
    unreadable: list[str],
    swept: dict[str, set[str]],
    crons: Any,
    delete_windows: contextlib.ExitStack,
) -> web.Response:
    """The per-row body of :func:`api_sessions_clear`, inside the rows' windows."""
    from kiro_crew import session_ledger

    count = 0
    failed = 0
    cleanup_claims: list[tuple[str, _HistoryDeleteClaim]] = []
    undeletable: list[dict[str, str]] = [
        {"id": key, "code": CRON_OWNERSHIP_UNKNOWN_CODE} for key in unreadable
    ]
    for key in clearable:
        # Re-check per iteration so a tab opened during an earlier await wins.
        if key in _open_slot_transcript_keys(state):
            skipped += 1
            continue

        delete_claim = _claim_history_delete(delete_windows, state, key)
        # Per ROW, the same precondition the single delete applies: the ledger
        # exclusion is written before this row's transcript is unlinked, because a
        # failure after the unlink has nothing left to refuse and would leave this
        # slot's surviving units foldable by the next session on the recycled key. A
        # row whose exclusion cannot be written is reported undeletable and its
        # transcript is left alone; a row that then does not delete is rolled back.
        try:
            result, delete_claim = await asyncio.to_thread(
                _delete_history_session,
                log,
                key,
                delete_claim,
                skip_pinned=True,
                exact_owner_keys=swept.get(key, ()),
                exclude=lambda _slot: _exclude_slot_units(state, session_ledger, delete_claim),
            )
            if result is None:
                skipped += 1
                await _rollback_ledger_exclusion(session_ledger, delete_claim, restore_carry=True)
            elif result:
                # Counted as CLEARED whatever the unstrand does: the transcript is gone,
                # so calling the row undeletable would be false. A failure there leaves a
                # sharing session's record excluded, which is logged for what it is.
                try:
                    await _unstrand_shared_ledger_unit(state, session_ledger, delete_claim)
                except session_ledger.LedgerExclusionError:
                    logger.error(
                        "Bulk clear: %s was deleted, but a session sharing its crew log "
                        "unit still has that unit excluded from its ledger record; restore "
                        "it by hand in the slot's control directory",
                        key,
                    )
                cleanup_claims.append((key, delete_claim))
                count += 1
            else:
                failed += 1
                await _rollback_ledger_exclusion(session_ledger, delete_claim, restore_carry=True)
        except _OwnerKeyUnreadable:
            undeletable.append({"id": key, "code": CRON_OWNERSHIP_UNKNOWN_CODE})
        except session_ledger.LedgerExclusionError:
            # The exclusion or its rollback could not be written, so this row's transcript
            # is left alone and the row says why. Nothing was unlinked: the exclusion runs
            # inside the same hold, before the delete.
            undeletable.append({"id": key, "code": LEDGER_EXCLUSION_UNWRITABLE_CODE})
        except Exception:
            failed += 1
            # The rollback can fail too, and it must not turn one row's failure into the
            # whole batch's: the remaining rows are still deletable.
            try:
                await _rollback_ledger_exclusion(session_ledger, delete_claim, restore_carry=True)
            except session_ledger.LedgerExclusionError:
                logger.error(
                    "Bulk clear: %s was not deleted and its ledger exclusion could not be "
                    "rolled back; that session's record reads empty until it is restored "
                    "by hand in the slot's control directory",
                    key,
                )
            logger.warning("api_sessions_clear: delete raised for %s", key, exc_info=True)

    if cleanup_claims:
        # One post-unlink scan catches owners created anywhere in the batch
        # window. Extend each immutable claim before its fenced cleanup runs.
        after = await _owner_keys_after_unlink(
            crons, [cleanup_key for cleanup_key, _claim in cleanup_claims]
        )
        cleanup_tasks = [
            _remove_slot_for_history_key(
                state,
                cleanup_key,
                delete_claim=replace(
                    delete_claim,
                    cron_owner_keys=(
                        delete_claim.cron_owner_keys | frozenset(after.get(cleanup_key, ()))
                    ),
                ),
            )
            for cleanup_key, delete_claim in cleanup_claims
        ]
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        state.release_deleted_slot_keys(
            candidate
            for cleanup_key, _claim in cleanup_claims
            for candidate in _history_delete_candidate_keys(cleanup_key)
        )
    if count:
        state.push_slots_update()
        state.push_refresh("history")
    logger.info(
        "api_sessions_clear: cleared=%d skipped=%d failed=%d undeletable=%d",
        count,
        skipped,
        failed,
        len(undeletable),
    )
    # ``ok`` means nothing clearable was left behind: an undeletable row stays on
    # disk exactly as a failed one does, so either one makes the clear partial.
    return web.json_response(
        {
            "ok": failed == 0 and not undeletable,
            "cleared": count,
            "skipped": skipped,
            "failed": failed,
            "undeletable": undeletable,
        }
    )


def _user_only_query(request: web.Request) -> bool:
    """The ``user_only`` flag, parsed exactly as ``GET /api/sessions`` parses it."""
    return (request.query.get("user_only") or "").lower() in ("1", "true", "yes")


def _clearable_history_keys(
    state: DashboardState,
    log: Any,
    *,
    user_only: bool = False,
) -> tuple[list[str], int, list[str]]:
    """The history sessions a bulk clear would remove.

    With *user_only*, a row the ``user_only`` listing hides (a machine
    namespace, or a never-used session) is left out entirely: it is neither
    cleared nor counted as skipped, because the reader never saw it.

    ONE implementation, shared by ``api_sessions_clear`` and the clearable-count
    endpoint, so the number a confirmation displays cannot drift from the set the
    delete takes. Two implementations would let the dialog
    promise a number the delete does not honour, which is the whole reason a
    count exists.

    Takes NO age cutoff, deliberately. ``DELETE /api/sessions`` accepts none and
    removes every clearable session, so a count filtered by age would report a
    SUBSET of what the delete then permanently unlinks — a confirmation showing a
    smaller number than the delete honours. There is no cutoff to offer until the
    delete itself grows one, and then both sides grow it together through this
    function.

    Returns ``(clearable, skipped, unreadable)``. A session is skipped when it is
    reachable as an open tab or when its metadata says ``pinned``. A session whose
    metadata could not be read is excluded and listed in ``unreadable`` by key,
    rather than folded into a count the caller cannot act on. Present but
    unparseable metadata is unreadable: ``get_metadata_status`` returns
    ``({}, False)``, so both the preview and delete exclude it rather than treating
    missing identity as permission. The bulk clear reports those rows by id and
    code; the single delete refuses them with 409 ``cron_ownership_unknown``.

    Reads the filesystem (``list_sessions`` globs and stats every session file),
    so callers offload it off the event loop.
    """
    open_keys = _open_slot_transcript_keys(state)

    clearable: list[str] = []
    skipped = 0
    unreadable: list[str] = []
    for row in log.list_sessions():
        key = row.get("key", "")
        if not key:
            continue
        if user_only and not _is_user_listed_row(log, row):
            continue
        if key in open_keys:
            skipped += 1
            continue
        # Mirror delete_session(skip_pinned=True): pinned and unreadable metadata
        # both mean "leave it alone". This read is unlocked, so what comes back is
        # a SNAPSHOT the delete may narrow: it re-checks pinned under its lock and
        # re-checks open tabs per iteration, so a session pinned or reopened after
        # this pass is skipped there. The count can therefore over-report a
        # concurrent change, but nothing here can make the delete take a session
        # its own locked check refuses.
        try:
            meta, readable = log.get_metadata_status(key)
        except Exception:
            # Not reachable through get_metadata_status's documented returns; a
            # genuinely unexpected failure must not read as permission to delete.
            unreadable.append(key)
            continue
        if not readable or not isinstance(meta, dict):
            # Reported by key rather than folded into ``skipped``: the single
            # delete refuses this row with 409 cron_ownership_unknown, and a
            # clear that only said "skipped: N" hid which rows can never be
            # cleared and why (the by-id remedy needs the id).
            unreadable.append(key)
            continue
        if meta.get("pinned"):
            skipped += 1
            continue
        clearable.append(key)
    return clearable, skipped, unreadable


async def api_sessions_clearable_count(request: web.Request) -> web.Response:
    """GET /api/sessions/clearable/count — how many sessions a bulk clear removes.

    The confirmation for a bulk delete has to state how many sessions it will
    remove, and ``DELETE /api/sessions`` offers no way to learn that before
    committing. This answers the question and nothing else — it
    is a GET, so no code path here can delete anything.

    A GET rather than a ``dry_run`` flag on the DELETE, deliberately departing
    from ``POST /api/system/session-storage/cleanup``'s pattern: a flag on the
    destructive verb means a caller that drops the flag deletes instead of
    counting, while a GET cannot delete however it is called.

    Takes one parameter, ``user_only``, read the same way the DELETE reads it.
    The count is of exactly the set ``DELETE /api/sessions`` removes for the same
    flag, because that delete accepts no cutoff — see
    :func:`_clearable_history_keys` for why offering one here would report a
    subset of what the delete actually takes.
    """
    refusal = _app_bulk_history_refusal(request, "sessions_clearable_count")
    if refusal is not None:
        return refusal
    state: DashboardState = request.app["state"]
    if not state.conversation_log:
        return web.json_response(
            {"error": "no conversation log", "code": "count_unavailable"}, status=400
        )

    clearable, _skipped, _unreadable = await asyncio.to_thread(
        _clearable_history_keys,
        state,
        state.conversation_log,
        user_only=_user_only_query(request),
    )
    return web.json_response({"sessions": len(clearable)})


# Every ``api_session*`` handler is an owner surface, so a private member's
# internal call is refused before it runs (audit label = handler name). Deleting
# the owner's history is the owner's call, so a non-owner dashboard session is
# refused too.
guard_owner_surface_routes(
    globals(),
    prefix="api_session",
    member_scoped=frozenset(),
    dashboard_owner_only=True,
)

"""The Subagents panel's finished runs, folded out of the crew log.

The durable half of the ``subscribe_subagents`` replay: ``ws.py`` resolves the
slots and gates the frames; this module reads each unit's ``subagents`` fold into
bounded, newest-first panel records and counts what the row cap cut. ``ws.py``
re-exports every name here, so callers keep importing from it.
"""

from __future__ import annotations

import heapq
import logging
import time
from collections.abc import Callable, Collection

logger = logging.getLogger(__name__)

#: What a card says about a child whose closer recorded no usable ending. The same
#: wording the run-folder reader gives an orphan, because it is the same fact: the
#: writer went away without recording how the run ended.
_ORPHANED_UNKNOWN = "Orphaned (unknown cause)"


def fold_subagent_outcome(outcome: str, reason: str) -> tuple[str, str, bool]:
    """One fold row's ending as the ``(outcome, error, stopped)`` a card draws.

    The fold keeps the subagent runtime's OWN outcome string, deliberately: that
    enum is open, so enforcing a set at the fold would turn "the upstream
    vocabulary grew" into a lost record. A card cannot draw an open set, and the
    client's terminal classification falls through to ``done`` for a value it does
    not recognise -- so a child whose ending the log recorded as ``unknown`` would
    be drawn as a success. That is the one mapping this does: a value outside the
    three the client reads becomes ``failed``, which is what the persisted reader
    this replaced answered for an ending it could not classify either.

    The error text follows the same rule. The closer's own ``reason`` is used when
    it wrote one; crash-repair writes none, and its row gets the wording the
    folder reader gave an orphan, because that is the same fact -- the writer went
    away without recording an ending.

    A stop is not an error, so it carries no text: the consumers drop it for a
    stop anyway, and a stop drawn with error text reads as a failure.
    """
    if outcome == "stopped":
        return "stopped", "", True
    if outcome == "completed":
        return "completed", "", False
    if outcome == "failed":
        return "failed", reason, False
    return "failed", reason or _ORPHANED_UNKNOWN, False


def read_fold_subagent_records(
    units: list[tuple[str, str]],
    *,
    keep: int,
    max_age_secs: float,
    exclude_ids: Collection[str],
    admit: Callable[[dict], bool],
    now: float | None = None,
    cut: dict[str, dict] | None = None,
) -> tuple[list[dict], int]:
    """Bounded, newest-first panel records folded out of the CREW LOG.

    The durable half of the Subagents panel, and the crew log is where it comes
    from: the log is the record, so every count, outcome, duration and cost a card
    draws is a fold of it rather than a second store kept in step by hand. What
    this reads is the ``subagents`` fold's rows plus the ``class`` fold's ``app``,
    both folded in ONE pass per unit.

    *units* is ``(slot_key, unit_id)`` per crew-log UNIT, resolved on the event
    loop by the caller. A unit id is the ACP session id the log was opened under,
    which is NOT the slot's session key: a slot owns one id at a time, so its
    history is spread over a unit per id it ran under and a finished child can sit
    in any of them (``session_ledger.crew_log_units``). The slot is carried
    alongside because it is what a frame is routed by and what the ownership gate
    asks about, and deriving it back from a unit id is not possible.

    ``app`` comes from the ``class`` fold, which keeps the FIRST owner the session
    ever had and never moves off it. That is what the cross-app guard needs: a
    slot key is caller-supplied and not namespaced, so the key a run was recorded
    under can later be minted by a different app, and comparing the live slot's
    owner against the log's own recorded owner is what refuses the old run to the
    new one.

    A dismissed run never reaches here: the user's dismissal is an entry in this
    same log, so the ``subagents`` fold stops offering the row and this read has
    no dismissal store to consult. The log is where it has to be recorded, because
    a store beside the log is reclaimed on its own schedule: a registry keyed on
    the run's folder at both ends drops its record when that folder is pruned,
    while this read still finds the child the log kept.

    Only a CLOSED child is returned. A row with no outcome is a child still in
    flight, and this process is not the one tracking it: a card drawn from it
    would wear a running pill nothing will ever advance. The fold's own ``running``
    count is what states those, and the panel section draws it.

    Ordered newest-first on each row's ``started_ms``, which is why the fold keeps
    it: ``seq_spawned`` orders rows inside one log and says nothing across two.
    Returns the records and how many ``keep`` cut. A caller passing *cut* also
    gets that count per slot, as ``{slot: {"n": cut, "at_least": bool}}``, with
    ``at_least`` set for a slot one of whose units had a finished run cut by the
    fold's own retain cap and contributed an admitted row.

    Blocking. A caller on the event loop runs it on
    :func:`~kiro_crew.dashboard.subagent_replay_scope.replay_executor`.
    """
    from kiro_crew.dashboard.subagent_replay_scope import UNIT_ROWS

    if keep <= 0:
        return [], 0
    tally = {} if cut is None else cut
    stamp = time.time() if now is None else now
    cutoff = (stamp - max_age_secs) * 1000.0 if max_age_secs > 0 else float("-inf")
    skip = set(exclude_ids)
    # A bounded min-heap on the dispatch stamp, so the working set is capped
    # whatever the folds hold.
    #
    # The two keys after the stamp are what make the order TOTAL, and a heap
    # needs that: on any tie the tuple comparison falls through to the next key,
    # and a dict as the next key raises ``TypeError``. Neither earlier key is
    # enough on its own. ``started_ms`` is a millisecond clock, so a host whose
    # granularity is coarser gives every child of one burst the same stamp.
    # ``seq`` then orders them -- but only within ONE unit, since seqs are per
    # log and a row the fold could not date reads 0, so two units tie on both.
    # ``order`` is the position this row was pushed in, unique by construction,
    # so the fall-through always stops before the record. It says nothing about
    # time; it is there to be an arbitrary order that is at least the SAME one
    # on every read of the same logs.
    newest: list[tuple[float, int, int, dict]] = []
    order = 0
    overflow = 0
    for slot_key, unit_id in units:
        if not slot_key or not unit_id:
            # No slot is no card to route to, and no unit is no log to fold.
            continue
        try:
            # The OWNER comes from ``crew_log.read.recorded_class``, not from this
            # unit's class projection directly, because an absent answer and an
            # unowned session are different facts that the projection's own ``app``
            # cannot tell apart: both read as "". A unit whose class history has a
            # hole in it, or whose front was trimmed by retention, or that never
            # recorded a class at all, would then be published as owned by nobody --
            # which compares EQUAL to a slot with no owning app, so the cross-app
            # guard admits the records and the old app's runs, task text included,
            # reach the slot's new holder. That function owns all three refusals and
            # answers None for each, so an untrustworthy history skips the unit
            # instead of lowering the guard. Both folds are cached by the unit's
            # bytes (``UNIT_ROWS``), so an unchanged log is not folded again.
            recorded_app, rows, capped = UNIT_ROWS.rows_and_cap(unit_id)
            if recorded_app is None:
                logger.debug(
                    "crew log: %s's class history is not readable, so its subagent "
                    "rows are skipped rather than published as owned by nobody",
                    unit_id,
                )
                continue
            app = recorded_app
        except Exception:
            logger.debug("crew log: folding %s for the subagent panel failed", unit_id)
            continue
        floor = capped
        for agent_id, row in rows.items():
            if not isinstance(row, dict):
                continue
            agent_id = str(agent_id)
            outcome = row.get("outcome")
            if not outcome or not isinstance(outcome, str):
                continue
            if agent_id in skip:
                continue
            started_ms = row.get("started_ms")
            started_ms = float(started_ms) if isinstance(started_ms, (int, float)) else 0.0
            if started_ms < cutoff:
                continue
            ending, error, stopped = fold_subagent_outcome(outcome, str(row.get("reason") or ""))
            ms = row.get("ms")
            credits = row.get("credits")
            record = {
                "id": agent_id,
                "task": str(row.get("task") or ""),
                "agent": str(row.get("agent") or ""),
                "model": str(row.get("model") or ""),
                "app": app,
                "slot": slot_key,
                "unit": unit_id,
                "started": started_ms / 1000.0,
                "elapsed": ms / 1000.0 if isinstance(ms, int) and not isinstance(ms, bool) else 0.0,
                "outcome": ending,
                "error": error,
                "stopped": stopped,
            }
            # Absent, not zero: ``usage`` owns the session's credit aggregate and a
            # row carries only what THIS child reported, so a child that was never
            # billed must not be published as one that cost nothing.
            if isinstance(credits, (int, float)) and not isinstance(credits, bool):
                record["credits"] = float(credits)
            # The caller's own visibility and ownership bounds, BEFORE the cap.
            # Filtering afterwards lets a record the caller may not see occupy a
            # slot its own runs need, and makes the cut count describe a
            # population that is not the caller's.
            if not admit(record):
                continue
            # The cap kept the unit's FIRST dispatches, so the ones it cut are newer
            # than this row and share its slot and owner: inside the window, and the
            # caller's own.
            if floor:
                tally.setdefault(slot_key, {"n": 0, "at_least": False})["at_least"] = True
                floor = False
            seq = row.get("seq_spawned")
            seq = seq if isinstance(seq, int) and not isinstance(seq, bool) else 0
            order += 1
            if len(newest) < keep:
                heapq.heappush(newest, (started_ms, seq, order, record))
                continue
            overflow += 1
            # Displaced on the SAME total order the sort below reports, so what the
            # cap keeps is the prefix of the order it publishes. Comparing the stamp
            # alone refuses to displace on a tie, so a burst of children sharing one
            # millisecond keeps whichever arrived FIRST and then reports them
            # newest-first -- a list that is ordered but is not the newest runs.
            if newest[0][:3] < (started_ms, seq, order):
                record = heapq.heappushpop(newest, (started_ms, seq, order, record))[3]
            # Counted under the slot of the record that left, which is not always
            # the one being read.
            tally.setdefault(record["slot"], {"n": 0, "at_least": False})["n"] += 1
    # Newest first on the SAME total order the heap used, so a tie resolves the
    # same way here as it did there. Sorting on the stamp alone leaves tied rows
    # in whatever order the heap happened to hold them, which is a different
    # order on a different platform for identical logs.
    ordered = sorted(newest, key=lambda item: item[:3], reverse=True)
    return [item[3] for item in ordered], overflow

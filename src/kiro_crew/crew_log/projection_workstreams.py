"""The ``workstreams`` slot fold: every board a slot reaches, each task costed by its own
worker, and the units that board closure spans.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Final

# Reads the work-unit listing (``work_crew_log_units``) from ``session_ledger``.
from kiro_crew import session_ledger
from kiro_crew.crew_log.entry_types import WORK_CHARGE_ENTRY_TYPE, WORK_ENTRY_TYPE
from kiro_crew.crew_log.projection_usage import _CREDIT_SOURCE_OF, _credit_charge
from kiro_crew.crew_log.projection_values import TEXT_LIMIT, _as_id, _as_int, _as_str, _rows_in
from kiro_crew.crew_log.projection_work import (
    _WORK_BASELINE_FIELDS,
    _WORK_CONDUCTOR_FIELDS,
    _WORK_WORKER_FIELDS,
    _work_field,
    _work_iso,
    _work_stamp,
    _work_text,
)
from kiro_crew.crew_log.schema import Entry

# workstreams -- every board this slot reaches, each task costed by its own worker
# --------------------------------------------------------------------------- #
#
# WHY A SECOND FOLD OVER THE SAME ENTRIES. ``work`` answers for ONE board: it binds
# to the slot, drops every entry naming another, and resets itself when a new
# generation appears under that slot. A project report asks a wider question -- what
# is this crewmate running, across every board its own units and its workers' units
# reach -- and it asks what each task COST, which no work entry carries. Neither half
# can come from ``work``: the board it drops is a workstream the report must show, and
# the spend lives in the ``usage`` entries of the bound worker's own log.
#
# So this fold groups by BOARD, keyed by ``(slot, generation)``, and joins each task
# to the spend of the worker session bound to it. It reuses what is already here
# rather than restating it: the same work entry type and the same conductor/worker
# field tables as ``work``, and the same ``_bill_credits`` screen (via
# :data:`_CREDIT_SOURCE_OF` and :func:`_credit_charge`) the ``usage`` fold bills with.
#
# HOW A CREDIT ENTRY FINDS ITS WORKER. An ``Entry`` carries no slot, so the fold reads
# the one entry that marks where a unit begins and names its session's slot --
# ``session/opened`` -- and attributes every charge after it to that slot. That is the
# same marker ``usage`` keeps in :data:`USAGE_TYPES` for the same reason. A charge in a
# unit whose opener this fold never saw is attributed to nothing and counted in
# ``spend_unattributed``, never silently to the board: an unattributable charge is a
# gap in the report, and reporting it as a task's cost would be an invention.
#
# COST IS NEVER ZERO BY DEFAULT. A task whose worker reported no charge renders
# ``credits: 0.0`` WITH ``credits_reported: false``, which is the ``usage`` fold's own
# posture (``reported`` beside each bucket): an unmetered provider writes no
# ``credits`` key, and folding that in as a measurement nobody made is the one error
# a spend report cannot be corrected out of.

#: Boards one fold retains, newest activity first. A crewmate reads its own
#: workstreams, and a page that lists more than this is not read -- so the cap is the
#: page's, and every board past it is counted in ``omitted`` rather than dropped
#: silently.
WORKSTREAMS_BOARD_LIMIT: Final[int] = 12

#: Tasks retained per board. Counted per board in ``tasks_omitted``, so a board that
#: ran more says so instead of reading as complete at the cap.
WORKSTREAMS_TASK_LIMIT: Final[int] = 40

#: Hourly buckets kept per spender and per board: two days, which is the window the
#: report's bar chart draws. The NEWEST are kept; older hours fall off the front.
WORKSTREAMS_SERIES_LIMIT: Final[int] = 48

#: Spender slots one fold retains. A board's workers are bound by its conductor, so
#: this is bounded in practice by the boards x their items; the cap is what keeps a
#: slot whose units name an unbounded number of sessions from growing the state.
WORKSTREAMS_SPENDER_LIMIT: Final[int] = WORKSTREAMS_BOARD_LIMIT * WORKSTREAMS_TASK_LIMIT

#: Relayed closer ids remembered so a closer is billed once (:func:`_workstreams_charged`).
WORKSTREAMS_CHARGED_LIMIT: Final[int] = 4096

#: Units one read of this fold may collect, across every round of the bind walk. The
#: ceiling that makes the walk a bounded read rather than one that grows with the bind
#: graph, and the walk's ONLY bound: the fold shows 12 boards of 40 tasks, so a slot
#: reaching more units than this has more history than the page can draw either way.
WORKSTREAMS_UNIT_LIMIT: Final[int] = 512

#: Work events one TASK's drawer shows, newest first. The drawer answers "what is
#: this, and is the claim true" with the item's own narrative, and twenty lines is
#: where a reader stops scrolling and starts asking the crewmate instead.
WORKSTREAMS_EVENT_LIMIT: Final[int] = 20

#: Events the WHOLE fold carries, across every board and task.
#:
#: The per-task cap alone does not bound this: 12 boards of 40 tasks at 20 events
#: each is 9,600 lines, which is neither a state worth checkpointing nor a page a
#: frame will mint. So the fold keeps ONE newest-first ring across every task and
#: spends it globally -- a busy board's recent work crowds out an idle board's older
#: work, which is the right way round for a reader opening a drawer.
#:
#: A task whose lines fell out of the ring is not silently empty: it carries
#: ``events_seen``, so the drawer says "20 of 34" or "no events recorded" and never
#: implies a task did nothing when the truth is that the fold stopped carrying it.
WORKSTREAMS_EVENT_BUDGET: Final[int] = 400

#: Characters of the conductor's ``decision`` a task row carries.
#:
#: The store takes 2000 and the ``work`` fold returns all of it. This fold cuts it,
#: because here it is one of 480 possible rows rather than one board's own field, and
#: the drawer shows the ruling's substance in its first lines. A reader who needs the
#: whole text has the conductor's own board for it.
WORKSTREAMS_DECISION_LIMIT: Final[int] = 400

# The entry types this fold reads are :data:`WORKSTREAMS_TYPES`, declared beside the
# ``usage`` fold's billing table because they are spelled FROM it.


def _workstreams_hour(stamp_ms: int) -> str:
    """An entry's epoch-millisecond ``time`` as the UTC hour it fell in.

    UTC, and not the local spelling ``_work_iso`` renders: an hourly bucket is a KEY,
    two buckets are compared by it, and a local offset that shrinks at an autumn DST
    change spells a later hour as an earlier string -- so the chart's bars would swap
    places for one hour a year. The report draws a bar per hour and labels it from
    this key, so the key has to be ordered by the time it names.
    """
    try:
        moment = datetime.fromtimestamp(stamp_ms / 1000, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return ""
    return moment.strftime("%Y-%m-%dT%H:00Z")


def _workstreams_bump(series: dict[str, Any], hour: str, key: str, amount: float) -> None:
    """Add *amount* to *hour*'s *key* bucket, keeping the newest hours only.

    The trim is by KEY order, which :func:`_workstreams_hour` makes a time order, so a
    late-arriving entry for an hour already evicted does not reopen it at the cost of
    the newest one.
    """
    if not hour:
        return
    bucket = series.get(hour)
    if bucket is None:
        if len(series) >= WORKSTREAMS_SERIES_LIMIT and hour < min(series):
            # Older than every hour kept and the window is full: the chart does not
            # draw it, and admitting it would evict an hour the chart does draw.
            return
        bucket = series[hour] = {"credits": 0.0, "accepted": 0}
        for stale in sorted(series)[: max(0, len(series) - WORKSTREAMS_SERIES_LIMIT)]:
            del series[stale]
        bucket = series.setdefault(hour, {"credits": 0.0, "accepted": 0})
    grown = bucket[key] + amount
    if isinstance(grown, float) and not math.isfinite(grown):
        return
    bucket[key] = grown


def _workstreams_start() -> dict[str, Any]:
    return {
        # The board this fold was asked about, which is the slot whose units lead the
        # fold. Kept so a render can say which of the boards is the crewmate's own.
        "slot": "",
        # The session slot of the unit being folded, taken off its ``session/opened``.
        # This is what a charge is attributed to; empty means no opener has been seen
        # in this unit yet, and a charge then lands in ``spend_unattributed``.
        "cursor": "",
        "boards": {},
        "order": [],
        "omitted": 0,
        # Session units this fold could not read, because the ceiling on what it
        # retains was reached. Seeded by the bind hook from the selection that chose
        # them; 0 means the record's whole reachable history is in this answer.
        "units_omitted": 0,
        # Per spender: what that session spent, how many charges the total covers, how
        # long its turns ran, and its own hourly buckets. Joined to a task at RENDER by
        # the task's ``worker_session_key`` rather than at step time, because a bind
        # can be folded after the charge it explains (the conductor's units lead, but a
        # board recorded from a worker's own baseline has no conductor unit at all).
        "spend": {},
        "spenders_omitted": 0,
        # The relayed closers already billed (``work/charged``), oldest first, as a dict
        # for O(1) membership. Bounded at :data:`WORKSTREAMS_CHARGED_LIMIT`.
        "charged": {},
        # Charges in a unit whose opener was never folded. A gap the report states
        # rather than a cost it assigns to a task that did not incur it.
        "spend_unattributed": 0.0,
        "spend_unattributed_reported": 0,
        # THE NEWEST ENTRY THIS FOLD APPLIED, which is how the page tells "nothing is
        # happening" from "nothing is reaching me". A gateway that died mid-run leaves
        # every number on the page intact and correct as of a time the page cannot
        # otherwise name, so without this the report reads as current forever.
        #
        # Across every entry kind the fold steps, not only work records: a charge
        # arriving with no work entry beside it is still the log advancing, and
        # treating that as silence would raise the band while spend was still moving.
        "last_entry_ms": 0,
        "last_entry_at": "",
        # ONE newest-last ring of work events across every board and task, which is
        # what a task drawer reads its own narrative from. Global rather than per
        # task because the per-task cap alone does not bound the fold -- see
        # `WORKSTREAMS_EVENT_BUDGET` for why, and for what a task whose lines fell
        # out of it says instead of nothing.
        "events": [],
    }


def _workstreams_bind_slot(state: dict[str, Any], slot: str) -> None:
    """The board this fold was asked about, as the reader names it.

    The unit-overflow count is collected here too, because this is the one hook that
    runs between choosing the units and rendering them. A fold cannot count what it
    never saw: a unit refused for want of room contributes no entry, so without this
    the snapshot would report a short answer as a whole one.
    """
    state["slot"] = slot
    state["units_omitted"] = _workstreams_units_dropped(slot)


def _workstreams_new_board(board_id: str, slot: str, generation: str) -> dict[str, Any]:
    return {
        "id": board_id,
        "slot": slot,
        "generation": generation,
        "goal": "",
        "round": 0,
        "items": {},
        "order": [],
        "tasks_omitted": 0,
        "last_activity_ms": 0,
        "last_activity_at": "",
        "series": {},
    }


def _workstreams_new_item(item_id: str) -> dict[str, Any]:
    """One task row's state, and THE declaration of what this fold keeps per item.

    The step copies a field only when the item already has a key for it, so this dict
    is the filter: the event tail, the acceptance, the artifacts map and the
    conductor's ``decision`` are absent because the report draws one line per task and
    the ``work`` fold already serves the board in full. ``decision`` in particular is
    2000 clamped characters per item that nothing on this page renders.
    """
    return {
        "item_id": item_id,
        "title": "",
        "state": "open",
        "verdict": None,
        "worker_session_key": None,
        "round": 0,
        "status": None,
        "summary": "",
        "pr": None,
        # WHEN THIS TASK LAST PRODUCED ANYTHING, so a reader can tell a worker that is
        # thinking from one that is dead -- the question the row's duration cannot
        # answer, because duration is work time and reads the same either way.
        #
        # Written from the entry's own time on the actions where the WORKER is the one
        # speaking, and deliberately NOT on a conductor action: a decide or a verdict
        # is the conductor moving, and counting it would reset the silence of exactly
        # the worker whose silence is being asked about.
        #
        # The millisecond stamp is kept beside its ISO rendering for the reason the
        # board's pair is: the comparison is numeric, and an ISO string is empty for a
        # stamp no ``datetime`` can hold.
        "last_report_ms": 0,
        "last_report_at": "",
        # WHEN THIS TASK OPENED AND WHEN IT CLOSED -- the two ends of its span, which
        # `last_report_at` and `duration_ms` together cannot give. Duration is the
        # WORKER SESSION's measured work time, so a session bound to three tasks
        # reports one figure for all three and nothing that places any of them on a
        # clock; `last_report_at` is one instant. A per-worker timeline needs a start
        # and an end per task, which is what these two are -- and they are spelled
        # exactly as the ``work`` fold spells its own, so a page that reads one fold's
        # span reads the other's with the same helper rather than a second vocabulary.
        #
        # ISO, not the millisecond pair ``last_report_at`` keeps beside it, because
        # nothing here COMPARES them: the opener is written once and the closer is
        # written from the item's own state, so neither needs the numeric guard a
        # monotonic watermark does. ``_workstreams_item_span`` is the only writer.
        "created_at": "",
        "closed_at": "",
        # The conductor's latest ruling on this item, clamped -- see
        # `WORKSTREAMS_DECISION_LIMIT`. The drawer shows it because "what did the
        # conductor decide" is the question a reader opens one task to ask.
        "decision": "",
        # How many events this task has produced, ALL of them, whether or not the
        # fold is still carrying their text. The count and the carried tail are
        # separate facts: a drawer reading only the tail cannot tell a task that did
        # nothing from one whose lines fell out of the global ring, and saying "no
        # events recorded" about the second is a lie about the work.
        "events_seen": 0,
        # The hour this item was ACCEPTED, so the chart's accepted line is drawn from
        # the item's own transition rather than recomputed from a board total. Kept on
        # the item (not added straight into the series) because a later close can move
        # an item out of ``accepted`` again, and a counter already added cannot be
        # moved back -- see ``_workstreams_item_state``.
        "accepted_hour": "",
    }


def _workstreams_board_of(state: dict[str, Any], entry: Entry) -> dict[str, Any] | None:
    """The board *entry* belongs to, minting it on first sight, or ``None`` when the
    fold is already at its board cap.

    Keyed by ``(slot, generation)``, because that pair is what names ONE board: a slot
    reused after an earlier board was purged carries both boards' entries forever (the
    log is append-only), and the generation is the id minted with each board's record.
    A board from before the stamp existed has no generation and keys on its slot alone,
    which is the one board such a slot can have.
    """
    data = entry.data
    slot = _as_str(data.get("slot"))
    if not slot:
        return None
    generation = _as_str(data.get("generation"))
    board_id = f"{slot}#{generation}" if generation else slot
    board = state["boards"].get(board_id)
    if board is None:
        if len(state["boards"]) >= WORKSTREAMS_BOARD_LIMIT:
            state["omitted"] += 1
            return None
        board = state["boards"][board_id] = _workstreams_new_board(board_id, slot, generation)
        state["order"].append(board_id)
    if entry.time >= board["last_activity_ms"]:
        board["last_activity_ms"] = entry.time
        board["last_activity_at"] = _work_iso(entry.time)
    return board


def _workstreams_event(
    state: dict[str, Any], board_id: str, item: dict[str, Any], entry: Entry
) -> None:
    """Record one work event into the fold's global ring, if it is one.

    An entry with no ``event_kind`` is a field write and not a line of narrative, so
    it is counted nowhere: the work vocabulary makes the kind the thing that says
    "this is an event", and the ``work`` fold reads it the same way.

    The count on the item moves whether or not the text is carried, which is what
    lets a drawer distinguish a task that did nothing from one the ring has moved
    past. Oldest lines fall off the front, because the question a drawer asks is
    about now.
    """
    kind = _as_str(entry.data.get("event_kind"))
    if not kind:
        return
    item["events_seen"] = _as_int(item.get("events_seen")) + 1
    events: list[dict[str, Any]] = state["events"]
    status = entry.data.get("status")
    events.append(
        {
            "board": board_id,
            "item_id": item["item_id"],
            "at": _work_iso(entry.time),
            "kind": kind[:TEXT_LIMIT],
            "status": _as_str(status)[:TEXT_LIMIT] if isinstance(status, str) else None,
            # Cut to `TEXT_LIMIT` by `_as_str` itself, which is the cut this field
            # wants and the reason there is no second limit beside it: a drawer line
            # is read at a glance, and the full text is in the `work` fold for
            # anyone who needs it. A constant here would add a number that never
            # fires, because `_as_str` has already cut below it.
            "text": _as_str(entry.data.get("event")),
        }
    )
    if len(events) > WORKSTREAMS_EVENT_BUDGET:
        del events[: len(events) - WORKSTREAMS_EVENT_BUDGET]


def _workstreams_item_state(
    board: dict[str, Any], item: dict[str, Any], new: str, hour: str
) -> None:
    """Move *item* to state *new*, keeping the board's accepted-per-hour line honest.

    An item can be accepted and then closed again under another state, so the line is
    maintained as a DIFFERENCE: the hour an item was accepted in is remembered on the
    item, and a move out of ``accepted`` takes its count back off that same hour. A
    counter incremented on the way in and never on the way out would leave the chart
    claiming an acceptance the board does not hold.
    """
    was = item["state"]
    item["state"] = new
    if was == new:
        return
    if new == "accepted":
        item["accepted_hour"] = hour
        _workstreams_bump(board["series"], hour, "accepted", 1)
    elif was == "accepted":
        _workstreams_bump(board["series"], item["accepted_hour"], "accepted", -1)
        item["accepted_hour"] = ""


def _workstreams_span_opened(item: dict[str, Any], data: Mapping[str, Any], stamp_ms: int) -> None:
    """Stamp *item*'s ``created_at`` if it has none yet. Idempotent after that.

    Written ONCE, by whichever entry reaches the item first -- which is not always a
    ``create``. This fold mints an item from any action that names it (see
    :func:`_workstreams_work`), so a board rebuilt from a baseline, or a worker report
    folded ahead of its conductor's unit, would otherwise have no opener at all and
    its row could not be placed on a clock.

    A committed stamp the entry carries wins over the append time, the way
    :func:`_work_stamp` already has it, so a span survives a rebuild unchanged.
    """
    if not item["created_at"]:
        item["created_at"] = _work_stamp(data.get("created_at"), stamp_ms)


def _workstreams_span_closed(item: dict[str, Any], data: Mapping[str, Any], stamp_ms: int) -> None:
    """Keep *item*'s ``closed_at`` in step with the state it now holds.

    Read off the item's own ``state`` AFTER the entry has been applied, rather than
    from the ``close`` action, and that is what makes it ordering-tolerant: the state
    is already guarded by :func:`_workstreams_item_state`, so this follows it instead
    of keeping a second opinion about which action closes an item.

    AND IT IS CLEARED WHEN AN ITEM REOPENS. An item can be closed and then moved back
    to ``open`` -- the same transition ``_workstreams_item_state`` takes an accepted
    count back off an hour for -- and a close stamp left standing would draw a bar
    that ended, for a task that is still running.

    Called on the CONDUCTOR path only, because only a conductor entry can be the one
    that closed an item. A terminal item this fold never saw a conductor entry for
    keeps an empty ``closed_at``, which is the fold's own posture everywhere else:
    stamping it with whatever entry happened to arrive next would state a close time
    the record never claimed, and a page reading it would draw a bar ending at a
    moment nothing happened.
    """
    if item["state"] == "open":
        item["closed_at"] = ""
    elif not item["closed_at"]:
        item["closed_at"] = _work_stamp(data.get("closed_at"), stamp_ms)


def _workstreams_work(state: dict[str, Any], entry: Entry) -> None:
    """Apply one work record to the board it names."""
    data = entry.data
    board = _workstreams_board_of(state, entry)
    if board is None:
        return
    action = _as_str(data.get("action"))
    hour = _workstreams_hour(entry.time)
    if action == "goal":
        board["goal"] = _work_text("goal", data.get("goal"))
        board["round"] = max(board["round"], _as_int(data.get("round")))
        return
    item_id = _as_id(data.get("item_id"))
    if not item_id:
        state["omitted"] += 1
        return
    item = board["items"].get(item_id)
    if item is None:
        if len(board["items"]) >= WORKSTREAMS_TASK_LIMIT:
            board["tasks_omitted"] += 1
            return
        # Minted by whichever action reaches the item first, not only by ``create``.
        # The units are folded conductor-first, but a worker's report can still be the
        # first record of an item this fold sees -- a board rebuilt from a baseline, or
        # a conductor unit pruned -- and parking the entry the way ``work`` does would
        # buy a report ordering this report-per-task row does not need.
        item = board["items"][item_id] = _workstreams_new_item(item_id)
        board["order"].append(item_id)
        if data.get("baseline") is True:
            # A BASELINE CARRIES THE WHOLE COMMITTED ITEM, so it seeds every field
            # this fold keeps before the action delta below narrows to that action's
            # own. Without this a task first seen through its close arrives with the
            # close fields and nothing else: no title, no verdict, no worker -- a row
            # the reader sees as an untitled task that was never worked, when the
            # entry it was built from named all three.
            for name in _WORK_BASELINE_FIELDS:
                if name in data and name in item:
                    item[name] = _work_field(name, data[name])
            # The worker's own stamp rides along for the same reason the fields do:
            # a baselined task that reported before the rebuild is not silent.
            stamp = data.get("last_report_at")
            if isinstance(stamp, str) and stamp:
                item["last_report_at"] = _work_stamp(stamp, entry.time)
    _workstreams_event(state, board["id"], item, entry)
    # The span's open end, before either party's field copy: it depends on no field
    # and must be set by whichever entry got here first, whoever wrote it.
    _workstreams_span_opened(item, data, entry.time)
    if data.get("actor") == "worker" or action == "report":
        # THE WORKER SPOKE, so this is the stamp the row's silence is measured from.
        # Set before the field copy and unconditionally: a report that carries no
        # field this fold keeps is still the worker producing something, and reading
        # it as silence would call a working worker dead.
        if entry.time >= item["last_report_ms"]:
            item["last_report_ms"] = entry.time
            item["last_report_at"] = _work_iso(entry.time)
        for name in _WORK_WORKER_FIELDS:
            if name in data and name in item:
                item[name] = _work_field(name, data[name])
        return
    fields = _WORK_CONDUCTOR_FIELDS.get(action, ())
    for name in fields:
        if name not in data or name not in item:
            continue
        if name == "state":
            _workstreams_item_state(board, item, _work_field("state", data[name]), hour)
            continue
        if name == "decision":
            # CUT HERE, not at the store's 2000: `_work_field` returns a decision
            # whole because the `work` fold serves one board, and this one serves up
            # to 480 rows inside a page. See `WORKSTREAMS_DECISION_LIMIT`.
            item[name] = _work_text(name, data[name])[:WORKSTREAMS_DECISION_LIMIT]
            continue
        item[name] = _work_field(name, data[name])
    # AFTER the field copy, so the state this reads is the one this entry set.
    _workstreams_span_closed(item, data, entry.time)
    if "round" in data:
        board["round"] = max(board["round"], _as_int(data.get("round")))


def _workstreams_spend(state: dict[str, Any], entry: Entry) -> None:
    """Attribute one closer of THIS board's own session to the session that incurred it."""
    data = entry.data
    billed = _credit_charge(data.get("credits"))
    # ``duration_ms`` is the turn closer's spelling and ``ms`` the subagent and
    # background closers'; reading only one keeps half the log's durations out.
    measured = _as_int(data.get("duration_ms")) or _as_int(data.get("ms"))
    if billed is None and not measured:
        return
    slot = state["cursor"]
    if not slot:
        # No opener folded in this unit, so there is nothing this charge can be
        # attributed to. Counted, never assigned.
        if billed is not None:
            grown = state["spend_unattributed"] + billed
            if math.isfinite(grown) and grown >= state["spend_unattributed"]:
                state["spend_unattributed"] = grown
                state["spend_unattributed_reported"] += 1
        return
    _workstreams_bill(state, slot, billed, measured, entry.time)


def _workstreams_charged(state: dict[str, Any], entry: Entry) -> None:
    """One bound worker's charge, relayed into this board's log (``work/charged``).

    Attributed to the worker the entry names, and counted ONCE per closer: a closer can
    reach this log more than once (relayed up through a nested board, or retried), and
    each copy carries the same closer id. The ids seen are bounded at
    :data:`WORKSTREAMS_CHARGED_LIMIT`, oldest forgotten first.
    """
    data = entry.data
    # Read before ``_as_str``, which cuts to ``TEXT_LIMIT``.
    raw_closer = data.get("closer")
    raw_closer = raw_closer if isinstance(raw_closer, str) else ""
    by = _as_str(data.get("by"))[:TEXT_LIMIT]
    if not raw_closer or not by:
        return
    # The key is a digest of the WHOLE closer, so two closers that share a long prefix
    # (a long session id, different seq) are never one key, and the key stays small.
    closer = hashlib.sha256(raw_closer.encode("utf-8")).hexdigest()[:32]
    seen = state["charged"]
    if closer in seen:
        return
    seen[closer] = True
    while len(seen) > WORKSTREAMS_CHARGED_LIMIT:
        seen.pop(next(iter(seen)))
    billed = _credit_charge(data.get("credits"))
    measured = _as_int(data.get("duration_ms"))
    if billed is None and not measured:
        return
    _workstreams_bill(state, by, billed, measured, entry.time)


def _workstreams_bill(
    state: dict[str, Any], slot: str, billed: "float | None", measured: int, stamp: int
) -> None:
    """Add one charge and duration to *slot*'s spend row."""
    row = state["spend"].get(slot)
    if row is None:
        if len(state["spend"]) >= WORKSTREAMS_SPENDER_LIMIT:
            state["spenders_omitted"] += 1
            return
        row = state["spend"][slot] = {
            "credits": 0.0,
            "reported": 0,
            "duration_ms": 0,
            "series": {},
        }
    if measured:
        row["duration_ms"] += measured
    if billed is None:
        return
    # The same invariant ``_bill_credits`` keeps, for the same reason: a spend total
    # stays finite and never goes down, and a broken one survives in the savepoint.
    grown = row["credits"] + billed
    if not math.isfinite(grown) or grown < row["credits"]:
        return
    row["credits"] = grown
    row["reported"] += 1
    _workstreams_bump(row["series"], _workstreams_hour(stamp), "credits", billed)


def _workstreams_advanced(state: dict[str, Any], entry: Entry) -> None:
    """Remember that the fold applied *entry*, for the page's own staleness.

    Guarded on ``>=`` like the board's stamp: a unit folded out of order must not
    pull the watermark backwards and make a current page claim it is old.
    """
    if entry.time >= state["last_entry_ms"]:
        state["last_entry_ms"] = entry.time
        state["last_entry_at"] = _work_iso(entry.time)


def _workstreams_step(state: dict[str, Any], entry: Entry) -> None:
    if entry.type == "session/opened":
        # Where a unit begins, and the only entry that names its session's slot. Every
        # charge after this line is that session's until the next opener.
        state["cursor"] = _as_str(entry.data.get("slot"))[:TEXT_LIMIT]
        return
    if entry.type == WORK_ENTRY_TYPE:
        _workstreams_advanced(state, entry)
        _workstreams_work(state, entry)
        return
    if entry.type == WORK_CHARGE_ENTRY_TYPE:
        _workstreams_advanced(state, entry)
        _workstreams_charged(state, entry)
        return
    if entry.type in _CREDIT_SOURCE_OF:
        _workstreams_advanced(state, entry)
        _workstreams_spend(state, entry)


def _workstreams_copy(state: dict[str, Any]) -> dict[str, Any]:
    """A copy of every container ``step`` can reach, and no deeper.

    The kernel requires a new object on a real change and the same one on none, so the
    step edits a copy. Spelled out rather than left to the deep-copy fallback because
    this state nests three levels (board -> item, board -> series, spender -> series)
    and a deep copy of all of it is paid on every entry the fold reads -- which, with
    three closers in ``affects``, is most of a busy worker's log.
    """
    boards = {}
    for board_id, board in state["boards"].items():
        boards[board_id] = {
            **board,
            "items": {item_id: dict(item) for item_id, item in board["items"].items()},
            "order": list(board["order"]),
            "series": {hour: dict(bucket) for hour, bucket in board["series"].items()},
        }
    return {
        **state,
        "boards": boards,
        "order": list(state["order"]),
        # The ring itself is copied; its rows are not, because nothing mutates a row
        # after it is appended -- an event is a line of record, written once.
        "events": list(state["events"]),
        "charged": dict(state["charged"]),
        "spend": {
            slot: {
                **row,
                "series": {hour: dict(bucket) for hour, bucket in row["series"].items()},
            }
            for slot, row in state["spend"].items()
        },
    }


def _workstreams_series(series: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One board's hourly buckets as a list, OLDEST FIRST, at the series cap.

    Oldest first because the chart draws left to right in time; the cap is applied
    here as well as in the step so a merge of several spenders' windows cannot hand a
    reader more hours than the contract states.
    """
    hours = sorted(series)[-WORKSTREAMS_SERIES_LIMIT:]
    return [
        {
            "hour": hour,
            "credits": round(float(series[hour]["credits"]), 6),
            "accepted": int(series[hour]["accepted"]),
        }
        for hour in hours
    ]


def _workstreams_merge(into: dict[str, Any], series: Mapping[str, Any]) -> None:
    """Add every bucket of *series* into *into*, keeping the newest hours."""
    for hour, bucket in series.items():
        _workstreams_bump(into, hour, "credits", float(bucket["credits"]))
        _workstreams_bump(into, hour, "accepted", int(bucket["accepted"]))


def _workstreams_bound_tasks(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Which task each worker session was bound to, keyed by that session's slot.

    THE NESTING LINK. A bound worker can be a conductor of its own board, and that
    board's slot IS the worker session key its parent bound -- the same identity
    :func:`_workstreams_units` follows to reach the sub-board's units. So the task a
    nested board hangs under is already in this state, and the link is read here
    rather than recorded in the step: a bind can be folded after the sub-board's own
    entries, and a link written at step time would then be missing for exactly the
    boards that have one.

    First bind wins, in board order then item order, so a worker bound to two tasks
    names one parent and names the same one on every read. Bounded by the board and
    task caps already applied, so this map adds no limit of its own.
    """
    bound: dict[str, dict[str, Any]] = {}
    for board_id in state["order"]:
        board = state["boards"].get(board_id)
        if board is None:
            continue
        for item_id in board["order"][:WORKSTREAMS_TASK_LIMIT]:
            item = board["items"].get(item_id)
            if item is None:
                continue
            worker = item["worker_session_key"]
            if not isinstance(worker, str) or not worker or worker in bound:
                continue
            bound[worker] = {
                "board": board_id,
                "item_id": item_id,
                "title": item["title"],
            }
    return bound


def _workstreams_tails(state: Mapping[str, Any]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """The fold's global event ring, grouped into one newest-first tail per task.

    Grouped at RENDER rather than kept per task at step time, because the budget is
    global: a per-task list would have to be trimmed against a total it cannot see,
    and the trimming would have to walk every other task to know whose turn it is.
    One pass over a bounded ring answers it instead.

    Keyed by (board, item) and not by item alone: two boards can carry the same item
    id -- a board rebuilt under a new generation keeps its predecessor's ids -- and
    merging their lines would put one board's narrative in the other's drawer.
    """
    tails: dict[tuple[str, str], list[dict[str, Any]]] = {}
    events = state.get("events")
    if not isinstance(events, list):
        return tails
    # REVERSED, so the newest line is first and the per-task cut keeps the newest.
    for row in reversed(events):
        if not isinstance(row, Mapping):
            continue
        key = (_as_str(row.get("board")), _as_str(row.get("item_id")))
        tail = tails.setdefault(key, [])
        if len(tail) >= WORKSTREAMS_EVENT_LIMIT:
            continue
        tail.append({name: row[name] for name in ("at", "kind", "status", "text") if name in row})
    return tails


def _workstreams_render(state: dict[str, Any]) -> dict[str, Any]:
    """The report's own shape: one row per board, each task costed by its worker.

    THE JOIN HAPPENS HERE, not in the step, so it cannot depend on whether a bind was
    folded before or after the charge it explains.

    A board's ``credits`` sums its DISTINCT workers' spend. A worker session bound to
    two items of one board spent its credits once, so each task row reports that
    session's spend and the board counts it a single time -- a board total built by
    adding up its task rows would multiply one session's bill by the number of items it
    served. ``credits_reported`` is false exactly when no charge was folded for the
    sessions concerned, which is what tells "spent nothing" from "nobody measured".

    Each task row also carries the ``spender`` it was costed through, and a
    nested board carries the ``parent`` task it hangs under
    (:func:`_workstreams_bound_tasks`). Those two together make this flat board list a
    TREE a reader can roll up: the parent link says where a sub-board belongs, and the
    spender is what lets a roll-up bill one worker once however many tasks of that
    subtree it served -- the rule this render already keeps for a board total, and the
    one a reader adding up task rows cannot otherwise reproduce.

    ``spender`` is an OPAQUE per-render alias, never the worker's session key. This
    payload is embedded in a page any dashboard caller can read, and the conductor
    ledger's rule is that no reader but the conductor sees a session key. An alias
    keeps the only property a reader needs -- two rows billed to the same worker
    carry the same token, two different workers do not -- while naming nobody. It is
    minted per render, so the same worker is a different token in the next read and
    the tokens cannot be accumulated across reads into a worker's history.
    """
    spend: Mapping[str, Any] = state["spend"]
    bound = _workstreams_bound_tasks(state)
    tails = _workstreams_tails(state)
    boards: list[dict[str, Any]] = []
    everywhere: dict[str, Any] = {}
    counted: set[str] = set()
    #: session key -> alias, for THIS render only. Ordinal rather than a hash: a hash
    #: of a session key is still derived from it, so a caller holding a candidate key
    #: could confirm a guess by hashing it the same way.
    aliases: dict[str, str] = {}

    def spender_of(worker: Any) -> str | None:
        """The alias for *worker*, minting one on first sight. None when unbound."""
        if not isinstance(worker, str) or not worker:
            return None
        alias = aliases.get(worker)
        if alias is None:
            #: A bare ordinal: each template supplies its own label around it, so a
            #: token carrying the word "worker" would render as "worker worker 2".
            alias = str(len(aliases) + 1)
            aliases[worker] = alias
        return alias

    #: board id -> alias, for THIS render only. A board's own id is its conductor's
    #: slot key (plus a generation), so the id is a THIRD party's session key and
    #: leaks the same way a worker's would. The alias keeps what a page needs from an
    #: id -- telling two boards apart, and a sub-board finding its parent -- and the
    #: goal beside it is what a reader actually reads.
    board_aliases: dict[str, str] = {}

    def board_alias_of(board_id: str) -> str:
        alias = board_aliases.get(board_id)
        if alias is None:
            alias = f"board-{len(board_aliases) + 1}"
            board_aliases[board_id] = alias
        return alias

    for board_id in state["order"]:
        board = state["boards"].get(board_id)
        if board is None:
            continue
        tasks: list[dict[str, Any]] = []
        total = 0.0
        charges = 0
        board_series: dict[str, Any] = {}
        _workstreams_merge(board_series, board["series"])
        seen: set[str] = set()
        needs_you = 0
        accepted = 0
        for item_id in board["order"][:WORKSTREAMS_TASK_LIMIT]:
            item = board["items"].get(item_id)
            if item is None:
                continue
            worker = item["worker_session_key"]
            row = spend.get(worker) if isinstance(worker, str) and worker else None
            if item["state"] == "accepted":
                accepted += 1
            # OPEN items only. A worker's last report outlives the conductor's close,
            # so a closed item can still carry the `question` or `blocked` it was
            # closed over, and counting it asks the person about finished work.
            if item["state"] == "open" and item["status"] in ("question", "blocked"):
                needs_you += 1
            tasks.append(
                {
                    "item_id": item_id,
                    "title": item["title"],
                    "state": item["state"],
                    "status": item["status"],
                    "summary": item["summary"],
                    "verdict": item["verdict"],
                    "pr": item["pr"],
                    "round": item["round"],
                    # The spender the cost came through, so a roll-up over a subtree
                    # can bill it once. A task with no bound worker carries None,
                    # which is also why it has no cost of its own.
                    "spender": spender_of(worker),
                    "credits": round(float(row["credits"]), 6) if row else 0.0,
                    # FALSE, never a zero cost: a worker whose provider does not bill
                    # in credits wrote no charge, and a task with no bound worker at
                    # all has nobody to have billed.
                    "credits_reported": bool(row and row["reported"]),
                    "duration_ms": int(row["duration_ms"]) if row else 0,
                    # WHEN THIS TASK LAST SPOKE, which `duration_ms` cannot answer:
                    # duration is work time, so a worker thinking for twenty minutes
                    # and a worker that died twenty minutes ago read identically.
                    # Empty for a task no worker has reported on, which is absence of
                    # silence rather than a long one.
                    "last_report_at": item["last_report_at"],
                    # THE TWO ENDS OF THIS TASK'S SPAN, so a page can place the row on
                    # a clock beside every other row and show what ran at the same
                    # time. `duration_ms` above cannot: it is the worker SESSION's
                    # total work time, one figure however many tasks that session
                    # served, and it names no instant. Either may be empty -- a task
                    # whose opener this fold never saw, or one still open -- and a
                    # page draws an unplaceable or an unfinished end rather than
                    # inventing a bound. See `_workstreams_span_opened` and
                    # `_workstreams_span_closed`.
                    "created_at": item["created_at"],
                    "closed_at": item["closed_at"],
                    # THE DRAWER'S OWN FIELDS. A reader opening one task asks what
                    # the conductor decided and what the item's own record says, and
                    # the page cannot fetch either: the frame it is minted into has
                    # `connect-src 'none'`, so whatever a drawer shows has to be in
                    # the document already.
                    "decision": item["decision"],
                    "events": tails.get((board_id, item_id), []),
                    # The count beside the tail, so "20 of 34" and "no events
                    # recorded" are different sentences. See the budget's own note.
                    "events_seen": _as_int(item.get("events_seen")),
                }
            )
            if row is None or not isinstance(worker, str) or worker in seen:
                continue
            seen.add(worker)
            total += float(row["credits"])
            charges += int(row["reported"])
            _workstreams_merge(board_series, row["series"])
            if worker not in counted:
                counted.add(worker)
                _workstreams_merge(everywhere, row["series"])
        _workstreams_merge(everywhere, board["series"])
        # A nested board: its own slot is a worker session key some task bound. Absent
        # for a board nobody bound, and for a board whose bind is its own (a conductor
        # bound as the worker of one of its items is not its own subtask).
        parent = bound.get(board["slot"])
        if parent is not None and parent["board"] == board_id:
            parent = None
        if parent is not None:
            # The parent link names a BOARD, so it goes through the same table: a raw
            # id here would carry the very key the board's own `id` withholds, and the
            # two must agree or the tree cannot find its parent.
            parent = dict(parent)
            parent["board"] = board_alias_of(parent["board"])
        boards.append(
            {
                "id": board_alias_of(board_id),
                "goal": board["goal"],
                "round": board["round"],
                "parent": parent,
                "total": len(board["order"]),
                "accepted": accepted,
                "open": sum(
                    1
                    for item_id in board["order"]
                    if board["items"].get(item_id, {}).get("state") == "open"
                ),
                "needs_you": needs_you,
                "credits": round(total, 6),
                "credits_reported": charges > 0,
                "last_activity_at": board["last_activity_at"],
                "tasks": tasks,
                "tasks_omitted": board["tasks_omitted"]
                + max(0, len(board["order"]) - WORKSTREAMS_TASK_LIMIT),
                "series": _workstreams_series(board_series),
            }
        )
        #: Whether this is the SUBJECT's own board, decided from the board's slot
        #: before the id is aliased. The alias carries no slot, by design, so the
        #: ordering question has to be answered while the slot is still in hand.
        #: Popped before the row is returned: it is an ordering fact, not a field the
        #: page is told about, and the rendered keys are pinned as an exact set.
        boards[-1]["_own"] = board["slot"] == state["slot"]
        #: The MILLISECOND stamp, carried for the sort and popped with ``_own``.
        #: ``last_activity_at`` is rendered by ``_work_iso`` and carries a LOCAL
        #: offset, so comparing two of those as strings orders them by wall-clock
        #: text: across an autumn DST hour or a timezone change a later board reads
        #: as older, and at ``WORKSTREAMS_BOARD_LIMIT`` the board moved into
        #: ``omitted`` can be the newest one. The epoch value has no such ambiguity
        #: and is already kept beside it.
        boards[-1]["_ms"] = board["last_activity_ms"]
    # Newest activity first, which is the order the page lists them in. The board's own
    # slot leads a tie so the crewmate's own workstream is not shuffled below a
    # sub-board that happens to share its last stamp.
    boards.sort(key=lambda row: (row["_ms"], row["_own"]), reverse=True)
    for row in boards:
        del row["_own"]
        del row["_ms"]
    return {
        "schema": 1,
        "slot": state["slot"],
        "items": boards[:WORKSTREAMS_BOARD_LIMIT],
        "omitted": state["omitted"] + max(0, len(boards) - WORKSTREAMS_BOARD_LIMIT),
        "series": _workstreams_series(everywhere),
        # The charges no session could be named for, stated rather than folded into a
        # board. 0.0 with a false flag is "nothing was unattributable".
        "unattributed": {
            "credits": round(float(state["spend_unattributed"]), 6),
            "credits_reported": state["spend_unattributed_reported"] > 0,
        },
        "spenders_omitted": state["spenders_omitted"],
        # SESSION UNITS the ceiling refused, stated once for the whole answer. A page
        # drawing boards from a truncated read looks complete, so this is what lets it
        # say "and older history this does not reach" instead of implying there is
        # none. It counts units rather than boards because a unit is what was refused;
        # how many boards were in it is unknowable without reading it.
        "units_omitted": _as_int(state.get("units_omitted")),
        # WHEN THE LOG LAST REACHED THIS FOLD. The page's own staleness question is
        # not "did a field fail to resolve" -- the frame's band already answers that
        # -- but "are these numbers still being produced". A gateway that died leaves
        # every value here intact and correct as of a time nothing else on the page
        # names, so this is what lets the report say so instead of reading as current.
        # Empty when the fold has applied nothing at all, which is a page with no
        # numbers rather than old ones.
        "last_entry_at": state["last_entry_at"],
    }


def _workstreams_rows(state: Mapping[str, Any]) -> int:
    """``workstreams``: each board keeps its tasks and its hourly series, and each
    spender keeps its own series."""
    boards = state.get("boards")
    rows = (
        _rows_in(boards)
        + _rows_in(state.get("order"))
        + _rows_in(state.get("events"))
        + _rows_in(state.get("charged"))
    )
    if isinstance(boards, dict):
        for board in boards.values():
            if isinstance(board, Mapping):
                rows += (
                    _rows_in(board.get("items"))
                    + _rows_in(board.get("order"))
                    + _rows_in(board.get("series"))
                )
    spend = state.get("spend")
    rows += _rows_in(spend)
    if isinstance(spend, dict):
        for row in spend.values():
            if isinstance(row, Mapping):
                rows += _rows_in(row.get("series"))
    return rows


def _own_board_units(slot: str) -> "tuple[tuple[str, ...], int]":
    """The units a board's project report is folded over, and how many were refused.

    The board's OWN units only, newest :data:`WORKSTREAMS_UNIT_LIMIT` of them. Everything
    those folds need from other sessions is written into these units by the gateway: a
    bound worker's report (``api_work_report``), each worker charge, and a nested board's
    records (``kiro_crew.crew_log.work_relay``). So no worker's log is opened and no bind
    is walked to choose what to read, and the cost of a read follows this board's own
    history. The refused count is the units past the limit, oldest first, so a snapshot
    can say it is short.
    """
    units = tuple(session_ledger.work_crew_log_units(slot))
    if len(units) <= WORKSTREAMS_UNIT_LIMIT:
        return units, 0
    return units[-WORKSTREAMS_UNIT_LIMIT:], len(units) - WORKSTREAMS_UNIT_LIMIT


def _workstreams_units(slot: str) -> "tuple[str, ...]":
    """:func:`_own_board_units` for the workstreams fold, with the refused count
    handed to its render through :data:`_WORKSTREAMS_UNITS_DROPPED`.

    The side map is this fold's and not the walk's: a slot-keyed fold is rendered from
    its state alone, so a number the SELECTION discovered has no other way in. A tree
    fold renders the count itself and takes the walk's second return instead.
    """
    units, dropped = _own_board_units(slot)
    _WORKSTREAMS_UNITS_DROPPED[slot] = dropped
    if len(_WORKSTREAMS_UNITS_DROPPED) > _WORKSTREAMS_DROPPED_SLOTS:
        # Bounded like everything else here. The map is a handoff and not a cache, so
        # the oldest pending entry is the one nobody came back for.
        _WORKSTREAMS_UNITS_DROPPED.pop(next(iter(_WORKSTREAMS_UNITS_DROPPED)), None)
    return units


#: How many slots may have an unread unit-overflow count pending at once. One read
#: selects units and then folds them, so an entry normally lives for one call; this
#: only bounds the leak when a selection is never followed by its fold.
_WORKSTREAMS_DROPPED_SLOTS: Final[int] = 64

#: Units this fold refused for want of room, by slot, written by
#: :func:`_workstreams_units` and read ONCE by :func:`_workstreams_bind_slot`.
#:
#: A handoff rather than shared state: the count is known only where the units are
#: chosen, which is outside the fold, and it has to reach the render so a snapshot can
#: say it is short. Keyed by slot because two reads for different slots can interleave,
#: and read-and-cleared so a later fold of the same slot cannot inherit this number.
#: Absent means zero, which is also what a warm fold that skipped selection reports.
_WORKSTREAMS_UNITS_DROPPED: Final[dict[str, int]] = {}


def _workstreams_units_dropped(slot: str) -> int:
    """How many units the last selection for *slot* refused. Clears on read."""
    return _WORKSTREAMS_UNITS_DROPPED.pop(slot, 0)

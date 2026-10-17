"""The ``radar`` slot fold: the Issue Radar crew ledger, served by its owner and by no
generic route (``OWNER_SERVED_SLOT_PROJECTION``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Final

from kiro_crew.crew_log.entry_types import (
    RADAR_CI_BOUNDS,
    RADAR_CI_KEYS,
    RADAR_CLEARABLE_FIELDS,
    RADAR_CREW_LEVEL_EVENT_KIND,
    RADAR_DEFAULT_SKIP_SCOPE,
    RADAR_EDITING_PHASES,
    RADAR_ENTRY_TYPE,
    RADAR_EVENT_KINDS,
    RADAR_LABELS_LIMIT,
    RADAR_NUMBER_BOUNDS,
    RADAR_PHASES,
    RADAR_SKIP_SCOPES,
    RADAR_TERMINAL_PHASES,
)
from kiro_crew.crew_log.schema import Entry

# radar -- the Issue Radar crew ledger
# --------------------------------------------------------------------------- #

#: Ceilings on the state a crew's fold RETAINS. The crew page reads at most
#: ``_MAX_EVENTS`` lines (500), so the event tail keeps that many; the oldest go
#: first, which is the order every reader already drops them in. Phase lines are
#: kept PER ITEM so a long-parked lane's entry line cannot be pushed out by other
#: items' chatter -- the lane that has sat longest is the one the pipeline view
#: exists to show. Text is re-clamped on the way in because these bytes come off a
#: file a reader does not control.
RADAR_EVENT_LIMIT: Final[int] = 500
RADAR_PHASE_LINE_LIMIT: Final[int] = 200
#: Rejected approaches kept PER ITEM, newest last; a crew that rejects more than
#: this on one issue has stopped learning from the list, and the oldest rows are
#: the ones a resume can do without.
RADAR_TRIED_LIMIT: Final[int] = 100
#: Work items and passes kept PER CREW. Past the bound the fold EVICTS -- an item:
#: a finished one first, oldest finish first, then the open one longest without
#: progress; a pass: the earliest decided -- and COUNTS what it evicted in
#: ``counts``, so a bounded record is told from a complete one. A crew that has
#: touched more distinct issues than this has a history, not a working set, and the
#: working set is what a resume needs; an evicted pass is one the repository may
#: investigate again, which the spec accepts as this index's bound.
RADAR_ITEM_LIMIT: Final[int] = 500
RADAR_SKIP_LIMIT: Final[int] = 5000
#: Clamp for every free-text field the fold reads. It is a SHAPE gate on bytes a
#: reader does not control, not an input cap, so it must equal the largest value the
#: record tool would have accepted -- ``validation.MAX_MEDIUM_STRING``, the cap on
#: ``next``, ``decision``, ``why``, ``tried_approach`` and ``tried_rejected_because``.
#: Lower than that and an ordinary accepted field is truncated on every read, which
#: is silent state corruption rather than a bound. Not imported from ``validation``
#: on purpose -- the fold stays free of the MCP layer -- so
#: ``test_issue_radar_crew_store_fold_bounds`` pins the two constants equal instead.
RADAR_TEXT_LIMIT: Final[int] = 5000
RADAR_SCHEMA_VERSION: Final[int] = 1


def _radar_iso(stamp_ms: int) -> str:
    """An entry's epoch-millisecond ``time`` as the UTC ``Z`` spelling the record keeps.

    Derived from the envelope rather than written into the entry: one clock, and no
    way for an entry to claim a time the log disagrees with. A stamp outside the
    range a ``datetime`` holds answers ``""`` rather than raising, because this reads
    bytes a reader does not control and one damaged line must cost a display value,
    not every read of the crew forever.
    """
    try:
        moment = datetime.fromtimestamp(stamp_ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _radar_text(value: Any, limit: int = RADAR_TEXT_LIMIT) -> str:
    """*value* as a clamped string, or ``""``. The fold's own shape gate."""
    if not isinstance(value, str):
        return ""
    return value[:limit]


def _radar_int(value: Any) -> int | None:
    """*value* as an int, or ``None``. Bools are refused: JSON ``true`` is not a number."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _radar_number(value: Any, field: str) -> int | None:
    """*value* as an int inside the record tool's range for *field*, or ``None``.

    The magnitude half of the same rule :func:`_radar_text` applies to a string and
    :func:`radar_ci_state` to a counter: these bytes come off a file a reader does not
    control, so a number outside the range the tool would have accepted is dropped
    rather than retained. Dropping is the existing answer for a number of the wrong
    type, and an item or skip keyed on such a number was never one the tool wrote.
    """
    number = _radar_int(value)
    if number is None:
        return None
    low, high = RADAR_NUMBER_BOUNDS[field]
    return number if low <= number <= high else None


def radar_ci_state(value: Mapping[str, Any]) -> dict[str, Any]:
    """The members of a CI reading the record tool would have accepted, bounded.

    Only :data:`RADAR_CI_KEYS`, each with the tool's own type and ceiling from
    :data:`RADAR_CI_BOUNDS`: a string verdict clipped to its length, a counter kept
    only when it is an int within the tool's range. Any other member, and any member
    of the wrong shape, is dropped. The fold applies this to the bytes it reads and
    the crew store's carry to a pre-projection file, so a reading that reached the
    log by any path is retained within the same bounds.
    """
    kept: dict[str, Any] = {}
    for key in RADAR_CI_KEYS:
        if key not in value:
            continue
        kind, bound = RADAR_CI_BOUNDS[key]
        member = value[key]
        if kind is str:
            if isinstance(member, str) and member:
                kept[key] = member[:bound]
            continue
        number = _radar_int(member)
        if number is not None and 0 <= number <= bound:
            kept[key] = number
    return kept


def _radar_event_id(ts: str, crew_id: str, number: int | None, kind: str, text: str) -> str:
    """The content-addressed line id the crew ledger has always given a progress line.

    Kept byte-identical to the pre-projection formula so a reader keyed on ids sees
    the same id for the same line. ``number`` renders as the empty string on a
    crew-level line, which cannot collide with a real number.
    """
    shown = "" if number is None else int(number)
    raw = f"{ts}|{crew_id}|{shown}|{kind}|{text}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _radar_start() -> dict[str, Any]:
    return {
        "crew_id": "",
        "owner": "",
        "repo": "",
        "items": {},
        "events": [],
        "skips": {},
        "phase_lines": {},
        # ``[line id, payload digest]`` pairs for the newest entries folded: the
        # collapse of a REPEATED entry keys on the whole update, not on the line's
        # display identity, so two same-millisecond calls that differ only in the
        # fields they patch both fold.
        "last_update": {},
        # How many items and passes the bounds above evicted from this fold, so a
        # reader can tell a bounded record from a complete one.
        "evicted_items": 0,
        "evicted_skips": 0,
    }


def _radar_payload_digest(data: Mapping[str, Any]) -> str:
    """A digest of the whole update, so a repeat is told from a same-looking one."""
    try:
        raw = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        raw = repr(sorted(data.items(), key=lambda kv: str(kv[0])))
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


def _radar_new_item(crew_id: str, owner: str, repo: str, number: int) -> dict[str, Any]:
    """A work item before its first update, in the key order the record has always had."""
    return {
        "schema": RADAR_SCHEMA_VERSION,
        "crew_id": crew_id,
        "owner": owner,
        "repo": repo,
        "number": number,
        "phase": "selected",
        "outcome": None,
        "decision": "",
        "why": "",
        "next": "",
        "tried": [],
        "worktree": "",
        "branch": "",
        "base_sha": "",
        "pr_number": None,
        "ci_state": {},
        "claim_comment_id": None,
        "labels_applied": [],
        "claimed_at": None,
        "last_progress_at": None,
        "finished_at": None,
    }


def _radar_record_skip(
    state: dict[str, Any], key: str, number: int, skip: Mapping[str, Any], crew_id: str, ts: str
) -> None:
    """Record one pass in this crew's contribution to the repository's skip index.

    FIRST decision wins, as the shared index always did: the first crew's reason is
    the audit trail a human reads, a later identical pass adds nothing, and a
    different conclusion is a disagreement to surface on the later crew's own item
    rather than a silent edit of someone else's record. ``crew_id`` and
    ``decided_at`` on the row default to the entry's own and are overridden only by
    a carried row, which re-states a decision made elsewhere and earlier.
    """
    if not isinstance(skip.get("reason"), str):
        return
    skips: dict[str, dict[str, Any]] = state["skips"]
    if key in skips:
        return
    scope = skip.get("scope")
    skips[key] = {
        "number": number,
        "reason": _radar_text(skip["reason"]),
        "scope": (
            scope
            if isinstance(scope, str) and scope in RADAR_SKIP_SCOPES
            else RADAR_DEFAULT_SKIP_SCOPE
        ),
        "crew_id": _radar_text(skip.get("crew_id"), 64) or crew_id,
        "decided_at": _radar_text(skip.get("decided_at"), 64) or ts,
        # The writer saw another crew's decision standing when it recorded this pass;
        # the union never lets such a row stand over the one it saw. The writer's
        # observation orders the two, so a clock stepped backward cannot re-order them.
        "deferred": skip.get("deferred") is True,
    }
    while len(skips) > RADAR_SKIP_LIMIT:
        # The EARLIEST decided pass goes first: the shared index keeps a number's
        # first decision, and of this crew's rows the oldest is the one most likely
        # already re-decided by the issue itself (closed, or reopened and worked).
        oldest = min(skips, key=lambda k: (str(skips[k].get("decided_at") or ""), k))
        del skips[oldest]
        state["evicted_skips"] += 1


def _radar_bound_items(state: dict[str, Any], keep: str) -> None:
    """Evict work items past :data:`RADAR_ITEM_LIMIT`, never the one just written.

    A FINISHED item goes before any open one -- an open item is work the crew still
    owes, and the record exists so it can resume that work -- oldest finish first;
    only when every other item is open does the one longest without progress go. An
    evicted item takes its phase history with it, so the two stay bounded together,
    and is counted.

    An item in an EDITING phase is never a victim, whatever the pressure. The
    one-editor rule is decided by scanning the items this record still holds, so
    evicting the item that holds the edit makes the rule answer "no editor" and admit
    a second one -- and an append-only log cannot retract the conflicting lines the two
    then write. The bound stays meaningful because the rule admits ONE editing item at
    a time, so this withholds at most one candidate out of the limit; if it somehow
    withholds them all, the bound is exceeded instead, which is what this function
    already does when the only item left is the one just written.
    """
    items: dict[str, dict[str, Any]] = state["items"]
    while len(items) > RADAR_ITEM_LIMIT:
        candidates = [
            key for key in items if key != keep and items[key]["phase"] not in RADAR_EDITING_PHASES
        ]
        if not candidates:
            return
        finished = [key for key in candidates if items[key]["phase"] in RADAR_TERMINAL_PHASES]
        pool = finished or candidates
        victim = min(
            pool,
            key=lambda k: (
                str(items[k].get("finished_at") or items[k].get("last_progress_at") or ""),
                k,
            ),
        )
        del items[victim]
        state["phase_lines"].pop(victim, None)
        state["evicted_items"] += 1


def _radar_step(state: dict[str, Any], entry: Entry) -> None:
    if entry.type != RADAR_ENTRY_TYPE:
        return
    data = entry.data
    crew_id = _radar_text(data.get("crew_id"), 64)
    if not crew_id:
        return
    if not state["crew_id"]:
        # The first entry names the crew; every unit a crew's slot ran under belongs
        # to that one crew, so a later entry naming another is a planted or damaged
        # line and is left out rather than folded into a record it does not own.
        state["crew_id"] = crew_id
        state["owner"] = _radar_text(data.get("owner"), 256)
        state["repo"] = _radar_text(data.get("repo"), 256)
    elif crew_id != state["crew_id"]:
        return
    ts = _radar_iso(entry.time)
    kind = data.get("event_kind")
    if not (isinstance(kind, str) and kind in RADAR_EVENT_KINDS):
        return
    text = _radar_text(data.get("event"))
    number = _radar_number(data.get("number"), "number")
    carried = data.get("carried") is True
    events: list[dict[str, Any]] = state["events"]

    if number is None:
        # A crew-level line -- the queue sweep that took nothing. Consecutive sweeps
        # COALESCE: "checked, took nothing" is a recurring latest-value fact, and a
        # crew is nudged on a timer, so one line per idle cycle would push the crew's
        # real work history out of its own bounded tail. The first sweep after real
        # work stands; a sweep landing on a sweep adds nothing, and its timestamp is
        # deliberately the older one -- when the idle stretch BEGAN is the reading a
        # human opening a quiet crew wants.
        if kind != RADAR_CREW_LEVEL_EVENT_KIND:
            return
        if events and events[-1].get("kind") == RADAR_CREW_LEVEL_EVENT_KIND:
            return
        events.append(
            {
                "id": _radar_event_id(ts, crew_id, None, kind, text),
                "ts": ts,
                "crew_id": crew_id,
                "kind": kind,
                "text": text,
            }
        )
        del events[: max(0, len(events) - RADAR_EVENT_LIMIT)]
        return
    if kind == RADAR_CREW_LEVEL_EVENT_KIND:
        # The pairing the writer enforces, re-applied to the bytes: a crew-level kind
        # with a number would file a queue sweep under an issue it never touched.
        return

    key = str(number)
    line_id = _radar_event_id(ts, crew_id, number, kind, text)
    digest = _radar_payload_digest(data)
    last_update: dict[str, str] = state["last_update"]
    skip = data.get("skip")
    skip_row = skip if isinstance(skip, Mapping) else None
    carried_pass = carried and skip_row is not None and "phase" not in data
    # WHAT THIS UPDATE WROTE, and therefore what has to still be here for a retry of
    # it to be redundant. Every update writes an item except a carried pass, which
    # deliberately writes a skip and no item; an update carrying a `skip` writes one
    # too. Items and skips are bounded on SEPARATE rules and evict independently, so
    # asking whether EITHER survived would let a surviving skip vouch for an item the
    # item bound has since evicted -- and then a crash retry of the same update, which
    # is the one thing that would put the item back, returns here instead and leaves it
    # missing until some later DISTINCT update happens to rewrite it.
    still_present = (carried_pass or key in state["items"]) and (
        skip_row is None or key in state["skips"]
    )
    if last_update.get(key) == digest and still_present:
        # The same UPDATE twice IN A ROW for this item -- an append retried after a
        # crash or after a refused read-back, or one call landing twice. Only the
        # item's LAST applied update is compared, never a window of history: a retry
        # is by construction the next update for its item (the crew's writes are
        # serialized and the crew is waiting on the answer), while an item that
        # legitimately returns to an earlier state with identical fields after
        # intervening updates is a new update and applies. The digest covers the
        # whole update -- crew, number, kind, text and every field -- and not the
        # timestamped line id, which a retry re-stamps.
        #
        # Gated on the RECORD still existing, because this map and the two bounded
        # collections evict on different rules and different key sets: the digest of
        # an evicted item (or pass) can outlive it, and a retry matching that stale
        # digest would return here and leave the record GONE -- a row the crew was
        # told landed, absent. While the record is present the dedup is doing its
        # job; once it is not, the retry is what puts it back.
        return
    last_update.pop(key, None)
    last_update[key] = digest
    while len(last_update) > RADAR_EVENT_LIMIT:
        last_update.pop(next(iter(last_update)))
    if carried_pass and skip_row is not None:
        # A carried PASS on an issue this crew never worked -- the pre-projection
        # index was repository-wide, so the crew that carries it forward is usually
        # not the crew that decided it. It records the row and the line and NO work
        # item: an item would put an issue the crew never touched on its own page.
        _radar_record_skip(state, key, number, skip_row, crew_id, ts)
        events.append(
            {
                "id": line_id,
                "ts": ts,
                "crew_id": crew_id,
                "number": number,
                "kind": kind,
                "text": text,
            }
        )
        del events[: max(0, len(events) - RADAR_EVENT_LIMIT)]
        return
    items: dict[str, dict[str, Any]] = state["items"]
    existing = items.get(key)
    item = (
        existing
        if existing is not None
        else _radar_new_item(crew_id, state["owner"], state["repo"], number)
    )
    prev_phase = item["phase"] if existing is not None else None
    progressed = existing is None

    cleared = data.get("clear")
    if isinstance(cleared, list):
        # An explicit null in the update is carried as a CLEAR, named by field, since
        # a typed null is not a value the entry type admits. Applied before the set
        # fields, so a call that clears and sets the same field keeps the set value.
        for name in cleared:
            if not isinstance(name, str) or name not in RADAR_CLEARABLE_FIELDS:
                continue
            if name in ("pr_number", "claim_comment_id", "outcome"):
                item[name] = None
            elif name == "ci_state":
                item[name] = {}
            elif name == "labels_applied":
                item[name] = []
            else:
                item[name] = ""
            if name in ("pr_number", "ci_state", "next"):
                progressed = True

    phase = data.get("phase")
    if isinstance(phase, str) and phase in RADAR_PHASES:
        if phase != item["phase"]:
            progressed = True
        item["phase"] = phase
    for field_name in ("decision", "why", "worktree", "branch", "base_sha"):
        if isinstance(data.get(field_name), str):
            item[field_name] = _radar_text(data[field_name])
    if isinstance(data.get("next"), str):
        new_next = _radar_text(data["next"])
        if new_next != item["next"]:
            progressed = True
        item["next"] = new_next
    if "pr_number" in data:
        item["pr_number"] = _radar_number(data.get("pr_number"), "pr_number")
        progressed = True
    if "claim_comment_id" in data:
        item["claim_comment_id"] = _radar_number(data.get("claim_comment_id"), "claim_comment_id")
    ci_state = data.get("ci_state")
    if isinstance(ci_state, Mapping):
        # Merged KEY BY KEY, only the declared members, each re-bounded to the record
        # tool's own type and ceiling: a reading that named any other key would
        # otherwise grow the item by key with no bound, and one that carried an
        # oversized member would make every retained item hold it -- the entry type
        # admits an object here, and these are bytes read off a file.
        merged_ci: dict[str, Any] = radar_ci_state(item["ci_state"])
        merged_ci.update(radar_ci_state(ci_state))
        item["ci_state"] = merged_ci
        progressed = True
    labels = data.get("labels_applied")
    if isinstance(labels, list):
        item["labels_applied"] = [_radar_text(x, 256) for x in labels if isinstance(x, str)][
            :RADAR_LABELS_LIMIT
        ]
    if isinstance(data.get("outcome"), str):
        item["outcome"] = _radar_text(data["outcome"]).strip() or None
    tried = data.get("tried")
    if (
        isinstance(tried, Mapping)
        and isinstance(tried.get("approach"), str)
        and tried["approach"].strip()
    ):
        row = {
            "approach": _radar_text(tried["approach"]).strip(),
            "rejected_because": _radar_text(tried.get("rejected_because")),
        }
        # A carried entry RE-STATES a record, and a carry that did not fully land is
        # run again, so the same rejected approach can arrive twice; a live entry
        # is one call and appends as it always did.
        already = carried and any(
            r.get("approach") == row["approach"]
            and r.get("rejected_because") == row["rejected_because"]
            for r in item["tried"]
        )
        if not already:
            item["tried"].append({**row, "at": ts})
            del item["tried"][: max(0, len(item["tried"]) - RADAR_TRIED_LIMIT)]
            progressed = True

    # Stamps come off the entry's own clock. ``claimed_at`` is stamped once, the
    # first time the item is in any phase past ``selected``; ``last_progress_at``
    # moves ONLY on real progress, because the claim TTL is measured from it and a
    # bare read-back must not renew a claim. A CARRIED entry brings its own stamps:
    # it re-states a record that already had them, and re-stamping would make every
    # carried claim look freshly made.
    if carried:
        for stamp in ("claimed_at", "last_progress_at", "finished_at"):
            if stamp in data:
                item[stamp] = _radar_text(data.get(stamp), 64) or None
        if item["last_progress_at"] is None:
            item["last_progress_at"] = ts
    else:
        if item["claimed_at"] is None and item["phase"] != "selected":
            item["claimed_at"] = ts
        if item["last_progress_at"] is None or progressed:
            item["last_progress_at"] = ts
        if item["phase"] in RADAR_TERMINAL_PHASES:
            if not item["finished_at"]:
                item["finished_at"] = ts
        else:
            # Reopened, or never finished: a resolved issue can come back and be
            # handled again by the same crew, which reuses this very item, so EVERY
            # field that describes a finished result is dropped together.
            item["finished_at"] = None
            item["outcome"] = None
    items[key] = item
    _radar_bound_items(state, keep=key)

    if skip_row is not None:
        _radar_record_skip(state, key, number, skip_row, crew_id, ts)

    # The line carries ``phase`` ONLY when this entry created the item or moved it,
    # so a reader can treat "a line carrying a phase" as "an ENTRY into that phase":
    # a CI reading that leaves the item in ``awaiting-ci`` must not reset the lane's
    # dwell clock, or the item polled most often is the one whose stall is hidden.
    moved = existing is None or prev_phase != item["phase"]
    line: dict[str, Any] = {
        "id": line_id,
        "ts": ts,
        "crew_id": crew_id,
        "number": number,
        "kind": kind,
        "text": text,
    }
    if moved:
        line["phase"] = item["phase"]
        phase_lines: dict[str, list[dict[str, str]]] = state["phase_lines"]
        rows = phase_lines.setdefault(key, [])
        rows.append({"phase": item["phase"], "at": item["last_progress_at"] if carried else ts})
        del rows[: max(0, len(rows) - RADAR_PHASE_LINE_LIMIT)]
    events.append(line)
    del events[: max(0, len(events) - RADAR_EVENT_LIMIT)]


def _radar_render(state: dict[str, Any]) -> dict[str, Any]:
    """The crew's ledger, in the shapes its readers already expect.

    ``items`` newest progress first and ``events`` newest first, the orders the crew
    page has always listed them in. ``skips`` is THIS crew's contribution to the
    repository's shared index -- the index itself is the union over every crew of
    the repository, folded by the app. ``phase_lines`` is the per-item history of
    phase entries the pipeline view draws lanes from.
    """
    items = sorted(
        (
            dict(
                record,
                tried=[dict(row) for row in record["tried"]],
                ci_state=dict(record["ci_state"]),
                labels_applied=list(record["labels_applied"]),
            )
            for record in state["items"].values()
        ),
        key=lambda record: record.get("last_progress_at") or "",
        reverse=True,
    )
    return {
        "schema": RADAR_SCHEMA_VERSION,
        "crew_id": state["crew_id"],
        "owner": state["owner"],
        "repo": state["repo"],
        "items": items,
        "events": [dict(line) for line in reversed(state["events"])],
        "skips": {key: dict(row) for key, row in state["skips"].items()},
        "phase_lines": {
            key: [dict(row) for row in rows] for key, rows in state["phase_lines"].items()
        },
        "counts": {
            "open": sum(
                1
                for record in state["items"].values()
                if record["phase"] not in RADAR_TERMINAL_PHASES
            ),
            "evicted_items": state["evicted_items"],
            "evicted_skips": state["evicted_skips"],
        },
    }

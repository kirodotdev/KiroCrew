"""The four member projection units, keyed by ``types.PROJ_*``.

Each unit's ``apply`` returns the SAME state object for an irrelevant event,
so the registry treats it as a no-op and emits nothing.

The roster view carries no ``name``: ``init`` has no name to work with and the
log body never restates it. The service overlays ``name`` (from the header)
and ``slug`` onto the roster view in :meth:`MemberEventLogService.snapshot`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from kiro_crew.eventlog import types
from kiro_crew.eventlog.types import Event

# Fields copied last-wins from a member/config event into the roster.
_CONFIG_FIELDS = (
    "kiro_agent",
    "workspace",
    "memory_store",
    "model",
    "source",
    "starred",
    "avatar",
    "display_name",
)

_ACTIVITY_RING = 50

# How many slots' conversation boundaries the roster fold retains. A member
# drives one DM slot in ordinary use, so this is headroom rather than a working
# figure -- but the key is a slot key read off an event, the log is append-only
# with no compaction, and nothing here can ever drop a key once folded, so an
# unbounded map is one planted or long-lived log away from a roster row that is
# served on every roster read and grows forever. Eviction takes the OLDEST
# boundary (lowest `ts`), because the newest is the one a pane is reading to
# decide what the model still remembers -- and it is COUNTED into
# `CONVERSATION_STARTS_EVICTED`, because a dropped key is indistinguishable from
# a slot that was never reset.
_CONVERSATION_STARTS_MAX = 16

#: Roster field carrying how many conversation boundaries the cap above has
#: dropped for this member. Named rather than inlined because the pane and the
#: tests both read it, and a typo in one of two string literals is a field that
#: silently reads as zero -- which is the exact reading the count exists to rule
#: out.
CONVERSATION_STARTS_EVICTED = "conversation_starts_evicted"


def _evicted_boundaries(state: dict) -> int:
    """How many boundaries this state says were dropped, or 0 for no answer.

    Validated rather than read, for the reason every value in this fold is: a
    savepoint restored off disk lands in this state whole, so the held count can
    be a string, a `bool` or negative. Anything that is not a count reads as no
    evictions, which only ever UNDER-reports -- and this field exists to be
    believed when it is non-zero, so a damaged one must not invent losses.
    """
    held = state.get(CONVERSATION_STARTS_EVICTED)
    if isinstance(held, bool) or not isinstance(held, int) or held < 0:
        return 0
    return held


# The furthest ahead of the fold's own wall clock a `member/message` timestamp
# may push `last_active_ts`. The monotone rule (see `RosterProjection.apply`)
# has no upper bound of its own: it latches the greatest `ts` it has ever seen
# and, being an append-only fold, has nothing that can ever walk that value
# back down. A `ts` from a jumped-forward clock -- a VM resume, an NTP step, a
# hand-edited ISO string -- is therefore permanent, and pins its member above
# every genuinely-active crewmate for the life of the store. A small tolerance
# absorbs ordinary clock skew between the hosts that stamp and fold the event;
# anything past it is clamped down to the ceiling so the member still reads as
# active now (the event did happen) without ranking ahead of the present.
_FUTURE_TS_SKEW = 300.0


def scope_activity_view(view: dict, owner: str) -> dict:
    """An activity view holding only *owner*'s records, with counts to match.

    Slugification is lossy, so two distinct member NAMES can share one slug and
    therefore one log. The fold cannot tell them apart: a projection's ``apply``
    and ``view`` see events and nothing else, and the owning name lives in the
    log HEADER, which is not an event. So the scoping belongs where the header
    name is known -- the service, beside the ``name`` it already overlays onto
    the roster view for exactly the same reason.

    The counts are recomputed here rather than kept from ``view``: counts taken
    over the unfiltered ring would describe a different set of records than the
    list served next to them, which is a worse answer than either alone.
    """
    records = [r for r in view.get("recent", []) if isinstance(r, dict)]
    kept = [r for r in records if r.get("member") == owner]
    now = datetime.now(timezone.utc).timestamp()
    day = 86400.0
    today = 0
    week = 0
    for r in kept:
        secs = _parse_ts(r.get("ts"))
        if secs is None:
            continue
        age = now - secs
        if age < day:
            today += 1
        if age < 7 * day:
            week += 1
    out = dict(view)
    out["recent"] = kept
    out["today"] = today
    out["week"] = week
    return out


def _parse_ts(ts: Any) -> float | None:
    """Best-effort epoch seconds from an ISO-8601 string or a number."""
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str) and ts:
        s = ts.strip()
        # Accept a trailing Z as UTC.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


# ---------------------------------------------------------------------------
# roster
# ---------------------------------------------------------------------------
def _fold_conversation_start(state: dict, event: Event) -> dict:
    """``conversation_starts`` with *event*'s ``slot/reset`` boundary folded in.

    Returns the SAME state object when the event says nothing this fold can use,
    so the registry reads it as a no-op and announces nothing.

    The boundary is the entry's own ``data["ts"]`` -- the instant the
    conversation was discarded, captured by the route before the teardown. The
    envelope's ``time`` is a LATER moment (the append happens after a provider
    shutdown the teardown awaits) and is read only as a fallback for a line
    carrying no ``ts``.

    MONOTONE per slot key, not last-wins: two resets on one slot can land out of
    append order -- the first stalled in its shutdown while the second, started
    later, appends first -- and last-wins would then move the boundary BACKWARDS
    onto messages the second reset discarded, durably showing them as current.
    The superseded boundaries stay in the log for anybody asking when they
    happened.

    The payload is validated rather than trusted. These bytes come off a file
    this fold does not control -- a damaged line, a hand edit inside the member's
    log directory -- and the value is served to a browser on every roster read,
    so a `slot_key` of the wrong type, shape or LENGTH is dropped here instead of
    being carried into a pane that would then hide an arbitrary prefix of a
    conversation.

    The MOMENT is bounded as well as typed, by the same ceiling the recency fold
    applies and for a sharper reason: the browser turns this number into a
    ``Date``, and a value outside that type's range (a hand-edited
    ``9223372036854775807``) makes the pane's own marker construction raise
    instead of drawing a line. A boundary in the future is not a boundary anyone
    can be past, so the ceiling costs nothing a real reset needs.
    """
    from kiro_crew.members import DM_SLOT_KEY_MAX_CHARS, DM_SLOT_KEY_PREFIX

    data = event.get("data") or {}
    slot_key = data.get("slot_key")
    if not isinstance(slot_key, str) or not slot_key:
        return state
    # The key is BOUNDED as well as typed, in both shape and length. The entry
    # cap below limits how many keys the map holds and says nothing about how
    # big one is, and this value leaves the machine on every roster read -- so a
    # damaged line's multi-megabyte key would ride into the browser inside a map
    # that reports as small. Both bounds come from the builder's own module
    # rather than being chosen here: `DM_SLOT_KEY_PREFIX` is the one spelling of
    # the shape the only writer produces (`eventlog_hooks.record_slot_reset`
    # resolves the owner from it), and `DM_SLOT_KEY_MAX_CHARS` is summed from the
    # four inputs `member_slot_key` concatenates, so a V2 member whose slug and
    # private store are both at their caps still folds.
    if len(slot_key) > DM_SLOT_KEY_MAX_CHARS or not slot_key.startswith(DM_SLOT_KEY_PREFIX):
        return state
    # The writer's own `ts`, not the envelope's `time`. The two are different
    # moments: the entry is appended after the teardown has awaited a provider
    # shutdown, and a turn admitted during that wait belongs to the SUCCESSOR
    # conversation -- so folding the append time would put the boundary after
    # that turn's rows and hide a live prompt as discarded history. The envelope
    # is the fallback for an entry that carries no `ts`, which is the closest
    # moment such a line can be placed at.
    ts = data.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        ts = event.get("time")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return state
    # A POSITIVE range assertion, not two refusals. `NaN <= 0` and
    # `NaN > ceiling` are both False, so a pair of rejections lets a NaN through
    # -- and a NaN in this value is serialized into the roster response, where it
    # is not valid JSON and takes the whole crewmates read down in the browser.
    # The assertion is the WHOLE check and nothing converts the value first:
    # `math.isfinite` would have to turn it into a float, and a Python int off a
    # damaged line has no size limit, so `ts: 1e400` written out in full digits
    # raises OverflowError instead of being dropped -- in an append-only log that
    # is a member projection which raises on every read of that log for good.
    # Comparing a big int against a float is exact and cannot overflow, and the
    # range admits no NaN and neither infinity, so there is nothing left for a
    # float check to catch.
    ceiling = datetime.now(timezone.utc).timestamp() * 1000.0 + _FUTURE_TS_SKEW * 1000.0
    if not (0 < ts <= ceiling):
        return state
    held = state.get("conversation_starts")
    starts = dict(held) if isinstance(held, dict) else {}
    # MONOTONE per slot, like `last_active_ts` in this same fold and for the same
    # shape of reason: two resets on one slot can land out of append order -- the
    # first awaits a provider shutdown while the second, started later, appends
    # first -- and last-wins would then move the boundary BACKWARDS onto messages
    # the second reset discarded, durably showing them as current. "Where the
    # current conversation starts" only ever moves forward.
    previous = starts.get(slot_key)
    if isinstance(previous, dict):
        held_ts = previous.get("ts")
        # The held value is range-checked the same way the incoming one is, and
        # for both of this fold's reasons. It is not only ever a value this fold
        # stored: a savepoint restored off disk lands here whole, so a damaged
        # one can hold a NaN or an int of any size -- and a float check on it
        # would raise exactly where the incoming check must not. A held moment
        # outside the range is no moment at all, so it blocks nothing and the
        # real reset takes its place.
        if (
            isinstance(held_ts, (int, float))
            and not isinstance(held_ts, bool)
            and 0 < held_ts <= ceiling
            and held_ts >= ts
        ):
            return state
    starts[slot_key] = {"ts": ts}
    # Eviction is COUNTED, never silent. The cap bounds the map, and dropping a
    # key inside it leaves that slot reading EXACTLY like a slot nobody has ever
    # reset -- so the pane draws its discarded conversation as current, which is
    # the one failure this whole feature exists to prevent, with nothing anywhere
    # saying a boundary was known and then let go. The count cannot bring the
    # moment back; it is the difference between "never reset" and "dropped", and
    # that is what anyone reading the row has to be able to tell apart.
    evicted = _evicted_boundaries(state)
    if len(starts) > _CONVERSATION_STARTS_MAX:
        ordered = sorted(
            starts.items(),
            key=lambda item: item[1].get("ts", 0) if isinstance(item[1], dict) else 0,
        )
        for stale_key, _ in ordered[: len(starts) - _CONVERSATION_STARTS_MAX]:
            starts.pop(stale_key, None)
            evicted += 1
    if starts == held and evicted == _evicted_boundaries(state):
        return state
    new = dict(state)
    new["conversation_starts"] = starts
    # Absent means zero, so the ordinary row -- one DM slot per member, nowhere
    # near the cap -- carries no extra field on any roster read. The key appears
    # only once there is something to report.
    if evicted:
        new[CONVERSATION_STARTS_EVICTED] = evicted
    return new


class RosterProjection:
    key = types.PROJ_ROSTER
    #: 3 because the fold gained `conversation_starts` (see
    #: `_fold_conversation_start`), and a savepoint written by a fold WITHOUT
    #: that rule holds no boundary at all for a slot that was reset before the
    #: upgrade -- a resumed fold applies only the events after its watermark, so
    #: that member's pane would show a discarded conversation as current for the
    #: life of the store. A bump discards such a savepoint
    #: (`projection/checkpoint.py` refuses a payload whose `state_version`
    #: differs) and the member's own log is re-folded from the start, which is
    #: where the boundary is.
    #:
    #: 2 was for `last_active_ts` being MONOTONE (see `apply`), which is bookkeeping
    #: a savepoint written by a fold WITHOUT that rule can contradict. Such a
    #: savepoint can hold a recency a preview correction walked backwards, and
    #: resuming it applies only the events after it -- so the regressed value
    #: would stand for the life of the store, or until the member next spoke,
    #: and the Recent order would still be wrong after the upgrade. A bump is
    #: what discards it (`projection/checkpoint.py`: a `state_version` mismatch
    #: refuses the payload), after which the member's own log is re-folded from
    #: the start under the monotone rule. Cheap, and the only lossless answer:
    #: the events the bad savepoint consumed are the ones that hold the truth.
    state_version = 3

    def init(self) -> dict:
        return {}

    def apply(self, state: dict, event: Event) -> dict:
        etype = event["type"]
        data = event.get("data") or {}
        if etype == types.SLOT_RESET:
            return _fold_conversation_start(state, event)
        if etype == types.MEMBER_CONFIG:
            new = dict(state)
            for f in _CONFIG_FIELDS:
                if f in data:
                    new[f] = data[f]
            return new if new != state else state
        if etype == types.MEMBER_BINDING:
            slot_key = data.get("slot_key")
            if slot_key is not None and state.get("slot_key") != slot_key:
                new = dict(state)
                new["slot_key"] = slot_key
                return new
            return state
        if etype == types.MEMBER_MESSAGE:
            new = dict(state)
            # MONOTONE, unlike every other field here. "When was this member last
            # active" is an answer time only ever moves forward, so a fold that
            # took each event's `ts` last-wins could only ever be wrong when it
            # moved down -- and one writer moves it down by design.
            # `reconcile_member_preview` corrects a stale quote by appending a
            # `member/message` carrying the TRANSCRIPT's epoch, which is the last
            # thing SAID and is therefore older than any machinery turn since. A
            # last-wins fold let that correction reset recency to the last
            # speech, on every roster read, in an append-only log with nothing to
            # reopen it -- so a crewmate the user had just messaged sank back to
            # where the quote was from. Taking the greater keeps both writers
            # honest: the correction still lands its quote, and no writer has to
            # know what the recency was before it.
            #
            # CEILING. Monotone-greatest has no upper bound of its own, so a `ts`
            # from a jumped-forward clock would latch permanently and pin the
            # member atop Recent for good (see `_FUTURE_TS_SKEW`). Clamp the
            # candidate to the fold's own wall clock plus a skew tolerance before
            # the monotone compare: a future `ts` still advances a staler recency
            # to now (the activity is real) but can never rank ahead of it.
            ts = _parse_ts(data.get("ts"))
            if ts is not None and ts > 0:
                now = datetime.now(timezone.utc).timestamp()
                ceiling = now + _FUTURE_TS_SKEW
                if ts > ceiling:
                    ts = ceiling
                held = _parse_ts(state.get("last_active_ts")) or 0.0
                new["last_active_ts"] = ts if ts > held else state.get("last_active_ts")
            # A machinery row (tool call, patrol turn) bumps recency but carries
            # no preview; the last thing SAID stays on the row.
            if "preview" in data:
                new["last_message"] = data.get("preview")
            return new if new != state else state
        return state

    def view(self, state: dict) -> dict:
        return dict(state)


# ---------------------------------------------------------------------------
# activity
# ---------------------------------------------------------------------------
class ActivityProjection:
    key = types.PROJ_ACTIVITY
    state_version = 1

    def init(self) -> dict:
        return {"recent": []}  # newest-first ring of record data dicts

    def apply(self, state: dict, event: Event) -> dict:
        if event["type"] != types.ACTIVITY_RECORD:
            return state
        record = event.get("data") or {}
        recent = [record] + state["recent"]
        if len(recent) > _ACTIVITY_RING:
            recent = recent[:_ACTIVITY_RING]
        return {"recent": recent}

    def view(self, state: dict) -> dict:
        recent = state["recent"]
        now = datetime.now(timezone.utc).timestamp()
        day = 86400.0
        today = 0
        week = 0
        served: list[dict] = []
        for r in recent:
            secs = _parse_ts(r.get("ts")) if isinstance(r, dict) else None
            if secs is None:
                # A record with no readable timestamp cannot be placed on a
                # timeline, so it is skipped rather than served as garbage --
                # the same rule the REST activity read applies.
                continue
            age = now - secs
            if age < day:
                today += 1
            if age < 7 * day:
                week += 1
            # ``ts`` is served as EPOCH SECONDS, not as the ISO string the log
            # stores. The counting loop above already parses it, and the REST
            # activity read serves epoch seconds too, so a consumer reading one
            # of the two paths must not have to branch on which one it got.
            served.append({**r, "ts": secs})
        return {"recent": served, "today": today, "week": week}


# ---------------------------------------------------------------------------
# wake
# ---------------------------------------------------------------------------
class WakeProjection:
    key = types.PROJ_WAKE
    state_version = 1

    def init(self) -> dict:
        return {"patrol": "none"}

    def apply(self, state: dict, event: Event) -> dict:
        etype = event["type"]
        data = event.get("data") or {}
        if etype == types.PATROL_STARTED:
            return {
                "patrol": "armed",
                "slot_key": data.get("slot_key"),
                "since": event["time"],
            }
        if etype == types.PATROL_STOPPED:
            return {
                "patrol": "stopped",
                "slot_key": data.get("slot_key"),
                "stopped_reason": data.get("reason"),
                "since": event["time"],
            }
        return state

    def view(self, state: dict) -> dict:
        return dict(state)


# ---------------------------------------------------------------------------
# driving
# ---------------------------------------------------------------------------
class DrivingProjection:
    key = types.PROJ_DRIVING
    state_version = 1

    def init(self) -> dict:
        return {"open": frozenset()}

    def apply(self, state: dict, event: Event) -> dict:
        etype = event["type"]
        data = event.get("data") or {}
        slot_key = data.get("slot_key")
        if slot_key is None:
            return state
        open_set: frozenset = state["open"]
        if etype == types.SLOT_OPENED:
            if slot_key in open_set:
                return state
            return {"open": open_set | {slot_key}}
        if etype == types.SLOT_CLOSED:
            if slot_key not in open_set:
                return state
            return {"open": open_set - {slot_key}}
        return state

    def view(self, state: dict) -> dict:
        return {"open": sorted(state["open"])}


def all_units() -> list:
    return [
        RosterProjection(),
        ActivityProjection(),
        WakeProjection(),
        DrivingProjection(),
    ]

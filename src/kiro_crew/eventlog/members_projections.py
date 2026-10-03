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
class RosterProjection:
    key = types.PROJ_ROSTER
    #: Two rules here are bookkeeping a savepoint written WITHOUT them can
    #: contradict, and a savepoint is resumed by applying only the events after
    #: it -- so a value it got wrong stands for the life of the store rather
    #: than being recomputed. `last_active_ts` is MONOTONE, and a savepoint from
    #: a last-wins fold can hold a recency a preview correction walked backwards,
    #: leaving the Recent order wrong after the upgrade. `has_message` did not
    #: exist, so a savepoint from before it answers "no message" for a member
    #: whose `member/message` events all sit below the watermark -- and the
    #: roster's listing rule reads that as a crewmate to leave off the list.
    #: A bump is what discards such a payload (`projection/checkpoint.py`: a
    #: `state_version` mismatch refuses it), after which the member's own log is
    #: re-folded from the start under both rules. Cheap, and the only lossless
    #: answer: the events the stale savepoint consumed hold the truth.
    state_version = 3

    def init(self) -> dict:
        return {}

    def apply(self, state: dict, event: Event) -> dict:
        etype = event["type"]
        data = event.get("data") or {}
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
                # A member's DM slot key carries its private memory GENERATION
                # (`members.member_slot_key` appends `.memory-<store>`), so a
                # new generation is a new slot key and a new, empty transcript
                # under it. `has_message` describes the thread the member is
                # bound to NOW, so it is cleared with the thread -- otherwise a
                # rotated crewmate keeps claiming a message that lives in a
                # conversation nothing opens any more.
                #
                # Only when the fold ALREADY held a key, though. A log that has
                # never recorded a binding gets one from the first thread open
                # and from the legacy fold (`service._migrate_legacy_locked`),
                # and the legacy one can land after `member/message` events the
                # live hook already wrote -- so treating a FIRST binding as a
                # change would clear a fact about the very thread it names.
                if state.get("slot_key") is not None:
                    new["has_message"] = False
                return new
            return state
        if etype == types.MEMBER_MESSAGE:
            new = dict(state)
            # Whether this member's DM thread holds a message at all, which is
            # what the Crewmates roster's listing rule asks (`has_dm_message` in
            # `dashboard/handlers/members.py`): a crewmate nobody has written to
            # is reached through the search box rather than listed unasked. Set
            # once and left alone, so a member who has chatted emits this
            # transition exactly once per thread instead of on every message.
            #
            # An event whose `preview` is present and EMPTY is the exception,
            # and it is not a message. That shape has one writer --
            # `reconcile_member_preview` correcting a stale quote down to blank
            # -- because `member_message_payload` omits the key entirely unless
            # the preview is non-empty, so no live row produces it. Reading it
            # as a message is backwards: it says the last thing SAID here is
            # nothing. Without this, a crewmate whose memory generation rotated
            # would be listed by the very read that blanks its stale quote --
            # the binding clears the flag over the new empty thread, and the
            # correction would re-set it.
            #
            # SILENT rather than a clear, though: the correction speaks about
            # speech, and a thread can hold machinery rows with nothing said in
            # it, so it is no evidence that the thread is empty either.
            if not state.get("has_message") and data.get("preview", None) != "":
                new["has_message"] = True
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
            ts = _parse_ts(data.get("ts"))
            if ts is not None and ts > 0:
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

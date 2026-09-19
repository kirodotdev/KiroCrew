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
)

_ACTIVITY_RING = 50


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
    state_version = 1

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
                return new
            return state
        if etype == types.MEMBER_MESSAGE:
            ts = data.get("ts")
            preview = data.get("preview")
            new = dict(state)
            new["last_active_ts"] = ts
            new["last_message"] = preview
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
        for r in recent:
            secs = _parse_ts(r.get("ts")) if isinstance(r, dict) else None
            if secs is None:
                continue
            age = now - secs
            if age < day:
                today += 1
            if age < 7 * day:
                week += 1
        return {"recent": list(recent), "today": today, "week": week}


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

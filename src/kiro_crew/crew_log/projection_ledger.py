"""The ``ledger`` slot fold: a session's work ledger, folded from the ``ledger/recorded``
entries :mod:`kiro_crew.session_ledger` appends, whose phases, event kinds and field caps
that module owns.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from kiro_crew.crew_log.schema import Entry

# The ledger fold reads a record whose semantics -- which phases end a workstream,
# which event kinds exist, how much of each field is kept -- belong to the ledger
# subsystem. They are imported rather than restated so one owner sets them, the
# same direction ``store`` already takes for the store-name fold.
from kiro_crew.session_ledger import _MAX_ARTIFACT_KEY as LEDGER_ARTIFACT_KEY_LIMIT
from kiro_crew.session_ledger import _MAX_ARTIFACTS as LEDGER_ARTIFACT_LIMIT
from kiro_crew.session_ledger import _MAX_EVENTS as LEDGER_EVENT_LIMIT
from kiro_crew.session_ledger import _MAX_PHASE as LEDGER_PHASE_LIMIT
from kiro_crew.session_ledger import _MAX_TEXT as LEDGER_TEXT_LIMIT
from kiro_crew.session_ledger import _MAX_TRIED as LEDGER_TRIED_LIMIT
from kiro_crew.session_ledger import EVENT_KINDS as LEDGER_EVENT_KINDS
from kiro_crew.session_ledger import LEDGER_ENTRY_TYPE
from kiro_crew.session_ledger import SCHEMA_VERSION as LEDGER_SCHEMA_VERSION
from kiro_crew.session_ledger import TERMINAL_PHASES as LEDGER_TERMINAL_PHASES

# --------------------------------------------------------------------------- #
# ledger
# --------------------------------------------------------------------------- #


def _ledger_iso(stamp_ms: int) -> str:
    """An entry's epoch-millisecond ``time`` as the record's local ISO spelling.

    The envelope already carries when each update happened, so the record's
    timestamps are DERIVED from it rather than written into the entry -- one clock,
    and no way for an entry to claim a time the log disagrees with.

    A value outside the range a ``datetime`` can hold answers ``""``, the same thing
    an absent stamp answers. ``fromtimestamp`` raises ``OverflowError`` or ``OSError``
    on one, and this reads bytes a reader does not control: a damaged or planted
    ``time`` would otherwise turn every read of that slot into a crash, permanently,
    since the line stays on disk and nothing rewrites it. Losing one stamp costs a
    reader a display value; raising costs it the whole record.
    """
    try:
        return datetime.fromtimestamp(stamp_ms / 1000).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


def _ledger_field(value: Any, limit: int = LEDGER_TEXT_LIMIT) -> str:
    """*value* as a clamped string, or ``""``. The fold's own shape gate.

    The writer clamps too, but these bytes come off a file a reader does not
    control, so the length is re-applied here: a planted or damaged line is exactly
    the input that ignores the writer's rule, and every field below is RETAINED in
    a state a nudge turn carries.
    """
    if not isinstance(value, str):
        return ""
    return value[:limit]


def _ledger_start() -> dict[str, Any]:
    return {
        "goal": "",
        "phase": "",
        "next": "",
        "tried": [],
        "artifacts": {},
        "events": [],
        "created_at": "",
        "last_progress_at": "",
        "finished_at": "",
    }


def _ledger_step(state: dict[str, Any], entry: Entry) -> None:
    if entry.type != LEDGER_ENTRY_TYPE:
        return
    data = entry.data
    stamp = _ledger_iso(entry.time)
    if not state["created_at"]:
        state["created_at"] = stamp
    # An ABSENT field means unchanged, which is what lets a partial update be one
    # entry; only a present one is applied. ``isinstance`` rather than truthiness,
    # so a caller clearing a field to "" is applied rather than ignored.
    if isinstance(data.get("goal"), str):
        state["goal"] = _ledger_field(data["goal"])
    if isinstance(data.get("phase"), str):
        state["phase"] = _ledger_field(data["phase"], LEDGER_PHASE_LIMIT)
        # Re-derived on every phase write rather than latched: a workstream that
        # leaves a terminal phase is in flight again, and a stale ``finished_at``
        # would keep the snapshot suppressed for a session that resumed.
        state["finished_at"] = stamp if state["phase"] in LEDGER_TERMINAL_PHASES else ""
    if isinstance(data.get("next"), str):
        state["next"] = _ledger_field(data["next"])
    tried = data.get("tried")
    if isinstance(tried, Mapping) and isinstance(tried.get("approach"), str):
        rows: list[dict[str, str]] = state["tried"]
        rows.append(
            {
                "approach": _ledger_field(tried["approach"]),
                "rejected_because": _ledger_field(tried.get("rejected_because")),
                "at": stamp,
            }
        )
        # Bounded like every other fold state here: the oldest rejected approach
        # ages out so a long workstream cannot grow the record without limit.
        if len(rows) > LEDGER_TRIED_LIMIT:
            del rows[: len(rows) - LEDGER_TRIED_LIMIT]
    artifacts = data.get("artifacts")
    if isinstance(artifacts, Mapping):
        merged: dict[str, str] = state["artifacts"]
        for key, value in artifacts.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            folded = _ledger_field(key, LEDGER_ARTIFACT_KEY_LIMIT)
            # Popped before reassigning: a plain update keeps the key's ORIGINAL
            # insertion position, so updating the oldest pointer on a full map would
            # leave it first in line for the age-out below -- dropping the very
            # artifact this entry just set.
            merged.pop(folded, None)
            merged[folded] = _ledger_field(value)
        while len(merged) > LEDGER_ARTIFACT_LIMIT:
            merged.pop(next(iter(merged)))
    event = data.get("event")
    if isinstance(event, str) and event.strip():
        kind = data.get("event_kind")
        # The kind is a FILTER over the text, not the fact, so an unrecognized one
        # degrades to ``note`` rather than discarding the event. A phase that moved
        # without a recognized kind cannot reach the file at all: the writer refuses
        # it, so nothing here has to reconstruct that rule.
        if not (isinstance(kind, str) and kind in LEDGER_EVENT_KINDS):
            kind = "note"
        events: list[dict[str, str]] = state["events"]
        events.append({"ts": stamp, "kind": kind, "text": _ledger_field(event.strip())})
        if len(events) > LEDGER_EVENT_LIMIT:
            del events[: len(events) - LEDGER_EVENT_LIMIT]
    state["last_progress_at"] = stamp


def _ledger_render(state: dict[str, Any]) -> dict[str, Any]:
    """The state RECORD, in the shape every reader of the ledger already expects.

    Deliberately the same ten keys the ledger's document carried when it was a file
    of its own, so the MCP tool, the route and the injected snapshot did not have to
    learn a new shape to stop being a second copy of the truth. ``schema`` describes
    the RECORD, which is unchanged; where the record lives is not something a
    consumer of it branches on.
    """
    return {
        "schema": LEDGER_SCHEMA_VERSION,
        "goal": state["goal"],
        "phase": state["phase"],
        "next": state["next"],
        "tried": [dict(row) for row in state["tried"]],
        "artifacts": dict(state["artifacts"]),
        "events": [dict(row) for row in state["events"]],
        "created_at": state["created_at"],
        "last_progress_at": state["last_progress_at"],
        "finished_at": state["finished_at"],
    }

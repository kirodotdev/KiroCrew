"""The engine-measured half of the ``usage`` fold's per-turn context rows.

``context/composed`` says what Kiro Crew put in front of the model. ``context/measured``
(written by the turn's closer from :mod:`kiro_crew.acp.kas_profile`) says what the agent
engine found in the turn's LAST model request, by category, and how long the turn
waited for its first output. This module folds the second onto the row of the first.

**O(1) per entry.** Nothing here walks the window or the log:

- a measurement attaches to the TAIL row only. The closer writes it just before the
  turn's ``turn/completed``, after every composition of the turn, so the turn's row is
  the newest one; anything else attaches nowhere.
- the engine's ``history.user`` covers every user message still in its context, and
  Kiro Crew sent each of those as one composed prompt. So Crew's share of it is a
  RUNNING SUM of composed characters (``context_crew_chars``), added to once per
  composition and restarted when the engine's context restarts: a new unit, or a
  measurement whose ``compaction_summary`` changed or which says ``compacted``. It is
  never re-derived by re-reading rows.
- a duplicate measurement of the same engine turn is recognised against ONE stored key
  (``context_measured_last``), not a set.

**Bounded row size.** A row's measurement is ONE string of at most
:data:`MEASURED_FIELDS` comma-separated numbers of at most eight digits each, decoded
at render. The window holds :data:`~kiro_crew.crew_log.projection_usage.CONTEXT_TURNS_LIMIT`
rows, and a dict per row would not fit the slot-fold cell budget that window is sized
against; ``test_a_full_window_stays_under_the_slot_fold_cell_budget`` measures the worst
case with this string present. A category this build does not know adds to the row's
``unknown_chars`` and is kept BY NAME in the session-wide ``context_measured_unknown``,
bounded by :data:`MEASURED_UNKNOWN_LIMIT`.

Characters throughout, the same unit ``context/composed`` records. No figure here is a
token count, and none is divided by one.
"""

from __future__ import annotations

from typing import Any, Final

from kiro_crew.crew_log.projection_values import _as_int, _as_str, _keyable
from kiro_crew.crew_log.schema import Entry
from kiro_crew.engine_context import (
    BREAKDOWN_SCHEMA,
    HISTORY_PARTS,
    KNOWN_CATEGORIES,
    MAX_COUNT,
    count,
)

#: Leads every encoded measurement, so a reader can refuse a string it cannot read.
MEASURED_FORMAT: Final[str] = "m1"

_HISTORY_KEYS: Final[tuple[str, ...]] = tuple(field for _wire, field in HISTORY_PARTS)
_TTFT_KEYS: Final[tuple[str, ...]] = ("total_ms", "setup_ms", "model_ms")

#: The positions of the encoded string after its format tag. An absent value is an
#: empty position, so "not measured" never reads back as zero.
_SLOTS: Final[tuple[str, ...]] = (
    "model_calls",
    "total_chars",
    *(f"cat:{name}" for name in KNOWN_CATEGORIES),
    *(f"hist:{name}" for name in _HISTORY_KEYS),
    "unknown_chars",
    *(f"ttft:{name}" for name in _TTFT_KEYS),
    "crew_chars",
    "flags",
)
MEASURED_FIELDS: Final[int] = len(_SLOTS)
_INDEX: Final[dict[str, int]] = {name: i for i, name in enumerate(_SLOTS)}

#: The running sum restarted at this turn: the engine's context restarted.
FLAG_RESET: Final[int] = 1
#: The breakdown came under a schema this build does not know.
FLAG_SCHEMA_UNKNOWN: Final[int] = 2
#: The engine compacted during the turn, before the measured request went out.
FLAG_COMPACTED: Final[int] = 4
#: The entry carried a breakdown. Absent on a time-to-first-token-only entry.
FLAG_BREAKDOWN: Final[int] = 8
#: A turn was composed again after it closed (a retry, a regenerate, a rewind) since
#: the sum last restarted. The engine may have dropped the earlier attempt, so Crew's
#: share may count it twice; a reader is told the split is approximate.
FLAG_RERUN: Final[int] = 16

#: Distinct unknown category names the session keeps by name. Past it a new name is
#: not kept; its characters still reach the row's ``unknown_chars``.
MEASURED_UNKNOWN_LIMIT: Final[int] = 16


def measured_start() -> dict[str, Any]:
    """The state keys this module owns, merged into the ``usage`` fold's start."""
    return {
        # Composed characters since the engine's context last restarted. Crew's share
        # of the engine's ``history.user``; see the module docstring.
        "context_crew_chars": 0,
        # The newest composed turn's key (``unit:turn``) and its characters across
        # every composition of it, which is what the sum restarts from.
        "context_crew_turn_key": "",
        "context_crew_turn_chars": 0,
        # The newest composed turn number in this unit, the newest CLOSED turn's key,
        # and whether a turn was composed again since the sum restarted (``FLAG_RERUN``).
        "context_crew_last_turn": 0,
        "context_crew_closed_key": "",
        "context_crew_rerun": False,
        # The engine's ``compaction_summary`` as last measured, -1 before any.
        "context_kas_summary_chars": -1,
        # ``unit:turn:engine turn id`` of the newest measurement folded.
        "context_measured_last": "",
        "context_measured_unknown": {},
    }


def note_unit_start(state: dict[str, Any]) -> None:
    """A new unit is a new engine session: its context starts empty."""
    state["context_crew_chars"] = 0
    state["context_crew_turn_key"] = ""
    state["context_crew_turn_chars"] = 0
    state["context_crew_last_turn"] = 0
    state["context_crew_closed_key"] = ""
    state["context_crew_rerun"] = False
    state["context_kas_summary_chars"] = -1


#: Stop reasons of a turn that ran to its end, so the engine kept its prompt.
_NORMAL_STOPS: Final[frozenset[str]] = frozenset(
    {"end_turn", "max_tokens", "max_turn_requests", "refusal"}
)


def note_closed(state: dict[str, Any], unit_no: int, turn_no: int, stop_reason: str) -> None:
    """Remember the newest closed turn, so a composition of it again reads as a rerun.

    A turn that did not end normally (failed, cancelled, interrupted, an error) may have
    had its prompt rewound out of the engine before a retry, and the sum still counts
    it, so the split is marked approximate until the sum next restarts. Nothing is
    subtracted, because the removal is not confirmed.
    """
    state["context_crew_closed_key"] = f"{unit_no}:{turn_no}"
    if stop_reason not in _NORMAL_STOPS:
        state["context_crew_rerun"] = True


def note_composed(state: dict[str, Any], unit_no: int, turn_no: int, chars: int) -> None:
    """Add one composition to the running sum, and to its turn's own total.

    A composition of a turn that already closed, or of a turn number below the newest
    one in this unit, is a rerun: Crew sent it again, so it is added, and the sum is
    marked approximate until it next restarts, because the engine may hold only one
    of the attempts. Several compositions of one turn before it closes are not.
    The turn's own total restarts with a rerun, so a compaction during the new
    attempt restarts the sum from that attempt alone.
    """
    chars = max(0, chars)
    key = f"{unit_no}:{turn_no}"
    rerun = key == state["context_crew_closed_key"] or (
        key != state["context_crew_turn_key"] and turn_no < state["context_crew_last_turn"]
    )
    if rerun:
        state["context_crew_rerun"] = True
        state["context_crew_closed_key"] = ""
    state["context_crew_last_turn"] = turn_no
    if key == state["context_crew_turn_key"] and not rerun:
        state["context_crew_turn_chars"] += chars
    else:
        state["context_crew_turn_key"] = key
        state["context_crew_turn_chars"] = chars
    state["context_crew_chars"] += chars


def _encode(values: list[int | None]) -> str:
    return ",".join([MEASURED_FORMAT, *("" if v is None else str(v) for v in values)])


def _decode_values(text: Any) -> list[int | None] | None:
    if not isinstance(text, str):
        return None
    parts = text.split(",")
    if len(parts) != MEASURED_FIELDS + 1 or parts[0] != MEASURED_FORMAT:
        return None
    out: list[int | None] = []
    for part in parts[1:]:
        if part == "":
            out.append(None)
        elif part.isascii() and part.isdigit():
            out.append(int(part))
        else:
            return None
    return out


def decode_measured(text: Any) -> dict[str, Any] | None:
    """A row's encoded measurement as the dict a reader is handed, else ``None``."""
    values = _decode_values(text)
    if values is None:
        return None

    def at(name: str) -> int | None:
        return values[_INDEX[name]]

    flags = at("flags") or 0
    ttft = {name: v for name in _TTFT_KEYS if (v := at(f"ttft:{name}")) is not None}
    out: dict[str, Any] = {
        "breakdown": bool(flags & FLAG_BREAKDOWN),
        "schema_known": not flags & FLAG_SCHEMA_UNKNOWN,
        "compacted": bool(flags & FLAG_COMPACTED),
        "reset": bool(flags & FLAG_RESET),
        "rerun": bool(flags & FLAG_RERUN),
        "crew_chars": at("crew_chars") or 0,
        "ttft": ttft if "total_ms" in ttft else None,
    }
    if flags & FLAG_BREAKDOWN:
        out["model_calls"] = at("model_calls")
        out["total_chars"] = at("total_chars")
        out["categories"] = {
            name: v for name in KNOWN_CATEGORIES if (v := at(f"cat:{name}")) is not None
        }
        out["history"] = {
            name: v for name in _HISTORY_KEYS if (v := at(f"hist:{name}")) is not None
        }
        out["unknown_chars"] = at("unknown_chars") or 0
    return out


def _note_unknown(state: dict[str, Any], name: str, chars: int, *, new_turn: bool) -> None:
    """Keep a category this build does not know by name: turns seen, newest size.

    The size is the newest measurement rather than a sum: each is one request's
    content, and most of it repeats from the request before. ``new_turn`` is false for
    a second footer of a turn already counted, so a turn counts once.
    """
    unknown = state["context_measured_unknown"]
    row = unknown.get(name)
    if row is None:
        if len(unknown) >= MEASURED_UNKNOWN_LIMIT or not _keyable(name):
            return
        row = {"chars": 0, "turns": 0}
    unknown[name] = {"chars": chars, "turns": row["turns"] + (1 if new_turn else 0)}


def measured_step(state: dict[str, Any], entry: Entry) -> None:
    """Fold one ``context/measured`` onto the tail row of the context window."""
    data = entry.data
    rows = state["context_turns"]
    turn_no = _as_int(data.get("turn"))
    tail = rows[-1] if rows else None
    if (
        tail is None
        or tail["turn"] != turn_no
        or tail["unit"] != state["context_unit"]
        or tail.get("_closed")
    ):
        return
    engine_turn = _as_str(data.get("turn_id"))
    key = f"{state['context_unit']}:{turn_no}:{engine_turn}"
    if engine_turn and key == state["context_measured_last"]:
        # The same engine turn's footer again: it measured nothing new.
        return
    state["context_measured_last"] = key
    previous = _decode_values(tail.get("measured"))
    kept_flags = (previous[_INDEX["flags"]] or 0) if previous is not None else 0
    values: list[int | None] = [None] * MEASURED_FIELDS
    flags = kept_flags & FLAG_RESET
    breakdown = data.get("breakdown")
    if isinstance(breakdown, dict):
        flags |= FLAG_BREAKDOWN
        if breakdown.get("schema") != BREAKDOWN_SCHEMA:
            flags |= FLAG_SCHEMA_UNKNOWN
        for field in ("model_calls", "total_chars"):
            values[_INDEX[field]] = count(breakdown.get(field))
        unknown_chars = 0
        categories = breakdown.get("categories")
        if isinstance(categories, dict):
            for name, chars in categories.items():
                size = count(chars)
                if size is None or not isinstance(name, str):
                    continue
                slot = _INDEX.get(f"cat:{name}")
                if slot is not None:
                    values[slot] = size
                else:
                    unknown_chars = min(unknown_chars + size, MAX_COUNT)
                    _note_unknown(state, name, size, new_turn=not kept_flags & FLAG_BREAKDOWN)
        values[_INDEX["unknown_chars"]] = unknown_chars
        history = breakdown.get("history")
        summary: int | None = None
        if isinstance(history, dict):
            for name in _HISTORY_KEYS:
                values[_INDEX[f"hist:{name}"]] = count(history.get(name))
            summary = values[_INDEX["hist:compaction_summary"]]
        compacted = breakdown.get("compacted") is True
        if compacted:
            flags |= FLAG_COMPACTED
        seen = state["context_kas_summary_chars"]
        # A first reading that already carries a summary counts as one too: the
        # engine compacted at some point this fold did not see measured.
        if compacted or (summary is not None and summary != seen and (seen >= 0 or summary > 0)):
            # The engine's context restarted from a summary, so the user messages it
            # still holds begin at this turn's prompt.
            state["context_crew_chars"] = state["context_crew_turn_chars"]
            state["context_crew_rerun"] = False
            flags |= FLAG_RESET
        if summary is not None:
            state["context_kas_summary_chars"] = summary
    elif previous is not None and kept_flags & FLAG_BREAKDOWN:
        # A second entry for this turn without a breakdown keeps the first one's.
        values = list(previous)
        flags = kept_flags
    ttft = data.get("ttft")
    if isinstance(ttft, dict) and count(ttft.get("total_ms")) is not None:
        for name in _TTFT_KEYS:
            values[_INDEX[f"ttft:{name}"]] = count(ttft.get(name))
    elif previous is not None:
        for name in _TTFT_KEYS:
            values[_INDEX[f"ttft:{name}"]] = previous[_INDEX[f"ttft:{name}"]]
    values[_INDEX["crew_chars"]] = min(max(0, state["context_crew_chars"]), MAX_COUNT)
    if state["context_crew_rerun"]:
        flags |= FLAG_RERUN
    values[_INDEX["flags"]] = flags
    # REPLACED, never edited in place: the row dicts are shared with snapshots.
    rows[-1] = {**tail, "measured": _encode(values)}

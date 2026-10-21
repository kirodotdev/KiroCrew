"""A crewmate's own record as slot folds: ``goal``, its goal and milestones, and
``waiting``, what it waits on the person for.

Both entries are SNAPSHOTS the gateway appends when the fact changes
(``dashboard/goal_recorded``, ``dashboard/waiting_changed``), so each fold keeps the
newest one whole. That is what lets a crewmate's Dashboard page bind a milestone's
reading or the waiting list and move the moment the record does, with no value the
crewmate typed. Every cell is re-clamped here, because the bytes come off a file the
reader does not control.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kiro_crew.crew_log.entry_types import (
    CREWMATE_OPTION_LIMIT,
    CREWMATE_ROW_LIMIT,
    CREWMATE_TEXT_LIMIT,
    CREWMATE_WAITING_LIMIT,
    GOAL_ENTRY_TYPE,
    WAITING_ENTRY_TYPE,
)
from kiro_crew.crew_log.projection_dashboard import _panel_iso
from kiro_crew.crew_log.projection_values import _as_int, _rows_in
from kiro_crew.crew_log.schema import Entry

#: The keys one milestone row keeps, and one waiting row.
_MILESTONE_TEXT_KEYS = ("text", "target", "current", "at")
_WAITING_TEXT_KEYS = ("kind", "text", "asked_at")


def _text(value: Any) -> str:
    return value[:CREWMATE_TEXT_LIMIT] if isinstance(value, str) else ""


def _rows(value: Any, limit: int = CREWMATE_ROW_LIMIT) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, Mapping)][:limit]


# --------------------------------------------------------------------------- #
# goal
# --------------------------------------------------------------------------- #


def _goal_start() -> dict[str, Any]:
    return {"goals": [], "milestones": [], "updated_at": "", "changes": 0}


def _goal_step(state: dict[str, Any], entry: Entry) -> None:
    if entry.type != GOAL_ENTRY_TYPE:
        return
    data = entry.data
    if not isinstance(data.get("goals"), list) or not isinstance(data.get("milestones"), list):
        # A snapshot missing either half is a damaged line: applying it would splice
        # one record's goals onto another's milestones.
        return
    state["goals"] = [{"text": _text(g.get("text"))} for g in _rows(data["goals"])]
    state["milestones"] = [
        {**{k: _text(m.get(k)) for k in _MILESTONE_TEXT_KEYS}, "met": m.get("met") is True}
        for m in _rows(data["milestones"])
    ]
    state["updated_at"] = _panel_iso(entry.time)
    state["changes"] = _as_int(state.get("changes")) + 1


def _goal_render(state: dict[str, Any]) -> dict[str, Any]:
    """The goal record, with the counts a page binds as cells.

    ``goal`` is the first goal's text, the headline most pages draw; ``goals`` and
    ``milestones`` are lists for a script to draw from. ``all_met`` is true only when
    there is at least one milestone and every one is met, so a goal with none set
    yet does not read as finished. ``changes`` counts snapshots folded, which is how
    a reader tells "no goal recorded yet" (0) from "a goal with nothing in it".
    """
    milestones = [dict(m) for m in state["milestones"]]
    met = sum(1 for m in milestones if m["met"])
    return {
        "goal": state["goals"][0]["text"] if state["goals"] else "",
        "goals": [dict(g) for g in state["goals"]],
        "milestones": milestones,
        "milestones_total": len(milestones),
        "milestones_met": met,
        "milestones_open": len(milestones) - met,
        "all_met": bool(milestones) and met == len(milestones),
        "updated_at": state["updated_at"],
        "changes": _as_int(state.get("changes")),
    }


def _goal_rows(state: Mapping[str, Any]) -> int:
    """``goal``: one row per goal and per milestone."""
    return _rows_in(state.get("goals")) + _rows_in(state.get("milestones"))


# --------------------------------------------------------------------------- #
# waiting
# --------------------------------------------------------------------------- #


def _waiting_start() -> dict[str, Any]:
    return {"items": [], "updated_at": "", "changes": 0}


def _waiting_step(state: dict[str, Any], entry: Entry) -> None:
    if entry.type != WAITING_ENTRY_TYPE:
        return
    items = entry.data.get("items")
    if not isinstance(items, list):
        return
    state["items"] = [
        {
            **{k: _text(row.get(k)) for k in _WAITING_TEXT_KEYS},
            "options": [_text(o) for o in row.get("options") or [] if isinstance(o, str)][
                :CREWMATE_OPTION_LIMIT
            ],
        }
        for row in _rows(items, CREWMATE_WAITING_LIMIT)
    ]
    state["updated_at"] = _panel_iso(entry.time)
    state["changes"] = _as_int(state.get("changes")) + 1


def _waiting_render(state: dict[str, Any]) -> dict[str, Any]:
    """The waiting list, newest first, with ``count`` and ``newest`` for a page's cells.

    ``newest`` is the newest item's text, or ``""`` when nothing waits, so a page can
    say "Nothing right now" without a script.
    """
    items = [{**row, "options": list(row["options"])} for row in state["items"]]
    return {
        "items": items,
        "count": len(items),
        "newest": items[0]["text"] if items else "",
        "updated_at": state["updated_at"],
        "changes": _as_int(state.get("changes")),
    }


def _waiting_rows(state: Mapping[str, Any]) -> int:
    """``waiting``: one row per item."""
    return _rows_in(state.get("items"))

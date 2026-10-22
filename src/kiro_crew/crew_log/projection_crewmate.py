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
    return {
        "goals": [],
        "goals_total": 0,
        "milestones": [],
        "milestones_total": 0,
        "updated_at": "",
        "changes": 0,
    }


def _goal_step(state: dict[str, Any], entry: Entry) -> None:
    if entry.type != GOAL_ENTRY_TYPE:
        return
    data = entry.data
    if not isinstance(data.get("goals"), list) or not isinstance(data.get("milestones"), list):
        # A snapshot missing either half is a damaged line: applying it would splice
        # one record's goals onto another's milestones.
        return
    goals = [g for g in data["goals"] if isinstance(g, Mapping)]
    state["goals"] = [{"text": _text(g.get("text"))} for g in goals[:CREWMATE_ROW_LIMIT]]
    # A snapshot carries the whole record's counts; an older one is counted here.
    state["goals_total"] = max(_as_int(data.get("goals_total")), len(goals))
    every = [m for m in data["milestones"] if isinstance(m, Mapping)]
    state["milestones"] = [
        {k: _text(m.get(k)) for k in _MILESTONE_TEXT_KEYS} for m in every[:CREWMATE_ROW_LIMIT]
    ]
    # The total comes from the whole snapshot, not the kept rows; the rows cut are
    # counted out loud.
    state["milestones_total"] = max(_as_int(data.get("milestones_total")), len(every))
    state["updated_at"] = _panel_iso(entry.time)
    state["changes"] = _as_int(state.get("changes")) + 1


def _goal_render(state: dict[str, Any]) -> dict[str, Any]:
    """The goal record, with the counts a page binds as cells.

    ``goal`` is the first goal's text, the headline most pages draw; ``goals`` and
    ``milestones`` are lists for a script to draw from. Each milestone carries its
    ``target`` and the reading the crewmate last recorded (``current``, with ``at``)
    as text, side by side: whether it is reached is the crewmate's own word in that
    reading, never judged here. The counts cover every goal and milestone in the
    snapshot; ``goals_omitted`` and ``milestones_omitted`` say how many rows each list
    cut. ``changes`` counts snapshots folded, which is how
    a reader tells "no goal recorded yet" (0) from "a goal with nothing in it".
    """
    milestones = [dict(m) for m in state["milestones"]]
    total = max(_as_int(state.get("milestones_total")), len(milestones))
    return {
        "goal": state["goals"][0]["text"] if state["goals"] else "",
        "goals": [dict(g) for g in state["goals"]],
        "goals_total": max(_as_int(state.get("goals_total")), len(state["goals"])),
        "goals_omitted": max(_as_int(state.get("goals_total")) - len(state["goals"]), 0),
        "milestones": milestones,
        "milestones_total": total,
        "milestones_omitted": total - len(milestones),
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
    return {"items": [], "total": 0, "updated_at": "", "changes": 0}


def _waiting_step(state: dict[str, Any], entry: Entry) -> None:
    if entry.type != WAITING_ENTRY_TYPE:
        return
    items = entry.data.get("items")
    if not isinstance(items, list):
        return
    state["items"] = [
        {
            **{k: _text(row.get(k)) for k in _WAITING_TEXT_KEYS},
            "options": [_text(o) for o in _list(row.get("options")) if isinstance(o, str)][
                :CREWMATE_OPTION_LIMIT
            ],
            "options_total": _options_total(row),
        }
        for row in _rows(items, CREWMATE_WAITING_LIMIT)
    ]
    # The producer cuts at the same bound and counts what it cut; an entry without a
    # total (or one smaller than its list) counts its own rows.
    rows = sum(1 for r in items if isinstance(r, Mapping))
    state["total"] = max(_as_int(entry.data.get("total")), rows)
    state["updated_at"] = _panel_iso(entry.time)
    state["changes"] = _as_int(state.get("changes")) + 1


def _list(value: Any) -> list[Any]:
    """*value* when it is a list, else ``[]``: a damaged line's field never crashes a read."""
    return value if isinstance(value, list) else []


def _options_total(row: Mapping[str, Any]) -> int:
    """How many choices *row* offers in all: its own count, else the ones it lists."""
    listed = row.get("options")
    count = sum(1 for o in listed if isinstance(o, str)) if isinstance(listed, list) else 0
    return max(_as_int(row.get("options_total")), count)


def _waiting_render(state: dict[str, Any]) -> dict[str, Any]:
    """The waiting list, newest first, with ``count`` and ``newest`` for a page's cells.

    ``count`` is how many wait in all, ``omitted`` how many of them the list cut, and
    each item's ``options_omitted`` how many of its choices it cut.

    ``newest`` is the newest item's text, or ``""`` when nothing waits, so a page can
    say "Nothing right now" without a script.
    """
    items = [
        {
            **{k: v for k, v in row.items() if k != "options_total"},
            "options": list(row["options"]),
            "options_omitted": max(_as_int(row.get("options_total")) - len(row["options"]), 0),
        }
        for row in state["items"]
    ]
    return {
        "items": items,
        "count": max(_as_int(state.get("total")), len(items)),
        "omitted": max(_as_int(state.get("total")) - len(items), 0),
        "newest": items[0]["text"] if items else "",
        "updated_at": state["updated_at"],
        "changes": _as_int(state.get("changes")),
    }


def _waiting_rows(state: Mapping[str, Any]) -> int:
    """``waiting``: one row per item."""
    return _rows_in(state.get("items"))

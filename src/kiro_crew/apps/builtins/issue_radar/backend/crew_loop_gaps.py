"""The idle interval this app last wrote on each crew's auto-nudge loop.

The watchdog moves a crew loop between the app's two gaps as the crew's items change
phase, and must leave alone an interval an operator set through
``PATCH /api/autonudge/{loop_id}``. Both write the same field, and an operator can pick
either gap's value, so the interval alone cannot say who wrote it. This file can: a
loop whose interval still equals what the app wrote here is the app's to move; any
other interval was set by someone else and is kept.

A row is ``{"loop": <loop id>, "idle": <seconds>}`` per crew id. The loop id makes a
row for an earlier loop of the same crew read as absent. A loop with no row was armed
before this file existed, so the caller falls back to the gap values.
State lives in ``crew-loop-gaps.json``, beside ``crew-wake-checks.json``.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from kiro_crew.atomic_write import atomic_write, read_json_or

from . import store

logger = logging.getLogger("kirocrew.app.issue-radar")

_SCHEMA = 1
_LOCK = threading.Lock()


def _path(owner: str, repo: str, root: Path | None) -> Path:
    return store.repo_data_dir(owner, repo, root) / "crew-loop-gaps.json"


def _read(owner: str, repo: str, root: Path | None) -> dict[str, dict[str, object]]:
    doc = read_json_or(_path(owner, repo, root), None, logger=logger, what="crew loop gaps")
    if not isinstance(doc, dict) or doc.get("schema") != _SCHEMA:
        return {}
    rows = doc.get("crews")
    return (
        {str(c): r for c, r in rows.items() if isinstance(r, dict)}
        if isinstance(rows, dict)
        else {}
    )


def written(owner: str, repo: str, root: Path | None, crew_id: str, loop_id: str) -> int | None:
    """The interval the app last wrote on loop *loop_id* of crew *crew_id*, or None."""
    with _LOCK:
        row = _read(owner, repo, root).get(crew_id)
    if not row or row.get("loop") != loop_id:
        return None
    idle = row.get("idle")
    return idle if isinstance(idle, int) and not isinstance(idle, bool) else None


def record(owner: str, repo: str, root: Path | None, crew_id: str, loop_id: str, idle: int) -> None:
    """Note that the app set loop *loop_id* of crew *crew_id* to *idle* seconds."""
    with _LOCK:
        rows = _read(owner, repo, root)
        if rows.get(crew_id) == {"loop": loop_id, "idle": idle}:
            return
        rows[crew_id] = {"loop": loop_id, "idle": idle}
        doc = {"schema": _SCHEMA, "crews": rows}
        atomic_write(_path(owner, repo, root), json.dumps(doc, indent=2, sort_keys=True))

"""The task event log: the append every write makes, and the reads that answer from events.

Each ``TaskStore`` write appends its ``task_events`` row inside its own transaction
(``_append_event_in_tx``), and ``append_event`` records one outside any state change.
The reads answer from a row's events rather than its columns: a row's recent events,
the newest event of each kind for a page of rows, and the lost-owner recoveries earlier
gateway lives granted.

A function here whose first parameter is ``self`` is a ``TaskStore`` method: the class
binds it under the same name in ``taskq/store.py``, so it reads the store's fields and
calls its sibling methods through ``self``.

The ``TaskStore`` fields this module uses. Writes: none. Reads: ``_lock``.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any, Sequence

from .model import ADOPTED_RESUME_EVENT, RECOVERING, TaskEvent
from .store_vocab import TaskStoreUnavailable, _typed_read

if TYPE_CHECKING:
    from .store import TaskStore


def _append_event_in_tx(
    self: TaskStore,
    conn: sqlite3.Connection,
    task_id: str,
    kind: str,
    data: dict[str, Any],
    *,
    ts: float | None = None,
) -> None:
    seq_row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM task_events WHERE task_id=?", (task_id,)
    ).fetchone()
    seq = int(seq_row[0]) if seq_row else 1
    conn.execute(
        "INSERT INTO task_events(task_id, seq, ts, kind, data_json) VALUES(?,?,?,?,?)",
        (
            task_id,
            seq,
            self.now() if ts is None else ts,
            kind,
            json.dumps(data, sort_keys=True, default=str),
        ),
    )


def append_event(
    self: TaskStore, task_id: str, kind: str, data: dict[str, Any] | None = None
) -> None:
    """Append one event outside any state change (progress, ``deliver``)."""
    with self._lock:
        conn = self._c()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._append_event_in_tx(conn, task_id, kind, data or {})
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            self._rollback(conn)
            raise TaskStoreUnavailable(f"task event write failed: {exc}") from exc


@_typed_read
def lost_owner_recoveries(self: TaskStore, task_id: str) -> int:
    """How many lost-owner recoveries *task_id* was granted by earlier gateway lives.

    Two writers grant one. The boot reconciler sends a row back to
    ``recovering`` (a ``transition`` event, ``reconciled: lost_owner``). A
    runner kind's adoption sweep resumes the row and records an
    ``adopted_resume`` event naming its incarnation; those are counted once
    per incarnation other than this one, so a second sweep in the same
    gateway life spends nothing. Counted from the row's own events, so the
    count survives every restart and is not capped by the :meth:`events`
    window.
    """
    with self._lock:
        row = (
            self._c()
            .execute(
                "SELECT (SELECT COUNT(*) FROM task_events WHERE task_id=? "
                "AND kind='transition' AND json_extract(data_json, '$.to')=? "
                "AND json_extract(data_json, '$.reconciled')='lost_owner') + "
                "(SELECT COUNT(DISTINCT json_extract(data_json, '$.incarnation')) "
                "FROM task_events WHERE task_id=? AND kind=? "
                "AND json_extract(data_json, '$.incarnation')!=?) AS n",
                (task_id, RECOVERING, task_id, ADOPTED_RESUME_EVENT, self.incarnation),
            )
            .fetchone()
        )
    return int(row["n"]) if row is not None else 0


@_typed_read
def events(self: TaskStore, task_id: str, *, limit: int = 200) -> list[TaskEvent]:
    with self._lock:
        rows = (
            self._c()
            .execute(
                "SELECT task_id, seq, ts, kind, data_json FROM task_events "
                "WHERE task_id=? ORDER BY seq DESC LIMIT ?",
                (task_id, int(limit)),
            )
            .fetchall()
        )
    out: list[TaskEvent] = []
    for r in reversed(rows):
        try:
            data = json.loads(r["data_json"])
        except (TypeError, ValueError):
            data = {}
        out.append(
            TaskEvent(
                task_id=r["task_id"],
                seq=int(r["seq"]),
                ts=float(r["ts"]),
                kind=str(r["kind"]),
                data=data if isinstance(data, dict) else {},
            )
        )
    return out


@_typed_read
def latest_events(
    self: TaskStore, task_ids: Sequence[str], kinds: Sequence[str]
) -> dict[tuple[str, str], TaskEvent]:
    """The newest event of each of *kinds* for each of *task_ids*, in ONE query.

    Keyed ``(task_id, kind)``; a pair with no such event is absent. For a
    reader that needs one fact per row of a page (the queued listing),
    where one :meth:`events` call per row would serialize a query per row
    on the writer thread.
    """
    ids = [str(i) for i in task_ids if i]
    if not ids or not kinds:
        return {}
    id_marks = ", ".join("?" for _ in ids)
    kind_marks = ", ".join("?" for _ in kinds)
    with self._lock:
        # SQLite fills the bare columns of a MAX() aggregate from the row
        # holding that maximum, so each group yields its newest event.
        rows = (
            self._c()
            .execute(
                "SELECT task_id, MAX(seq) AS seq, ts, kind, data_json FROM task_events "
                f"WHERE task_id IN ({id_marks}) AND kind IN ({kind_marks}) "
                "GROUP BY task_id, kind",
                [*ids, *kinds],
            )
            .fetchall()
        )
    out: dict[tuple[str, str], TaskEvent] = {}
    for r in rows:
        try:
            data = json.loads(r["data_json"])
        except (TypeError, ValueError):
            data = {}
        out[(str(r["task_id"]), str(r["kind"]))] = TaskEvent(
            task_id=str(r["task_id"]),
            seq=int(r["seq"]),
            ts=float(r["ts"]),
            kind=str(r["kind"]),
            data=data if isinstance(data, dict) else {},
        )
    return out

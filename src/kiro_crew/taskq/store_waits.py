"""Wait rows: the store writes and reads behind ``taskq.waits``' ledger.

``enter_wait`` moves a row into a ``WAITING`` state with its wait record and
``update_wait`` rewrites the record of a row already waiting, both under the caller's
generation; ``wake_wait`` ends the wait under generation+1, the re-admission fence. The
reads name the rows the ledger rebuilds and sweeps: the waiting rows, a parent's
children, and the children whose parent is terminal.

A function here whose first parameter is ``self`` is a ``TaskStore`` method: the class
binds it under the same name in ``taskq/store.py``, so it reads the store's fields and
calls its sibling methods through ``self``.

The ``TaskStore`` fields this module uses. Writes: none. Reads: ``_incarnation``,
``_lease_secs``, ``_lock``.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any

from .model import RETRY_WAIT, RUNNING, WAITING, InvalidTransition, TaskRecord
from .store_vocab import _SQL_TERMINAL, TaskStoreUnavailable, _typed_read

if TYPE_CHECKING:
    from .store import TaskStore

_SQL_WAITING = "(" + ",".join(f"'{s}'" for s in sorted(WAITING)) + ")"


def enter_wait(
    self: TaskStore, task_id: str, record: dict[str, Any], *, generation: int | None = None
) -> bool:
    """``running -> <record["state"]>`` with the wait record on the row.

    One write: the state, the record and a ``wait`` event land together or
    not at all. The lane slot is the caller's to release (admission owns
    it); the generation is NOT bumped, because the same run continues.
    """
    state = str(record.get("state") or "")
    if state not in WAITING:
        raise InvalidTransition("?", state)
    return self.transition(
        task_id,
        state,
        generation=generation,
        wait=record,
        detail={
            "wait": True,
            "reason": str(record.get("reason") or "")[:200],
            "resume": (record.get("resume_condition") or {}).get("kind"),
            "slot_released": bool(record.get("slot_released", True)),
            "residency_charged": bool(record.get("residency_charged", True)),
        },
    )


def update_wait(
    self: TaskStore, task_id: str, record: dict[str, Any], *, generation: int | None = None
) -> bool:
    """Rewrite the wait record of a row already in ``record["state"]``.

    For a children wait that gains an awaited child after entry. Same
    state, same generation: no transition, one ``wait_updated`` event.
    """
    state = str(record.get("state") or "")
    ts = self.now()
    with self._lock:
        conn = self._c()
        try:
            conn.execute("BEGIN IMMEDIATE")
            args: list[Any] = [
                json.dumps(record, sort_keys=True, default=str),
                ts,
                task_id,
                state,
            ]
            sql = "UPDATE tasks SET wait_json=?, updated_at=? WHERE id=? AND state=?"
            if generation is not None:
                sql += " AND generation=?"
                args.append(int(generation))
            cur = conn.execute(sql, args)
            if cur.rowcount == 1:
                self._append_event_in_tx(
                    conn,
                    task_id,
                    "wait_updated",
                    {"ids": list((record.get("resume_condition") or {}).get("ids") or [])},
                )
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            self._rollback(conn)
            raise TaskStoreUnavailable(f"wait update failed: {exc}") from exc
    return cur.rowcount == 1


def wake_wait(
    self: TaskStore,
    task_id: str,
    *,
    reason: str,
    generation: int | None = None,
    to: str = RETRY_WAIT,
    only_from: frozenset[str] | None = None,
    detail: dict[str, Any] | None = None,
) -> int | None:
    """End a wait under generation+1; returns the new generation.

    ``to`` is where the row goes: ``running`` ONLY when the caller holds
    the lane slot and the runtime is resident (admission's ``resume_grant``,
    called after the pump granted the slot); every other wake -- a runner
    coroutine that still has to reacquire its lane, a parked row whose run
    is gone, an answer delivered to a ``waiting_input`` row -- lands in
    ``retry_wait`` with ``next_run_at = now`` and no lease: claimable at
    once, so the dispatcher / the runner's re-admission claims it and writes
    ``running`` only when capacity was actually granted. A crash in that
    gap leaves a claimable row, not a ``running`` row with a dead owner
    that reconciles to ``unknown_side_effect``.

    ``only_from`` narrows the default fence -- any state in
    :data:`model.WAITING` -- to the wait the caller's wake ANSWERS, exactly
    as :meth:`cancel`'s does for a cancel: a wake carries the answer to ONE
    question, and ``waiting_input`` / ``waiting_dependency`` /
    ``waiting_children`` are different questions, so a caller that read one
    of them names it here and a row that moved to another between the read
    and this call is refused instead of having the wrong wait ended. The
    refusal is the ``rejected_transition`` the default fence already
    records, and the predicate is tested in THIS transaction.

    The bump is the re-admission fence (RFC §14.2): callbacks issued during
    the wait carry the old generation and are rejected as ``stale_result``.
    None when the row is missing, not waiting (or not in ``only_from``), or
    the generation is stale.
    """
    if to not in (RUNNING, RETRY_WAIT):
        raise ValueError(f"wake target must be running or retry_wait, not {to!r}")
    ts = self.now()
    with self._lock:
        conn = self._c()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state, generation FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            old, gen = str(row["state"]), int(row["generation"])
            if generation is not None and int(generation) != gen:
                self._append_event_in_tx(
                    conn,
                    task_id,
                    "stale_result",
                    {"from_generation": int(generation), "current": gen, "wanted": to},
                )
                conn.execute("COMMIT")
                return None
            fence = WAITING if only_from is None else (WAITING & only_from)
            if old not in fence:
                self._append_event_in_tx(
                    conn, task_id, "rejected_transition", {"from": old, "to": to}
                )
                conn.execute("COMMIT")
                return None
            if to == RUNNING:
                conn.execute(
                    "UPDATE tasks SET state=?, generation=generation+1, wait_json=NULL, "
                    "lease_owner=?, lease_expires_at=?, updated_at=? "
                    "WHERE id=? AND state=? AND generation=?",
                    (RUNNING, self._incarnation, ts + self._lease_secs, ts, task_id, old, gen),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET state=?, generation=generation+1, wait_json=NULL, "
                    "lease_owner=NULL, lease_expires_at=NULL, next_run_at=?, updated_at=? "
                    "WHERE id=? AND state=? AND generation=?",
                    (RETRY_WAIT, ts, ts, task_id, old, gen),
                )
            data: dict[str, Any] = {
                "from": old,
                "to": to,
                "reason": reason[:200],
                "generation": gen + 1,
            }
            if detail:
                data.update(detail)
            self._append_event_in_tx(conn, task_id, "wake", data)
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            self._rollback(conn)
            raise TaskStoreUnavailable(f"task wake failed: {exc}") from exc
    self._note_state(task_id, to)
    return gen + 1


@_typed_read
def children_of(self: TaskStore, parent_id: str) -> list[TaskRecord]:
    with self._lock:
        rows = (
            self._c()
            .execute(
                "SELECT * FROM tasks WHERE parent_id=? ORDER BY created_at, rowid",
                (parent_id,),
            )
            .fetchall()
        )
    return [TaskRecord.from_row(r) for r in rows]


@_typed_read
def waiting_rows(self: TaskStore, *, state: str | None = None) -> list[TaskRecord]:
    """Rows in a ``WAITING`` state (one state when given), oldest first."""
    if state is not None:
        if state not in WAITING:
            return []
        sql, args = "SELECT * FROM tasks WHERE state=? ORDER BY created_at, rowid", [state]
    else:
        sql, args = (
            f"SELECT * FROM tasks WHERE state IN {_SQL_WAITING} ORDER BY created_at, rowid",
            [],
        )
    with self._lock:
        rows = self._c().execute(sql, args).fetchall()
    return [TaskRecord.from_row(r) for r in rows]


@_typed_read
def orphaned_children(self: TaskStore) -> list[TaskRecord]:
    """Non-terminal rows whose parent row is terminal: nothing will collect them."""
    with self._lock:
        rows = (
            self._c()
            .execute(
                "SELECT c.* FROM tasks c JOIN tasks p ON p.id=c.parent_id "
                f"WHERE c.state NOT IN {_SQL_TERMINAL} AND p.state IN {_SQL_TERMINAL} "
                "ORDER BY c.created_at, c.rowid"
            )
            .fetchall()
        )
    return [TaskRecord.from_row(r) for r in rows]

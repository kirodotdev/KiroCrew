"""Accepted-work rows: the durable admission records beside the task rows.

The ``accepted_work`` table holds one row per unit of work an admission gate
accepted (``kiro_crew.shared_crew.work_store`` is its only writer). It lives in
``tasks.db`` so it shares the task store's write-before-ack discipline, its
corruption quarantine, its journal-mode choice and its single writer thread,
and so a restart reconciles admission records and task rows from one file.

Rows are opaque here: ``record_json`` is the caller's serialized record, and
the indexed columns (``scope``, ``budget_key``, ``session_key``, ``state``,
``revision``) exist so the store can enforce idempotency and compare-and-set
without decoding it. Every write is one transaction.

A function here whose first parameter is ``self`` is a ``TaskStore`` method: the class
binds it under the same name in ``taskq/store.py``.

The ``TaskStore`` fields this module uses. Writes: none. Reads: ``_lock``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from .store_vocab import TaskStoreUnavailable, _typed_read

if TYPE_CHECKING:
    from .store import TaskStore


@dataclass(frozen=True)
class WorkRow:
    work_id: str
    scope: str
    budget_key: str
    session_key: str
    state: str
    revision: int
    accepted_at: float
    updated_at: float
    record_json: str
    payload_json: str | None


def _row(r: sqlite3.Row) -> WorkRow:
    return WorkRow(
        work_id=str(r["work_id"]),
        scope=str(r["scope"]),
        budget_key=str(r["budget_key"]),
        session_key=str(r["session_key"]),
        state=str(r["state"]),
        revision=int(r["revision"]),
        accepted_at=float(r["accepted_at"]),
        updated_at=float(r["updated_at"]),
        record_json=str(r["record_json"]),
        payload_json=None if r["payload_json"] is None else str(r["payload_json"]),
    )


def work_insert_or_get(self: TaskStore, row: WorkRow) -> tuple[WorkRow, bool]:
    """Insert *row* unless its ``scope`` exists; return ``(stored row, inserted)``.

    The idempotency decision and the insert are one ``BEGIN IMMEDIATE``
    transaction, so two concurrent submissions of one scope store one row and
    both callers read it back.
    """
    with self._lock:
        conn = self._c()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM accepted_work WHERE scope=?", (row.scope,)
            ).fetchone()
            if existing is not None:
                conn.execute("COMMIT")
                return _row(existing), False
            conn.execute(
                "INSERT INTO accepted_work(work_id, scope, budget_key, session_key, state, "
                "revision, accepted_at, updated_at, record_json, payload_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    row.work_id,
                    row.scope,
                    row.budget_key,
                    row.session_key,
                    row.state,
                    row.revision,
                    row.accepted_at,
                    row.updated_at,
                    row.record_json,
                    row.payload_json,
                ),
            )
            conn.execute("COMMIT")
            return row, True
        except sqlite3.Error as exc:
            self._rollback(conn)
            raise TaskStoreUnavailable(f"accepted-work insert failed: {exc}") from exc


def work_compare_and_set(
    self: TaskStore,
    work_id: str,
    *,
    expected_revision: int,
    state: str,
    revision: int,
    updated_at: float,
    record_json: str,
    clear_payload: bool = False,
) -> bool:
    """Write the next revision of *work_id* only if it is still *expected_revision*."""
    with self._lock:
        conn = self._c()
        try:
            conn.execute("BEGIN IMMEDIATE")
            payload_sql = ", payload_json=NULL" if clear_payload else ""
            cur = conn.execute(
                "UPDATE accepted_work SET state=?, revision=?, updated_at=?, record_json=?"
                f"{payload_sql} WHERE work_id=? AND revision=?",
                (state, revision, updated_at, record_json, work_id, expected_revision),
            )
            conn.execute("COMMIT")
            return cur.rowcount == 1
        except sqlite3.Error as exc:
            self._rollback(conn)
            raise TaskStoreUnavailable(f"accepted-work update failed: {exc}") from exc


@_typed_read
def work_get(self: TaskStore, work_id: str) -> WorkRow | None:
    with self._lock:
        r = self._c().execute("SELECT * FROM accepted_work WHERE work_id=?", (work_id,)).fetchone()
    return None if r is None else _row(r)


@_typed_read
def work_by_scope(self: TaskStore, scope: str) -> str | None:
    """The work id stored under idempotency *scope*, or None."""
    with self._lock:
        r = (
            self._c()
            .execute("SELECT work_id FROM accepted_work WHERE scope=?", (scope,))
            .fetchone()
        )
    return None if r is None else str(r[0])


@_typed_read
def work_rows(
    self: TaskStore,
    *,
    states: Sequence[str] | None = None,
    session_key: str | None = None,
    budget_key: str | None = None,
    limit: int = 1000,
) -> list[WorkRow]:
    """Rows matching every given filter, oldest accepted first, at most *limit*."""
    clauses: list[str] = []
    args: list[object] = []
    if states is not None:
        if not states:
            return []
        clauses.append("state IN (" + ",".join("?" for _ in states) + ")")
        args.extend(states)
    if session_key is not None:
        clauses.append("session_key=?")
        args.append(session_key)
    if budget_key is not None:
        clauses.append("budget_key=?")
        args.append(budget_key)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    args.append(max(1, int(limit)))
    with self._lock:
        rows = (
            self._c()
            .execute(
                f"SELECT * FROM accepted_work{where} ORDER BY accepted_at, work_id LIMIT ?",
                args,
            )
            .fetchall()
        )
    return [_row(r) for r in rows]


def work_purge(self: TaskStore, *, states: Sequence[str], updated_before: float) -> int:
    """Delete rows in *states* last written before *updated_before*; returns the count."""
    if not states:
        return 0
    with self._lock:
        conn = self._c()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "DELETE FROM accepted_work WHERE updated_at < ? AND state IN ("
                + ",".join("?" for _ in states)
                + ")",
                (updated_before, *states),
            )
            conn.execute("COMMIT")
            return int(cur.rowcount)
        except sqlite3.Error as exc:
            self._rollback(conn)
            raise TaskStoreUnavailable(f"accepted-work purge failed: {exc}") from exc


def storage_charge(self: TaskStore, budget_key: str, delta: int, *, at: float) -> int:
    """Add *delta* bytes (never below zero) to *budget_key*'s ledger; returns the total."""
    with self._lock:
        conn = self._c()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO principal_storage(budget_key, bytes, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(budget_key) DO UPDATE SET bytes=MAX(0, bytes + ?), "
                "updated_at=excluded.updated_at",
                (budget_key, max(0, int(delta)), at, int(delta)),
            )
            row = conn.execute(
                "SELECT bytes FROM principal_storage WHERE budget_key=?", (budget_key,)
            ).fetchone()
            conn.execute("COMMIT")
            return int(row[0]) if row is not None else 0
        except sqlite3.Error as exc:
            self._rollback(conn)
            raise TaskStoreUnavailable(f"storage ledger write failed: {exc}") from exc


@_typed_read
def storage_bytes(self: TaskStore, budget_key: str) -> int:
    with self._lock:
        row = (
            self._c()
            .execute("SELECT bytes FROM principal_storage WHERE budget_key=?", (budget_key,))
            .fetchone()
        )
    return int(row[0]) if row is not None else 0


__all__ = [
    "storage_bytes",
    "storage_charge",
    "WorkRow",
    "work_by_scope",
    "work_compare_and_set",
    "work_get",
    "work_insert_or_get",
    "work_purge",
    "work_rows",
]

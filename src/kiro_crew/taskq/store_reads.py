"""Row reads: one row, the dispatch reads, the pending listings and the counts.

The dispatch reads (``fetch_dispatchable``, ``fetch_dispatchable_fair``,
``pending_lanes``) name the rows a ``claim`` would take now, and ``next_eligible_at``
says when time alone releases the next one. The listings and counts answer the queue's
callers: the pending rows of a kind, session or batch, the active rows, the per-state
counts and the oldest wait, and the rows no owner holds long after they fell due
(``overdue_unleased_rows``, a ``kirocrew doctor`` read). Each read reports a database
failure as ``TaskStoreUnavailable`` (``_typed_read``).

A function here whose first parameter is ``self`` is a ``TaskStore`` method: the class
binds it under the same name in ``taskq/store.py``, so it reads the store's fields and
calls its sibling methods through ``self``.

The ``TaskStore`` fields this module uses. Writes: none. Reads: ``_lock``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

from .model import ADMITTED, RECOVERING, TaskRecord
from .store_vocab import _SQL_ACTIVE, _SQL_CLAIMABLE, _SQL_TERMINAL, _SQL_UNSTARTED, _typed_read

if TYPE_CHECKING:
    from . import lanes as _lanes
    from .store import TaskStore

#: The ``children_only`` filter the dispatch reads and their wake share: nested rows.
_SQL_CHILD_ONLY = " AND parent_id IS NOT NULL AND parent_id <> ''"


@_typed_read
def get(self: TaskStore, task_id: str) -> TaskRecord | None:
    with self._lock:
        row = self._c().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return TaskRecord.from_row(row) if row is not None else None


@_typed_read
def state_of(self: TaskStore, task_id: str) -> str | None:
    with self._lock:
        row = self._c().execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
    return str(row["state"]) if row is not None else None


def _admitted_exclusion(ids: Sequence[str]) -> tuple[str, list[Any]]:
    ids = [str(i) for i in ids if i]
    if not ids:
        return "", []
    return (
        f" AND NOT (state='{ADMITTED}' AND id IN ({', '.join('?' for _ in ids)}))",
        ids,
    )


def _exclusion(exclude_ids: Sequence[str]) -> tuple[str, list[Any]]:
    ids = [str(i) for i in exclude_ids if i]
    if not ids:
        return "", []
    return f" AND id NOT IN ({', '.join('?' for _ in ids)})", ids


@_typed_read
def pending_lanes(
    self: TaskStore,
    kind: str,
    *,
    exclude_ids: Sequence[str] = (),
    children_only: bool = False,
) -> dict[str, int]:
    """Eligible (claimable now) row count per lane, for the fair dispatcher."""
    ts = self.now()
    excl_sql, excl_args = self._exclusion(exclude_ids)
    child_sql = _SQL_CHILD_ONLY if children_only else ""
    with self._lock:
        rows = (
            self._c()
            .execute(
                f"SELECT lane, COUNT(*) FROM tasks WHERE kind=? AND state IN {_SQL_CLAIMABLE} "
                "AND (next_run_at IS NULL OR next_run_at<=?) "
                "AND (lease_expires_at IS NULL OR lease_expires_at<?)"
                f"{child_sql}{excl_sql} GROUP BY lane",
                [kind, ts, ts, *excl_args],
            )
            .fetchall()
        )
    return {str(r[0] or ""): int(r[1]) for r in rows}


@_typed_read
def fetch_dispatchable_fair(
    self: TaskStore,
    kind: str,
    *,
    limit: int,
    scheduler: "_lanes.LaneScheduler",
    exclude_ids: Sequence[str] = (),
    children_only: bool = False,
    lanes: Sequence[str] | None = None,
    per_lane_limit: int | None = None,
) -> list[TaskRecord]:
    """Claimable-now rows of *kind* in weighted round-robin order across lanes.

    Each lane contributes its oldest eligible rows (FIFO by ``created_at``
    inside the lane, at most *per_lane_limit* of them); *scheduler*
    interleaves the lanes, carrying its credit across calls. ``lanes``
    restricts the read to those lanes; ``children_only`` to nested rows
    (``parent_id`` set) -- the rows allowed to take the reserved child
    slot. With one lane pending this is exactly the FIFO order of
    :meth:`fetch_dispatchable`.
    """
    if limit <= 0:
        return []
    if lanes is not None and not lanes:
        return []
    ts = self.now()
    excl_sql, excl_args = self._exclusion(exclude_ids)
    child_sql = _SQL_CHILD_ONLY if children_only else ""
    lane_sql, lane_args = ("", [])
    if lanes is not None:
        lane_sql = f" AND lane IN ({', '.join('?' for _ in lanes)})"
        lane_args = [str(x) for x in lanes]
    rank = int(limit if per_lane_limit is None else max(1, per_lane_limit))
    with self._lock:
        rows = (
            self._c()
            .execute(
                "SELECT * FROM ("
                "SELECT *, ROW_NUMBER() OVER (PARTITION BY lane ORDER BY created_at, rowid) "
                f"AS lane_rank FROM tasks WHERE kind=? AND state IN {_SQL_CLAIMABLE} "
                "AND (next_run_at IS NULL OR next_run_at<=?) "
                "AND (lease_expires_at IS NULL OR lease_expires_at<?)"
                f"{child_sql}{lane_sql}{excl_sql}) WHERE lane_rank<=? ORDER BY lane, lane_rank",
                [kind, ts, ts, *lane_args, *excl_args, rank],
            )
            .fetchall()
        )
    by_lane: dict[str, list[TaskRecord]] = {}
    for r in rows:
        rec = TaskRecord.from_row(r)
        by_lane.setdefault(rec.lane, []).append(rec)
    return scheduler.interleave(by_lane, limit=limit, age_of=lambda rec: rec.created_at)


@_typed_read
def fetch_dispatchable(
    self: TaskStore,
    kind: str,
    *,
    limit: int,
    exclude_ids: Sequence[str] = (),
    session_key: str | None = None,
) -> list[TaskRecord]:
    """Oldest-first rows of *kind* that a claim would succeed on right now."""
    ts = self.now()
    excl_sql, excl_args = self._exclusion(exclude_ids)
    sess_sql, sess_args = ("", [])
    if session_key is not None:
        sess_sql, sess_args = " AND session_key=?", [session_key]
    with self._lock:
        rows = (
            self._c()
            .execute(
                f"SELECT * FROM tasks WHERE kind=? AND state IN {_SQL_CLAIMABLE} "
                "AND (next_run_at IS NULL OR next_run_at<=?) "
                "AND (lease_expires_at IS NULL OR lease_expires_at<?)"
                f"{excl_sql}{sess_sql} ORDER BY created_at, rowid LIMIT ?",
                [kind, ts, ts, *excl_args, *sess_args, int(limit)],
            )
            .fetchall()
        )
    return [TaskRecord.from_row(r) for r in rows]


@_typed_read
def list_pending(
    self: TaskStore,
    kind: str,
    *,
    session_key: str | None = None,
    exclude_ids: Sequence[str] = (),
    limit: int | None = None,
    include_admitted: bool = False,
    exclude_admitted_ids: Sequence[str] = (),
    app: str | None = None,
) -> list[TaskRecord]:
    """Every waiting row of *kind* (deferred ones included), oldest first.

    Unbounded by default, like :meth:`fetch_pending_by_batch` and
    :meth:`active_rows`: the caller is the Stop-all cascade
    (``cancellation.cancel_for_parent_impl`` through
    ``taskq_pending_ids_for``), which stops the rows this answer names and
    nothing else -- so a row past a cap is one the user stopped that stays
    queued and dispatchable. A caller that wants a page (a listing, a probe)
    passes *limit* and gets the oldest that many.

    *include_admitted* adds ``admitted`` rows (claimed, not started); see
    :data:`_SQL_UNSTARTED`, and *exclude_admitted_ids* drops the ``admitted``
    ones among those ids (a claim whose run is registered). *app* keeps only
    rows whose params name that owning app, applied before *limit* so a page
    is that app's own rows.
    """
    excl_sql, excl_args = self._exclusion(exclude_ids)
    adm_sql, adm_args = self._admitted_exclusion(exclude_admitted_ids)
    sess_sql, sess_args = ("", [])
    if session_key is not None:
        sess_sql, sess_args = " AND session_key=?", [session_key]
    app_sql, app_args = ("", [])
    if app is not None:
        app_sql, app_args = " AND COALESCE(json_extract(params_json, '$.app'), '')=?", [app]
    lim_sql, lim_args = ("", [])
    if limit is not None:
        lim_sql, lim_args = " LIMIT ?", [int(limit)]
    states = _SQL_UNSTARTED if include_admitted else _SQL_CLAIMABLE
    with self._lock:
        rows = (
            self._c()
            .execute(
                f"SELECT * FROM tasks WHERE kind=? AND state IN {states}"
                f"{sess_sql}{app_sql}{excl_sql}{adm_sql} ORDER BY created_at, rowid{lim_sql}",
                [kind, *sess_args, *app_args, *excl_args, *adm_args, *lim_args],
            )
            .fetchall()
        )
    return [TaskRecord.from_row(r) for r in rows]


@_typed_read
def fetch_pending_by_batch(
    self: TaskStore, kind: str, batch_id: str, *, exclude_ids: Sequence[str] = ()
) -> list[TaskRecord]:
    """Waiting rows of *kind* whose params name *batch_id* (wave accounting)."""
    excl_sql, excl_args = self._exclusion(exclude_ids)
    with self._lock:
        rows = (
            self._c()
            .execute(
                f"SELECT * FROM tasks WHERE kind=? AND state IN {_SQL_CLAIMABLE} "
                f"AND json_extract(params_json, '$.batch_id')=?{excl_sql} "
                "ORDER BY created_at, rowid",
                [kind, batch_id, *excl_args],
            )
            .fetchall()
        )
    return [TaskRecord.from_row(r) for r in rows]


@_typed_read
def next_eligible_at(
    self: TaskStore,
    kind: str,
    *,
    exclude_ids: Sequence[str] = (),
    children_only: bool = False,
) -> float | None:
    """When the earliest waiting row of *kind* that time alone holds back
    becomes claimable; None when no row is held by time.

    A row is held by time while it is deferred (``next_run_at``) or leased
    (``lease_expires_at``), and claimable at the later of the two -- the
    same eligibility the dispatch reads apply (``pending_lanes``,
    ``fetch_dispatchable_fair``), over the same rows: one they may not
    claim (excluded, or not a child on a ``children_only`` pass) has no
    wake to offer them. A row with neither is not a wake: no time has to
    pass for it, so a pass that found nothing left it out for a reason the
    clock does not change. Read as "due at 0", it would re-arm that empty
    pass at once, on every pass, for as long as the row is held.
    """
    excl_sql, excl_args = self._exclusion(exclude_ids)
    child_sql = _SQL_CHILD_ONLY if children_only else ""
    with self._lock:
        row = (
            self._c()
            .execute(
                "SELECT MIN(MAX(COALESCE(next_run_at, 0), COALESCE(lease_expires_at, 0))) "
                f"FROM tasks WHERE kind=? AND state IN {_SQL_CLAIMABLE} "
                "AND (next_run_at IS NOT NULL OR lease_expires_at IS NOT NULL)"
                f"{child_sql}{excl_sql}",
                [kind, *excl_args],
            )
            .fetchone()
        )
    if not row or row[0] is None:
        return None
    return float(row[0])


@_typed_read
def count_pending(
    self: TaskStore,
    kind: str | None = None,
    *,
    exclude_ids: Sequence[str] = (),
    session_key: str | None = None,
    eligible_only: bool = False,
    include_admitted: bool = False,
    exclude_admitted_ids: Sequence[str] = (),
    include_recovering: bool = True,
) -> int:
    """Rows waiting for dispatch (claimable states), optionally per session.

    ``eligible_only`` drops rows deferred past now; the default counts a
    deferred row too, because it is still accepted work the parent is owed.
    ``include_admitted`` counts claimed, not started rows as well
    (:data:`_SQL_UNSTARTED`), less the ``admitted`` ones among
    *exclude_admitted_ids*. ``include_recovering=False`` drops
    ``recovering`` rows: claimable, but each one a run that had started and
    lost its owner (a restart) and is being rebuilt, so a count of spawns
    waiting for their FIRST start -- the queue-depth chip's -- leaves them
    out, while every count of work still owed keeps them.
    """
    excl_sql, excl_args = self._exclusion(exclude_ids)
    adm_sql, adm_args = self._admitted_exclusion(exclude_admitted_ids)
    where = [f"state IN {_SQL_UNSTARTED if include_admitted else _SQL_CLAIMABLE}"]
    if not include_recovering:
        where.append(f"state <> '{RECOVERING}'")
    args: list[Any] = []
    if kind is not None:
        where.append("kind=?")
        args.append(kind)
    if session_key is not None:
        where.append("session_key=?")
        args.append(session_key)
    if eligible_only:
        where.append("(next_run_at IS NULL OR next_run_at<=?)")
        args.append(self.now())
    with self._lock:
        row = (
            self._c()
            .execute(
                f"SELECT COUNT(*) FROM tasks WHERE {' AND '.join(where)}{excl_sql}{adm_sql}",
                [*args, *excl_args, *adm_args],
            )
            .fetchone()
        )
    return int(row[0]) if row else 0


@_typed_read
def count(self: TaskStore, *, state: str | None = None, kind: str | None = None) -> int:
    where, args = [], []
    if state is not None:
        where.append("state=?")
        args.append(state)
    if kind is not None:
        where.append("kind=?")
        args.append(kind)
    sql = "SELECT COUNT(*) FROM tasks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    with self._lock:
        row = self._c().execute(sql, args).fetchone()
    return int(row[0]) if row else 0


@_typed_read
def count_by_state(self: TaskStore, *, kind: str | None = None) -> dict[str, int]:
    sql = "SELECT state, COUNT(*) AS n FROM tasks"
    args: list[Any] = []
    if kind is not None:
        sql += " WHERE kind=?"
        args.append(kind)
    sql += " GROUP BY state"
    with self._lock:
        rows = self._c().execute(sql, args).fetchall()
    return {str(r["state"]): int(r["n"]) for r in rows}


@_typed_read
def list_rows(
    self: TaskStore,
    *,
    state: str | None = None,
    kind: str | None = None,
    session_key: str | None = None,
    lane: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[TaskRecord]:
    """Rows oldest-first, optionally filtered; ``lane`` is the stored
    fairness lane (every row carries one: derived at accept, backfilled by
    the v3 migration), so the filter is a SQL predicate on ``tasks_lane``."""
    where, args = [], []
    if state is not None:
        where.append("state=?")
        args.append(state)
    if kind is not None:
        where.append("kind=?")
        args.append(kind)
    if session_key is not None:
        where.append("session_key=?")
        args.append(session_key)
    if lane is not None:
        where.append("lane=?")
        args.append(lane)
    sql = "SELECT * FROM tasks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at, rowid LIMIT ? OFFSET ?"
    with self._lock:
        rows = self._c().execute(sql, [*args, int(limit), int(offset)]).fetchall()
    return [TaskRecord.from_row(r) for r in rows]


@_typed_read
def active_rows(self: TaskStore, *, exclude_owner: str | None = None) -> list[TaskRecord]:
    """Rows some incarnation owns; optionally only those NOT owned by *exclude_owner*."""
    sql = f"SELECT * FROM tasks WHERE state IN {_SQL_ACTIVE}"
    args: list[Any] = []
    if exclude_owner is not None:
        sql += " AND (lease_owner IS NULL OR lease_owner<>?)"
        args.append(exclude_owner)
    sql += " ORDER BY created_at, rowid"
    with self._lock:
        rows = self._c().execute(sql, args).fetchall()
    return [TaskRecord.from_row(r) for r in rows]


@_typed_read
def oldest_wait_secs(self: TaskStore, *, kind: str | None = None) -> float:
    """Age of the oldest row still waiting for dispatch; 0 when none."""
    sql = f"SELECT MIN(created_at) FROM tasks WHERE state IN {_SQL_CLAIMABLE}"
    args: list[Any] = []
    if kind is not None:
        sql += " AND kind=?"
        args.append(kind)
    with self._lock:
        row = self._c().execute(sql, args).fetchone()
    if not row or row[0] is None:
        return 0.0
    return max(0.0, self.now() - float(row[0]))


@_typed_read
def overdue_unleased_rows(self: TaskStore, overdue_secs: float) -> list[TaskRecord]:
    """Rows that are not terminal, hold no lease, and fell due *overdue_secs* ago or more.

    Due means ``next_run_at`` is set and at or before ``now - overdue_secs``; a row
    with no ``next_run_at`` was never deferred and is not counted. No lease means
    ``lease_owner`` is empty: no incarnation is running or re-dispatching the row.
    While a gateway runs, a due claimable row is claimed when a slot frees, so a row
    still in this shape long after its due time is one nothing is picking up, and a
    claimable sub-agent row in it keeps its parent's pending count above zero.
    Oldest due first. A read: nothing is written.
    """
    cutoff = self.now() - float(overdue_secs)
    with self._lock:
        rows = (
            self._c()
            .execute(
                f"SELECT * FROM tasks WHERE state NOT IN {_SQL_TERMINAL} "
                "AND lease_owner IS NULL AND next_run_at IS NOT NULL AND next_run_at<=? "
                "ORDER BY next_run_at, rowid",
                (cutoff,),
            )
            .fetchall()
        )
    return [TaskRecord.from_row(r) for r in rows]

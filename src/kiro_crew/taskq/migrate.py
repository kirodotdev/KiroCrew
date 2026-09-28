"""Schema versioning for ``tasks.db`` and the one-time import of older records.

Two jobs, both idempotent:

* :func:`apply_schema` brings a connection to ``SCHEMA_VERSION``: one
  ``CREATE ... IF NOT EXISTS`` set in the current shape, run on every open, and
  the version stamp in ``meta``. A store stamped NEWER than this build is
  refused. There is no older shape to step forward from: every build that has
  written ``tasks.db`` wrote this one, so the first upgrade step lands only
  when the shape changes.
* :func:`import_legacy` reads the stores that predate the task queue -- the
  subagent run folders and TaskRunner's ``runs.json`` -- and gives each still
  outstanding record a row, keyed on the id it already has. Those files stay
  where they are as artifact and evidence stores; ``result_ref`` points at them.
  Running the import twice inserts nothing the second time.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .model import (
    KIND_SUBAGENT,
    KIND_TASKRUNNER_STEP,
    RECOVERING,
    SIDE_EFFECT_UNKNOWN,
    TaskRecord,
)

logger = logging.getLogger(__name__)

#: The one shape any build has written. The first real upgrade bumps this and
#: adds its ``if current < N`` step to :func:`apply_schema`.
SCHEMA_VERSION = 1

#: Prefix that keeps a TaskRunner run's id from colliding with a subagent id in
#: the shared ``tasks`` table; the run's own id follows it.
TASKRUNNER_ID_PREFIX = "taskrunner:"

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS tasks (
      id TEXT PRIMARY KEY,
      parent_id TEXT,
      root_id TEXT NOT NULL,
      session_key TEXT NOT NULL,
      kind TEXT NOT NULL,
      harness TEXT NOT NULL,
      provider TEXT,
      params_json TEXT NOT NULL,
      workspace TEXT,
      scope_ref TEXT NOT NULL,
      state TEXT NOT NULL,
      attempts INTEGER NOT NULL DEFAULT 0,
      next_run_at REAL,
      lease_owner TEXT,
      lease_expires_at REAL,
      generation INTEGER NOT NULL DEFAULT 0,
      progress_json TEXT,
      result_ref TEXT,
      deadline_at REAL,
      idempotency_key TEXT,
      side_effect_class TEXT NOT NULL DEFAULT 'unknown',
      created_at REAL NOT NULL,
      updated_at REAL NOT NULL,
      wait_json TEXT,
      lane TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS tasks_dispatch ON tasks(state, next_run_at, root_id)",
    "CREATE INDEX IF NOT EXISTS tasks_kind_state ON tasks(kind, state, created_at)",
    "CREATE INDEX IF NOT EXISTS tasks_session ON tasks(session_key, state)",
    # UNIQUE only when present: SQLite treats NULLs as distinct in a unique
    # index, so rows without a caller-supplied key never collide.
    "CREATE UNIQUE INDEX IF NOT EXISTS tasks_idempotency ON tasks(idempotency_key)",
    """
    CREATE TABLE IF NOT EXISTS task_events (
      task_id TEXT NOT NULL,
      seq INTEGER NOT NULL,
      ts REAL NOT NULL,
      kind TEXT NOT NULL,
      data_json TEXT NOT NULL,
      PRIMARY KEY (task_id, seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meta (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    )
    """,
    # Wake-by-child lookup.
    "CREATE INDEX IF NOT EXISTS tasks_parent ON tasks(parent_id, state)",
    # Per-lane dispatch heads (fairness lanes).
    "CREATE INDEX IF NOT EXISTS tasks_lane ON tasks(lane, state, next_run_at, created_at)",
)


def read_schema_version(conn: sqlite3.Connection) -> int:
    """The schema version recorded in ``meta``; 0 for a database with none."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.OperationalError:
        return 0
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def apply_schema(conn: sqlite3.Connection) -> int:
    """Create the schema if absent and stamp it; returns the version in force.

    Idempotent on every open: each statement is ``IF NOT EXISTS``, so a healthy
    store does no work and a store missing an index regains it. Runs inside one
    transaction so a crash leaves either nothing or the whole shape. A database
    NEWER than this code understands is refused: writing rows an older writer
    does not know the columns of is how a downgrade corrupts a store.
    """
    current = read_schema_version(conn)
    if current > SCHEMA_VERSION:
        raise sqlite3.DatabaseError(
            f"tasks.db schema version {current} is newer than this build supports "
            f"({SCHEMA_VERSION}); refusing to write to it"
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in _SCHEMA:
            conn.execute(statement)
        # Upgrade steps (``if current < N: ...``) land here, in order, once the
        # shape changes; ``current`` is what an old store reports.
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return SCHEMA_VERSION


# ── legacy import ─────────────────────────────────────────────────────────────


@dataclass
class ImportReport:
    """What one :func:`import_legacy` pass did, for the log and the doctor."""

    subagents_imported: int = 0
    taskrunner_imported: int = 0
    skipped_existing: int = 0
    skipped_invalid: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def imported(self) -> int:
        return self.subagents_imported + self.taskrunner_imported


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def legacy_subagent_records(subagents_dir: Path, *, now: float) -> list[TaskRecord]:
    """Rows for every non-tombstoned subagent folder under *subagents_dir*.

    Mirrors ``subagent_persistence.list_orphans``: a folder with a tombstone
    already has a terminal record the orphan reconciler honours, so it is not
    a task the queue owes anything for. A folder whose ``state.json`` is
    unreadable is skipped -- the reconciler's own corrupt-state handling covers
    it and inventing a row from nothing would only mislabel it.
    """
    records: list[TaskRecord] = []
    try:
        entries = sorted(p for p in subagents_dir.iterdir() if p.is_dir())
    except (FileNotFoundError, NotADirectoryError, OSError):
        return records
    for folder in entries:
        if (folder / "tombstone.json").exists():
            continue
        state = _read_json_object(folder / "state.json")
        if state is None:
            continue
        agent_id = str(state.get("id") or folder.name)
        if agent_id != folder.name:
            continue  # a state file that names another run is not this run's
        parent_session = str(state.get("parent_session") or "")
        params: dict[str, Any] = {
            "task": str(state.get("task") or ""),
            "parent_session_key": parent_session,
            "agent": str(state.get("agent") or ""),
            "max_turns": int(state.get("max_turns") or 0),
            "memory_store": str(state.get("memory_store") or ""),
            "_preassigned_id": agent_id,
            "_legacy_import": True,
        }
        started = state.get("started")
        created = float(started) if isinstance(started, (int, float)) else now
        records.append(
            TaskRecord(
                id=agent_id,
                kind=KIND_SUBAGENT,
                session_key=parent_session,
                harness=str(state.get("provider") or ""),
                params=params,
                scope_ref={"memory_store": str(params["memory_store"])},
                state=RECOVERING,
                attempts=1,
                result_ref=str(folder),
                side_effect_class=SIDE_EFFECT_UNKNOWN,
                created_at=created,
                updated_at=now,
            )
        )
    return records


def legacy_taskrunner_records(runs_path: Path, *, now: float) -> list[TaskRecord]:
    """Rows for every ``paused`` run in TaskRunner's ``runs.json``.

    ``paused`` is the status TaskRunner assigns to a run interrupted by a
    gateway crash, so it is the only status that names work the queue owes a
    continuation for. Planned, completed, failed and cancelled runs are left to
    TaskRunner's own registry. A run's transient ``auto_approve`` is never
    carried: unattended recovery must not restore a persisted bypass.
    """
    records: list[TaskRecord] = []
    try:
        items = json.loads(runs_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return records
    if not isinstance(items, list):
        return records
    for item in items:
        if not isinstance(item, dict) or item.get("status") != "paused":
            continue
        run_id = str(item.get("task_id") or "")
        if not run_id:
            continue
        params = {
            "task_id": run_id,
            "name": str(item.get("name") or ""),
            "spec_path": str(item.get("spec_path") or ""),
            "_legacy_import": True,
        }
        records.append(
            TaskRecord(
                id=f"{TASKRUNNER_ID_PREFIX}{run_id}",
                kind=KIND_TASKRUNNER_STEP,
                session_key=str(item.get("session_key") or ""),
                params=params,
                scope_ref={"auto_approve": False},
                state=RECOVERING,
                attempts=1,
                result_ref=str(runs_path),
                side_effect_class=SIDE_EFFECT_UNKNOWN,
                created_at=now,
                updated_at=now,
            )
        )
    return records


def import_legacy(
    insert_if_absent: Callable[[TaskRecord], bool],
    *,
    subagents_dir: Path | None,
    taskrunner_runs_path: Path | None,
    now: float | None = None,
) -> ImportReport:
    """Give every outstanding legacy record a row; skip the ones that have one.

    *insert_if_absent* is the store's keyed insert (``TaskStore.insert_if_absent``);
    taking it as a callable keeps this module free of a connection of its own
    and lets the import run before the dispatcher starts, on the store's
    single writer.
    """
    ts = time.time() if now is None else now
    report = ImportReport()
    records: list[tuple[str, TaskRecord]] = []
    if subagents_dir is not None:
        records.extend(("subagent", r) for r in legacy_subagent_records(subagents_dir, now=ts))
    if taskrunner_runs_path is not None:
        records.extend(
            ("taskrunner", r) for r in legacy_taskrunner_records(taskrunner_runs_path, now=ts)
        )
    for source, record in records:
        try:
            inserted = insert_if_absent(record)
        except Exception as exc:  # noqa: BLE001 - one bad row must not abort the import
            report.errors.append(f"{record.id}: {exc}")
            continue
        if not inserted:
            report.skipped_existing += 1
        elif source == "subagent":
            report.subagents_imported += 1
        else:
            report.taskrunner_imported += 1
    if report.imported or report.errors:
        logger.info(
            "taskq import: %d subagent, %d taskrunner rows imported; %d already present; %d errors",
            report.subagents_imported,
            report.taskrunner_imported,
            report.skipped_existing,
            len(report.errors),
        )
    return report

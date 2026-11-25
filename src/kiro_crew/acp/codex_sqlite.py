"""Seed an isolated Codex state database without rebuilding rollout history."""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import time
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Mapping

logger = logging.getLogger(__name__)

SQLITE_HOME_ENV = "CODEX_SQLITE_HOME"
_STATE_DATABASE = "state_5.sqlite"
_BACKUP_TIMEOUT_SECS = 10.0
_BACKUP_PAGES = 256
_BACKUP_RETRY_SECS = 0.05
_BACKFILL_STATUS = "SELECT status FROM backfill_state WHERE id = 1"


def _copy_refused() -> type[Exception]:
    """The snapshot code's error for an immutable copy it refuses; looked up only
    when an exception is being matched, so a seed that never falls back never
    imports the snapshot package."""
    from kiro_crew import snapshot

    return snapshot.DatabaseCopyFailed


def _open_source(source: Path) -> tuple[sqlite3.Connection, Exception | None]:
    """A read-only connection to *source*, and the refusal an ``immutable`` open
    stands in for, else ``None``.

    A plain read-only open of a WAL database must create its ``-shm`` file, so a
    Codex home the gateway may read but not write refuses it whenever no other
    connection holds the database open. Whether an immutable read may stand in is
    the snapshot code's decision (``snapshot._immutable_read_may_stand_in``): the
    refusal's own result code, and no ``-wal`` or ``-journal`` beside the file.
    """
    uri = source.as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as probe:
            probe.execute("PRAGMA schema_version").fetchone()
        return sqlite3.connect(uri, uri=True, timeout=1), None
    except sqlite3.DatabaseError as refused:
        from kiro_crew import snapshot

        if not snapshot._immutable_read_may_stand_in(source, refused):
            raise
        return sqlite3.connect(uri + "&immutable=1", uri=True, timeout=1), refused


def seed_private_state(env: Mapping[str, str], scratch: Path) -> None:
    """Copy a completed index into this runtime's private directory.

    SQLite's online backup includes committed WAL data and leaves the source
    database unchanged. Each runtime receives an independent database; only the
    state index is copied, never credentials or the logging database. An operator
    override or an existing destination is left alone.

    Called on the spawn worker before the child exists. A missing, incomplete or
    unavailable source falls back to Codex's ordinary cold initialization.
    """
    if env.get(SQLITE_HOME_ENV):
        return
    destination = scratch / _STATE_DATABASE
    if destination.exists():
        return
    codex_home = Path(env.get("CODEX_HOME") or str(Path.home() / ".codex"))
    source = codex_home / _STATE_DATABASE
    if not source.is_file():
        return

    temporary: Path | None = None
    deadline = time.monotonic() + _BACKUP_TIMEOUT_SECS

    def check_budget(status: int, remaining: int, total: int) -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("Codex state snapshot exceeded its preparation budget")

    try:
        src_conn, immutable_for = _open_source(source)
        with closing(src_conn) as src, ExitStack() as reader_lock:
            # An immutable read takes no lock, so the reader lock the snapshot code
            # holds for it is taken before the first read and kept, with this
            # connection open, until the changed-during-copy check has run.
            before: os.stat_result | None = None
            if immutable_for is not None:
                from kiro_crew import snapshot

                before = reader_lock.enter_context(
                    snapshot._hold_reader_lock(source, immutable_for)
                )
            row = src.execute(_BACKFILL_STATUS).fetchone()
            if row != ("complete",):
                logger.warning(
                    "Codex SQLite: source index is incomplete; using cold initialization"
                )
                return
            with tempfile.NamedTemporaryFile(
                dir=scratch, prefix="codex-state-", suffix=".seed", delete=False
            ) as staging:
                temporary = Path(staging.name)
            with closing(sqlite3.connect(temporary)) as dst:
                src.backup(
                    dst,
                    pages=_BACKUP_PAGES,
                    progress=check_budget,
                    sleep=_BACKUP_RETRY_SECS,
                )
                if dst.execute(_BACKFILL_STATUS).fetchone() != ("complete",):
                    raise ValueError("Codex source index changed to an incomplete backfill")
                thread_count = dst.execute("SELECT count(*) FROM threads").fetchone()[0]
            if before is not None:
                from kiro_crew import snapshot

                snapshot._refuse_if_changed_since(source, before, temporary)
            temporary.replace(destination)
            temporary = None
            logger.info(
                "Codex SQLite: seeded isolated runtime index from completed snapshot (%d threads)",
                thread_count,
            )
    except (OSError, sqlite3.Error, TimeoutError, ValueError, _copy_refused()) as exc:
        logger.warning("Codex SQLite: snapshot unavailable; using cold initialization: %s", exc)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

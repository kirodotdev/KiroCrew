"""Seed an isolated Codex state database without rebuilding rollout history."""

from __future__ import annotations

import logging
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Mapping

logger = logging.getLogger(__name__)

SQLITE_HOME_ENV = "CODEX_SQLITE_HOME"
_STATE_DATABASE = "state_5.sqlite"
_BACKUP_TIMEOUT_SECS = 10.0
_BACKUP_PAGES = 256
_BACKUP_RETRY_SECS = 0.05


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
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=1)) as src:
            row = src.execute("SELECT status FROM backfill_state WHERE id = 1").fetchone()
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
                if dst.execute("SELECT status FROM backfill_state WHERE id = 1").fetchone() != (
                    "complete",
                ):
                    raise ValueError("Codex source index changed to an incomplete backfill")
                thread_count = dst.execute("SELECT count(*) FROM threads").fetchone()[0]
            temporary.replace(destination)
            temporary = None
            logger.info(
                "Codex SQLite: seeded isolated runtime index from completed snapshot (%d threads)",
                thread_count,
            )
    except (OSError, sqlite3.Error, TimeoutError, ValueError) as exc:
        logger.warning("Codex SQLite: snapshot unavailable; using cold initialization: %s", exc)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

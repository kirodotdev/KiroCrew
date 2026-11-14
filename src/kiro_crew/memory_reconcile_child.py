"""Run the FULL memory metadata reconcile pass in a child process.

The full pass reads every row and writes the ones whose content changed. Run in the
gateway process it competes for the GIL with every other Python thread, so a pass
that takes about 3 s of CPU alone took minutes of wall time beside one busy thread
(issue #36596). A child process has its own GIL. The marker-based incremental check
stays in the parent: it is cheap and needs no child.

This module is the spawn target, so it imports nothing heavy at module level.
"""

from __future__ import annotations

import logging
import multiprocessing
from typing import Any

from kiro_crew._sqlite_compat import sqlite3

logger = logging.getLogger(__name__)

#: How long the parent waits for the child. The full pass takes seconds alone, so
#: this is ~200x the measured cost on a 478 MB store; a longer wait is a stuck child.
CHILD_TIMEOUT_SECS = 600.0
_BUSY_TIMEOUT_MS = 5000
_TERMINATE_GRACE_SECS = 5.0


class _Collect(logging.Handler):
    """Keeps the pass's own warnings so the parent can log them where its handlers are."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def run_child(db_path: str, conn: Any = None) -> None:
    """Spawn target: the same ``with db: reconcile(db, full=True)`` the parent would run.

    A busy write lock is not an error here: ``reconcile`` returns without a marker
    and the child exits 0, which is the in-process busy path. Any exception exits
    non-zero and the parent falls back to the in-process pass.
    """
    from kiro_crew import memory_record_metadata, sqlite_wal

    collected = _Collect()
    meta_logger = logging.getLogger(memory_record_metadata.__name__)
    meta_logger.addHandler(collected)
    db = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_MS / 1000)
    try:
        # The parent keeps this -wal open: this close must not delete it.
        sqlite_wal.keep_wal_on_close(db)
        with db:
            memory_record_metadata.reconcile(db, full=True)
    finally:
        sqlite_wal.close_keeping_wal(db)
        meta_logger.removeHandler(collected)
        if conn is not None:
            conn.send(collected.messages)


def run_full_pass_in_child(db_path: str, *, timeout: float = CHILD_TIMEOUT_SECS) -> bool:
    """Run the full pass in a spawned child; True when it finished (marker written or lock busy).

    False means the caller must run the pass in process; one WARNING says why. The
    parent holds no transaction on the file while this runs.
    """
    reason = ""
    proc = None
    try:
        ctx = multiprocessing.get_context("spawn")
        receiver, sender = ctx.Pipe(duplex=False)
        proc = ctx.Process(
            target=run_child, args=(db_path, sender), name="kirocrew-memory-reconcile", daemon=True
        )
        proc.start()
        sender.close()
        proc.join(timeout)  # blocks in the OS wait, not in a Python loop
        if proc.is_alive():
            reason = f"timed out after {timeout:.0f}s"
            proc.terminate()
            proc.join(_TERMINATE_GRACE_SECS)
            if proc.is_alive():
                proc.kill()
                proc.join()
        elif proc.exitcode != 0:
            reason = f"exited with code {proc.exitcode}"
        elif receiver.poll():
            for message in receiver.recv():  # the pass's own warnings, as in process
                logging.getLogger("kiro_crew.memory_record_metadata").warning("%s", message)
    except Exception as exc:  # noqa: BLE001 - no child is a degraded mode, not a crash
        reason = f"could not start ({type(exc).__name__}: {exc})"
    if reason:
        logger.warning("Memory reconcile child %s; running the pass in process", reason)
        return False
    return True

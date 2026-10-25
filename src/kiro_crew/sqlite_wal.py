"""Switch a SQLite connection into WAL mode without losing a race to another opener.

``PRAGMA journal_mode=WAL`` on a file still in a rollback journal mode needs an
exclusive lock, taken by upgrading the read lock the pragma already holds. SQLite
does not call the busy handler for that upgrade, so ``busy_timeout`` does not cover
it: when another connection holds any lock at that moment -- typically a second
opener of the same new file running the same pragma -- the switch fails at once with
"database is locked". A file already in WAL needs no lock to report its mode.

``enable_wal`` reads the mode first and returns when it is already ``wal``; otherwise
it retries the switch while the failure is a lock conflict, until its deadline.
"""

from __future__ import annotations

import sqlite3 as _stdlib_sqlite3
import time
from typing import Any, Callable

from kiro_crew._sqlite_compat import sqlite3

#: Both libraries a connection here may come from: the stores use the compat
#: module (pysqlite3 where installed), some app stores the stdlib one.
_OPERATIONAL_ERRORS = (sqlite3.OperationalError, _stdlib_sqlite3.OperationalError)

#: First wait after a refused switch; each later wait doubles, up to the cap.
_FIRST_WAIT_SECS = 0.005
_MAX_WAIT_SECS = 0.1


def enable_wal(
    conn: Any,
    *,
    timeout_secs: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Put *conn*'s database in WAL mode and return the mode SQLite reports.

    Returns without taking a lock when the file is already in WAL. A lock conflict
    on the switch is retried until *timeout_secs* have passed, then re-raised; any
    other error is raised at once. The returned mode is what SQLite kept, which is
    not ``wal`` where WAL is unavailable (an in-memory database); callers that
    ignored the pragma's result before may ignore it here too.
    """
    row = conn.execute("PRAGMA journal_mode").fetchone()
    if row is not None and str(row[0]).lower() == "wal":
        return "wal"
    deadline = clock() + timeout_secs
    wait = _FIRST_WAIT_SECS
    while True:
        try:
            row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        except _OPERATIONAL_ERRORS as exc:
            # SQLITE_BUSY reads "database is locked"; SQLITE_LOCKED reads
            # "database table is locked" or "database schema is locked".
            remaining = deadline - clock()
            if "locked" not in str(exc).lower() or remaining <= 0:
                raise
            sleep(min(wait, remaining))
            wait = min(wait * 2, _MAX_WAIT_SECS)
            continue
        return str(row[0]).lower() if row is not None else "wal"

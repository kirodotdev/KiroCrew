"""Switch a SQLite connection into WAL mode without losing a race to another opener.

``PRAGMA journal_mode=WAL`` on a file still in a rollback journal mode needs an
exclusive lock, taken by upgrading the read lock the pragma already holds. SQLite
does not call the busy handler for that upgrade, so ``busy_timeout`` does not cover
it: when another connection holds any lock at that moment -- typically a second
opener of the same new file running the same pragma -- the switch fails at once with
"database is locked". A file already in WAL needs no lock to report its mode.

``enable_wal`` reads the mode first and returns when it is already ``wal``; otherwise
it retries the switch while the failure is a lock conflict, until its deadline.

``keep_wal_on_close`` and ``truncate_wal_before_close`` stop a closing connection
from deleting a ``-wal`` that another process may still be writing through.
"""

from __future__ import annotations

import logging
import sqlite3 as _stdlib_sqlite3
import time
from typing import Any, Callable

from kiro_crew._sqlite_compat import sqlite3

#: Both libraries a connection here may come from: the stores use the compat
#: module (pysqlite3 where installed), some app stores the stdlib one.
_OPERATIONAL_ERRORS = (sqlite3.OperationalError, _stdlib_sqlite3.OperationalError)

logger = logging.getLogger(__name__)

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


def keep_wal_on_close(conn: Any) -> bool:
    """Stop *conn*'s close from checkpointing and deleting the ``-wal`` and ``-shm``.

    When the last connection a process holds on a WAL database closes, SQLite
    tries for an exclusive lock on the file. If it gets one, it checkpoints and
    deletes ``-wal`` and ``-shm``, on the belief that no other process uses them.
    That belief rests on POSIX record locks, and a process loses all of its locks
    on a file when it closes ANY descriptor of that file
    (https://www.sqlite.org/howtocorrupt.html, section 2.2). So a process that has
    lost its locks can still be writing through a ``-wal`` that another process's
    close just deleted. Its later commits land in an unlinked file that no other
    process can see, and the database splits.

    ``SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE`` makes the close skip that step, so this
    connection never deletes the files another process may be writing through.
    Committed data is unaffected: the next opener reads the ``-wal`` as usual.
    Callers that want the ``-wal`` emptied on close run
    :func:`truncate_wal_before_close` first.

    Returns whether the option is set. A driver without ``Connection.setconfig``
    (``pysqlite3``, Python before 3.12) keeps SQLite's default and returns False.
    """
    setconfig = getattr(conn, "setconfig", None)
    option = getattr(_stdlib_sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE", None)
    if setconfig is None or option is None:
        return False
    try:
        setconfig(option, True)
    except (*_OPERATIONAL_ERRORS, sqlite3.ProgrammingError, _stdlib_sqlite3.ProgrammingError):
        return False
    return True


def truncate_wal_before_close(conn: Any) -> None:
    """Checkpoint *conn*'s database and empty its ``-wal`` in place. Never raises.

    For a connection set up by :func:`keep_wal_on_close`, whose own close does not
    checkpoint. A ``TRUNCATE`` checkpoint copies every committed page into the
    database and cuts the ``-wal`` to zero bytes without deleting it. It is
    coordinated through the ``-shm`` locks, so it is safe while another process
    has the database open: when a reader or writer is active it waits up to the
    connection's busy timeout and then gives up, leaving the ``-wal`` for whichever
    process closes last. An empty ``-wal`` carries nothing an opener could replay.
    """
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    except Exception:  # best effort: the committed data is already safe in the -wal
        logger.debug("WAL checkpoint before close did not run", exc_info=True)

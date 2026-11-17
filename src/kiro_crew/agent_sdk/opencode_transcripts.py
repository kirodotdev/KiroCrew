"""Delete OpenCode's own record of one session.

OpenCode keeps every session in one SQLite file, ``opencode.db`` under its data
directory (``$XDG_DATA_HOME/opencode``, else ``~/.local/share/opencode``, on every
platform). ``OPENCODE_DB`` moves it: an absolute path is used as is, ``:memory:``
keeps nothing on disk, and any other value names a file in the data directory.
Measured on opencode 1.19.1: an incognito chat left its ``session`` row, its
``message`` and ``part`` rows (the prompt and the answer) and its ``event`` rows
(the same text again, as the session's event log) behind after the process ended.
OpenCode's ACP adapter advertises no delete verb, only ``session/close``, which
keeps the record.

So a disposable session (incognito or temporary) deletes its own rows after its
process has ended: every row keyed by the session's id, in each table that has a
``session_id`` column, its event log (``event`` and ``event_sequence``, keyed by
``aggregate_id``) and its ``session`` row, and the same for any child session
OpenCode created under it (``parent_id``, a ``task`` subagent). Nothing else is
touched: no other session, no project, account or credential row. The delete runs
with ``secure_delete`` so the freed pages are zeroed, then checkpoints the
write-ahead log so the old pages leave it too, and closes without deleting a
``-wal`` another OpenCode process may still write through (``kiro_crew.sqlite_wal``).

The file is opened read-write without create, so a missing store stays missing. A
link or anything other than a regular file at the store's name deletes nothing.

shortcut: a dev build of OpenCode names its store ``opencode-<channel>.db``; only
the released ``opencode.db`` name is read. Upgrade when Crew launches a dev channel.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import stat
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.sqlite_wal import close_keeping_wal, keep_wal_on_close, truncate_wal_before_close

logger = logging.getLogger(__name__)

#: An OpenCode session id as it hands them out: ``ses_`` and base-62 characters.
_SESSION_ID_RE = re.compile(r"ses_[0-9A-Za-z]{1,64}")
#: A table name this module will put in a statement; anything else is skipped.
_TABLE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DB_ENV = "OPENCODE_DB"
_DATA_HOME_ENV = "XDG_DATA_HOME"
_HOME_ENV = "HOME"
_IN_MEMORY = ":memory:"
_DB_NAME = "opencode.db"
#: How long to wait for another OpenCode process to release its write lock.
_BUSY_TIMEOUT_S = 5.0


def _env(name: str, overrides: Mapping[str, str]) -> str | None:
    """*name* from the session's ``extra_env``, else the gateway's environment."""
    if name in overrides:
        return overrides[name]
    return os.environ.get(name)


def opencode_db_path(env_overrides: Mapping[str, str] | None = None) -> Path | str | None:
    """The store a Crew OpenCode session writes, ``":memory:"``, or ``None``.

    *env_overrides* is the session's ``extra_env``, merged over the gateway's
    environment at spawn, so a name in it wins. ``None`` when no home resolves or a
    directory is relative: OpenCode resolves that from its own working directory,
    which this side cannot name.
    """
    overrides = env_overrides if isinstance(env_overrides, Mapping) else {}
    db = _env(_DB_ENV, overrides)
    if db == _IN_MEMORY:
        return _IN_MEMORY
    if db and Path(db).is_absolute():
        return Path(db)
    data_home = _env(_DATA_HOME_ENV, overrides)
    if data_home:
        base = Path(data_home)
        if not base.is_absolute():
            return None
    elif _HOME_ENV in overrides and not platform_compat.IS_WINDOWS:
        home = Path(overrides[_HOME_ENV])
        if not home.is_absolute():
            return None
        base = home / ".local" / "share"
    else:
        try:
            base = Path.home() / ".local" / "share"
        except (RuntimeError, KeyError):
            return None
    return base / "opencode" / (db or _DB_NAME)


def _session_tables(conn: sqlite3.Connection) -> list[str]:
    """Every table in the store that has a ``session_id`` column."""
    names = [
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
        if isinstance(row[0], str) and _TABLE_NAME_RE.fullmatch(row[0])
    ]
    return [
        name
        for name in names
        if any(col[1] == "session_id" for col in conn.execute(f'PRAGMA table_info("{name}")'))
    ]


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _session_tree(conn: sqlite3.Connection, session_id: str) -> list[str]:
    """*session_id* and every session OpenCode created under it."""
    if not _has_table(conn, "session"):
        return [session_id]
    rows = conn.execute(
        "WITH RECURSIVE tree(id) AS ("
        " SELECT ?"
        " UNION SELECT session.id FROM session JOIN tree ON session.parent_id = tree.id"
        ") SELECT id FROM tree",
        (session_id,),
    ).fetchall()
    return [row[0] for row in rows]


def _keyed_tables(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(table, id column) for every place a session's rows live."""
    keyed = [(name, "session_id") for name in _session_tables(conn)]
    for name in ("event", "event_sequence"):
        if _has_table(conn, name):
            keyed.append((name, "aggregate_id"))
    if _has_table(conn, "session"):
        keyed.append(("session", "id"))
    return keyed


def _count_left(conn: sqlite3.Connection, keyed: list[tuple[str, str]], ids: list[str]) -> int:
    marks = ",".join("?" * len(ids))
    return sum(
        conn.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" IN ({marks})', ids
        ).fetchone()[0]
        for table, column in keyed
    )


def _delete_rows(conn: sqlite3.Connection, session_id: str) -> bool:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA secure_delete = ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        ids = _session_tree(conn, session_id)
        keyed = _keyed_tables(conn)
        marks = ",".join("?" * len(ids))
        for table, column in keyed:
            conn.execute(f'DELETE FROM "{table}" WHERE "{column}" IN ({marks})', ids)
        left = _count_left(conn, keyed, ids)
        conn.execute("COMMIT")
    except BaseException:
        with suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise
    return left == 0


def delete_opencode_session_rows(
    session_id: str, env_overrides: Mapping[str, str] | None = None
) -> bool:
    """Delete OpenCode's rows for *session_id* and its child sessions.

    Returns whether the cleanup completed: ``True`` when no row for the session is
    left (also when the store or the session never existed, or the store is in
    memory), ``False`` when the id is not an OpenCode id, no store resolves, the
    store is a link or not a regular file, or the store could not be changed.
    """
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        return False
    target = opencode_db_path(env_overrides)
    if target == _IN_MEMORY:
        return True
    if not isinstance(target, Path):
        return False
    try:
        mode = os.lstat(target).st_mode
    except FileNotFoundError:
        return True
    except OSError as exc:
        logger.warning("opencode session cleanup: cannot inspect the store (%s)", exc.strerror)
        return False
    if not stat.S_ISREG(mode):
        logger.error("opencode session cleanup: refusing a store that is not a regular file")
        return False
    try:
        conn = sqlite3.connect(
            f"{target.as_uri()}?mode=rw", uri=True, timeout=_BUSY_TIMEOUT_S, isolation_level=None
        )
    except sqlite3.Error as exc:
        logger.warning("opencode session cleanup: cannot open the store (%s)", type(exc).__name__)
        return False
    # Another OpenCode process may have the store open: this close must not delete
    # the -wal it writes through, and the checkpoint takes the old pages out of it.
    kept = keep_wal_on_close(conn)
    try:
        done = _delete_rows(conn, session_id)
        truncate_wal_before_close(conn)
        return done
    except sqlite3.Error as exc:
        # Logged by type only: the message can carry the path, which names the user.
        logger.warning(
            "opencode session cleanup: the store refused the delete (%s)", type(exc).__name__
        )
        return False
    finally:
        if kept:
            conn.close()
        else:
            close_keeping_wal(conn)


__all__ = ["delete_opencode_session_rows", "opencode_db_path"]

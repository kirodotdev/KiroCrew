"""The change log a memory store keeps for its index: one row per changed memory.

SQLite triggers write it, so every writer feeds it: this process, another
gateway process, the CLI, a cron, an older Kiro Crew build that knows nothing
about the index. A row names the memory by its natural id (a fact's or lesson's
key, an episode's id), never by rowid, which VACUUM may renumber, and never by
value, so the log copies no memory content.

The log's AUTOINCREMENT counter (``sqlite_sequence``) is the store's change
counter. It only grows, survives a trim, and costs one keyed read, so an index
or a cache compares it to the counter it last saw to learn whether anything
changed, in any process.

Both lineages log into the same idents: ``("semantic", key)`` for facts,
preferences and lessons, and ``("episodic", id)`` for episodes. On the crew
lineage the triggers sit on ``memory_items`` and pick the relation by ``kind``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Final

from kiro_crew import memory_schema
from kiro_crew._sqlite_compat import sqlite3

logger = logging.getLogger(__name__)

LOG_TABLE: Final = "memory_index_log"
#: One row: the random id of this log's creation (see :func:`log_id`).
LOG_ID_TABLE: Final = "memory_index_log_id"
_TRIGGER_PREFIX: Final = "memory_index_log_"

#: The hard bound on the log, in seqs from its oldest row. Twice the most an index
#: catches up in line (``store_index.CATCH_UP_MAX``), about 160 KB of rows. Every
#: writer appends through the triggers, so every writer enforces it: whoever owns
#: the index, and whatever state it is in, nothing grows the log past this.
MAX_ROWS: Final = 4096
#: The one row left when the bound empties the log. Its seq sits past every row it
#: dropped, so an index that had not applied them finds a gap and rebuilds.
OVERFLOW_REL: Final = "overflow"

_LOG_DDL: Final = (
    f"CREATE TABLE IF NOT EXISTS {LOG_TABLE} ("
    "seq INTEGER PRIMARY KEY AUTOINCREMENT, rel TEXT NOT NULL, ident TEXT NOT NULL)"
)

# An identity (subject, predicate, scope) feeds a fact's search text, and lives in
# memory_record_meta; its record_id is "key:<fact key>".
_IDENTITY_LOG: Final = (
    f"INSERT INTO {LOG_TABLE} (rel, ident) SELECT 'semantic', substr(NEW.record_id, 5) "
    "WHERE NEW.record_id LIKE 'key:%' AND NEW.kind IN ('fact', 'directive');"
)


def _log(rel_sql: str, ident_sql: str) -> str:
    return (
        f"INSERT INTO {LOG_TABLE} (rel, ident) SELECT {rel_sql}, {ident_sql} "
        f"WHERE {ident_sql} IS NOT NULL;"
    )


def _v1_triggers() -> dict[str, str]:
    triggers: dict[str, str] = {}
    for rel, table, pk, columns in (
        ("semantic", "semantic_memory", "key", "key, value_json, embedding, is_deleted"),
        ("episodic", "episodic_memories", "id", "id, text, embedding, is_deleted"),
    ):
        new, old = _log(f"'{rel}'", f"NEW.{pk}"), _log(f"'{rel}'", f"OLD.{pk}")
        triggers[f"{_TRIGGER_PREFIX}{rel}_ins"] = (
            f"CREATE TRIGGER {_TRIGGER_PREFIX}{rel}_ins AFTER INSERT ON {table} BEGIN {new} END"
        )
        triggers[f"{_TRIGGER_PREFIX}{rel}_upd"] = (
            f"CREATE TRIGGER {_TRIGGER_PREFIX}{rel}_upd AFTER UPDATE OF {columns} ON {table} "
            f"BEGIN {new} {old} END"
        )
        triggers[f"{_TRIGGER_PREFIX}{rel}_del"] = (
            f"CREATE TRIGGER {_TRIGGER_PREFIX}{rel}_del AFTER DELETE ON {table} BEGIN {old} END"
        )
    return triggers


def _crew_triggers() -> dict[str, str]:
    episode = memory_schema.KIND_EPISODE

    def logged(row: str) -> str:
        return _log(
            f"CASE WHEN {row}.kind = '{episode}' THEN 'episodic' ELSE 'semantic' END",
            f"CASE WHEN {row}.kind = '{episode}' THEN {row}.id ELSE {row}.key END",
        )

    columns = "kind, key, text, value_json, embedding, is_deleted"
    return {
        f"{_TRIGGER_PREFIX}items_ins": (
            f"CREATE TRIGGER {_TRIGGER_PREFIX}items_ins AFTER INSERT ON memory_items "
            f"BEGIN {logged('NEW')} END"
        ),
        f"{_TRIGGER_PREFIX}items_upd": (
            f"CREATE TRIGGER {_TRIGGER_PREFIX}items_upd AFTER UPDATE OF {columns} ON memory_items "
            f"BEGIN {logged('NEW')} {logged('OLD')} END"
        ),
        f"{_TRIGGER_PREFIX}items_del": (
            f"CREATE TRIGGER {_TRIGGER_PREFIX}items_del AFTER DELETE ON memory_items "
            f"BEGIN {logged('OLD')} END"
        ),
    }


def triggers(lineage: str) -> dict[str, str]:
    """Every trigger the log needs on a file of *lineage*, by name."""
    found = _crew_triggers() if lineage == memory_schema.LINEAGE_CREW else _v1_triggers()
    found[f"{_TRIGGER_PREFIX}identity_ins"] = (
        f"CREATE TRIGGER {_TRIGGER_PREFIX}identity_ins AFTER INSERT ON memory_record_meta "
        f"BEGIN {_IDENTITY_LOG} END"
    )
    found[f"{_TRIGGER_PREFIX}identity_upd"] = (
        f"CREATE TRIGGER {_TRIGGER_PREFIX}identity_upd AFTER UPDATE OF subject, predicate, scope "
        f"ON memory_record_meta BEGIN {_IDENTITY_LOG} END"
    )
    # The bound. A trigger's own insert does not fire triggers (recursive_triggers
    # is off), so the overflow row below does not re-enter it.
    found[f"{_TRIGGER_PREFIX}bound"] = (
        f"CREATE TRIGGER {_TRIGGER_PREFIX}bound AFTER INSERT ON {LOG_TABLE} "
        f"WHEN NEW.seq - (SELECT min(seq) FROM {LOG_TABLE}) >= {MAX_ROWS} BEGIN "
        f"DELETE FROM {LOG_TABLE}; "
        f"INSERT INTO {LOG_TABLE} (rel, ident) VALUES ('{OVERFLOW_REL}', ''); END"
    )
    return found


def _installed(db: sqlite3.Connection) -> dict[str, str]:
    return dict(
        db.execute(
            "SELECT name, sql FROM sqlite_schema WHERE type = 'trigger' AND name LIKE ?",
            (_TRIGGER_PREFIX + "%",),
        )
    )


def is_installed(db: sqlite3.Connection, lineage: str) -> bool:
    """Whether the log table and exactly this build's triggers exist. Reads only."""
    for table in (LOG_TABLE, LOG_ID_TABLE):
        if not db.execute("SELECT 1 FROM sqlite_schema WHERE name = ?", (table,)).fetchone():
            return False
    return _installed(db) == triggers(lineage)


def is_present(db: sqlite3.Connection) -> bool:
    """Whether any part of the log is on the file. Reads only."""
    return bool(
        db.execute(
            "SELECT 1 FROM sqlite_schema WHERE name IN (?, ?)", (LOG_TABLE, LOG_ID_TABLE)
        ).fetchone()
        or _installed(db)
    )


def install(db: sqlite3.Connection, lineage: str) -> None:
    """Create the log and (re)create its triggers. Runs inside the caller's
    transaction and never commits."""
    db.execute(_LOG_DDL)
    db.execute(f"CREATE TABLE IF NOT EXISTS {LOG_ID_TABLE} (id TEXT NOT NULL)")
    if db.execute(f"SELECT 1 FROM {LOG_ID_TABLE}").fetchone() is None:
        db.execute(f"INSERT INTO {LOG_ID_TABLE} (id) VALUES (?)", (uuid.uuid4().hex,))
    wanted = triggers(lineage)
    for name in _installed(db):
        if name not in wanted:
            db.execute(f"DROP TRIGGER IF EXISTS {name}")
    for name, sql in wanted.items():
        db.execute(f"DROP TRIGGER IF EXISTS {name}")
        db.execute(sql)


def strip(db: sqlite3.Connection) -> None:
    """Remove the triggers and the log, inside the caller's transaction.

    For a store whose index is turned off (nothing would trim the log), and for
    a copy that leaves the host: the snapshot redactor rewrites keys in place,
    which would otherwise copy each original key into the log.
    """
    for name in _installed(db):
        db.execute(f"DROP TRIGGER IF EXISTS {name}")
    db.execute(f"DROP TABLE IF EXISTS {LOG_TABLE}")
    db.execute(f"DROP TABLE IF EXISTS {LOG_ID_TABLE}")


def strip_now(db: sqlite3.Connection) -> bool:
    """Strip the log in a transaction of its own, for a store whose index stopped
    (mode off, or a backend that cannot open): nothing would trim it any more.

    Skipped inside a caller's open transaction. Returns whether no log is left;
    a busy store keeps it for the next try.
    """
    if not is_present(db):
        return True
    if db.in_transaction:
        return False
    try:
        db.execute("BEGIN IMMEDIATE")
        try:
            strip(db)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    except sqlite3.Error:
        logger.warning("memory index: could not strip the change log; will retry")
        return False
    return True


def log_id(db: sqlite3.Connection) -> str:
    """The random id written when the log was created, or "" with no log.

    A log stripped and created again (in any process) gets a new id and a counter
    that starts over, so an index built against the old log can tell.
    """
    try:
        row = db.execute(f"SELECT id FROM {LOG_ID_TABLE} LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return ""
    return str(row[0]) if row else ""


def current_seq(db: sqlite3.Connection) -> int:
    """The store's change counter: the highest seq ever logged, 0 with no log."""
    try:
        row = db.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (LOG_TABLE,)).fetchone()
    except sqlite3.OperationalError:  # no AUTOINCREMENT table on the file yet
        return 0
    return int(row[0]) if row else 0


def oldest_seq(db: sqlite3.Connection) -> int:
    """The lowest seq still in the log, or 0 when it is empty or absent."""
    try:
        row = db.execute(f"SELECT min(seq) FROM {LOG_TABLE}").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row and row[0] is not None else 0


def changes_after(db: sqlite3.Connection, seq: int, limit: int) -> list[tuple[int, str, str]]:
    """Up to *limit* ``(seq, rel, ident)`` rows logged after *seq*, oldest first."""
    return [
        (int(s), str(rel), str(ident))
        for s, rel, ident in db.execute(
            f"SELECT seq, rel, ident FROM {LOG_TABLE} WHERE seq > ? ORDER BY seq LIMIT ?",
            (seq, limit),
        )
    ]


def trim(db: sqlite3.Connection, upto: int) -> None:
    """Drop rows at or below *upto*, inside the caller's transaction."""
    db.execute(f"DELETE FROM {LOG_TABLE} WHERE seq <= ?", (upto,))

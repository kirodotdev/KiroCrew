"""Add back the rows a member store lacks from an older copy of the same store.

A whole-store restore (:mod:`kiro_crew.member_memory_backup`) replaces everything, so
the rows written between the backup and the restore survive only in the
``superseded-*`` copy the restore kept. This module reads the ``memory.db`` of such a
copy and adds to the live store each row it does not have, as the row
was: same id, text, value, timestamps, facets, record metadata and revisions.

It writes through the live store's own connection, under its ``_db_lock``, in one
``BEGIN IMMEDIATE`` transaction, so a concurrent writer waits for it or it waits for
the writer, and nothing either commits is lost. A dry run takes the same path and
rolls back.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from kiro_crew import member_memory_backup
from kiro_crew._sqlite_compat import sqlite3

if TYPE_CHECKING:
    from kiro_crew.vector_memory import VectorMemoryStore

#: ``memory_meta`` keys naming the vector space a database's vectors were made in.
_SPACE_KEYS = ("embedding_space_sig", "embedding_rebuild_generation")


@dataclass
class MergeReport:
    """What a merge added (or, dry, would add) and what it left out."""

    applied: bool
    added: dict[str, int] = field(default_factory=dict)
    duplicates: int = 0
    conflicts: list[tuple[str, str]] = field(default_factory=list)
    vectors_kept: int = 0
    vectors_dropped: int = 0

    @property
    def total_added(self) -> int:
        return sum(self.added.values())


def _columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in db.execute(f"PRAGMA table_info({table})")]


def _space(db: sqlite3.Connection) -> tuple[str | None, ...]:
    return tuple(
        (row[0] if row else None)
        for row in (
            db.execute("SELECT value FROM memory_meta WHERE key=?", (key,)).fetchone()
            for key in _SPACE_KEYS
        )
    )


@contextmanager
def source_database(path: Path, db_path: Path) -> Iterator[Path]:
    """The source ``memory.db`` at *path*, checked to be this member's own store.

    Only a ``memory.db`` file is taken, such as the one inside a ``superseded-*``
    copy; a backup bundle goes through a whole-store restore instead.
    """
    name, owner = member_memory_backup._identity(db_path)
    if path.name != "memory.db" or not path.is_file():
        raise ValueError(
            "--merge-from takes a memory.db file, such as the one inside a superseded-* copy"
        )
    if path.resolve() == db_path.resolve():
        raise ValueError("The merge source is the live store itself")
    member_memory_backup._check_database(path, name, owner)
    yield path


def merge_missing_rows(
    store: VectorMemoryStore, source_db: Path, *, apply: bool = False
) -> MergeReport:
    """Add the live rows of *source_db* that *store* has no row for.

    A source row is skipped as a duplicate when the live store has a row (live or
    deleted) with its id or key, or, for a keyless row (an episode), a live keyless
    row with its exact text. The caller has
    already checked that *source_db* is this member's store
    (:func:`source_database`).
    """
    if store.algorithm_version != "v2":
        raise ValueError("Merging rows back needs a private V2 member store")
    report = MergeReport(applied=apply)
    fresh: dict[str, list[str]] = {}
    uri = source_db.absolute().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as src:
        src.row_factory = sqlite3.Row
        same_space = _space(src)
        with store._db_lock:
            db = store.db
            db.execute("BEGIN IMMEDIATE")
            try:
                keep_vectors = _space(db) == same_space and same_space[0] is not None
                source_items = _columns(src, "memory_items")
                item_cols = [c for c in _columns(db, "memory_items") if c in source_items]
                meta_cols = [
                    c
                    for c in _columns(db, "memory_record_meta")
                    if c in _columns(src, "memory_record_meta")
                ]
                rev_cols = [
                    c
                    for c in _columns(db, "memory_revisions")
                    if c != "id" and c in _columns(src, "memory_revisions")
                ]
                ids = {r[0] for r in db.execute("SELECT id FROM memory_items")}
                keys = {
                    r[0] for r in db.execute("SELECT key FROM memory_items WHERE key IS NOT NULL")
                }
                # A fact's text is its value, so only keyless rows dedupe by text.
                texts = {
                    r[0]
                    for r in db.execute(
                        "SELECT text FROM memory_items WHERE key IS NULL AND is_deleted=0"
                    )
                }
                rows = src.execute(
                    f"SELECT {','.join(item_cols)} FROM memory_items "
                    "WHERE is_deleted=0 ORDER BY created_at, id"
                ).fetchall()
                for row in rows:
                    item = dict(zip(item_cols, row))
                    keyed = item.get("key") is not None
                    if (
                        item["id"] in ids
                        or (keyed and item["key"] in keys)
                        or (not keyed and item["text"] in texts)
                    ):
                        report.duplicates += 1
                        continue
                    had_vector = item.get("embedding") is not None
                    if had_vector and not keep_vectors:
                        item["embedding"] = None
                    db.execute("SAVEPOINT merge_row")
                    try:
                        _copy_row(db, src, item, item_cols, meta_cols, rev_cols)
                    except sqlite3.IntegrityError as exc:
                        db.execute("ROLLBACK TO merge_row")
                        db.execute("RELEASE merge_row")
                        report.conflicts.append((item["id"], str(exc)))
                        continue
                    db.execute("RELEASE merge_row")
                    if had_vector and keep_vectors:
                        report.vectors_kept += 1
                    elif had_vector:
                        report.vectors_dropped += 1
                    ids.add(item["id"])
                    if keyed:
                        keys.add(item["key"])
                    else:
                        texts.add(item["text"])
                    report.added[item["kind"]] = report.added.get(item["kind"], 0) + 1
                    if item.get("embedding") is None:
                        fresh.setdefault(item["kind"], []).append(item["id"])
                if apply:
                    db.commit()
                else:
                    db.rollback()
            except BaseException:
                db.rollback()
                raise
    if apply and report.total_added:
        from kiro_crew.vector_memory_runtime import embedding_repair

        store._invalidate_episodic_scoring()
        for kind, identities in fresh.items():
            embedding_repair.queue_fresh_vectors(store, kind, identities)
    return report


def _copy_row(
    db: sqlite3.Connection,
    src: sqlite3.Connection,
    item: dict,
    item_cols: list[str],
    meta_cols: list[str],
    rev_cols: list[str],
) -> None:
    """Insert one item with its record metadata, revisions and full-text row."""
    rid = item["id"]
    db.execute(
        f"INSERT INTO memory_items ({','.join(item_cols)}) "
        f"VALUES ({','.join('?' for _ in item_cols)})",
        [item[c] for c in item_cols],
    )
    meta = src.execute(
        f"SELECT {','.join(meta_cols)} FROM memory_record_meta WHERE record_id=?", (rid,)
    ).fetchone()
    if meta is not None:
        db.execute(
            f"INSERT INTO memory_record_meta ({','.join(meta_cols)}) "
            f"VALUES ({','.join('?' for _ in meta_cols)})",
            tuple(meta),
        )
    for revision in src.execute(
        f"SELECT {','.join(rev_cols)} FROM memory_revisions WHERE record_id=? ORDER BY id",
        (rid,),
    ):
        db.execute(
            f"INSERT INTO memory_revisions ({','.join(rev_cols)}) "
            f"VALUES ({','.join('?' for _ in rev_cols)})",
            tuple(revision),
        )
    db.execute("DELETE FROM memory_fts WHERE path=?", (rid,))
    indexed = src.execute("SELECT content FROM memory_fts WHERE path=?", (rid,)).fetchall()
    for (content,) in indexed:
        db.execute("INSERT INTO memory_fts (path, content) VALUES (?, ?)", (rid, content))


def run_cli(store_name: str, source: Path, *, apply: bool) -> MergeReport:
    """``kirocrew memory restore --store <member> --merge-from <source> [--apply]``."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.memory_stores import (
        declared_store_names,
        memory_store_namespace_lock,
        owned_store_path,
    )
    from kiro_crew.vector_memory_member import declared_store

    if store_name not in declared_store_names():
        raise ValueError(f"memory store {store_name!r} is not declared; nothing was merged")
    db_path = owned_store_path(store_name)
    if db_path is None or not member_memory_backup.is_member_store(db_path):
        raise ValueError("--merge-from needs a member (bot) memory store")
    if not source.exists():
        raise ValueError(f"{source} does not exist; nothing was merged")
    cfg = KiroCrewConfig.load()
    with memory_store_namespace_lock():
        if member_memory_backup._pending_journal(db_path)[1] is not None:
            raise ValueError(
                "A restore of this store is pending a gateway restart; cancel it or restart "
                "first, then merge"
            )
        with source_database(source, db_path) as source_db:
            store = declared_store(
                db_path, store_id=store_name, config=cfg, embedding_dim=cfg.memory.embedding_dim
            )
            store.init()
            try:
                return merge_missing_rows(store, source_db, apply=apply)
            finally:
                store.close()


def report_lines(report: MergeReport, source: Path) -> list[str]:
    """The CLI's summary of *report*."""
    verb = "Added" if report.applied else "Would add"
    kinds = ", ".join(f"{n} {kind}" for kind, n in sorted(report.added.items())) or "nothing"
    lines = [
        f"{verb} {report.total_added} row(s) from {source.name}: {kinds}.",
        f"Already in the live store (same id, key or text): {report.duplicates}.",
        f"Vectors kept: {report.vectors_kept}; left for the repair sweep: "
        f"{report.vectors_dropped}.",
    ]
    for rid, reason in report.conflicts:
        lines.append(f"Skipped {rid}: {reason}")
    if not report.applied:
        lines.append("DRY RUN: nothing was written. Add --apply to merge.")
    return lines

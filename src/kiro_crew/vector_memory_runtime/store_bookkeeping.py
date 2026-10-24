"""The store's bookkeeping tables: record revisions, the audit trail and ``memory_meta``.

Both lineages carry these tables. Record revisions and eligibility (``_record_mutation``,
``with_record_metadata``, ``_ineligible_ids``, ``_eligible_rows``) keep
``memory_record_meta`` and ``memory_revisions`` in step with every write. ``_log_event``
appends to the ``memory_events`` audit trail; ``get_events``, ``rotate_events`` and
``get_rejection_stats`` read and bound it. ``_read_meta``, ``_write_meta`` and
``_write_meta_in_transaction`` read and upsert ``memory_meta``.

The functions are bound on ``VectorMemoryStore`` as the class attributes of the same name
and take the store as ``self``. Store fields read: ``_db_lock``, ``_sem_rel``,
``_sem_guard``, ``_epi_rel`` and ``_epi_guard``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew import memory_record_metadata as record_meta

if TYPE_CHECKING:
    from kiro_crew.vector_memory import VectorMemoryStore

# The store's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.vector_memory")


_MAX_EVENTS = 10_000


def with_record_metadata(self: VectorMemoryStore, rows: list[dict]) -> list[dict]:
    """Attach the current revision to the trusted pre-extraction snapshot."""
    with self._db_lock:
        result = []
        for raw in rows:
            row = dict(raw)
            metadata = record_meta.get_record_metadata(self.db, f"key:{row['key']}")
            result.append(
                {
                    **row,
                    "record_revision": metadata.get("revision", 0),
                    "record_metadata": {
                        field: metadata[field]
                        for field in (
                            "category",
                            "subject",
                            "predicate",
                            "scope",
                            "valid_from",
                            "valid_until",
                        )
                        if metadata.get(field)
                    },
                }
            )
        return result


def _ineligible_ids(self: VectorMemoryStore, record_ids: list[str] | None = None) -> set[str]:
    """Filter before rank/cap; validity is evaluated again on every recall."""

    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    _MAX_SQL_PARAMS = vm._MAX_SQL_PARAMS

    predicate = "status != 'active' OR valid_from != '' OR valid_until != ''"
    if record_ids is None:
        rows = self._fetch_all_locked(
            "SELECT record_id, status, valid_from, valid_until "
            f"FROM memory_record_meta WHERE {predicate}"
        )
    else:
        unique = list(dict.fromkeys(record_ids))
        rows = []
        for start in range(0, len(unique), _MAX_SQL_PARAMS):
            chunk = unique[start : start + _MAX_SQL_PARAMS]
            placeholders = ",".join("?" * len(chunk))
            rows.extend(
                self._fetch_all_locked(
                    "SELECT record_id, status, valid_from, valid_until "
                    f"FROM memory_record_meta WHERE record_id IN ({placeholders}) "
                    f"AND ({predicate})",
                    tuple(chunk),
                )
            )
    return record_meta.ineligible_record_ids(rows)


def _eligible_rows(self: VectorMemoryStore, rows, kind: str) -> list:
    # Only these candidate rows are ever tested against ``blocked``, so scope
    # the ineligibility lookup to their ids: it then goes through the
    # record_id PRIMARY KEY instead of scanning all of memory_record_meta
    # (which only grows). ``identity in blocked`` is unchanged because
    # ``blocked`` is restricted to ids drawn from these same candidates.
    identities = [
        identity
        for identity in (
            record_meta.record_identity_or_none(
                kind, row["id"] if kind == "episode" else row["key"]
            )
            for row in rows
        )
        if identity is not None
    ]
    blocked = self._ineligible_ids(identities)
    kept = []
    for row in rows:
        identity = record_meta.record_identity_or_none(
            kind, row["id"] if kind == "episode" else row["key"]
        )
        # A legacy row with no identity carries no metadata and cannot be
        # ranked: recall_evidence drops its non-string id and the payload
        # builder then reads row["id"]. Drop it here (both when blocked is
        # empty and when it is not) so it never reaches ranking; the row
        # stays visible in the read-only list and on disk, untouched.
        if identity is None or identity in blocked:
            continue
        kept.append(row)
    return kept


def _record_mutation(
    self: VectorMemoryStore,
    kind: str,
    item_id: str,
    before: dict | None,
    source: str,
    *,
    metadata: dict | None = None,
    operation: str = "update",
) -> dict:
    """Caller holds the lock and transaction; preserve canonical view shape."""
    with self._db_lock:
        relation = "episodic_memories" if kind == "episode" else "semantic_memory"
        column = "id" if kind == "episode" else "key"
        raw_id = item_id if kind == "episode" else item_id.removeprefix("key:")
        if metadata and metadata.get("status") in {"superseded", "expired", "forgotten"}:
            physical = self._epi_rel if kind == "episode" else self._sem_rel
            guard = self._epi_guard if kind == "episode" else self._sem_guard
            self.db.execute(
                f"UPDATE {physical} SET is_deleted=1 WHERE {column}=?{guard}", (raw_id,)
            )
        row = self.db.execute(f"SELECT * FROM {relation} WHERE {column}=?", (raw_id,)).fetchone()
        return record_meta.sync_record(
            self.db,
            kind=kind,
            record_id=item_id,
            before=before,
            after=dict(row) if row is not None else None,
            source=source,
            metadata=metadata,
            operation=operation,
            limit_v1_history=self.algorithm_version == "v1",
        )


def _log_event(
    self: VectorMemoryStore,
    event_type: str,
    memory_type: str,
    key: str,
    old_value: str | None,
    new_value: str | None,
    source: str,
) -> int | None:
    """Append to the audit trail; return the new row's id, or None when it was not written."""

    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    _now_iso = vm._now_iso

    try:
        # Every write path funnels through here, from both locked and
        # unlocked callers, so serialize on the (reentrant) _db_lock: an
        # unsynchronized INSERT races a concurrent writer's implicit BEGIN.
        with self._db_lock:
            cursor = self.db.execute(
                "INSERT INTO memory_events (event_type, memory_type, memory_key, "
                "old_value, new_value, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event_type, memory_type, key, old_value, new_value, source, _now_iso()),
            )
            self.db.commit()
            return cursor.lastrowid
    except Exception:
        logger.debug("Failed to log memory event", exc_info=True)
        return None


def get_events(self: VectorMemoryStore, limit: int = 50, offset: int = 0) -> list[dict]:
    """Return recent memory events with pagination."""
    rows = self._fetch_all_locked(
        "SELECT * FROM memory_events ORDER BY id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    return [dict(r) for r in rows]


def rotate_events(self: VectorMemoryStore, max_rows: int = _MAX_EVENTS) -> int:
    """Delete oldest events if over limit. Returns count deleted."""
    with self._db_lock:
        count = self.db.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
        if count <= max_rows:
            return 0
        to_delete = count - max_rows
        self.db.execute(
            "DELETE FROM memory_events WHERE id IN "
            "(SELECT id FROM memory_events ORDER BY id ASC LIMIT ?)",
            (to_delete,),
        )
        self.db.commit()
    return to_delete


def _read_meta(self: VectorMemoryStore, key: str) -> str | None:
    """Read a ``memory_meta`` value, or None when absent."""
    row = self._fetch_one_locked("SELECT value FROM memory_meta WHERE key = ?", (key,))
    return str(row["value"]) if row is not None else None


def _write_meta_in_transaction(self: VectorMemoryStore, key: str, value: str) -> None:
    """Write metadata under the caller's database lock and transaction."""

    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    _now_iso = vm._now_iso

    with self._db_lock:
        self.db.execute(
            "INSERT INTO memory_meta (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, _now_iso()),
        )


def _write_meta(self: VectorMemoryStore, key: str, value: str) -> None:
    """Upsert a ``memory_meta`` value."""
    with self._db_lock:
        self._write_meta_in_transaction(key, value)
        self.db.commit()


def get_rejection_stats(self: VectorMemoryStore) -> dict[str, int]:
    """Return counts of write rejections by reason.

    ``injection_blocked`` is counted across BOTH semantic and episodic
    writes. The other codes
    stay semantic-scoped: ``conflict_skip`` is also emitted for episodic
    FAISS dedup, so counting episodic there would conflate benign
    deduplication with policy rejections.
    """
    rows = self._fetch_all_locked(
        "SELECT event_type, COUNT(*) as count FROM memory_events "
        "WHERE event_type = 'injection_blocked' "
        "OR (memory_type = 'semantic' AND event_type IN "
        "('allowlist_reject', 'low_confidence', 'conflict_skip', 'value_empty')) "
        "GROUP BY event_type"
    )
    return {r["event_type"]: r["count"] for r in rows}

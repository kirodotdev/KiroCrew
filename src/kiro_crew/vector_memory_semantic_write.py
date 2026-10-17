"""The semantic write transaction: ``VectorMemoryStore._write_semantic``.

One locked read, conflict policy and upsert: V1 keeps its precedence (a user-explicit value
wins, an automated write needs comparable confidence), V2 keeps a changed automated
assertion as a review proposal unless it is an owner or verified correction, and every write
checks the record revision it was read at. A committed write runs the publication tail in
``vector_memory_runtime.semantic``. A consolidation write joins its caller's transaction
and, when it changes a value, tombstones the member episodes that assert the old one: SQL on
``memory_items``, a relation only the crew lineage has, which the composition contract keeps
out of ``vector_memory_runtime``, so the module lives beside the facade.

Bound on ``VectorMemoryStore`` as ``_write_semantic``; takes the store as ``self``. Store
fields read: ``_db_lock``, ``_lineage``, ``_sem_rel`` and ``_sem_guard``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from kiro_crew import memory_record_metadata as record_meta
from kiro_crew import memory_schema, memory_v2
from kiro_crew.vector_memory_constants import _MAX_EPISODIC_RETIRED_PER_WRITE
from kiro_crew.vector_memory_runtime import semantic as _semantic

if TYPE_CHECKING:
    from kiro_crew.vector_memory import VectorMemoryStore

# The store's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.vector_memory")


def _write_semantic(
    self: VectorMemoryStore,
    key: str,
    value_json: str,
    confidence: float,
    source: str,
    *,
    metadata: dict | None = None,
    expected_revision: int | None = None,
    correction: record_meta.CorrectionEvidence | None = None,
    _consolidation: bool = False,
    defer_embedding: bool = False,
    embedding: list[float] | None = None,
    embedding_resolved: bool = False,
    embedding_generation: int | None = None,
    retirement_embedding: list[float] | None = None,
    retirement_embedding_resolved: bool = False,
    retirement_value_json: str | None = None,
) -> str | None:
    """Retain V1 conflict scoring; propose inferred changes in private V2.

    ``defer_embedding`` skips BOTH blocking embeds this write can reach — the
    value's own vector and the similarity arm of the stale-episodic retirement
    — leaving each of them in the state it already reaches when the embedder
    answers ``None``: a NULL vector for the repair sweep, and a retirement
    that matches on text alone.
    """

    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    _now_iso = vm._now_iso
    sqlite3 = vm.sqlite3

    private_policy = self.algorithm_version == "v2"
    with self._db_lock:
        if not _consolidation:
            self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute(
                "SELECT * FROM semantic_memory WHERE key = ?", (key,)
            ).fetchone()
            if not private_policy and existing and not existing["is_deleted"]:
                reason = None
                old_conf = existing["confidence"]
                if source != "user_explicit":
                    if _semantic._is_degenerate_value_json(existing["value_json"]):
                        # Neither precedence rule has content to protect here, and
                        # refusing is what makes such a row permanent: the automated
                        # writer this branch turns away is the only writer that would
                        # ever repair it.
                        logger.info(
                            "Semantic repair: replacing degenerate value for %r from %s",
                            key,
                            source,
                        )
                    elif existing["source"] == "user_explicit":
                        reason = (
                            "Existing entry set by user cannot be overwritten by automated source"
                        )
                    elif confidence <= old_conf and abs(confidence - old_conf) >= 0.1:
                        reason = f"Existing entry has higher confidence ({old_conf:.2f} vs {confidence:.2f})"
                if reason:
                    self.db.rollback()
                    self._log_event(
                        "conflict_skip",
                        "semantic",
                        key,
                        existing["value_json"],
                        value_json,
                        source,
                    )
                    return reason
            before = dict(existing) if existing is not None else None
            kind = "directive" if key.startswith("lesson.") else "fact"
            current = record_meta.get_record_metadata(self.db, f"key:{key}")
            if expected_revision is not None and expected_revision != current.get("revision", 0):
                if _consolidation:
                    raise ValueError("Memory changed since extraction")
                self.db.rollback()
                return "Memory changed since it was read; reload before correcting"
            verified = bool(
                isinstance(correction, record_meta.CorrectionEvidence)
                and existing is not None
                and not existing["is_deleted"]
                and correction.key == key
                and correction.old_value_json == existing["value_json"]
                and correction.new_value_json == value_json
                and correction.revision == current.get("revision")
            )
            if verified and correction is not None:
                metadata = {
                    **(metadata or {}),
                    "source_ref": correction.source_ref,
                    "observed_at": correction.observed_at,
                }
            metadata_changed = bool(
                existing
                and metadata
                and any(current.get(field, "") != value for field, value in metadata.items())
            )
            changed = bool(
                existing
                and (
                    not _semantic._json_value_equal(existing["value_json"], value_json)
                    or existing["is_deleted"]
                )
            )
            if (
                (private_policy or (existing and existing["is_deleted"]))
                and (changed or metadata_changed)
                and source != "user_explicit"
                and not verified
            ):
                proposal = dict(
                    before or {},
                    value_json=value_json,
                    confidence=confidence,
                    source=source,
                    is_deleted=0,
                )
                proposal_id = record_meta.propose_conflict(
                    self.db,
                    kind=kind,
                    record_id=key,
                    before=before or {},
                    after=proposal,
                    source=source,
                    metadata=metadata,
                )
                if not _consolidation:
                    self.db.commit()
                if not _consolidation:
                    self._log_event(
                        "conflict_skip",
                        "semantic",
                        key,
                        existing["value_json"],
                        value_json,
                        source,
                    )
                return f"Conflicting update saved for review (proposal {proposal_id}); current fact retained"
            if private_policy and existing and not changed and source != "user_explicit":
                # A model reaffirming an owner fact must not erase its origin.
                if metadata:
                    self._record_mutation(
                        kind, key, before, source, metadata=metadata, operation="observe"
                    )
                if not _consolidation:
                    self.db.commit()
                return None
            now = _now_iso()
            self.db.execute(
                memory_schema.semantic_upsert(self._lineage),
                memory_schema.semantic_upsert_params(
                    self._lineage, key, value_json, confidence, source, now
                ),
            )
            if metadata and metadata.get("status") in {"superseded", "expired", "forgotten"}:
                self.db.execute(
                    f"UPDATE {self._sem_rel} SET is_deleted=1 WHERE key=?{self._sem_guard}",
                    (key,),
                )
            self._record_mutation(
                kind,
                key,
                before,
                source,
                metadata=metadata,
                operation="update" if existing else "create",
            )
            if private_policy:
                self.db.execute(
                    "INSERT INTO memory_events (event_type,memory_type,memory_key,old_value,"
                    "new_value,source,created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        "update" if existing else "create",
                        "semantic",
                        key,
                        existing["value_json"] if existing else None,
                        value_json,
                        source,
                        now,
                    ),
                )
            # Invalidate the resident scoring set for BOTH paths: the
            # semantic row was applied to this connection above, so the
            # cache is stale even when the commit is deferred to the
            # consolidation caller. An own-connection commit does not move
            # data_version, so without this a consolidation would hide its
            # new facts from recall until an unrelated write or a restart.
            # The commit itself stays deferred for consolidation batches.
            self._invalidate_semantic_scoring()
            if not _consolidation:
                self.db.commit()
        except (ValueError, sqlite3.IntegrityError) as exc:
            if _consolidation:
                raise
            self.db.rollback()
            return str(exc)
        except Exception:
            self.db.rollback()
            raise

    if _consolidation:
        if (
            existing
            and not existing["is_deleted"]
            and not _semantic._json_value_equal(existing["value_json"], value_json)
        ):
            old_text = json.loads(existing["value_json"])
            if isinstance(old_text, str) and len(old_text) >= 3:
                with self._db_lock:
                    retired = 0
                    for row in self.db.execute(
                        "SELECT * FROM episodic_memories WHERE is_deleted=0 ORDER BY created_at DESC,id"
                    ).fetchall():
                        if not memory_v2.superseded_value_is_asserted(row["text"], key, old_text):
                            continue
                        self.db.execute(
                            "UPDATE memory_items SET is_deleted=1 WHERE id=?", (row["id"],)
                        )
                        self._record_mutation(
                            "episode",
                            row["id"],
                            dict(row),
                            source,
                            metadata={"status": "superseded"},
                            operation="supersede",
                        )
                        self.db.execute(
                            "INSERT INTO memory_events (event_type,memory_type,memory_key,old_value,"
                            "new_value,source,created_at) VALUES (?,?,?,?,?,?,?)",
                            (
                                "conflict_retire",
                                "episodic",
                                row["id"],
                                row["text"][:200],
                                key,
                                "semantic_update",
                                _now_iso(),
                            ),
                        )
                        retired += 1
                        if retired >= _MAX_EPISODIC_RETIRED_PER_WRITE:
                            break
        return None

    _semantic.publish_committed_write(
        self,
        key,
        value_json,
        source,
        existing,
        private_policy=private_policy,
        defer_embedding=defer_embedding,
        embedding=embedding,
        embedding_resolved=embedding_resolved,
        embedding_generation=embedding_generation,
        retirement_embedding=retirement_embedding,
        retirement_embedding_resolved=retirement_embedding_resolved,
        retirement_value_json=retirement_value_json,
    )
    return None

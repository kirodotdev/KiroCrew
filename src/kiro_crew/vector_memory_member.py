"""The V2 member database: provisioning, admission identity, history and consolidation.

``create_member_database`` provisions a member file exclusively,
``read_member_database_identity`` and ``_member_identity`` validate one, and
``open_member_database`` and ``declared_store`` open a store bound to that identity. The
database also carries the member's history and its FTS projection (``append_history``,
``read_history_entries``, ``read_editable_history``, ``replace_today_history``,
``search_memory``, ``rebuild_memory_index``), the consolidation receipt and the atomic
publication of one extracted span (``consolidation_receipt``, ``apply_consolidation``), and
owner-selected seeding (``seed_item_if_absent``).

This SQL names relations only the crew lineage has (``memory_items``, ``memory_history``,
``memory_fts``, ``memory_consolidations``), which the composition contract keeps out of
``vector_memory_runtime``, so the module lives beside the facade.
:mod:`kiro_crew.vector_memory` re-exports every module-level name here. The functions are
bound on ``VectorMemoryStore`` as the class attributes of the same name and take the store
as ``self``. Store fields read: ``_db_lock``, ``_lineage``, ``_epi_rel`` and ``_epi_guard``;
``_member_store`` sets ``_member_identity`` and ``_memory_store_name`` on a new store.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew import memory_record_metadata as record_meta
from kiro_crew import memory_schema, memory_stores, platform_compat
from kiro_crew.lesson_validation import contains_volatile_lesson_fact, extracted_lesson_applies
from kiro_crew.owner_only_files import owner_only_opener
from kiro_crew.project_scope import canonical_scope, scope_is_admissible
from kiro_crew.validation import normalize_lesson_category
from kiro_crew.vector_memory_constants import (
    _MAX_EPISODIC_PER_CONSOLIDATION,
    _MAX_LESSONS_PER_CONSOLIDATION,
    _MAX_SEMANTIC_PER_CONSOLIDATION,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Callable

    from kiro_crew._sqlite_compat import sqlite3
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.vector_memory import VectorMemoryStore

# The store's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.vector_memory")


def consolidation_source_digest(messages: list[dict]) -> str:
    """Fingerprint an exact transcript prefix without retaining another copy."""
    encoded = json.dumps(
        messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _member_identity(db: sqlite3.Connection) -> tuple[str, str]:
    """Validate an existing database without repairing it."""
    row = db.execute(
        "SELECT format_version, member_id, store_id FROM member_database WHERE singleton=1"
    ).fetchone()
    if row is None or row[0] != memory_schema.MEMBER_DATABASE_FORMAT or not row[1] or not row[2]:
        raise ValueError("Unsupported or incomplete member memory database")
    if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise ValueError("Member memory database integrity check failed")
    for table in (
        "memory_items",
        "memory_record_meta",
        "memory_revisions",
        "memory_history",
        "memory_consolidations",
        "memory_fts",
    ):
        db.execute(f"SELECT * FROM {table} LIMIT 0")
    db.execute(
        "SELECT source_id,source_total,source_count,source_digest,receipt_json,created_at "
        "FROM memory_consolidations LIMIT 0"
    )
    return str(row[1]), str(row[2])


def read_member_database_identity(path: Path) -> tuple[str, str]:
    """Inspect only an existing file; missing and old-format files are errors."""

    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    sqlite3 = vm.sqlite3

    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return _member_identity(db)
    finally:
        db.close()


def create_member_database(path: Path, *, member_id: str, store_id: str) -> None:
    """Provision one new database exclusively; never overwrite existing bytes."""

    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    _now_iso = vm._now_iso
    sqlite3 = vm.sqlite3

    if not member_id or not store_id or store_id == "default":
        raise ValueError("A member and store identity are required")
    path = Path(path)
    platform_compat.make_owner_only_dir(path.parent)
    # Exclusive creation prevents both clobbering data and concurrent provisioning,
    # and the 0600 creation mode means the reservation is never readable by others.
    with open(path, "xb", opener=owner_only_opener):
        pass
    platform_compat.restrict_to_owner(path)  # lockdown-ok: empty reservation; SQLite writes follow
    db = sqlite3.connect(path, isolation_level=None)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(
            "BEGIN IMMEDIATE;" + memory_schema.CREW_SCHEMA_SQL + memory_schema.MEMBER_SCHEMA_SQL
        )
        record_meta.ensure_schema(db)
        now = _now_iso()
        db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT)")
        db.execute(
            "INSERT INTO schema_version VALUES (?,?)", (memory_schema.CREW_SCHEMA_VERSION, now)
        )
        db.execute(
            "INSERT INTO member_database VALUES (1,?,?,?)",
            (memory_schema.MEMBER_DATABASE_FORMAT, member_id, store_id),
        )
        db.executemany(
            "INSERT INTO memory_meta (key,value,updated_at) VALUES (?,?,?)",
            ((memory_schema.LINEAGE_META_KEY, memory_schema.LINEAGE_CREW, now),),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def open_member_database(
    path: Path, *, member_id: str, store_id: str, **vector_options
) -> "VectorMemoryStore":
    """Open a canonically admitted member store without creating or migrating it."""
    store = _member_store(path, member_id=member_id, store_id=store_id, **vector_options)
    store.init()
    return store


def _member_store(
    path: Path, *, member_id: str, store_id: str, **vector_options
) -> "VectorMemoryStore":
    """A member store bound to its canonical identity, not yet opened."""

    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    VectorMemoryStore = vm.VectorMemoryStore

    store = VectorMemoryStore(path, **vector_options)
    store._member_identity = (member_id, store_id)
    store._memory_store_name = store_id
    return store


def declared_store(
    path: Path, *, store_id: str, config: KiroCrewConfig, **vector_options
) -> "VectorMemoryStore":
    """The store *store_id* declares, bound the way its declaration says, NOT yet opened.

    A declared V2 store is bound to its canonical member identity, so ``init()``
    admits it through the ``mode=rw`` identity check :func:`open_member_database`
    takes and never creates or migrates it. Every other declaration gets the V1
    store, whose ``init()`` refuses a member file. That is the one choice every
    opener of a NAMED store has to make. A caller making it by hand has to know
    that a member store must not be opened with a bare ``init()``, which raises
    "Private memory requires canonical member admission" on every V2 file.

    Returned unopened, so a caller holds the handle before ``init()`` runs and
    can close it on any path out of the open (a cancellation, the CLI's settlement
    of a database it created). *config* is the loaded config, read for the
    declaration and passed on as the store's own ``config=``.

    The global store is V1 whatever its entry says, as it is to
    :func:`kiro_crew.memory_stores.memory_store_version`, so a hand-edited
    ``default`` record cannot route the operator's own memory through member
    admission.
    """

    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    VectorMemoryStore = vm.VectorMemoryStore

    declaration = config.memory_stores.get(store_id)
    if (
        memory_stores.named_store_or_empty(store_id)
        and declaration is not None
        and declaration.memory_version == 2
    ):
        return _member_store(
            path,
            member_id=declaration.owner_member_id,
            store_id=store_id,
            config=config,
            **vector_options,
        )
    return VectorMemoryStore(path, config=config, **vector_options)


def _write_history(self: VectorMemoryStore, day: str, content: str) -> None:
    """Publish current history and its search projection; caller owns transaction."""

    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    _now_iso = vm._now_iso

    from kiro_crew.hooks import FileTooLargeError
    from kiro_crew.memory import MemoryStore

    max_bytes = MemoryStore._HISTORY_SNAPSHOT_MAX_BYTES
    if len(content.encode("utf-8")) > max_bytes:
        raise FileTooLargeError(f"Member history exceeds the {max_bytes}-byte write limit")
    with self._db_lock:
        row = self.db.execute("SELECT revision FROM memory_history WHERE day=?", (day,)).fetchone()
        revision = (row[0] if row else 0) + 1
        now = _now_iso()
        self.db.execute(
            "INSERT INTO memory_history VALUES (?,?,?,?) ON CONFLICT(day) DO UPDATE SET "
            "content=excluded.content,revision=excluded.revision,updated_at=excluded.updated_at",
            (day, content, revision, now),
        )
        self.db.execute("DELETE FROM memory_fts WHERE path=?", (f"history:{day}",))
        self.db.execute(
            "INSERT INTO memory_fts(path,content) VALUES (?,?)", (f"history:{day}", content)
        )


def _append_history(self: VectorMemoryStore, entry: str) -> None:
    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    datetime = vm.datetime

    with self._db_lock:
        now = datetime.now().astimezone()
        day = now.date().isoformat()
        row = self.db.execute("SELECT content FROM memory_history WHERE day=?", (day,)).fetchone()
        content = row[0] if row else f"# {day}\n"
        content += f"\n#### {now.strftime('%H:%M %Z')}\n{entry.strip()}\n"
        self._write_history(day, content)


def append_history(self: VectorMemoryStore, entry: str) -> None:
    if self.algorithm_version != "v2":
        raise ValueError("Database history requires member memory")
    with self._db_lock:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._append_history(entry)
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise


def read_history_entries(
    self: VectorMemoryStore,
    *,
    since: str | None = None,
    limit: int = 366,
    max_bytes: int = 8 * 1024 * 1024,
) -> list[dict]:
    from kiro_crew.hooks import FileTooLargeError

    with self._db_lock:
        rows = self.db.execute(
            # Guard the projection in SQLite, before a text value crosses into
            # Python. BLOB length counts UTF-8 bytes, including embedded NULs.
            "SELECT day,CASE WHEN length(CAST(content AS BLOB))<=? THEN content END AS content,"
            "length(CAST(content AS BLOB)) AS content_bytes,updated_at "
            "FROM memory_history WHERE (? IS NULL OR day>=?) "
            "ORDER BY day DESC LIMIT ?",
            (max_bytes, since, since, limit),
        )
        result: list[dict] = []
        size = 0
        for row in rows:
            if row["content"] is None:
                raise FileTooLargeError(f"Member history exceeds the {max_bytes}-byte read limit")
            if size + row["content_bytes"] > max_bytes:
                break
            size += row["content_bytes"]
            result.append(
                {
                    "date": row["day"],
                    "path": f"history:{row['day']}",
                    "content": row["content"],
                    "updated_at": row["updated_at"],
                }
            )
        return list(reversed(result))


def _read_editable_history_for_day(self: VectorMemoryStore, day: str) -> str:
    from kiro_crew.hooks import FileTooLargeError
    from kiro_crew.memory import MemoryStore

    max_bytes = MemoryStore._HISTORY_SNAPSHOT_MAX_BYTES
    with self._db_lock:
        row = self.db.execute(
            "SELECT CASE WHEN length(CAST(content AS BLOB))<=? THEN content END "
            "FROM memory_history WHERE day=?",
            (max_bytes, day),
        ).fetchone()
        if row is not None and row[0] is None:
            raise FileTooLargeError(f"Member history exceeds the {max_bytes}-byte read limit")
        return row[0] if row else ""


def read_editable_history(self: VectorMemoryStore) -> str:
    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    datetime = vm.datetime

    day = datetime.now().astimezone().date().isoformat()
    with self._db_lock:
        return self._read_editable_history_for_day(day)


def replace_today_history(
    self: VectorMemoryStore,
    content: str,
    *,
    expected_baseline: str,
    validate_current: Callable[[str], None],
) -> bool:
    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    datetime = vm.datetime

    day = datetime.now().astimezone().date().isoformat()
    with self._db_lock:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            current = self._read_editable_history_for_day(day)
            validate_current(current)
            if current != expected_baseline:
                self.db.rollback()
                return False
            self._write_history(day, content)
            self.db.commit()
            return True
        except BaseException:
            self.db.rollback()
            raise


def search_memory(self: VectorMemoryStore, query: str, *, limit: int = 5) -> list[dict]:
    from kiro_crew._sqlite_compat import fts5_quote_tokens

    match = " ".join(fts5_quote_tokens(query))
    if not match:
        return []
    with self._db_lock:
        rows = self.db.execute(
            "SELECT path,snippet(memory_fts,1,'>>>','<<<','...',32) AS snippet,rank "
            "FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank",
            (match,),
        )
        result = []
        for row in rows:
            metadata = record_meta.get_record_metadata(self.db, row["path"])
            if metadata and not record_meta.eligible(metadata):
                continue
            result.append(dict(row))
            if len(result) >= limit:
                break
        return result


def rebuild_memory_index(self: VectorMemoryStore) -> int:
    with self._db_lock, self.db:
        self.db.execute("DELETE FROM memory_fts")
        self.db.execute(
            "INSERT INTO memory_fts(path,content) SELECT id,COALESCE(key,'')||' '||text "
            "FROM memory_items WHERE is_deleted=0"
        )
        self.db.execute(
            "INSERT INTO memory_fts(path,content) SELECT 'history:'||day,content FROM memory_history"
        )
        return self.db.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]


def consolidation_receipt(self: VectorMemoryStore, source_id: str) -> dict | None:
    """Read a committed source span before repeating an extraction."""
    with self._db_lock:
        row = self.db.execute(
            "SELECT source_total,source_count,source_digest,receipt_json "
            "FROM memory_consolidations WHERE source_id=?",
            (source_id,),
        ).fetchone()
        return (
            {
                "source_total": row[0],
                "source_count": row[1],
                "source_digest": row[2],
                "receipt": json.loads(row[3]),
            }
            if row
            else None
        )


def apply_consolidation(
    self: VectorMemoryStore,
    *,
    source_id: str,
    session_key: str,
    source_total: int,
    result: dict,
    snapshot: dict,
    messages: list[dict],
    facets: memory_schema.MemoryFacets | None = None,
) -> dict:
    """Publish one extracted span, its provenance and retry receipt atomically.

    Embeddings remain NULL until the writer's maintenance sweep. No provider,
    transcript or filesystem operation takes place inside this transaction.
    """

    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    _EPISODIC_TEXT_MAX = vm._EPISODIC_TEXT_MAX
    _EPISODIC_TEXT_MIN = vm._EPISODIC_TEXT_MIN
    _contains_injection = vm._contains_injection
    _lesson_key = vm._lesson_key
    _now_iso = vm._now_iso
    uuid4 = vm.uuid4

    if self.algorithm_version != "v2" or not source_id:
        raise ValueError("Consolidation requires a member database and stable source id")
    if type(source_total) is not int or source_total < len(messages):
        raise ValueError("Consolidation source total cannot precede its message count")
    source_count = len(messages)
    source_digest = consolidation_source_digest(messages)
    source = f"consolidation:{session_key}"
    receipt: dict = {"source_id": source_id, "semantic": 0, "episodic": 0, "lessons": 0}
    with self._db_lock:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            previous = self.db.execute(
                "SELECT source_total,source_count,source_digest,receipt_json "
                "FROM memory_consolidations WHERE source_id=?",
                (source_id,),
            ).fetchone()
            if previous:
                if tuple(previous[:3]) != (source_total, source_count, source_digest):
                    raise ValueError("Consolidation source identity changed")
                self.db.rollback()
                return json.loads(previous[3])
            semantic = result.get("semantic")
            for item in (semantic if isinstance(semantic, list) else [])[
                :_MAX_SEMANTIC_PER_CONSOLIDATION
            ]:
                if not isinstance(item, dict) or not isinstance(item.get("key"), str):
                    continue
                key = item["key"]
                if item.get("delete"):
                    before = self.db.execute(
                        "SELECT * FROM semantic_memory WHERE key=? AND is_deleted=0", (key,)
                    ).fetchone()
                    if before:
                        record_meta.propose_conflict(
                            self.db,
                            kind="fact",
                            record_id=key,
                            before=dict(before),
                            after=dict(before, is_deleted=1),
                            source=source,
                            operation="forget",
                        )
                    continue
                try:
                    confidence = float(item.get("confidence", 0.5))
                except (TypeError, ValueError):
                    continue
                if item.get("value") is None or not math.isfinite(confidence):
                    continue
                value = item["value"]
                if self.validate_semantic(key, value, confidence, source) is not None:
                    continue
                metadata = record_meta.normalize_metadata(item.get("metadata"))
                metadata.setdefault("source_ref", source_id)
                # Same identity resolution as set_semantic: a fact already
                # held by another key (or written earlier in this batch)
                # updates that key instead of inserting a duplicate holder.
                key = record_meta.identity_key(self.db, key, metadata)
                evidence = record_meta.verified_correction(
                    key=key,
                    before=snapshot.get(key, {}),
                    value=value,
                    quote=item.get("correction_quote"),
                    messages=messages,
                    session_key=session_key,
                )
                rejection = self._write_semantic(
                    key,
                    json.dumps(value, ensure_ascii=False),
                    confidence,
                    source,
                    metadata=metadata,
                    expected_revision=evidence.revision if evidence else None,
                    correction=evidence,
                    _consolidation=True,
                )
                if not rejection:
                    receipt["semantic"] += 1
                    if facets:
                        self.db.execute(
                            memory_schema.FACET_STAMP_SQL,
                            memory_schema.facet_stamp_params(f"key:{key}", facets),
                        )
            episodes = result.get("episodic")
            for item in (episodes if isinstance(episodes, list) else [])[
                :_MAX_EPISODIC_PER_CONSOLIDATION
            ]:
                if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                    continue
                text = item["text"].strip()
                tags = item.get("tags", [])
                try:
                    importance = float(item.get("importance", 0.5))
                except (TypeError, ValueError):
                    continue
                if (
                    not _EPISODIC_TEXT_MIN <= len(text) <= _EPISODIC_TEXT_MAX
                    or _contains_injection(text)
                    or not isinstance(tags, list)
                    or any(not isinstance(tag, str) for tag in tags)
                    or not math.isfinite(importance)
                    or not 0 <= importance <= 1
                ):
                    continue
                if self.db.execute(
                    "SELECT 1 FROM episodic_memories WHERE text=? AND is_deleted=0", (text,)
                ).fetchone():
                    continue
                item_id = str(uuid4())
                self.db.execute(
                    memory_schema.episodic_insert(self._lineage),
                    memory_schema.episodic_insert_params(
                        self._lineage,
                        item_id,
                        session_key,
                        text,
                        None,
                        json.dumps([tag.strip().lower()[:50] for tag in tags[:10] if tag.strip()]),
                        importance,
                        _now_iso(),
                        source,
                    ),
                )
                self._record_mutation(
                    "episode",
                    item_id,
                    None,
                    source,
                    metadata={"source_ref": source_id},
                    operation="create",
                )
                if facets:
                    self.db.execute(
                        memory_schema.FACET_STAMP_SQL,
                        memory_schema.facet_stamp_params(item_id, facets),
                    )
                receipt["episodic"] += 1
            lessons = result.get("lessons")
            for item in (lessons if isinstance(lessons, list) else [])[
                :_MAX_LESSONS_PER_CONSOLIDATION
            ]:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("rule"), str)
                    or not item["rule"].strip()
                ):
                    continue
                rule = item["rule"].strip()
                negative = item.get("negative")
                negative = negative.strip() or None if isinstance(negative, str) else None
                raw_scope = item.get("repo_scope")
                if raw_scope is not None and (
                    not isinstance(raw_scope, str)
                    or raw_scope.strip()
                    and not scope_is_admissible(raw_scope)
                ):
                    continue
                scope = canonical_scope(raw_scope)
                if contains_volatile_lesson_fact(rule, negative):
                    continue
                key = _lesson_key(rule, scope)
                value = {
                    "rule": rule,
                    "negative": negative,
                    "category": normalize_lesson_category(
                        item.get("category", "knowledge"), strict=True
                    ),
                }
                if scope:
                    value["repo_scope"] = scope
                # The authored startup tier, same contract as write_lesson:
                # absent when unstated, never ``null``. Same policy as the
                # consolidator's own _save_lessons (extracted_lesson_applies).
                applies = extracted_lesson_applies(item.get("applies"), logger)
                if applies:
                    value["applies"] = applies
                if self.validate_semantic(key, value, 0.9, source) is not None:
                    continue
                if not self._write_semantic(
                    key,
                    json.dumps(value, ensure_ascii=False),
                    0.9,
                    source,
                    metadata={"source_ref": source_id},
                    _consolidation=True,
                ):
                    receipt["lessons"] += 1
                    if facets:
                        self.db.execute(
                            memory_schema.FACET_STAMP_SQL,
                            memory_schema.facet_stamp_params(f"key:{key}", facets),
                        )
            entry = result.get("history_entry")
            if isinstance(entry, str) and entry.strip():
                self._append_history(entry)
            self.db.execute(
                "INSERT INTO memory_consolidations VALUES (?,?,?,?,?,?)",
                (
                    source_id,
                    source_total,
                    source_count,
                    source_digest,
                    json.dumps(receipt),
                    _now_iso(),
                ),
            )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
    self._invalidate_episodic_scoring()
    return receipt


def seed_item_if_absent(
    self: VectorMemoryStore,
    item: Mapping[str, object],
    *,
    source_store: str,
    source_id: str,
    kind: str,
) -> dict:
    """Explicit owner-selected copy; authorization belongs to the API caller.

    No source vector or timestamp is transplanted. Copies are new memories,
    with a durable source reference, and cannot replace or retire a target
    row. Their deferred vectors are repaired by the normal backfill sweep.
    """

    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    _now_iso = vm._now_iso

    if self.algorithm_version != "v2":
        raise ValueError("Explicit member seeding requires a private V2 destination")
    if not source_store or not source_id or kind not in memory_schema.ALL_KINDS:
        raise ValueError("A source store, item identity and valid memory kind are required")
    provenance = json.dumps(
        {
            "store": source_store,
            "item_id": source_id,
            "kind": kind,
            "copied_at": _now_iso(),
            "source": str(item.get("source", "")),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    facets = memory_schema.MemoryFacets(derived_from=provenance, surface="owner_seed")
    if kind != memory_schema.KIND_EPISODE:
        key = str(item.get("key", ""))
        try:
            value = json.loads(str(item["value_json"])) if "value_json" in item else item["value"]
        except (ValueError, TypeError, KeyError):
            return {"outcome": "rejected", "reason": "Invalid semantic value", "id": key}
        outcome = self.set_semantic_if_absent(key, value, 1.0, "user_seed", facets=facets)
        return {"outcome": outcome, "id": memory_schema.semantic_item_id(key)}
    text = str(item.get("text", ""))
    raw_tags = item.get("tags", [])
    try:
        tags = json.loads(raw_tags) if isinstance(raw_tags, str) else raw_tags
    except ValueError:
        tags = []
    if not isinstance(tags, list):
        tags = []
    try:
        raw_importance = item.get("importance", 0.5)
        importance = float(raw_importance) if isinstance(raw_importance, (int, float, str)) else 0.5
    except (ValueError, TypeError):
        importance = 0.5
    with self._db_lock:
        # Source identity survives owner corrections and forgetting. A
        # retry must not re-import the old text after either operation.
        for previous in self.db.execute(
            f"SELECT id, derived_from FROM {self._epi_rel} "
            f"WHERE derived_from != ''{self._epi_guard}"
        ).fetchall():
            try:
                origin = json.loads(previous["derived_from"])
            except (ValueError, TypeError):
                continue
            if isinstance(origin, dict) and (
                origin.get("store"),
                origin.get("item_id"),
                origin.get("kind"),
            ) == (source_store, source_id, kind):
                return {"outcome": "existing", "id": previous["id"]}
        if self.has_episodic_text(text):
            return {"outcome": "existing", "id": ""}
        written = self.write_episodic(
            text,
            tags=tags,
            importance=importance,
            source="user_seed",
            preserve_existing=True,
            defer_embedding=True,
            facets=facets,
        )
        if not written:
            return {
                "outcome": "rejected",
                "reason": "Duplicate, capacity or invalid episode",
                "id": "",
            }
        row = self._fetch_one_locked(
            f"SELECT id, derived_from FROM {self._epi_rel} "
            f"WHERE text = ? AND is_deleted = 0{self._epi_guard}",
            (text.strip(),),
        )
        if row is None or row["derived_from"] != provenance:
            # Provenance is essential for owner-selected copies, unlike a
            # best-effort carve facet. Never report a successful untraced copy.
            if row is not None:
                self.delete_episodic(row["id"], source="seed_provenance_failed")
            raise RuntimeError("Memory copy provenance could not be saved")
    return {"outcome": "imported", "id": row["id"]}

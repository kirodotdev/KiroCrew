"""Vector memory — structured semantic + episodic memory with audit trail.

Storage: ~/.kiro/crew/memory.db (SQLite, WAL mode)
FAISS index: ~/.kiro/crew/memory.faiss (optional, for vector search)

Semantic: key-value store with allow-list keys, confidence gating,
conflict resolution, injection detection, and event logging.
Episodic: conversation fragments with embeddings, importance scoring,
time-decay retrieval via FAISS (falls back to FTS5 without embeddings).

:class:`VectorMemoryStore` owns the one SQLite connection and its ``_db_lock``,
the store lifecycle, the member database, record revisions, audit events and
``memory_meta``, and the write paths this module's source guards pin here:
``_write_semantic``, ``write_episodic``, ``write_lesson`` and the writer loops
that feed them. The retrieval, ranking, repair and parsing rules it delegates to
live in :mod:`kiro_crew.vector_memory_runtime`. This module re-exports their
names and is the patch surface those modules read their seams from at call time.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import math
import struct
import threading
import time  # noqa: F401 -- patch seam: runtime modules read time through this module
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from sqlite3 import Error as StdlibSQLiteError
from typing import TYPE_CHECKING, Callable, Literal
from uuid import uuid4

from kiro_crew import memory_record_metadata as record_meta
from kiro_crew import memory_schema, memory_stores, memory_v2, platform_compat
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.config import live
from kiro_crew.config.loader import config_dir

# Scheduling classes for the shared embedding queue. This module stays decoupled
# from the embedding BACKEND (it takes an injected ``embed_fn``); the scheduling
# ints are imported rather than duplicated so the two cannot drift. Safe
# direction: ``embeddings`` reaches the store only through a Protocol, so it does
# not import this module and there is no cycle.
from kiro_crew.embeddings import (  # noqa: F401 -- bulk_pace_delay is the pacing patch seam
    PRIORITY_BULK,
    PRIORITY_INTERACTIVE,
    PRIORITY_NORMAL,
    bulk_pace_delay,
)

# ``PRIORITY_INTERACTIVE`` above and the unread names below stay importable from
# this module for existing ``from kiro_crew.vector_memory import ...`` callers.
from kiro_crew.lesson_cites import normalize_cited_commit, normalize_cites
from kiro_crew.lesson_validation import (  # noqa: F401
    LESSON_APPLIES_ON_TOPIC,
    LESSON_APPLIES_UNSTATED,
    LESSON_APPLIES_VALUES,
    authored_lesson_applies,
    contains_volatile_lesson_fact,
    extracted_lesson_applies,
    normalize_lesson_applies,
    order_by_request_relevance,
    render_lesson_tier,
    render_withheld_tier,
    tighter_lesson_budget,
)
from kiro_crew.memory_stores import MEMORY_DB_FILE
from kiro_crew.metrics.db_metrics import timed
from kiro_crew.project_scope import (  # noqa: F401
    canonical_scope,
    project_scope_satisfied,
    scope_is_admissible,
    scope_selector_is_inadmissible,
)
from kiro_crew.security import redact_and_truncate
from kiro_crew.validation import ALLOWED_LESSON_CATEGORIES, normalize_lesson_category  # noqa: F401

# Consolidation caps live in vector_memory_constants (a light module with no
# heavy transitive deps) so prompt-building callers can import them at top
# level without pulling this module's numpy/faiss imports; re-exported here so
# existing `from kiro_crew.vector_memory import _MAX_*` paths keep working.
from kiro_crew.vector_memory_constants import (  # noqa: F401
    _INJECTION_PATTERNS,
    _MAX_EPISODIC_PER_CONSOLIDATION,
    _MAX_EPISODIC_RETIRED_PER_WRITE,
    _MAX_LESSONS_PER_CONSOLIDATION,
    _MAX_SEMANTIC_PER_CONSOLIDATION,
    _contains_injection,
)
from kiro_crew.vector_memory_runtime import embedding as _embedding
from kiro_crew.vector_memory_runtime import embedding_repair as _embedding_repair
from kiro_crew.vector_memory_runtime import episodic_search as _episodic_search
from kiro_crew.vector_memory_runtime import faiss_index as _faiss_index
from kiro_crew.vector_memory_runtime import lessons as _lessons
from kiro_crew.vector_memory_runtime import migration as _migration
from kiro_crew.vector_memory_runtime import recall as _recall
from kiro_crew.vector_memory_runtime import retirement as _retirement
from kiro_crew.vector_memory_runtime import semantic as _semantic
from kiro_crew.vector_memory_runtime import text_scoring as _text_scoring
from kiro_crew.vector_memory_runtime.embedding import (  # noqa: F401
    _EmbeddingVector,
    _RecallQuery,
    _RecallSpaceChanged,
)
from kiro_crew.vector_memory_runtime.embedding_repair import _EMBED_SIG_KEY  # noqa: F401
from kiro_crew.vector_memory_runtime.episodic_search import (  # noqa: F401
    _EPISODIC_LONG_TEXT_CHARS,
    _EPISODIC_LONG_TEXT_THRESHOLD,
    _EPISODIC_RELEVANCE_THRESHOLD,
    EPISODIC_BLOCK_TEXT_CHARS,
    _EpisodicScoringSet,
)
from kiro_crew.vector_memory_runtime.lessons import (  # noqa: F401
    _LESSON_NEGATIVE_SEP,
    _decoded_lesson_value,
    _lesson_applies,
    _lesson_display_text,
    _lesson_embed_text,
    _lesson_fields,
    _lesson_fields_for_row,
    _lesson_row_report_text,
    _lesson_row_text,
    _lesson_scope,
    _lesson_scope_unusable,
    _renderable_lesson_text,
    _split_stored,
)
from kiro_crew.vector_memory_runtime.recall import _kept_episodes  # noqa: F401
from kiro_crew.vector_memory_runtime.semantic import (  # noqa: F401
    _EMPTY_VALUE_JSON,
    _KEY_PATTERN,
    _MAX_KEY_LEN,
    _MAX_VALUE_BYTES,
    _PREFS_OMISSION_NOTICE,
    _is_degenerate_value,
    _is_degenerate_value_json,
    _json_value_equal,
    _SemanticScoringSet,
    _strict_json_equal,
)
from kiro_crew.vector_memory_runtime.text_scoring import (  # noqa: F401
    _DENSE_SCRIPT_RANGES,
    _MMR_LAMBDA,
    _MMR_MAX_POOL,
    _ROW_STEM_CACHE_SIZE,
    _SEMANTIC_KEYWORD_WEIGHT,
    _SEMANTIC_VECTOR_WEIGHT,
    _STEM_CACHE_SIZE,
    MAX_MEMORY_SEARCH_QUERY,
    _contains_memory_search_text,
    _get_snowball,
    _hybrid_score,
    _is_selective_keyword,
    _jaccard,
    _keyword_score,
    _mmr_rerank,
    _normalize_memory_search_query,
    _row_stem_tokens,
    _row_stem_tokens_for_scan,
    _row_stem_tokens_uncached,
    _snowball_local,
    _stem_one,
    _stem_words,
    _tokenize,
)

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger(__name__)


# ── Optional deps ──

try:
    import numpy as np

    _HAS_NUMPY = True
except ImportError:
    np = None  # type: ignore[assignment]
    _HAS_NUMPY = False

try:
    import faiss

    _HAS_FAISS = True
except ImportError:
    faiss = None  # type: ignore[assignment]
    _HAS_FAISS = False

# ── Constants ──

# One owner for the filename: `memory_stores.resolve_store_path` composes the
# same name for a named store, and two spellings of it would silo a crew's
# vector memory into a file nothing else opens.
_DB_FILE = MEMORY_DB_FILE
_FAISS_FILE = "memory.faiss"


class SemanticRejectCode(str, Enum):
    KEY_FORMAT = "key_format"
    ALLOWLIST = "allowlist_reject"
    RESERVED_PREFIX = "reserved_prefix"
    CONFIDENCE = "low_confidence"
    VALUE_SIZE = "value_size"
    VALUE_EMPTY = "value_empty"
    VALUE_ENCODING = "value_encoding"
    INJECTION = "injection_blocked"
    CONFLICT = "conflict_skip"


class LessonWriteOutcome(str, Enum):
    """What a lesson write actually DID, for callers that must tell the cases apart.

    A bare ``bool`` cannot: ``False`` covers several unrelated things --
    validation refused the value, a dedup rule claimed the write,
    the submit was a genuine no-op, or a bare re-submit deliberately kept the stored
    NOT-clause. The first two mean "your lesson did not land"; the last two mean
    "your lesson is fine, there was nothing to do". A caller that cannot tell them
    apart has to guess, and a caller reading every ``False`` as "the vector store
    did not take it" writes a second record into ``lessons.jsonl``.

    The vocabulary matches :meth:`kiro_crew.learn.LessonStore.save_or_enrich`, which
    returns ``inserted``/``enriched``/``unchanged``, so the two stores
    describe the same events with the same words.
    """

    INSERTED = "inserted"
    ENRICHED = "enriched"
    UNCHANGED = "unchanged"
    DEDUPED = "deduped"
    REFUSED = "refused"


class EpisodicWriteOutcome(str, Enum):
    """What an episodic write DID, for callers that must tell refusals apart.

    ``write_episodic`` returns ``False`` for both refusals. A caller that
    records refused rows as handled (the ops-mission-control ledger cursor) must
    retry an ``AT_CAPACITY`` refusal once space frees, but never a final one, so
    it needs the cause the store decided, not a guess made afterwards.
    """

    WRITTEN = "written"
    #: Treated as final: an active row already has this text (prefix/exact dedup)
    #: or a similar vector (similarity conflict), or the text itself is
    #: unacceptable (length bounds, injection screen). Only the text refusals are
    #: final in fact; a dedup or similarity refusal can change once the colliding
    #: row is tombstoned, and a caller that cursors ``REFUSED`` (the ledger
    #: indexer) accepts losing that later retry.
    REFUSED = "refused"
    #: A merge-only (``preserve_existing``) write found a full V1 store.
    AT_CAPACITY = "at_capacity"


# The two outcomes that changed the store. UNCHANGED is deliberately NOT here: the
# lesson IS stored as submitted, but nothing was written, so a caller asking "did I
# need to do something" gets no, while a caller asking "is my lesson stored" reads
# ``stored`` below.
_LESSON_WROTE_OUTCOMES = frozenset({LessonWriteOutcome.INSERTED, LessonWriteOutcome.ENRICHED})


@dataclass(frozen=True)
class LessonWriteResult:
    """A lesson write's outcome plus the short reason code behind it.

    ``reason`` names WHICH rule produced the outcome -- a
    :class:`SemanticRejectCode` value for ``REFUSED``, the dedup rule's name for
    ``DEDUPED``, and ``kept_stored_clause`` for the one ``UNCHANGED`` case that is
    not a byte-identical re-submit. It is ``None`` when the outcome says everything
    there is to say. Surfaces that report back to a human or a model (the CLI, the
    ``/api/lessons`` response, the ``learn_add`` tool result) need the reason; the
    ones that only branch on success do not.

    ``superseded`` names the stored rules THIS CALL DELETED. Every field above
    describes what happened to the SUBMITTED lesson, and that was the whole
    vocabulary -- so a write that tombstoned somebody else's stored rule reported
    a plain ``inserted`` with ``reason=None``, and the caller was told its lesson
    was saved with nothing naming what the save cost. Supersede-on-dedup is
    deliberate (see :meth:`VectorMemoryStore.write_lesson`, and the docstring's
    "longer wins" / "newer replaces older"), and this field does not change it:
    it only reports which rows that rule deleted. It is
    empty on every path that deleted nothing, so a surface can render it with a
    bare ``if`` and say nothing when there is nothing to say.

    **Truthiness is deliberate, and it is why this type is the whole return value
    rather than something shipped beside a ``bool``.** Three callers plus ~55
    assertions read ``write_lesson``'s answer with a
    bare ``if``/``assert``. Returning any ordinary object would make every one of
    them unconditionally true -- silently, since a bare ``if`` on a truthy value is not
    a type error and mypy cannot flag it. :meth:`__bool__` closes exactly that hole:
    ``bool(result)`` is ``wrote``, byte-for-byte the
    predicate those callers are written against. So there is one method, one
    name, and nothing to migrate to -- a caller that needs the detail reads
    :attr:`outcome`, and a caller that only needs "did this write something" keeps
    using the truth value.
    """

    outcome: LessonWriteOutcome
    reason: str | None = None
    #: Rules this call tombstoned. A tuple, not a list, because the dataclass is
    #: frozen and a mutable default would let a caller edit a write's own record of
    #: what it destroyed. Defaults to empty so the ~60 existing construction sites
    #: -- ``LessonWriteResult(OUTCOME)`` and ``LessonWriteResult(OUTCOME, reason)``
    #: -- are unchanged, and any surface that ignores the field keeps its behaviour.
    superseded: tuple[str, ...] = ()
    #: The tier the STORED row actually carries after this write, which is not the
    #: submitted one on an enrichment: the tier is write-once, so a clause-only
    #: re-submit omitting ``applies`` keeps the persisted value while the submitted
    #: value is ``None``. A caller that guards a DELETION on the tier has to read
    #: this, because the submitted value answers a different question -- gating on
    #: it let an ordinary enrichment of a stored finding skip the guard entirely and
    #: retire a standing rule. ``None`` means the row is unstated, which every read
    #: path serves AS a standing rule. Defaults to ``None`` so the ~60 existing
    #: construction sites are unchanged.
    applies: str | None = None

    def __bool__(self) -> bool:
        """``wrote`` -- the exact predicate the old ``bool`` return answered.

        Preserving it is the whole point: see the class docstring. Do NOT redefine
        this as ``stored``, which would quietly turn a no-op re-submit into a write
        for every caller that branches on the truth value.

        Removing it does NOT redden the wide assertion surface, which is exactly why
        it is easy to lose: without it a result object is truthy by default, so every
        positive ``assert store.write_lesson(...)`` keeps passing while asserting
        nothing at all. Only the negative assertions and the dedicated tests in
        ``TestWriteLessonTruthValueIsTheOldBool`` catch its absence -- verified by
        deleting this method, which left 160 tests green and reddened 5.
        """
        return self.wrote

    @property
    def wrote(self) -> bool:
        """The store changed -- a row was inserted, or an existing row enriched."""
        return self.outcome in _LESSON_WROTE_OUTCOMES

    @property
    def stored(self) -> bool:
        """The lesson is in the store as submitted -- written now, or already there.

        Distinct from :attr:`wrote` (and from the truth value): a no-op re-submit did
        not write anything, yet the caller's lesson is stored, so telling them it
        failed would be false.
        """
        return self.outcome is LessonWriteOutcome.UNCHANGED or self.wrote


_AUDITABLE_REJECT_CODES = {
    SemanticRejectCode.ALLOWLIST,
    SemanticRejectCode.CONFIDENCE,
    SemanticRejectCode.INJECTION,
    SemanticRejectCode.RESERVED_PREFIX,
    SemanticRejectCode.VALUE_EMPTY,
}

_SECURITY_REJECT_CODES = {
    SemanticRejectCode.INJECTION,
    SemanticRejectCode.RESERVED_PREFIX,
}
# Named explicitly rather than derived as "not a security code": ALLOWLIST and CONFIDENCE
# predate the dedupe and get_rejection_stats counts them per attempt.
_AUDIT_ONCE_REJECT_CODES = {
    SemanticRejectCode.VALUE_EMPTY,
}
_MAX_EVENTS = 10_000
# Bound on the warn-once promotion-refusal set. The project.<proj>.tool key form is
# derived from arbitrary episodic text, so the key space is unbounded in principle.
_MAX_PROMOTION_REFUSED = 1_000
# Same bound, same reason, for the audit-once set in log_reject_event.
_MAX_AUDITED_REJECTS = 1_000
_DEFAULT_CONFIDENCE_THRESHOLD = 0.8
_DEFAULT_DEDUP_THRESHOLD = 0.88
_DEFAULT_EPISODIC_MAX = 10_000
_DEFAULT_EPISODIC_LIMIT = 8  # must match MemoryConfig.episodic_max_results default
_EPISODIC_TEXT_MIN = 10
_EPISODIC_TEXT_MAX = 2000
# Episodic recency decay: score factor exp(-rate * days_old), per day. The
# built-in rate applies when memory.decay_rates configures nothing else; the
# reserved "default" key in that mapping replaces it for untagged/unmatched
# rows. Rates outside [_DECAY_RATE_MIN, _DECAY_RATE_MAX] are clamped: 0 means
# a memory never ages out, and by 10/day a single day already scales a score
# by e^-10, so larger values are indistinguishable in ranking.
_DEFAULT_DECAY_RATE = 0.03
_DECAY_RATE_MIN = 0.0
_DECAY_RATE_MAX = 10.0
_DECAY_DEFAULT_KEY = "default"
_FAISS_SAVE_INTERVAL = 100  # save index every N writes
# Ceiling on the resident episodic scoring set (the embedding matrix plus the
# three small scoring columns). Above it the tier falls back to reading the
# population per call: the whole point of holding it is to spend memory to avoid
# that read, and past this size the trade stops being a good one. Sized to cover
# a store at _DEFAULT_EPISODIC_MAX rows at the shipped 1024-d width, so a default
# install is always inside it.
_EPISODIC_SCORING_MAX_BYTES = 64 * 1024 * 1024
# Semantic context needs the rendered keys and values as well as embeddings, so
# its resident snapshot is bounded independently. Above this ceiling retrieval
# preserves the existing per-call SQLite read rather than retaining unbounded
# user data in the gateway process.
_SEMANTIC_SCORING_MAX_BYTES = 64 * 1024 * 1024
# Conservative ceiling on bound parameters in one statement. sqlite's own limit is
# 32,766 on the bundled build but only 999 on hosts still on a pre-3.32 library,
# and there is no cheap way to read it on every supported runtime, so batched id
# lookups chunk at a value both accept.
_MAX_SQL_PARAMS = 500


_BUILTIN_PREFIXES = [
    "pref.*",
    "project.*",
    "user.*",
    "lesson.*",
]

# ── Schema ──

_SCHEMA_V1 = f"""
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS semantic_memory (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    confidence REAL DEFAULT 0.5,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    is_deleted INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_semantic_deleted ON semantic_memory(is_deleted);

CREATE TABLE IF NOT EXISTS episodic_memories (
    id TEXT PRIMARY KEY,
    conversation_id TEXT,
    text TEXT NOT NULL,
    embedding BLOB,
    tags TEXT DEFAULT '[]',
    importance REAL DEFAULT 0.5,
    created_at TEXT NOT NULL,
    last_accessed_at TEXT,
    is_deleted INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_episodic_deleted ON episodic_memories(is_deleted);
CREATE INDEX IF NOT EXISTS idx_episodic_created ON episodic_memories(created_at);
CREATE INDEX IF NOT EXISTS idx_episodic_conversation ON episodic_memories(conversation_id);
{memory_schema.MEMORY_EVENTS_SQL}"""


def _migrate_v2(db: sqlite3.Connection) -> None:
    """Add embedding BLOB column (idempotent; SQLite lacks IF NOT EXISTS for ADD COLUMN)."""
    try:
        db.execute("ALTER TABLE semantic_memory ADD COLUMN embedding BLOB")
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


# Named separately because it is v1's THIRD migration, applied to files that
# already carry v1's first two. The DDL itself is lineage-agnostic and lives with
# ``memory_events`` in ``memory_schema``.
_MEMORY_META_TABLE = memory_schema.MEMORY_META_SQL


_MIGRATIONS: list[tuple[int, str, "Callable[[sqlite3.Connection], None] | None"]] = [
    (1, _SCHEMA_V1, None),
    (2, "", _migrate_v2),
    (3, _MEMORY_META_TABLE, None),
]

_MAX_BACKFILLS_PER_CALL = 5  # cap lazy embedding backfills to bound latency


def _lesson_slug(rule: str) -> str:
    """The key slug write_lesson derives for *rule*. Single source of truth."""
    return hashlib.md5(rule.encode(), usedforsecurity=False).hexdigest()[:12]


def _lesson_key(rule: str, repo_scope: str | None = None) -> str:
    """The semantic key a lesson is stored under.

    An unscoped lesson keys on the rule alone, byte-identical to what it has always
    been, so no stored row moves and the legacy-string reader in ``_split_stored``
    (which confirms a candidate prefix by re-deriving ``_lesson_slug``) keeps
    working -- legacy rows are always unscoped.

    A scoped lesson folds its scope into the digest, because the same rule scoped to
    two repositories is two lessons. Sharing one key would let the second write
    overwrite the first through ``set_semantic`` and silently re-scope it, which is
    worse than the cross-scope superseding this separation prevents.
    """
    if not repo_scope:
        return f"lesson.{_lesson_slug(rule)}"
    # NUL separator so a rule ending in the scope text cannot collide with a
    # differently-split pair. Reuses the one digest helper rather than hashing here.
    basis = f"{rule}\x00{repo_scope}"
    return f"lesson.{_lesson_slug(basis)}"


# ── Helpers ──


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _sanitize_decay_rates(raw: Mapping[str, object] | None) -> dict[str, float]:
    """Validate and clamp user-configured per-tag episodic decay rates.

    The mapping comes from hand-edited config JSON (``memory.decay_rates``), so
    entries are screened rather than trusted: a non-string key or a non-numeric
    or non-finite rate is dropped with a warning (logged once, at store
    construction — retrieval never re-validates per row), and numeric rates are
    clamped to [``_DECAY_RATE_MIN``, ``_DECAY_RATE_MAX``]. Keys are lowercased
    to match the case-insensitive tag matching used by episodic retrieval
    (:meth:`VectorMemoryStore._matches_tags`).
    """
    out: dict[str, float] = {}
    if not raw:
        return out
    if not isinstance(raw, Mapping):
        logger.warning("memory.decay_rates ignored: expected a mapping, got %r", type(raw).__name__)
        return out
    for key, val in raw.items():
        if not isinstance(key, str) or not key.strip():
            logger.warning("memory.decay_rates: ignoring non-string key %r", key)
            continue
        # bool is an int subclass, but true/false is not a rate; NaN/Infinity
        # are parsed by json.loads yet are not usable rates either.
        if (
            isinstance(val, bool)
            or not isinstance(val, (int, float))
            or (isinstance(val, float) and not math.isfinite(val))
        ):
            logger.warning("memory.decay_rates[%r]: ignoring non-numeric rate %r", key, val)
            continue
        # Clamp BEFORE converting to float: JSON admits arbitrary-precision
        # integers, and float() (like math.isfinite()) raises OverflowError past
        # ~1e308 -- crashing store construction on a garbage config value
        # instead of clamping it. int/float comparison is exact in Python, so
        # the clamp itself never overflows.
        out[key.strip().lower()] = float(min(max(val, _DECAY_RATE_MIN), _DECAY_RATE_MAX))
    return out


# ── Store ──


# A whole-population retrieval scan, as opposed to a bounded or single-row read.
# Only the two whole-population surfaces are attributed; everything else lands in
# the all-tables totals.
_ScanSurface = Literal["semantic", "episodic"]


@dataclass
class _ReadCounters:
    """How much this store READ, as monotonic per-instance totals.

    A whole-population scan is invisible from outside the process: a SELECT
    moves neither ``PRAGMA data_version`` nor the WAL, so a second process
    cannot tell one materialized row from a thousand, and wall-clock timing is
    not admissible evidence. These counters are the in-band signal instead, so a
    caller can assert that a second identical search did not re-read the
    population the way ``_EpisodicScoringSet`` already avoids on the
    episodic side.

    Cost is a method call and a few integer adds per SELECT, so counting is
    always on; only the EXPOSURE is a surface decision. Every increment happens
    under ``_db_lock`` (the fetch helpers hold it, and the one direct caller
    increments inside its own locked block), so a snapshot taken under the same
    lock is never torn and no count is lost to a concurrent reader.
    """

    statements_executed: int = 0
    rows_read: int = 0
    semantic_rows_read: int = 0
    semantic_full_scans: int = 0
    episodic_rows_read: int = 0
    episodic_full_scans: int = 0

    def record(self, rows: int, scan: _ScanSurface | None = None) -> None:
        """Credit one materialized SELECT of *rows* rows.

        *scan* marks the read as a whole-population retrieval scan of that
        surface; leaving it None still credits the all-tables totals, which is
        the right answer for a bounded or keyed read.
        """
        self.statements_executed += 1
        self.rows_read += rows
        if scan == "semantic":
            self.semantic_rows_read += rows
            self.semantic_full_scans += 1
        elif scan == "episodic":
            self.episodic_rows_read += rows
            self.episodic_full_scans += 1

    def snapshot(self) -> dict[str, int]:
        """Return the totals as a plain JSON-serializable dict."""
        return asdict(self)


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
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return _member_identity(db)
    finally:
        db.close()


def create_member_database(path: Path, *, member_id: str, store_id: str) -> None:
    """Provision one new database exclusively; never overwrite existing bytes."""
    if not member_id or not store_id or store_id == "default":
        raise ValueError("A member and store identity are required")
    path = Path(path)
    platform_compat.make_owner_only_dir(path.parent)
    # Exclusive creation prevents both clobbering data and concurrent provisioning.
    with path.open("xb"):
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


class VectorMemoryStore:
    """SQLite-backed structured memory with semantic keys and audit trail."""

    def __init__(
        self,
        db_path: Path | None = None,
        confidence_threshold: float = _DEFAULT_CONFIDENCE_THRESHOLD,
        extra_prefixes: list[str] | None = None,
        dedup_threshold: float = _DEFAULT_DEDUP_THRESHOLD,
        episodic_max: int = _DEFAULT_EPISODIC_MAX,
        embedding_dim: int = 1024,
        episodic_limit: int = _DEFAULT_EPISODIC_LIMIT,
        decay_rates: dict[str, float] | None = None,
        config: object | None = None,
    ):
        self._db_path = db_path or (config_dir() / _DB_FILE)
        from kiro_crew.memory_stores import named_store_of_db

        self._memory_store_name = named_store_of_db(self._db_path)
        self._member_identity: tuple[str, str] | None = None
        self._faiss_path = self._db_path.parent / _FAISS_FILE
        self._confidence_threshold = confidence_threshold
        self._dedup_threshold = dedup_threshold
        self._episodic_max = episodic_max
        self._episodic_limit = episodic_limit
        self._embedding_dim = embedding_dim
        # Per-tag episodic recency decay (memory.decay_rates). Sanitized once
        # here — clamped, non-numeric entries warned about and dropped — so the
        # per-row resolver (_decay_rate_for) only ever sees clean floats. The
        # reserved "default" key is split out: it replaces the built-in rate
        # for rows matching no configured tag and never participates in
        # per-tag matching.
        _rates = _sanitize_decay_rates(decay_rates)
        self._decay_default = _rates.pop(_DECAY_DEFAULT_KEY, _DEFAULT_DECAY_RATE)
        self._decay_by_tag = _rates
        self._prefixes = list(_BUILTIN_PREFIXES)
        if extra_prefixes:
            self._prefixes.extend(extra_prefixes)
        self._db: sqlite3.Connection | None = None
        # POSIX permits replacing an open SQLite file. Private V2 stores hold a
        # shared lock outside the swappable store directory for the full
        # connection lifetime so startup restore can refuse rather than move a
        # generation that a CLI process can keep writing. Windows keeps this
        # ``None`` because SQLite's native file handle denies the directory
        # rename and platform_compat has no non-serializing shared lock there.
        self._store_use_lock_fd: int | None = None
        # Which schema lineage this FILE is. v1 is the floor and the only value a
        # store that never calls init() can have, so every derived statement
        # below renders in its v1 spelling until init() proves otherwise.
        # Resolved ONCE there rather than branched at each use, so the runtime
        # write path carries no per-statement conditional.
        self._lineage: str = memory_schema.LINEAGE_V1
        self._bind_lineage(memory_schema.LINEAGE_V1)
        # Serializes the db + FAISS critical sections. Writes are offloaded to
        # worker threads (history consolidation, dashboard handlers) while reads
        # (search_episodic via context assembly) run on the event loop thread, so
        # concurrent access to the shared sqlite connection and the (non-thread-
        # safe) FAISS index / _faiss_id_map must be serialized. Reentrant because
        # locked write sections call helpers (save_faiss_index) that re-acquire.
        # NOTE: never hold this across a blocking embed call — embeds happen
        # before the locked region so the lock only guards local db/FAISS work.
        self._db_lock = threading.RLock()
        # Read-volume totals. Guarded by _db_lock (see _ReadCounters) rather than
        # a lock of their own: every increment already sits inside a locked fetch,
        # so the counting adds no synchronization to the read path.
        self._reads = _ReadCounters()
        # FAISS state
        self._faiss_index: object | None = None  # faiss.IndexFlatIP (untyped)
        self._faiss_id_map: list[str] = []
        self._faiss_writes_since_save = 0
        self._faiss_data_version: int | None = None
        # Resident episodic scoring set for the numpy sqlite tier, plus the
        # in-process half of its validity token. The generation is bumped by
        # every writer that changes which rows are scored or what they score as;
        # it is deliberately NOT gated on _HAS_FAISS, because the backfill
        # rebuilds the FAISS index only when faiss is installed and this tier is
        # precisely the one that runs when it is not.
        self._episodic_scoring: _EpisodicScoringSet | None = None
        self._episodic_scoring_generation = 0
        # Cleared for the store's lifetime when the cross-process half of the
        # token is unavailable (PRAGMA data_version needs sqlite >= 3.9.0 and an
        # older library returns no row rather than erroring). Without it a second
        # process writing the same file would be served stale rows, so the tier
        # keeps reading the population per call instead.
        self._episodic_scoring_supported = True
        # The exact (dim, generation, data_version) state whose build last came
        # back over budget. Memoizing the refusal under the SAME validity tokens
        # as a successful build means an over-budget store pays the population
        # scan once per state change instead of once per search (which would be
        # strictly worse than the pre-cache baseline), while a store that
        # shrinks below the ceiling re-probes as soon as a write bumps the
        # generation or another process moves data_version. A sticky boolean
        # (the `_episodic_scoring_supported` shape) would never re-probe.
        self._episodic_scoring_refused: tuple[int, int, int] | None = None
        # Query-aware semantic context reads a stable, query-independent row
        # population. Keep that population resident only while the same
        # in-process generation and cross-process SQLite version remain current.
        self._semantic_scoring: _SemanticScoringSet | None = None
        self._semantic_scoring_generation = 0
        self._semantic_scoring_supported = True
        self._semantic_scoring_refused: tuple[int, int] | None = None
        # Promotion keys already refused: the refusal is deterministic, so warn once per store
        # per distinct reject cause. Bounded and oldest-first, so an evicted cause may warn
        # once more rather than the set growing for the process lifetime.
        self._promotion_refused: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._audited_rejects: OrderedDict[tuple[str, str], None] = OrderedDict()
        # Optional sync embedding function for migration (set by caller)
        self.embed_fn: Callable[[str], list[float] | None] | None = None
        # Optional factory that builds an embed_fn on demand. When set, _try_embed()
        # will lazily rebind self.embed_fn if it is None — handles the case where
        # the embedding model was unavailable at gateway boot but landed later, without
        # requiring a gateway restart.
        self.embed_fn_factory: Callable[[], Callable[[str], list[float] | None] | None] | None = (
            None
        )
        self._embed_fn_rebind_cooldown_secs: float = 30.0
        self._embed_fn_last_rebind_attempt: float = 0.0
        # Bumped whenever the vector space changes (a live embedding-model swap).
        # _try_embed compares it across the embed call: a vector produced in the
        # OLD space must not be committed after the store has moved on, because
        # reconcile has already swept past that row and backfill only ever
        # revisits NULLs. A plain dim comparison is NOT enough -- two different
        # models of the same width are different spaces.
        self._space_generation = 0
        # Serializes the lazy-rebind block in _try_embed() so the cooldown invariant
        # ("at most one factory call per cooldown window") holds under multi-threaded
        # write load. Without it, two writers can both observe embed_fn is None and
        # cooldown elapsed at the same instant, then both call the factory + probe.
        self._embed_fn_rebind_lock = threading.Lock()
        # id -> time.monotonic() of the last last_accessed_at write for that
        # episodic row. Backs the debounce in _touch_last_accessed; swept when it
        # grows past _LAST_ACCESSED_CACHE_MAX.
        self._last_accessed_touch: dict[str, float] = {}
        # Retrieval tunables above are copies of the memory.* config, so a write to
        # config.json reaches them only through reconfigure(). Registered here (the
        # store is long-lived and read on the retrieval hot path, so point-of-use
        # loading is the wrong trade) and held on self because the watcher holds the
        # owner weakly.
        #
        # A caller building the store from a loaded config passes it as *config*, and
        # it is applied here, BEFORE the subscription exists. Applied after, a reload
        # the watcher delivered in between would be overwritten by the caller's older
        # snapshot (a raised cap put back down, evicting on the next write). A reload
        # the watcher dispatched between the caller's load and the registration never
        # reaches this store at all, so the new subscription is replayed
        # (``ConfigWatch.replay``), which hands it the watcher's adopted config (or
        # queues it for the next tick when the file has moved past that config).
        #
        # Only a store built FROM a config is replayed. A ``config=None`` store (a
        # foreign ``data_home`` such as onboarding import, an eval harness) keeps its
        # constructor defaults at construction; replaying would hand it this
        # install's ``memory.*`` tuning the moment it exists.
        if config is not None:
            self.reconfigure(config)
        self._config_sub = live.watch_object(self, "memory", name="VectorMemoryStore")
        if config is not None:
            live.watch().replay(self._config_sub)

    def reconfigure(self, cfg: object) -> None:
        """Push new ``memory.*`` retrieval settings onto this live store.

        Covers the five thresholds plus the decay table and the semantic-key
        prefixes -- everything this class copies out of config at construction and
        would otherwise hold until a gateway restart. Re-runs the loader's own
        sanitizer (:func:`_sanitize_decay_rates`) rather than copying raw values, so
        a hand-edited rate is clamped and a garbage entry dropped exactly as it is
        at boot.

        Embedding width is deliberately NOT touched: changing it invalidates every
        stored vector, which is a re-embed, not a value swap (see the dashboard's
        embedding-model apply route).
        """
        memory_cfg = getattr(cfg, "memory")
        self._confidence_threshold = float(getattr(memory_cfg, "semantic_confidence_threshold"))
        self._dedup_threshold = float(getattr(memory_cfg, "episodic_dedup_threshold"))
        self._episodic_limit = int(getattr(memory_cfg, "episodic_max_results"))
        self._episodic_max = int(getattr(memory_cfg, "episodic_max_count"))
        rates = _sanitize_decay_rates(getattr(memory_cfg, "decay_rates", None))
        self._decay_default = rates.pop(_DECAY_DEFAULT_KEY, _DEFAULT_DECAY_RATE)
        self._decay_by_tag = rates
        prefixes = list(_BUILTIN_PREFIXES)
        extra = getattr(memory_cfg, "semantic_keys", None)
        if extra:
            prefixes.extend(extra)
        self._prefixes = prefixes
        # The resident episodic scoring set carries the decay rates it was built
        # with, so a rate change makes it stale even though no row moved.
        self._invalidate_episodic_scoring()

    def _secret_bearing_files(self) -> tuple[Path, ...]:
        """Every file beside the DB that carries the user's memories.

        All of them, not just the DB, because on Windows the owner-only DIRECTORY is
        not sufficient for a file that already exists: **Bypass Traverse Checking** is
        granted to Everyone by default, so a permissive DACL on the file itself stays
        reachable even inside a tightened directory. The directory governs what SQLite
        and FAISS create from now on; this list is what repairs an existing install.

        - ``-wal`` / ``-shm``: a COMMITTED row lives in the ``-wal`` until a
          checkpoint moves it. Same suffix set ``memory.py`` uses to drop a corrupt
          index.
        - ``memory.faiss`` / ``memory.ids.json``: the embedding index and its
          id map, written with no lockdown of their own.

        Not exhaustive for the data home as a whole -- ``memory.py``'s FTS index
        (``memory_index.db``) and its sidecars carry the same secrets; ``memory.py``
        restricts those itself in ``MemoryStore._restrict_index_files``.
        """
        return (
            Path(f"{self._db_path}-wal"),
            Path(f"{self._db_path}-shm"),
            self._faiss_path,
            self._faiss_path.with_suffix(".ids.json"),
        )

    def _restrict_memory_files(self) -> None:
        """Make every memory-bearing file that exists owner-only.

        Called TWICE by :meth:`init` -- once before the connect and once after -- and
        the ordering is the point of the first call. The owner-only directory does not
        cover a file that already EXISTS on Windows, because Bypass Traverse Checking
        is granted to Everyone by default, so a permissive DACL on the file itself
        stays reachable inside a tightened directory. Restricting before
        ``sqlite3.connect`` means the migrations do not run against a file another
        local user can still write; restricting again after covers whatever SQLite
        just created.

        Missing files are skipped BY AN EXISTENCE CHECK, not by catching the failure:
        on Windows ``restrict_to_owner`` raises plain ``OSError`` for a missing path
        (the in-process DACL write's failure is translated to ``OSError``) rather
        than ``FileNotFoundError``, which only ever comes from the POSIX
        ``os.chmod``. Catching alone would log a false "may be readable by other
        users" warning for each missing file, twice per init. The race between
        the check and the call is benign: a file that appears in between is created by
        SQLite or FAISS inside the already-tightened directory, so it inherits
        owner-only access on both platforms and the next init covers it regardless.

        Any other failure warns rather than raising -- memory being unavailable is a
        supported degraded state, and ``restrict_to_owner`` documents this
        warn-and-continue handler as its caller contract.
        """
        for path in (self._db_path, *self._secret_bearing_files()):
            if not path.exists():
                continue  # SQLite and FAISS create theirs on demand
            try:
                platform_compat.restrict_to_owner(path)
            except OSError:
                logger.warning(
                    "Cannot restrict %s to owner; it may be readable by other users",
                    path,
                    exc_info=True,
                )

    def _bind_lineage(self, lineage: str) -> None:
        """Set the lineage and every statement fragment derived from it.

        ONE derivation, called from both ``__init__`` (the v1 floor) and ``init()`` (the
        detected answer). Written twice, the two omissions are not symmetric: a new
        attribute missing from ``__init__`` is an ``AttributeError``, but one missing
        from ``init()`` leaves its V1 SPELLING on a crew file — and while a wrong
        RELATION raises "cannot modify a view", a wrong GUARD raises nothing at all and
        simply reaches rows of the other kind.
        """
        self._lineage = lineage
        self._sem_rel = memory_schema.semantic_relation(lineage)
        self._epi_rel = memory_schema.episodic_relation(lineage)
        self._sem_guard = memory_schema.semantic_guard(lineage)
        self._epi_guard = memory_schema.episodic_guard(lineage)

    @property
    def algorithm_version(self) -> str:
        """Global and unowned legacy files never opt in to member algorithms."""
        return "v2" if getattr(self, "_memory_version", 1) == 2 else "v1"

    @property
    def policy_revision(self) -> str:
        return memory_v2.ALGORITHM_VERSION if self.algorithm_version == "v2" else "v1"

    def _write_history(self, day: str, content: str) -> None:
        """Publish current history and its search projection; caller owns transaction."""
        from kiro_crew.hooks import FileTooLargeError
        from kiro_crew.memory import MemoryStore

        max_bytes = MemoryStore._HISTORY_SNAPSHOT_MAX_BYTES
        if len(content.encode("utf-8")) > max_bytes:
            raise FileTooLargeError(f"Member history exceeds the {max_bytes}-byte write limit")
        with self._db_lock:
            row = self.db.execute(
                "SELECT revision FROM memory_history WHERE day=?", (day,)
            ).fetchone()
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

    def _append_history(self, entry: str) -> None:
        with self._db_lock:
            now = datetime.now().astimezone()
            day = now.date().isoformat()
            row = self.db.execute(
                "SELECT content FROM memory_history WHERE day=?", (day,)
            ).fetchone()
            content = row[0] if row else f"# {day}\n"
            content += f"\n#### {now.strftime('%H:%M %Z')}\n{entry.strip()}\n"
            self._write_history(day, content)

    def append_history(self, entry: str) -> None:
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
        self, *, since: str | None = None, limit: int = 366, max_bytes: int = 8 * 1024 * 1024
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
                    raise FileTooLargeError(
                        f"Member history exceeds the {max_bytes}-byte read limit"
                    )
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

    def _read_editable_history_for_day(self, day: str) -> str:
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

    def read_editable_history(self) -> str:
        day = datetime.now().astimezone().date().isoformat()
        with self._db_lock:
            return self._read_editable_history_for_day(day)

    def replace_today_history(
        self, content: str, *, expected_baseline: str, validate_current: Callable[[str], None]
    ) -> bool:
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

    def search_memory(self, query: str, *, limit: int = 5) -> list[dict]:
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

    def rebuild_memory_index(self) -> int:
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

    def consolidation_receipt(self, source_id: str) -> dict | None:
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
        self,
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
                            json.dumps(
                                [tag.strip().lower()[:50] for tag in tags[:10] if tag.strip()]
                            ),
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

    @memory_stores.named_store_operation
    def init(self) -> None:
        """Open under namespace admission, then hold the named generation until close."""
        from kiro_crew.memory_startup import require_memory_ready

        require_memory_ready(self._memory_store_name)
        if self._memory_store_name and self._store_use_lock_fd is None:
            from kiro_crew import member_memory_backup

            # Named V1 and V2 stores are both replaced as whole directories.
            # Take admission before opening SQLite so restore cannot replace a live handle.
            self._store_use_lock_fd = member_memory_backup.acquire_store_use_lock(self._db_path)
        # A repeated init() replaces the handle. Close the one it replaces, or the
        # orphan keeps its descriptors until the cyclic collector runs: on CPython
        # 3.11+ an unclosed sqlite3.Connection is a reference cycle (its statement
        # cache is an lru_cache wrapping the connection), so refcounting never
        # frees it.
        with self._db_lock:
            if self._db is not None:
                self._db.close()
                self._db = None
                # The validity token is per-connection (data_version), so a
                # fresh connection's baseline can equal a retained snapshot's;
                # drop both resident scoring sets on the swap or a stale one
                # survives the reconnect. Matches close()'s clears.
                self._episodic_scoring = None
                self._episodic_scoring_refused = None
                self._semantic_scoring = None
                self._semantic_scoring_refused = None
        try:
            self._init_database()
        except BaseException:
            try:
                self.close()
            except Exception:
                logger.warning(
                    "Failed to close a partially initialized memory store", exc_info=True
                )
            raise

    def _init_database(self) -> None:
        """Create DB, apply migrations, and set permissions under admission."""
        from kiro_crew.memory_startup import require_memory_ready

        require_memory_ready(self._memory_store_name)
        expected = getattr(self, "_member_identity", None)
        if expected is not None:
            # mode=rw is intentional: admission never provisions a missing file.
            self._db = sqlite3.connect(
                self._db_path.resolve().as_uri() + "?mode=rw",
                uri=True,
                check_same_thread=False,
            )
            self._db.row_factory = sqlite3.Row
            if _member_identity(self._db) != expected:
                raise ValueError("Member memory database identity does not match admission")
            self._bind_lineage(memory_schema.LINEAGE_CREW)
            self._memory_version = 2
            self._db.execute("PRAGMA busy_timeout=5000")
            return
        # Private files must never enter the legacy schema/migration path.
        if self._db_path.exists():
            probe = sqlite3.connect(self._db_path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                if probe.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name='member_database'"
                ).fetchone():
                    raise ValueError("Private memory requires canonical member admission")
            finally:
                probe.close()
        if self._memory_store_name:
            from kiro_crew.config.loader import KiroCrewConfig

            declaration = KiroCrewConfig.load().memory_stores.get(self._memory_store_name)
            if declaration is not None and declaration.memory_version == 2:
                raise ValueError(
                    "Private memory requires explicit provisioning and member admission"
                )
        # Owner-only lockdown, in two halves. This directory call covers everything
        # SQLite and FAISS create from here on -- inheritable on Windows, because
        # `make_owner_only_dir` routes through `restrict_dir_to_owner`. The per-file
        # pass below repairs what already EXISTS, which a tightened parent cannot do:
        # Windows grants *Bypass Traverse Checking* to Everyone by default, so a
        # pre-lockdown file stays reachable through it. Full reasoning -- the sidecar
        # file set, the every-init rationale, the Windows lockdown cost, the fail-soft
        # contract -- lives in docs/guides/windows-install.md, "The memory store".
        #
        # SCOPE: this covers the DB's own directory and nothing above it. With the
        # default `db_path` that directory happens to BE the data home
        # (`config_dir()`); with a named memory store it is that store's own
        # directory under `memory_stores/`. The whole-home guarantee does not rest
        # on which of those it is -- `config.paths.ensure_data_home` tightens the
        # home where the home is established, so a home whose crews all use named
        # stores is covered too.
        platform_compat.make_owner_only_dir(self._db_path.parent)
        # BEFORE the connect so the migrations do not run against a file another
        # local user can still write; repeated after it to cover what SQLite created.
        self._restrict_memory_files()
        self._db = sqlite3.connect(
            str(self._db_path), check_same_thread=False, isolation_level=None
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        # synchronous stays at the sqlite default (FULL). NORMAL would drop the
        # per-commit fsync, but under WAL that only survives a process crash --
        # an OS crash or power loss can still lose the unsynced WAL tail, and
        # here that tail is acknowledged semantic memories, lessons and episodic
        # rows. The write-volume problem it was meant to address is handled by
        # debouncing the last_accessed_at touch instead, which removes the
        # commits rather than weakening the ones that remain.
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.isolation_level = ""  # Restore implicit transaction handling

        # Apply migrations
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        self._db.commit()
        applied = {
            row[0] for row in self._db.execute("SELECT version FROM schema_version").fetchall()
        }
        # WHICH lineage, decided here and nowhere else. This is the single gate:
        # nine call sites construct a store, one of which (security.scan_memory)
        # sits outside the memory subsystem entirely and reaches the default store
        # as a bare VectorMemoryStore(), so a per-call-site check could not cover it.
        #
        # A file that already holds product tables answers itself, so no path is
        # consulted for it and the operator's running memory.db cannot take the
        # crew branch however the predicate below is later edited. Only a file
        # with no product tables asks where it lives.
        detected = memory_schema.detect_lineage(self._db)
        self._bind_lineage(detected or memory_schema.lineage_for_new_file(self._db_path))
        named_store = memory_stores.named_store_of_db(self._db_path)
        self._memory_version = 1
        migrations = (
            memory_schema.MIGRATIONS_CREW
            if self._lineage == memory_schema.LINEAGE_CREW
            else _MIGRATIONS
        )
        for ver, sql, fn in migrations:
            if ver not in applied:
                if sql:
                    self._db.executescript(sql)
                if fn:
                    fn(self._db)
                self._db.execute(
                    "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (ver, _now_iso()),
                )
                self._db.commit()
                logger.info("Applied memory schema migration v%s", ver)

        # Additive revision metadata is shared by both lineages. It never changes
        # their physical rows or version series; old binaries remain readable.
        with self._db:
            record_meta.ensure_schema(self._db)
            record_meta.reconcile(self._db)
            if self.algorithm_version == "v1":
                record_meta.limit_v1_accepted_history(self._db)

        if self._lineage == memory_schema.LINEAGE_CREW:
            if self._read_meta(memory_schema.LINEAGE_META_KEY) != self._lineage:
                self._write_meta(memory_schema.LINEAGE_META_KEY, self._lineage)
            if named_store and self._read_meta(memory_schema.STORE_NAME_META_KEY) is None:
                self._write_meta(memory_schema.STORE_NAME_META_KEY, named_store)

        # Second pass, after the connect: covers what SQLite has just created. Runs
        # on EVERY init, not only when init created the files -- an existing DB is
        # exactly the one that may have lost its protection since (restored backup,
        # home migration, manual edit, or an install predating this lockdown).
        # ``restrict_to_owner`` rather than ``chmod_safe``, which is a documented
        # no-op on Windows. Cost, file set and fail-soft contract:
        # docs/guides/windows-install.md, "The memory store".
        #
        # CALLER CONTRACT: an async caller must offload this. ``init()`` is
        # blocking end to end — the sqlite connect, the schema migrations, and
        # this lockdown pass (in-process on Windows since the advapi32
        # conversion, but still filesystem work that can stall on a slow
        # volume) — so calling it directly on an event loop stalls every task.
        # All async callers offload: ``eval.runner``, ``slack.gateway`` and
        # ``cli_server._run_task`` via ``asyncio.to_thread``;
        # ``dashboard/handlers/memory.py``'s standalone fallback routes
        # through ``_get_vector_store_async``, which offloads the init-bearing
        # path.
        self._restrict_memory_files()

        # Load persisted FAISS index (or rebuild from SQLite embeddings)
        try:
            self.load_faiss_index()
        except Exception:
            logger.warning(
                "FAISS index not loaded (faiss-cpu may not be installed yet)", exc_info=True
            )

    def close(self) -> None:
        with self._db_lock:
            try:
                if self._db:
                    self._db.close()
                    self._db = None
                self._space_generation += 1
                self._faiss_index = None
                self._faiss_id_map.clear()
                self._faiss_data_version = None
                self._episodic_scoring = None
                self._episodic_scoring_refused = None
                # Clear the semantic twin too: init() can swap the connection on
                # a live object in a long-lived process, and the validity token
                # is per-connection, so a fresh connection's baseline
                # data_version can equal a retained snapshot's — the snapshot
                # must not survive a connection swap.
                self._semantic_scoring = None
                self._semantic_scoring_refused = None
            finally:
                self._release_store_use_lock()

    def _release_store_use_lock(self) -> None:
        fd, self._store_use_lock_fd = self._store_use_lock_fd, None
        if fd is not None:
            from kiro_crew.member_memory_backup import release_store_use_lock

            release_store_use_lock(fd)

    @property
    def db(self) -> sqlite3.Connection:
        from kiro_crew.memory_startup import require_memory_ready

        require_memory_ready(self._memory_store_name)
        if self._db is None:
            raise RuntimeError("VectorMemoryStore not initialized — call init() first")
        return self._db

    # ── Locked fetch helpers ──
    #
    # The single ``check_same_thread=False`` connection is shared across the
    # event loop, executor threads (context assembly via run_in_embed_pool) and
    # worker threads (consolidation, dashboard handlers). sqlite3 caches
    # prepared statements per connection, so an unsynchronized statement racing
    # another thread's implicit transaction corrupts the statement cache —
    # observed in production as sqlite3.InterfaceError ("bad parameter or other
    # API misuse") and DatabaseError ("another row available") — or silently
    # corrupts row iteration. EVERY statement on ``self.db`` must therefore be
    # serialized on ``_db_lock`` (enforced by an AST guard in
    # test_vector_memory.py). Route plain SELECTs through these helpers; only
    # read-modify-write sections that must be atomic should take the lock
    # explicitly. Both helpers materialize results before releasing the lock,
    # so callers never iterate a live cursor unlocked — and per the lock's
    # contract, never call a blocking embed while holding it.

    def _fetch_all_locked(
        self,
        sql: str,
        params: Sequence[object] = (),
        *,
        scan: _ScanSurface | None = None,
    ) -> list[sqlite3.Row]:
        """Run a SELECT serialized on ``_db_lock``; return materialized rows.

        Pass *scan* at the few call sites that read a whole population, so the
        read-volume counters can attribute it to that surface (see
        :class:`_ReadCounters`). The default leaves the read in the all-tables
        totals only, which is correct for a bounded or keyed fetch.
        """
        with self._db_lock:
            rows = self.db.execute(sql, params).fetchall()
            self._reads.record(len(rows), scan)
            return rows

    def _fetch_one_locked(self, sql: str, params: Sequence[object] = ()) -> sqlite3.Row | None:
        """Run a SELECT serialized on ``_db_lock``; return the first row or None."""
        with self._db_lock:
            row = self.db.execute(sql, params).fetchone()
            self._reads.record(1 if row is not None else 0)
            return row

    def read_counters(self) -> dict[str, int]:
        """Return this store's monotonic read-volume totals.

        Per store INSTANCE and per process: the counts start at zero on
        construction, only ever rise, and are not persisted, so two processes
        over one database file report their own reads independently. See
        :class:`_ReadCounters` for what each key counts.
        """
        with self._db_lock:
            return self._reads.snapshot()

    # ── Key Validation ──

    def _validate_key(self, key: str) -> str | None:
        """Validate key format. Returns error message or None if valid."""
        return _semantic.validate_key(self, key)

    def _matches_allowlist(self, key: str) -> bool:
        """Check if key matches any allowlisted prefix."""
        return _semantic.matches_allowlist(self, key)

    def validate_semantic(
        self,
        key: str,
        value: object,
        confidence: float,
        source: str,
        *,
        value_json: str | None = None,
    ) -> tuple[SemanticRejectCode, str] | None:
        """Pre-flight check for set_semantic. Returns (code, message) or None."""
        return _semantic.validate_semantic(
            self, key, value, confidence, source, value_json=value_json
        )

    def log_reject_event(
        self,
        code: SemanticRejectCode,
        key: str,
        value: object,
        source: str,
        *,
        value_json: str | None = None,
    ) -> None:
        """Emit an audit event for a validation rejection."""
        return _semantic.log_reject_event(self, code, key, value, source, value_json=value_json)

    # ── Semantic CRUD ──

    def get_semantic(self, key: str) -> dict | None:
        """Get a single semantic memory entry by key."""
        if self.algorithm_version == "v2":
            row = self._fetch_one_locked(
                f"SELECT * FROM {self._sem_rel} WHERE key = ? AND is_deleted = 0"
                f"{self._sem_guard}",
                (key,),
            )
            return dict(row) if row else None
        row = self._fetch_one_locked(
            "SELECT * FROM semantic_memory WHERE key = ? AND is_deleted = 0", (key,)
        )
        return dict(row) if row else None

    def get_all_semantic(
        self, limit: int | None = None, offset: int = 0, *, q: str = ""
    ) -> list[dict]:
        """Get active semantic memory entries."""
        return _semantic.get_all_semantic(self, limit, offset, q=q)

    def _stamp_facets(self, item_id: str, facets: "memory_schema.MemoryFacets | None") -> None:
        """Stamp the carve axes on a crew row. A no-op on the v1 lineage."""
        return _semantic.stamp_facets(self, item_id, facets)

    def embed_semantic(self, key: str, value: object) -> list[float] | None:
        """Embed a semantic value before entering a caller-owned publication lock."""
        return _semantic.embed_semantic(self, key, value)

    def embed_semantic_retirement(self, key: str, value_json: str) -> list[float] | None:
        """Resolve V1 supersession similarity before a caller-owned publication lock."""
        return _semantic.embed_semantic_retirement(self, key, value_json)

    @timed("vector", "write")
    def set_semantic(
        self,
        key: str,
        value: object,
        confidence: float,
        source: str,
        *,
        facets: "memory_schema.MemoryFacets | None" = None,
        metadata: dict | None = None,
        expected_revision: int | None = None,
        correction: record_meta.CorrectionEvidence | None = None,
        defer_embedding: bool = False,
        embedding: list[float] | None = None,
        embedding_resolved: bool = False,
        embedding_generation: int | None = None,
        retirement_embedding: list[float] | None = None,
        retirement_embedding_resolved: bool = False,
        retirement_value_json: str | None = None,
    ) -> tuple[SemanticRejectCode, str] | None:
        """Write a semantic memory entry with full validation pipeline."""
        return _semantic.write_value(
            self,
            key,
            value,
            confidence,
            source,
            facets=facets,
            metadata=metadata,
            expected_revision=expected_revision,
            correction=correction,
            defer_embedding=defer_embedding,
            embedding=embedding,
            embedding_resolved=embedding_resolved,
            embedding_generation=embedding_generation,
            retirement_embedding=retirement_embedding,
            retirement_embedding_resolved=retirement_embedding_resolved,
            retirement_value_json=retirement_value_json,
        )

    def set_semantic_if_absent(
        self,
        key: str,
        value: object,
        confidence: float,
        source: str,
        *,
        facets: "memory_schema.MemoryFacets | None" = None,
    ) -> str:
        """Insert a semantic value without replacing a concurrent native write."""
        return _semantic.insert_if_absent(self, key, value, confidence, source, facets=facets)

    def seed_item_if_absent(
        self,
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
                value = (
                    json.loads(str(item["value_json"])) if "value_json" in item else item["value"]
                )
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
            importance = (
                float(raw_importance) if isinstance(raw_importance, (int, float, str)) else 0.5
            )
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

    def with_record_metadata(self, rows: list[dict]) -> list[dict]:
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

    def _ineligible_ids(self, record_ids: list[str] | None = None) -> set[str]:
        """Filter before rank/cap; validity is evaluated again on every recall."""
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
                if not chunk:
                    continue
                placeholders = ",".join("?" * len(chunk))
                rows.extend(
                    self._fetch_all_locked(
                        "SELECT record_id, status, valid_from, valid_until "
                        f"FROM memory_record_meta WHERE record_id IN ({placeholders}) "
                        f"AND ({predicate})",
                        tuple(chunk),
                    )
                )
        return {row["record_id"] for row in rows if not record_meta.eligible(dict(row))}

    def _eligible_rows(self, rows, kind: str) -> list:
        blocked = self._ineligible_ids()
        if not blocked:
            return list(rows)
        return [
            row
            for row in rows
            if record_meta.record_id_for(kind, row["id"] if kind == "episode" else row["key"])
            not in blocked
        ]

    def _record_mutation(
        self,
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
            row = self.db.execute(
                f"SELECT * FROM {relation} WHERE {column}=?", (raw_id,)
            ).fetchone()
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

    def _write_semantic(
        self,
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
                            reason = "Existing entry set by user cannot be overwritten by automated source"
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
                if expected_revision is not None and expected_revision != current.get(
                    "revision", 0
                ):
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
                            if not memory_v2.superseded_value_is_asserted(
                                row["text"], key, old_text
                            ):
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

    def propose_semantic_delete(self, key: str, source: str) -> bool:
        """An inferred deletion is a review proposal, never owner authorization."""
        return _semantic.propose_delete(self, key, source)

    def delete_semantic(
        self,
        key: str,
        source: str,
        *,
        expect_value_json: str | None = None,
        superseded_by: str | None = None,
        supersede_reason: str | None = None,
    ) -> bool:
        """Tombstone a semantic memory entry with its full prior revision."""
        return _semantic.delete_semantic(
            self,
            key,
            source,
            expect_value_json=expect_value_json,
            superseded_by=superseded_by,
            supersede_reason=supersede_reason,
        )

    def _retire_one_episodic(self, mem_id: str, text: str, superseded_by: str) -> None:
        """Tombstone one episode as superseded, recording enough to undo it."""
        return _retirement.retire_one_episodic(self, mem_id, text, superseded_by)

    def _retire_stale_episodic(
        self,
        key: str,
        old_value: str,
        *,
        defer_embedding: bool = False,
        query_embedding: list[float] | None = None,
        embedding_resolved: bool = False,
    ) -> None:
        """V1 keeps its original heuristic; member V2 requires literal evidence."""
        return _retirement.retire_stale_episodic(
            self,
            key,
            old_value,
            defer_embedding=defer_embedding,
            query_embedding=query_embedding,
            embedding_resolved=embedding_resolved,
        )

    def _retire_stale_episodic_v1(
        self,
        key: str,
        old_value: str,
        *,
        defer_embedding: bool = False,
        query_embedding: list[float] | None = None,
        embedding_resolved: bool = False,
    ) -> None:
        """Soft-delete episodic entries that reference a superseded semantic value."""
        return _retirement.retire_stale_episodic_v1(
            self,
            key,
            old_value,
            defer_embedding=defer_embedding,
            query_embedding=query_embedding,
            embedding_resolved=embedding_resolved,
        )

    @timed("vector", "search")
    def search_semantic(self, prefix: str) -> list[dict]:
        """Search semantic memory by key prefix."""
        return _semantic.search_semantic(self, prefix)

    # ── Context Injection ──

    def _fact_identities(self) -> dict[str, dict]:
        """Explicit entity/attribute terms expand sparse keys without fuzzy merging."""
        return _semantic.fact_identities(self)

    @staticmethod
    def _fact_label(row: dict) -> str:
        return _semantic.fact_label(row)

    def _semantic_candidates_v1(
        self, query_text: str, *, recall_query: _RecallQuery | None = None
    ) -> list[dict]:
        """The existing V1 hybrid policy, exposed to explicit bounded recall."""
        return _semantic.semantic_candidates_v1(self, query_text, recall_query=recall_query)

    def get_preferences_context(self, query_text: str = "", cap: int = 0) -> str:
        """Read stable pref.* records without searching facts or embedding a query."""
        return _semantic.get_preferences_context(self, query_text, cap)

    def get_semantic_context(
        self, query_text: str = "", cap: int = 1500, *, facts_only: bool = False
    ) -> str:
        """Format semantic memory for prompt injection with hybrid retrieval."""
        return _semantic.get_semantic_context(self, query_text, cap, facts_only=facts_only)

    def _semantic_candidates_v2(
        self, query_text: str, *, recall_query: _RecallQuery | None = None
    ) -> list[dict]:
        """Keep member preferences; retrieve facts only with relevant evidence."""
        return _semantic.semantic_candidates_v2(self, query_text, recall_query=recall_query)

    # ── Event Log ──

    def _log_event(
        self,
        event_type: str,
        memory_type: str,
        key: str,
        old_value: str | None,
        new_value: str | None,
        source: str,
    ) -> None:
        """Append to the audit trail."""
        try:
            # Every write path funnels through here, from both locked and
            # unlocked callers, so serialize on the (reentrant) _db_lock: an
            # unsynchronized INSERT races a concurrent writer's implicit BEGIN.
            with self._db_lock:
                self.db.execute(
                    "INSERT INTO memory_events (event_type, memory_type, memory_key, "
                    "old_value, new_value, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (event_type, memory_type, key, old_value, new_value, source, _now_iso()),
                )
                self.db.commit()
        except Exception:
            logger.debug("Failed to log memory event", exc_info=True)

    def get_events(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """Return recent memory events with pagination."""
        rows = self._fetch_all_locked(
            "SELECT * FROM memory_events ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [dict(r) for r in rows]

    def rotate_events(self, max_rows: int = _MAX_EVENTS) -> int:
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

    # ── FAISS Index ──

    def invalidate_episode_content(self) -> None:
        """Drop derived vectors after a content edit; SQLite stays authoritative."""
        return _faiss_index.invalidate_episode_content(self)

    def invalidate_semantic_content(self) -> None:
        """Drop the resident semantic scoring set after a content edit.

        Call after an edit transaction commits a change to `fact`/`directive`
        (`semantic_memory`) rows on this store's own connection. An own-connection
        commit does not move ``PRAGMA data_version`` and an external edit path does
        not bump the write-time generation, so without this the resident snapshot
        keeps ranking a tombstoned or stale row into context until an unrelated
        local write or a restart. The twin of :meth:`invalidate_episode_content`.
        """
        self._invalidate_semantic_scoring()

    def _faiss_content_signature(self) -> str:
        return _faiss_index.faiss_content_signature(self)

    def build_faiss_index(self) -> int:
        """Rebuild FAISS index from all episodic embeddings in SQLite. Returns count."""
        return _faiss_index.build_faiss_index(self)

    def save_faiss_index(self) -> None:
        """Save a SQLite-derived snapshot and stamp both files in the same epoch."""
        return _faiss_index.save_faiss_index(self)

    def load_faiss_index(self) -> bool:
        """Load FAISS index from disk. Returns True if loaded, False if rebuilt."""
        return _faiss_index.load_faiss_index(self)

    # ── Episodic CRUD ──

    def embed_episodic(self, text: str) -> list[float] | None:
        """Embed episodic *text* before entering a caller-owned write lock."""
        return self._try_embed(text) if self.embed_fn else None

    def write_episodic(
        self,
        text: str,
        embedding: list[float] | None = None,
        conversation_id: str = "",
        tags: list[str] | None = None,
        importance: float = 0.5,
        source: str = "consolidation",
        *,
        preserve_existing: bool = False,
        defer_embedding: bool = False,
        embedding_resolved: bool = False,
        embedding_generation: int | None = None,
        facets: "memory_schema.MemoryFacets | None" = None,
        metadata: dict | None = None,
    ) -> bool:
        """Write an episodic memory; ``True`` only if a row was written.

        See :meth:`write_episodic_outcome`, which this wraps, for the arguments
        and for WHY a write was refused.
        """
        return (
            self.write_episodic_outcome(
                text,
                embedding,
                conversation_id,
                tags,
                importance,
                source,
                preserve_existing=preserve_existing,
                defer_embedding=defer_embedding,
                embedding_resolved=embedding_resolved,
                embedding_generation=embedding_generation,
                facets=facets,
                metadata=metadata,
            )
            is EpisodicWriteOutcome.WRITTEN
        )

    def write_episodic_outcome(
        self,
        text: str,
        embedding: list[float] | None = None,
        conversation_id: str = "",
        tags: list[str] | None = None,
        importance: float = 0.5,
        source: str = "consolidation",
        *,
        preserve_existing: bool = False,
        defer_embedding: bool = False,
        embedding_resolved: bool = False,
        embedding_generation: int | None = None,
        facets: "memory_schema.MemoryFacets | None" = None,
        metadata: dict | None = None,
    ) -> "EpisodicWriteOutcome":
        """Write an episodic memory with optional embedding and dedup.

        Returns what the write DID. The capacity refusal is decided inside the
        same ``BEGIN IMMEDIATE`` transaction that would have inserted the row, so
        :attr:`EpisodicWriteOutcome.AT_CAPACITY` is the store's own verdict at
        refusal time -- a caller must not re-derive it from a later count, which a
        concurrent cap raise or eviction can change in between.

        *facets* stamps the crew lineage's carve axes and is ignored on v1. An
        episode is the kind that most needs them: it is delivered ONLY by
        per-prompt similarity search, so the carve is the only thing that can
        bound which episodes a query is even allowed to surface.

        ``preserve_existing`` rejects similarity and capacity conflicts instead
        of tombstoning an active entry. Import paths use it to remain merge-only,
        and so does any writer that passes ``defer_embedding``: with no vector the
        similarity dedup below cannot run, and a row admitted without it must not
        evict one it was never compared against.

        ``defer_embedding`` stores the row with a NULL embedding instead of
        embedding inline, leaving it for :meth:`backfill_missing_embeddings`.
        ``embedding_resolved`` says the caller already attempted inference and
        supplied its result, including ``None``; this method then performs no
        inference of its own. Such a caller also supplies ``embedding_generation``
        from :attr:`space_generation` before inference, so a model swap in the gap
        leaves the vector NULL. Inference cost grows steeply with text length (~0.4s per 2000-char chunk
        on CPU), so a bulk writer such as the onboarding importer would hold its
        caller for minutes. The row is FTS5 keyword-searchable immediately, and
        becomes semantically searchable once the sweep fills it in. Only for
        callers that schedule that sweep — a row left NULL forever is silently
        absent from vector search. Deferral also skips the similarity dedup
        (which needs a vector), so the caller keeps its own duplicate check.
        """
        text = text.strip()
        metadata = record_meta.normalize_metadata(metadata) if metadata is not None else None
        if facets and facets.derived_from:
            metadata = {**(metadata or {}), "source_ref": facets.derived_from}
        if len(text) < _EPISODIC_TEXT_MIN or len(text) > _EPISODIC_TEXT_MAX:
            logger.debug(
                "Episodic rejected: len=%d (min=%d max=%d)",
                len(text),
                _EPISODIC_TEXT_MIN,
                _EPISODIC_TEXT_MAX,
            )
            return EpisodicWriteOutcome.REFUSED

        # Prompt-injection screening (XPIA defense-in-depth).
        # Episodic text is derived from conversation transcripts, so a poisoned
        # turn could persist steering instructions that get re-injected into
        # future contexts. Mirror the semantic-KV screen (validate_semantic) and
        # drop the entry on match, emitting an auditable reject event.
        if _contains_injection(text):
            logger.warning("Episodic write rejected: blocked content patterns (src=%s)", source)
            # Scrub untrusted rejected content before persisting its audit snippet.
            # The dashboard also redacts all memory events before returning them.
            safe_snippet = redact_and_truncate(text, 200)
            self._log_event(
                SemanticRejectCode.INJECTION.value,
                "episodic",
                "",
                None,
                safe_snippet,
                source,
            )
            return EpisodicWriteOutcome.REFUSED

        clean_tags = [t.strip().lower()[:50] for t in (tags or [])[:10] if t.strip()]
        importance = max(0.0, min(1.0, importance))

        # Text-hash dedup: reject near-identical text before expensive embedding.
        # The store shares one SQLite connection across worker threads, so even
        # this read must use the same lock as the write-side double-check.
        text_prefix = text if self.algorithm_version == "v2" else text[:80].lower()
        dedup_predicate = (
            "text = ?" if self.algorithm_version == "v2" else "LOWER(SUBSTR(text, 1, 80)) = ?"
        )
        with self._db_lock:
            existing = self.db.execute(
                "SELECT id FROM episodic_memories WHERE is_deleted = 0 " f"AND {dedup_predicate}",
                (text_prefix,),
            ).fetchone()
        if existing:
            logger.debug("Episodic text-hash dedup: prefix matches id=%s", existing["id"])
            return EpisodicWriteOutcome.REFUSED

        # Auto-embed if no embedding provided and embed_fn available.
        #
        # `embed_generation` records which vector space the embedding below belongs
        # to. _try_embed already discards a vector produced ACROSS a space change,
        # but it returns before this function takes _db_lock, and a model swap can
        # land in that gap — most plausibly while the INSERT queues behind
        # reconcile's own lock hold. Committing then would leave a stale-space
        # vector that reconcile has already swept past and that backfill never
        # revisits, because backfill only refills NULLs. So carry the generation to
        # the write and re-check it while holding the lock.
        embed_generation = (
            embedding_generation
            if embedding_resolved and embedding_generation is not None
            else self._space_generation
        )
        if (
            embedding is None
            and not defer_embedding
            and not embedding_resolved
            and self.embed_fn is not None
        ):
            embedding = self._try_embed(text)

        embedding_blob: bytes | None = None
        if embedding is not None:
            if _HAS_NUMPY:
                vec = np.array(embedding, dtype=np.float32)
                norm = np.linalg.norm(vec)
                if norm > 0:
                    vec = vec / norm
                embedding_blob = vec.tobytes()
            else:
                # Normalize without numpy
                norm_f: float = math.sqrt(sum(x * x for x in embedding))
                normed = [x / norm_f for x in embedding] if norm_f > 0 else embedding
                embedding_blob = struct.pack(f"{len(normed)}f", *normed)

        # db + FAISS critical section — serialized against concurrent readers on
        # the event loop thread (search_episodic) and other writer threads. The
        # blocking embed above already ran outside the lock, so this only guards
        # local work. FAISS add + _faiss_id_map.append MUST stay atomic together:
        # a reader that sees index.ntotal == N+1 while len(id_map) == N would
        # IndexError (or the concurrent add/search would corrupt the C++ index).
        with self._embedding_config_guard(embedding), self._db_lock:
            if embedding_blob is not None and self._space_generation != embed_generation:
                # A model swap landed between the embed and this lock. Persist NULL
                # rather than a vector from the previous space — the backfill at the
                # end of the swap re-embeds this row in the new one. The text is
                # still written, so nothing is lost.
                logger.debug("Dropping an episodic embedding produced in a previous space")
                embedding_blob = None
                embedding = None
            # Re-check under the write lock. The fast check above avoids an
            # unnecessary embed in the common case, but cannot prevent a native
            # writer from inserting the same text between that check and this
            # critical section.
            existing = self.db.execute(
                "SELECT id FROM episodic_memories WHERE is_deleted = 0 " f"AND {dedup_predicate}",
                (text_prefix,),
            ).fetchone()
            if existing is not None:
                logger.debug(
                    "Episodic text-hash dedup under lock: prefix matches id=%s",
                    existing["id"],
                )
                return EpisodicWriteOutcome.REFUSED
            # Dedup via FAISS — only when THIS write has an embedding. The index
            # being non-empty says nothing about the current write: with embeddings
            # disabled (embedding_provider="none") or a transient embed failure,
            # `embedding_blob` is None and the query vector below would be unbound
            # (UnboundLocalError), losing the memory entirely. Degrade to a
            # non-deduped write instead (the text-prefix dedup above still applies).
            if (
                self.algorithm_version != "v2"
                and embedding_blob is not None
                and self._faiss_index is not None
                and self._faiss_index.ntotal > 0  # type: ignore[attr-defined]
            ):
                query_vec = np.frombuffer(embedding_blob, dtype=np.float32).reshape(1, -1)
                distances, indices = self._faiss_index.search(query_vec, 5)  # type: ignore[attr-defined]
                for dist, idx in zip(distances[0], indices[0]):
                    if idx == -1:
                        break
                    cosine_sim = float(dist)  # inner product on normalized = cosine
                    if cosine_sim > self._dedup_threshold:
                        existing_id = self._faiss_id_map[int(idx)]
                        existing = self._get_episodic(existing_id)
                        if existing is None:
                            # The matched vector points to a tombstoned/deleted row
                            # (a "ghost": tombstone paths set is_deleted=1 but never
                            # remove the vector from _faiss_index/_faiss_id_map, so it
                            # keeps matching). _get_episodic filters is_deleted=0, so it
                            # is None here. Treating that as a conflict would REJECT the
                            # new write against a deleted memory (data loss). Skip the
                            # ghost and keep scanning, mirroring search_episodic's
                            # `if not mem or mem["is_deleted"]: continue`.
                            continue
                        if preserve_existing:
                            self._log_event(
                                "conflict_skip",
                                "episodic",
                                existing_id,
                                "",
                                text[:200],
                                source,
                            )
                            return EpisodicWriteOutcome.REFUSED
                        if len(text) > len(existing["text"]) * 1.2:
                            self._delete_episodic_row(existing_id)
                            self._log_event(
                                "merge",
                                "episodic",
                                existing_id,
                                existing["text"][:200],
                                text[:200],
                                source,
                            )
                            break
                        else:
                            self._log_event(
                                "conflict_skip",
                                "episodic",
                                existing_id,
                                "",
                                text[:200],
                                source,
                            )
                            return EpisodicWriteOutcome.REFUSED

            if preserve_existing:
                mem_id = str(uuid4())
                now = _now_iso()
                self.db.execute("BEGIN IMMEDIATE")
                try:
                    if not self._embedding_current(embedding):
                        embedding_blob = None
                    active_count = self.db.execute(
                        "SELECT COUNT(*) FROM episodic_memories WHERE is_deleted = 0"
                    ).fetchone()[0]
                    if self.algorithm_version != "v2" and active_count >= self._episodic_max:
                        self.db.commit()
                        return EpisodicWriteOutcome.AT_CAPACITY
                    self.db.execute(
                        memory_schema.episodic_insert(self._lineage),
                        memory_schema.episodic_insert_params(
                            self._lineage,
                            mem_id,
                            conversation_id,
                            text,
                            embedding_blob,
                            json.dumps(clean_tags),
                            importance,
                            now,
                            source,
                        ),
                    )
                    self._record_mutation(
                        "episode", mem_id, None, source, metadata=metadata, operation="create"
                    )
                    self.db.commit()
                except Exception:
                    self.db.rollback()
                    raise
            else:
                self._enforce_episodic_cap()
                mem_id = str(uuid4())
                now = _now_iso()
                with self.db:
                    self.db.execute("BEGIN IMMEDIATE")
                    if not self._embedding_current(embedding):
                        embedding_blob = None
                    self.db.execute(
                        memory_schema.episodic_insert(self._lineage),
                        memory_schema.episodic_insert_params(
                            self._lineage,
                            mem_id,
                            conversation_id,
                            text,
                            embedding_blob,
                            json.dumps(clean_tags),
                            importance,
                            now,
                            source,
                        ),
                    )
                    self._record_mutation(
                        "episode", mem_id, None, source, metadata=metadata, operation="create"
                    )
                    self.db.commit()

            # Add to FAISS. The C++ index and the Python _faiss_id_map MUST commit
            # together — if index.ntotal ends up ahead of len(_faiss_id_map) a later
            # lookup IndexErrors and similarity results desync. Append the id first
            # (a cheap, reliable list op), then add the vector, and roll the id back
            # if the add raises so the two structures stay atomically in sync.
            self._invalidate_episodic_scoring()
            if embedding_blob is not None and self._faiss_index is not None:
                vec = np.frombuffer(embedding_blob, dtype=np.float32).reshape(1, -1)
                self._faiss_id_map.append(mem_id)
                try:
                    self._faiss_index.add(vec)  # type: ignore[attr-defined]
                except Exception:
                    self._faiss_id_map.pop()  # roll back partial add — keep in sync
                    raise
                self._faiss_writes_since_save += 1
                if self._faiss_writes_since_save >= _FAISS_SAVE_INTERVAL:
                    self.save_faiss_index()

        self._stamp_facets(mem_id, facets)
        self._log_event("create", "episodic", mem_id, None, text[:200], source)
        has_vec = embedding_blob is not None
        logger.debug(
            "Episodic written: id=%s src=%s imp=%.2f vec=%s text=%s…",
            mem_id[:8],
            source,
            importance,
            has_vec,
            text[:80],
        )
        return EpisodicWriteOutcome.WRITTEN

    def has_episodic_text(self, text: str) -> bool:
        """Return whether an active episodic memory exactly matches *text*."""
        return (
            self._fetch_one_locked(
                "SELECT 1 FROM episodic_memories WHERE is_deleted = 0 AND text = ? LIMIT 1",
                (text,),
            )
            is not None
        )

    @timed("vector", "search")
    def _episodic_relevance_threshold(self, text: str) -> float:
        """Minimum RAW cosine for a memory to be admitted as relevant context."""
        return _episodic_search.episodic_relevance_threshold(self, text)

    def _filter_by_relevance(self, candidates: list[dict]) -> list[dict]:
        """Drop candidates below the length-aware raw-cosine relevance gate."""
        return _episodic_search.filter_by_relevance(self, candidates)

    def search_episodic(
        self,
        query_embedding: list[float] | None = None,
        query_text: str = "",
        limit: int = 8,
        mmr: bool = True,
        tag_filter: list[str] | None = None,
        relevance_filter: bool = False,
        *,
        recall_query: _RecallQuery | None = None,
    ) -> list[dict]:
        """Search episodic memories by vector similarity with decay scoring."""
        return _episodic_search.search_episodic(
            self,
            query_embedding,
            query_text,
            limit,
            mmr,
            tag_filter,
            relevance_filter,
            recall_query=recall_query,
        )

    def _search_episodic_v2(
        self,
        query_embedding: list[float] | None,
        query_text: str,
        limit: int,
        mmr: bool,
        tag_filter: list[str] | None,
        relevance_filter: bool,
    ) -> list[dict]:
        """Fuse both evidence sources before admission and the result budget."""
        return _episodic_search.search_episodic_v2(
            self, query_embedding, query_text, limit, mmr, tag_filter, relevance_filter
        )

    def _sqlite_vector_search(
        self,
        query_embedding: list[float],
        query_text: str,
        limit: int,
        mmr: bool = True,
        tag_filter: list[str] | None = None,
        relevance_filter: bool = False,
    ) -> list[dict]:
        """Cosine similarity search using embeddings stored in SQLite."""
        return _episodic_search.sqlite_vector_search(
            self, query_embedding, query_text, limit, mmr, tag_filter, relevance_filter
        )

    def _invalidate_episodic_scoring(self) -> None:
        """Drop the resident episodic scoring set.

        Called by every writer that changes which episodic rows are scored, or
        what any of them scores as. Bumping the generation as well as clearing
        the reference is what makes it safe to call WITHOUT ``_db_lock``: a set
        built from a read that started before the bump carries the old
        generation, so it is rejected on the next lookup rather than installed
        over this invalidation.

        NOT called by :meth:`_touch_last_accessed` — ``last_accessed_at`` is
        never scored and is re-read per search from the winners' row bodies, so
        dropping the set on the search path's own write would make it useless.
        """
        self._episodic_scoring_generation += 1
        self._episodic_scoring = None

    def _invalidate_semantic_scoring(self) -> None:
        """Drop cached query-aware semantic rows after a scoring-relevant write."""
        self._semantic_scoring_generation += 1
        self._semantic_scoring = None

    def _semantic_scoring_set(self) -> _SemanticScoringSet | None:
        """Return cached semantic scoring rows, or ``None`` for a per-call scan."""
        return _semantic.semantic_scoring_set(self)

    def _sqlite_data_version(self) -> int | None:
        """``PRAGMA data_version``, or None when the library predates it.

        Moves when another CONNECTION commits to this database, and deliberately
        not for this connection's own commits, which is exactly the half of the
        validity token the in-process generation cannot cover. Costs a few
        microseconds. An sqlite older than 3.9.0 returns no row rather than
        raising, so a missing value is treated as "cannot detect", not as zero.
        """
        try:
            with self._db_lock:
                row = self.db.execute("PRAGMA data_version").fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        try:
            return int(row[0])
        except (TypeError, ValueError, IndexError):
            return None

    def _episodic_scoring_set(self, dim: int) -> _EpisodicScoringSet | None:
        """Return the resident scoring set for *dim*, building it if stale."""
        return _episodic_search.episodic_scoring_set(self, dim)

    def _build_episodic_scoring_set(self, dim: int, version: int) -> _EpisodicScoringSet | None:
        """Read the scoring columns for every active embedded row of width *dim*."""
        return _episodic_search.build_episodic_scoring_set(self, dim, version)

    def _rank_from_scoring_set(
        self,
        scoring: _EpisodicScoringSet,
        q: list[float],
        limit: int,
        mmr: bool,
        tag_filter: list[str] | None,
        relevance_filter: bool,
        now: datetime,
        blocked: set[str] | None = None,
    ) -> list[dict]:
        """Score, filter and rank from the resident set; resolve winner bodies."""
        return _episodic_search.rank_from_scoring_set(
            self, scoring, q, limit, mmr, tag_filter, relevance_filter, now, blocked
        )

    def _episodic_candidate(self, r: sqlite3.Row, cosine_sim: float, now: datetime) -> dict:
        """Build one episodic search candidate from a row and its cosine score."""
        return _episodic_search.episodic_candidate(self, r, cosine_sim, now)

    def get_episodic_list(
        self, limit: int = 50, offset: int = 0, tag_filter: list[str] | None = None, *, q: str = ""
    ) -> list[dict]:
        """Active episodes, with optional literal text/tag search before pagination."""
        query = _text_scoring._normalize_memory_search_query(q)
        if tag_filter:
            # Use JSON-quoted exact match to avoid substring false positives
            # e.g. "cr" should not match "cron" or "datacraft"
            tag_conds = " AND (" + " OR ".join(["tags LIKE ?" for _ in tag_filter]) + ")"
            tag_params: tuple[object, ...] = tuple(f'%"{t.lower()}"%' for t in tag_filter)
        else:
            tag_conds = ""
            tag_params = ()
        columns = "id, conversation_id, text, tags, importance, created_at, last_accessed_at"
        relation = "episodic_memories"
        if self.algorithm_version == "v2":
            columns += ", source, scope, surface, crew, session_key, derived_from"
            relation = "memory_items"
            tag_conds = " AND kind = 'episode'" + tag_conds
        if query:
            tag_conds += (
                " AND (memory_text_contains(text, ?, 0) OR memory_text_contains(tags, ?, 1))"
            )
            tag_params += (query, query)
        sql = (
            f"SELECT {columns} FROM {relation} WHERE is_deleted = 0{tag_conds} "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?"
        )
        params = (*tag_params, limit, offset)
        if query:
            with self._db_lock:
                self.db.create_function(
                    "memory_text_contains",
                    3,
                    _text_scoring._contains_memory_search_text,
                    deterministic=True,
                )
                rows = self._fetch_all_locked(sql, params)
        else:
            rows = self._fetch_all_locked(sql, params)
        return [dict(r) for r in rows]

    def get_retired_episodic(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """Episodes a semantic write SUPERSEDED, newest first, with what superseded them."""
        return _retirement.get_retired_episodic(self, limit, offset)

    def restore_episodic(self, mem_id: str, source: str = "user_explicit") -> bool:
        """Un-tombstone one episode. False when it is absent or already active.

        Restores rather than re-inserting, so the row keeps its id, its text, its
        vector and its ``created_at`` — a re-insert would look like a new memory and
        would re-enter the similarity dedup that may have been what removed it.
        """
        with self._db_lock, self.db:
            row = self.db.execute(
                "SELECT * FROM episodic_memories WHERE id = ? AND is_deleted = 1",
                (mem_id,),
            ).fetchone()
            if row is None:
                return False
            self.db.execute(
                f"UPDATE {self._epi_rel} SET is_deleted = 0 WHERE id = ?{self._epi_guard}",
                (mem_id,),
            )
            self._record_mutation(
                "episode",
                mem_id,
                dict(row),
                source,
                metadata={"status": "active"},
                operation="restore",
            )
            self.db.commit()
            # A warm NumPy set or FAISS index may have been built while this
            # episode was retired. A commit on this connection does not bump
            # PRAGMA data_version, so invalidate both derived populations now.
            self.invalidate_episode_content()
        # Logged so a restore is as auditable as the retire was, and so a row that keeps
        # being retired and restored is visible as a loop rather than as churn.
        self._log_event("restore", "episodic", mem_id, None, row["text"][:200], source)
        logger.info("Restored retired episodic entry %s", mem_id[:8])
        return True

    def delete_episodic(self, mem_id: str, source: str = "user_explicit") -> bool:
        """Tombstone an episodic memory."""
        existing = self._get_episodic(mem_id)
        if not existing:
            return False
        with self._db_lock, self.db:
            self.db.execute(
                f"UPDATE {self._epi_rel} SET is_deleted = 1 WHERE id = ?{self._epi_guard}",
                (mem_id,),
            )
            self._record_mutation("episode", mem_id, existing, source, operation="forget")
            self.db.commit()
            self._invalidate_episodic_scoring()
        self._log_event("delete", "episodic", mem_id, existing["text"][:200], None, source)
        return True

    def get_episodic_context(
        self,
        query_embedding: list[float] | None = None,
        query_text: str = "",
        cap: int = 3000,
    ) -> str:
        """Format episodic search results for prompt injection."""
        return _episodic_search.get_episodic_context(self, query_embedding, query_text, cap)

    # ── Facets: reading a carve back out ──

    def _require_facets(self) -> None:
        """Refuse a facet read unless this file is on the crew lineage.

        The discrimination is ``self._lineage``, resolved once in :meth:`init` from
        the file's own schema — never a ``hasattr`` probe and never a
        ``try``/``except`` around ``no such column``, both of which would answer
        for whatever the last statement happened to touch.

        REFUSAL rather than an empty result, and the same answer at every surface.
        See :class:`memory_schema.FacetsUnsupported`: on v1 the columns do not
        exist, so an empty page would report "this crew has no memories" for a
        store holding thousands of unfaceted rows — a wrong answer to the one
        question these methods exist to answer.
        """
        if self._lineage != memory_schema.LINEAGE_CREW:
            raise memory_schema.FacetsUnsupported(
                "this memory store is on the v1 schema lineage and carries no carve "
                "facets; a facet query is answerable only on a crew memory store"
            )

    def list_by_facets(
        self,
        filters: Mapping[str, str] | None = None,
        *,
        kind: str = "",
        limit: int = memory_schema.DEFAULT_FACET_PAGE,
        offset: int = 0,
    ) -> list[dict]:
        """One page of live rows matching every named facet, newest first.

        *filters* maps facet names (:data:`memory_schema.FACET_NAMES`) to exact
        values and ANDs them together; *kind* narrows to one row type. An axis the
        mapping omits is unconstrained, while an axis mapped to ``""`` selects the
        rows no writer attributed — the two are different questions, which is why
        this takes a mapping rather than a :class:`memory_schema.MemoryFacets`.

        Raises :class:`memory_schema.FacetsUnsupported` on the v1 lineage and
        :class:`memory_schema.UnknownFacet` for a name outside the closed set.
        Live rows only, like every other reader here; paging is stable because the
        order breaks ``created_at`` ties on ``id``. No ``embedding`` column is read
        or returned: a facet partitions and never scores.
        """
        self._require_facets()
        sql, params = memory_schema.facet_page_query(filters or {}, kind, limit, offset)
        return [dict(row) for row in self._fetch_all_locked(sql, params)]

    def count_by_facet(
        self,
        group_by: str,
        filters: Mapping[str, str] | None = None,
        *,
        kind: str = "",
    ) -> dict[str, int]:
        """Live-row counts per distinct value of *group_by*, most populous first.

        The "what is actually in this store's memory" question: which crews,
        surfaces, scopes or kinds filled it, and how much each contributed.
        *filters* and *kind* narrow the population first, so a count can be asked
        within a carve (``count_by_facet("surface", {"crew": "finance"})``).

        A ``dict`` keyed by the stored value, mirroring
        :meth:`get_rejection_stats`; ``""`` is a legitimate key and means the rows
        on which that axis was never stamped. Truncated to
        :data:`memory_schema.MAX_FACET_GROUPS` values because ``session_key``
        cardinality is unbounded, and the order makes that the least populous
        tail. Same two refusals as :meth:`list_by_facets`.
        """
        self._require_facets()
        sql, params = memory_schema.facet_count_query(group_by, filters or {}, kind)
        return {str(row["value"]): int(row["total"]) for row in self._fetch_all_locked(sql, params)}

    def memory_stats(self) -> dict:
        """Return counts and sizes for dashboard display."""
        row = self._fetch_one_locked(
            "SELECT "
            "(SELECT COUNT(*) FROM semantic_memory WHERE is_deleted=0) AS sem_active, "
            "(SELECT COUNT(*) FROM semantic_memory WHERE is_deleted=1) AS sem_deleted, "
            "(SELECT COUNT(*) FROM episodic_memories WHERE is_deleted=0) AS ep_active, "
            "(SELECT COUNT(*) FROM episodic_memories WHERE is_deleted=1) AS ep_deleted, "
            "(SELECT COUNT(*) FROM memory_events) AS events_count, "
            "(SELECT COUNT(*) FROM episodic_memories WHERE is_deleted=0 AND embedding IS NOT NULL) AS ep_with_vec"
        )
        assert row is not None  # a scalar-subquery SELECT always returns one row
        faiss_size = len(self._faiss_id_map) if self._faiss_id_map else 0
        return {
            "semantic_active": row[0],
            "semantic_deleted": row[1],
            "episodic_active": row[2],
            "episodic_deleted": row[3],
            "events_count": row[4],
            "faiss_index_size": faiss_size,
            "embedded_count": row[5],
            # The FAISS index is an optional in-RAM accelerator (needs both
            # faiss and numpy); without it, retrieval falls back to an exact
            # stdlib cosine scan over the same stored embeddings.
            "faiss_available": _HAS_FAISS and _HAS_NUMPY,
        }

    # ── Episodic Helpers ──

    @staticmethod
    def _matches_tags(mem: dict, tag_filter: list[str]) -> bool:
        """Check if an episodic entry matches ANY of the given tags."""
        return _episodic_search.matches_tags(mem, tag_filter)

    def _decay_rate_for(self, raw_tags: str | list[str] | None) -> float:
        """Resolve the per-day recency decay rate for an episodic row."""
        return _episodic_search.decay_rate_for(self, raw_tags)

    def _get_episodic(self, mem_id: str) -> dict | None:
        row = self._fetch_one_locked(
            "SELECT * FROM episodic_memories WHERE id = ? AND is_deleted = 0", (mem_id,)
        )
        return dict(row) if row else None

    #: Columns returned for episodic search hits. Deliberately omits the
    #: ``embedding`` BLOB — search results never read it (FAISS already holds the
    #: vectors) and it is by far the widest column in the row. Matches the column
    #: set the stdlib fallback (_sqlite_vector_search) puts in its candidates.
    _EPISODIC_SEARCH_COLUMNS = (
        "id, conversation_id, text, tags, importance, created_at, last_accessed_at"
    )

    def _get_episodic_batch(self, mem_ids: list[str]) -> dict[str, dict]:
        """Fetch several active episodic rows in one query, keyed by id."""
        return _episodic_search.get_episodic_batch(self, mem_ids)

    #: Minimum interval between last_accessed_at writes for the same episodic row.
    _LAST_ACCESSED_DEBOUNCE_SECS = 60.0
    #: Cap on the in-process debounce map before expired entries are swept.
    _LAST_ACCESSED_CACHE_MAX = 4096
    #: Per-kind paging cursors of the bounded repair sweep, created on first use by
    #: ``vector_memory_runtime.embedding_repair.backfill_rows``.
    _backfill_cursors: dict[str, str]

    def _touch_last_accessed(self, mem_ids: list[str]) -> None:
        """Record an access timestamp for episodic rows, debounced per row."""
        return _episodic_search.touch_last_accessed(self, mem_ids)

    def _delete_episodic_row(self, mem_id: str) -> None:
        with self._db_lock, self.db:
            before = self.db.execute(
                "SELECT * FROM episodic_memories WHERE id=?", (mem_id,)
            ).fetchone()
            self.db.execute(
                f"UPDATE {self._epi_rel} SET is_deleted = 1 WHERE id = ?{self._epi_guard}",
                (mem_id,),
            )
            self._record_mutation(
                "episode", mem_id, dict(before) if before else None, "dedup", operation="forget"
            )
            self.db.commit()
            self._invalidate_episodic_scoring()

    def _enforce_episodic_cap(self) -> None:
        """Enforce the legacy V1 cap; private V2 memory is retained until corrected or forgotten."""
        if self.algorithm_version == "v2":
            return
        with self._db_lock:
            count = self.db.execute(
                "SELECT COUNT(*) FROM episodic_memories WHERE is_deleted = 0"
            ).fetchone()[0]
            if count < self._episodic_max:
                return
            excess = count - self._episodic_max + 1
            rows = self.db.execute(
                "SELECT * FROM episodic_memories WHERE is_deleted = 0 "
                "ORDER BY importance ASC, created_at ASC LIMIT ?",
                (excess,),
            ).fetchall()
            for row in rows:
                self.db.execute(
                    f"UPDATE {self._epi_rel} SET is_deleted = 1 WHERE id = ?{self._epi_guard}",
                    (row["id"],),
                )
                self._record_mutation(
                    "episode", row["id"], dict(row), "capacity", operation="forget"
                )
            self.db.commit()
            self._invalidate_episodic_scoring()

    # ── Lessons ──

    def write_lesson(
        self,
        rule: str,
        category: str = "knowledge",
        negative: str | None = None,
        source: str = "user_explicit",
        rule_emb: list[float] | None = None,
        rule_emb_generation: int | None = None,
        repo_scope: str | None = None,
        *,
        applies: str | None = None,
        rule_emb_resolved: bool = False,
        defer_backfills: bool = False,
        facets: "memory_schema.MemoryFacets | None" = None,
        cites: list[dict[str, str]] | None = None,
        cited_commit: str | None = None,
    ) -> LessonWriteResult:
        """Write a lesson as a semantic entry with key lesson.<hash>.

        *cites* and *cited_commit* record the files the lesson describes and the
        commit the repository was at (see ``kiro_crew.lesson_cites``). They are
        stored only when given, so a lesson that cites nothing keeps the shape it
        always had, and a re-submit that carries none never strips stored ones.

        Returns which outcome occurred (see :class:`LessonWriteOutcome`) rather than a
        bare ``bool``, whose ``False`` conflated "validation refused this", "a dedup
        rule claimed it", "it is already stored exactly as submitted" and "your bare
        re-submit kept the stored clause" -- four facts a caller cannot act on without
        telling them apart. The result's TRUTH VALUE is still the old predicate (see
        :class:`LessonWriteResult`), so a caller that only needs "did this write
        something" keeps using ``if store.write_lesson(...)`` unchanged.

        Deduplicates against existing lessons:
        - Substring match: if existing contains new (or vice versa), longer wins
        - Topic overlap: if the shared significant words are >=50% of the LARGER of
          the two keyword sets, newer replaces older
        - Semantic similarity: if >85% cosine similarity, newer replaces older --
          unless a stored near-duplicate outranks this write (``user_explicit``
          over a lower-authority source, or strictly higher stored confidence),
          which reports ``deduped`` / ``semantic_similarity``.

        **A call that stores nothing deletes nothing.** Every supersede the scan
        decides on is QUEUED, and the queue drains only once ``set_semantic`` has
        committed the submission. So a ``deduped`` verdict from any branch, and a
        ``refused`` from the store's own validation, each preserve every live lesson
        row and report an empty ``superseded``. They are not byte no-ops on the
        database: a lazy embedding backfill for a row this call READ may already have
        been flushed, which changes a vector and no lesson.

        Draining after the commit puts the two outcomes in the safe order, and leaves
        one window it cannot close: a drain that stops partway -- a raised error, a
        killed process -- keeps the submission and leaves the rows it had not reached
        yet. That direction is deliberate. The residue is a duplicate, never a lost
        lesson, and the next write matching those rows retires them.

        Two of those three rules DELETE a stored lesson, and the substring rule's
        "longer wins" direction means a submitted rule can retire a stored one that
        is more general than it -- attaching a condition to a rule makes the text
        longer and the guidance NARROWER, so the row that survives can be the one
        that applies less often. That is the designed behaviour and this method
        keeps it: the
        alternative is a store that accumulates near-identical rules, which is what
        these three rules exist to prevent, and the onboarding import already shows
        the sanctioned way to opt out of it (route to ``set_semantic_if_absent``,
        which cannot replace anything -- see ``onboarding_import``).

        What it does NOT keep is the silence. Every rule that deletes records the
        rule text it removed in :attr:`LessonWriteResult.superseded`, so a caller is
        never handed a bare ``inserted`` for a call that destroyed a lesson the
        user still wanted. The result is the only place that can carry this: the
        deleted row is a tombstone, so it is gone from ``get_lessons``, from
        ``learn_list`` and from the injected lessons block by the time the caller
        looks.

        Pass ``rule_emb`` to reuse an embedding already computed by the caller
        and avoid a second blocking embed of the identical text. A caller doing
        that MUST also read :attr:`space_generation` BEFORE it embeds and pass it
        as ``rule_emb_generation``, so a model swap landing between that embed and
        this write is detected and the vector is left NULL for the backfill
        instead of being committed into the wrong space. ``rule_emb_resolved``
        additionally distinguishes an attempted embed that returned ``None`` from
        no attempt, and ``defer_backfills`` suppresses lazy legacy-row inference;
        together they let a caller keep inference outside a short publication lock.

        Runtime model-identity assertions and recognized concrete-ID model-selection
        imperatives in either persisted field are refused here, before embedding or
        deduplication. A model-version literal without either form remains durable. The
        JSONL fallback calls the same predicate, so MCP, dashboard, consolidation,
        task-runner, and direct callers share the boundary.
        """
        if contains_volatile_lesson_fact(rule, negative):
            return LessonWriteResult(LessonWriteOutcome.REFUSED, "volatile_session_fact")
        rule_lower = rule.lower()
        # A whitespace-only clause is no clause. `--negative "   "` is truthy, so
        # without this it composed "<rule> — NOT:    " and REPLACED a real stored
        # clause with blanks -- silent loss of the guidance the user had saved.
        #
        # isinstance FIRST, because this normalisation is what makes a non-string
        # reachable as a crash: consolidation passes the LLM's own
        # item.get("negative") straight through (history.py), so a model emitting
        # `"negative": 123` would hit .strip() and abort the whole run with
        # AttributeError. Before this normalisation existed an int only ever reached
        # an f-string, which interpolated it harmlessly -- so the guard is paying for
        # the strip, not for a pre-existing hole. A non-string is not usable
        # guidance, and str()-ifying it would store a repr as if the user wrote it,
        # so treat it as absent.
        negative = negative.strip() or None if isinstance(negative, str) else None
        # Same normalisation and the same non-string guard as the clause above:
        # consolidation forwards the model's own value unchecked, so a blank scope
        # stores as absent (applies everywhere) and a non-string is treated as
        # absent rather than reaching .strip() and aborting the run.
        # Canonicalise to the form the GATE compares, so storage and the gate agree
        # on what one scope is. See canonical_scope for why the raw string is wrong.
        #
        # A scope the gate can NEVER satisfy is refused here rather than normalised
        # or dropped. Both alternatives are wrong in opposite directions:
        # canonicalising "/src/pkg" strips the slash and ACTIVATES the lesson in
        # every repository holding src/pkg, which it was never validly scoped to;
        # returning None instead would store it GLOBALLY, which is the fail-open a
        # scoped lesson must never take. Refusing is the only answer that neither
        # invents a scope nor widens one, and it keeps this surface consistent with
        # the schema, which already rejects the same shapes.
        if repo_scope is not None and isinstance(repo_scope, str) and repo_scope.strip():
            if not scope_is_admissible(repo_scope):
                return LessonWriteResult(LessonWriteOutcome.REFUSED, "scope_inadmissible")
        repo_scope = canonical_scope(repo_scope)
        # The category is now part of the stored value, so an unusable one would be
        # scanned by validate_semantic and could REJECT the whole lesson -- turning a
        # bad label into lost guidance. Consolidation passes the LLM's own
        # item.get("category") straight through (history.py) with no validation,
        # unlike the REST and MCP paths, which are enum-restricted by
        # LEARN_ADD_SCHEMA. The shared helper clamps to that same enum
        # (write policy, strict=True), safely handling unhashable labels
        # (a dict or list from the LLM) that would make a raw set membership
        # test raise and abort consolidation instead of clamping.
        category = normalize_lesson_category(category, strict=True)
        # The tier goes the other way from the category, which CLAMPS an unusable
        # label so a bad string cannot cost the user the whole lesson. There is no
        # safe clamp for a tier: picking "directive" would inject a note into every
        # session, and picking "experience" would demote a standing rule. Both are
        # silent. So a misspelled tier raises here, which is a caller bug and
        # reaches that caller, while an OMITTED tier is the supported choice to
        # leave the row unclassified.
        applies = normalize_lesson_applies(applies)
        # What the STORED row will carry. Equal to the submission on an insert,
        # but the enrich branch below replaces it with the persisted value,
        # because the tier is write-once. Reported back so a caller guarding a
        # DELETION on the tier reads what is stored rather than what was sent.
        effective_applies = applies
        rule_words = self._lesson_keywords(rule_lower)
        # Same reasoning as write_episodic: carry the space generation to the write
        # so a swap landing between the embed and the lock cannot commit a vector
        # from the previous space.
        #
        # A caller-supplied ``rule_emb`` was embedded BEFORE this call, so its space
        # is provenance this method cannot infer — capturing here would compare the
        # post-swap generation against itself and wave the stale vector through.
        # Such callers pass the ``space_generation`` they read before embedding.
        if rule_emb is not None and rule_emb_generation is not None:
            lesson_embed_generation = rule_emb_generation
        else:
            lesson_embed_generation = self._space_generation
        if rule_emb is None and not rule_emb_resolved:
            rule_emb = self._try_embed(rule) if self.embed_fn else None
        backfills_done = 0
        # (blob, key, space generation, exact value embedded). The generation is
        # recorded per entry, not once for the call: these lazy backfills embed
        # inside the dedup scan below, so a swap can land between entries.
        pending_backfills: list[tuple[bytes, str, int, str, list[float]]] = []

        # PREFLIGHT the final value BEFORE the dedup scan below, which DELETES
        # superseded rows. The value was only validated by set_semantic at the very
        # end, so a value this store refuses (e.g. an injection-pattern ``negative``)
        # cost the caller its existing lesson: the dedup scan deleted the old row,
        # then set_semantic refused the replacement, and the route still returned
        # HTTP 200 with no lesson stored. Validating here makes the whole call a
        # no-op when the replacement cannot land.
        key = _lesson_key(rule, repo_scope)
        # The mapping shape keeps the two halves as separate fields, so they
        # survive a round-trip regardless of what characters the rule contains.
        # The legacy in-band form ("<rule><sep><negative>") is still READ below
        # and by every renderer — no migration; old rows upgrade only when a
        # re-submit rewrites them anyway. validate_semantic size-gates lesson
        # mappings on their content (legacy-equivalent bytes), so the JSON
        # envelope does not shrink the accepted rule capacity.
        lesson_value: dict[str, object] = {
            "rule": rule,
            "category": category,
            "negative": negative,
        }
        # The key is added only when a scope was given, so an unscoped lesson keeps
        # the exact stored shape it has always had and no existing row is churned.
        if repo_scope:
            lesson_value["repo_scope"] = repo_scope
        # Same additive rule for the tier: a caller that names none leaves the key
        # ABSENT, so the row is byte-identical to what this writer produced before
        # the field existed and reads as unclassified. Absent and "not applicable"
        # are the same answer here, which is why there is no stored sentinel.
        if applies:
            lesson_value["applies"] = applies
        # Same additive rule for the cited files. The commit means nothing without the
        # cites it was read for, so it is stored only beside them.
        cites = normalize_cites(cites)
        if cites:
            lesson_value["cites"] = cites
            cited_commit = normalize_cited_commit(cited_commit)
            if cited_commit:
                lesson_value["cited_commit"] = cited_commit
        value: object = lesson_value
        confidence = 1.0 if source == "user_explicit" else 0.9
        preflight = self.validate_semantic(key, value, confidence, source)
        if preflight is not None:
            code, message = preflight
            logger.info("Lesson rejected before dedup (%s): %s", code, message)
            return LessonWriteResult(LessonWriteOutcome.REFUSED, code.value)

        def _flush_backfills() -> None:
            for blob, bk, gen, body, vector in pending_backfills:
                with self._vector_commit(vector, best_effort=True) as current:
                    if not current or gen != self._space_generation:
                        continue
                    self.db.execute(
                        f"UPDATE {self._sem_rel} SET embedding = ? WHERE key = ? "
                        f"AND value_json = ? AND embedding IS NULL AND is_deleted = 0"
                        f"{self._sem_guard}",
                        (blob, bk, body),
                    )

        # TWO PASSES, and the order is load-bearing.
        #
        # Pass 1 resolves THIS lesson. Pass 2 runs the generic dedup rules, and those
        # can claim the write on an UNRELATED row -- a superset whose text contains our
        # rule. get_lessons() orders by md5 key, so whether such a row is scanned
        # before ours is effectively random, and doing both in one loop made the
        # outcome depend on that order: an unrelated superset seen first discarded an
        # enrichment we had already selected, and the clause was dropped on HTTP 200.
        # Resolving the exact match first makes the result order-independent, and
        # pass 2 is skipped entirely once pass 1 claims the write.
        # Deduplication is SCOPE-LOCAL, and both passes below share this list.
        #
        # A lesson scoped to one repository and a global one are different lessons
        # even when their wording is close, so a scoped write must never supersede,
        # enrich, or be discarded against a row from another scope. Without this the
        # generic dedup rules (substring containment, >50% keyword overlap, high
        # cosine similarity) reach across scopes and DELETE guidance the submitter
        # never addressed -- writing a repo-scoped rule could retire a global one
        # that merely shared most of its significant words.
        #
        lesson_rows = _lessons.lesson_rows_in_scope(self.get_lessons(), repo_scope)

        exact = _lessons.resolve_exact_rule(
            self,
            lesson_rows,
            rule=rule,
            key=key,
            negative=negative,
            category=category,
            applies=applies,
            confidence=confidence,
            source=source,
            cites=cites,
            cited_commit=cited_commit,
        )
        matched = exact is not None
        if exact is not None:
            if exact.result is not None:
                _flush_backfills()
                return exact.result
            key, value, effective_applies = exact.key, exact.value, exact.applies

        # Built once for the whole scan (query-side vector + norm are the same
        # for every row) rather than per candidate — see _stored_similarity_scorer.
        similarity = self._stored_similarity_scorer(rule_emb) if rule_emb else None

        # Every row this call tombstoned, in the order it went. Collected rather
        # than counted: a count tells the caller a lesson is gone without telling it
        # WHICH, and the row is a tombstone by the time the caller could look it up.
        # Populated only where the deletions actually run -- after the write lands --
        # so a result that reports a supersede is always a result whose write landed.
        superseded: list[str] = []

        # Every supersede the scan decides on, DEFERRED as (key, report): the
        # queue drains only after the write is COMMITTED, so no route that
        # declines the submission can cost a stored lesson. Deferring is the
        # whole invariant rather than one branch's detail -- rows are scanned in
        # recency order, so an eager delete in any branch can precede a later
        # row's refusal, and the caller is then handed a decline for a call that
        # emptied part of the store. Draining after ``set_semantic`` extends the
        # same guarantee to a value the store itself rejects.
        deferred_supersedes: list[tuple[str, str, str, str]] = []

        # AUTHORITY PRE-PASS -- non-mutating, decided before the scan's first
        # deletion. The invariant (settled after three review rounds circled
        # the same class): every refusal THIS change introduces is decidable
        # before the scan mutates anything. Rows are scanned in recency order,
        # not authority order, so a mid-scan authority decline could land
        # AFTER the untouched branches (or an earlier semantic supersede)
        # already deleted a row -- losing a stored lesson while storing
        # nothing. So the authority verdict is settled here, over the same
        # rows the semantic branch below will see: the pre-pass shares
        # ``backfills_done`` / ``pending_backfills`` with the main scan and
        # memoizes each computed blob onto its row dict, so a row embedded
        # here is never re-embedded or re-counted below, and the semantic
        # branch's visible set is a subset of the pre-pass's. (The pre-pass
        # can spend budget on rows the main scan never reaches -- it walks the
        # whole list, while the scan can return early on a lexical claimant --
        # so the SETS can differ even though no row is ever double-charged.)
        # A ``user_explicit`` write can never be declined, so the pass is
        # skipped for it entirely. The pass is non-mutating for its own reason,
        # independent of the scan's deferral below: an authority decline must not
        # spend the call's embed budget rewriting rows it is about to refuse.
        # Scan-wide authority ordering is a tracked follow-up decision.
        if (
            self.algorithm_version != "v2"
            and similarity is not None
            and source != "user_explicit"
            and not matched
        ):
            for existing in lesson_rows:
                pre_text = _lesson_row_text(existing)
                if pre_text is None:
                    continue
                # PURE semantic matches only. A row the mutating scan's
                # substring or topic-overlap branch would claim FIRST (per-row
                # branch order) keeps main's outcome for it -- those branches
                # are source-blind by main's design, and the pre-pass must not
                # decline a write that main would have resolved lexically
                # before the semantic test ever ran. The predicates mirror the
                # scan's own, on the same normalized text.
                pre_lower = pre_text.lower()
                if rule_lower in pre_lower or pre_lower in rule_lower:
                    continue
                if rule_words:
                    pre_words = self._lesson_keywords(pre_lower)
                    if pre_words and (
                        len(rule_words & pre_words) / max(len(rule_words), len(pre_words)) >= 0.5
                    ):
                        continue
                existing_emb_blob = existing.get("embedding")
                row_blob: bytes | None = None
                if (
                    existing_emb_blob
                    and isinstance(existing_emb_blob, bytes)
                    and len(existing_emb_blob) >= 4
                ):
                    row_blob = existing_emb_blob
                elif (
                    self.embed_fn
                    and not defer_backfills
                    and backfills_done < _MAX_BACKFILLS_PER_CALL
                ):
                    # Same lazy-backfill contract as the main scan (count even
                    # on failure; generation sampled BEFORE the embed). The
                    # blob is memoized onto the row dict so the main scan
                    # neither re-embeds nor re-counts this row; a failure is
                    # marked so the row is attempted at most once per call,
                    # exactly as before this pass existed.
                    backfill_generation = self._space_generation
                    existing_emb = self._try_embed(
                        _lessons._lesson_embed_text(json.loads(existing["value_json"])),
                        PRIORITY_BULK,
                    )
                    if existing_emb:
                        row_blob = struct.pack(f"{len(existing_emb)}f", *existing_emb)
                        pending_backfills.append(
                            (
                                row_blob,
                                existing["key"],
                                backfill_generation,
                                existing["value_json"],
                                existing_emb,
                            )
                        )
                        existing["embedding"] = row_blob
                    else:
                        existing["_authority_prepass_embed_failed"] = True
                    backfills_done += 1
                if row_blob is None:
                    continue
                if similarity({"embedding": row_blob}) > 0.85:
                    # Outranking means a ``user_explicit`` row over this
                    # lower-authority write, or a strictly higher stored
                    # confidence -- the confidence half on its own merit: the
                    # onboarding import stores the user's own lessons at
                    # confidence 1.0 under source "import", so a 0.9
                    # consolidation write must not retire them. Strict ``>``
                    # is a deliberate divergence from ``_write_semantic``'s
                    # same-key rule (which treats confidences within 0.1 as
                    # equal and lets the newer write win): near-duplicates are
                    # DIFFERENT rows with no same-key freshness to prefer, and
                    # equal confidence falls through to newest-wins below.
                    try:
                        existing_confidence = float(existing.get("confidence") or 0.0)
                    except (TypeError, ValueError):
                        existing_confidence = 0.0
                    if (
                        existing.get("source") == "user_explicit"
                        or existing_confidence > confidence
                    ):
                        logger.info(
                            "Lesson semantic dedup: higher-authority %r kept over %s",
                            existing["key"],
                            source,
                        )
                        # Nothing has been deleted: this return precedes the
                        # mutating scan entirely, so a declined write costs no
                        # stored row and ``superseded`` is always empty here.
                        _flush_backfills()
                        return LessonWriteResult(
                            LessonWriteOutcome.DEDUPED,
                            "semantic_similarity",
                            tuple(superseded),
                        )

        # Private V2 keeps exact-rule enrichment above, but similarity cannot
        # authorize deleting a different instruction. Corrections target a key
        # explicitly; uncertain conflicts remain visible for owner review.
        for existing in [] if matched or self.algorithm_version == "v2" else lesson_rows:
            existing_text = _lesson_row_text(existing)
            if existing_text is None:
                continue
            existing_lower = existing_text.lower()
            # Two renderings of one row, and the split is the point. Every COMPARISON
            # below stays on ``existing_text`` (the embed rendering) so no dedup
            # decision changes; only what a deletion REPORTS uses the display
            # rendering, which keeps the NOT-clause. Falls back to the comparison text
            # when a row has no display form, so the report can never be emptier than
            # the row it names.
            existing_report = _lesson_row_report_text(existing) or existing_text

            may_retire_existing, covering_row_arrives_less_often = _lessons.tier_permissions(
                existing, applies
            )

            # Substring dedup
            if rule_lower in existing_lower and not covering_row_arrives_less_often:
                logger.info(
                    "Lesson dedup: %s already covered by %s [%s]", key, existing["key"], category
                )
                _flush_backfills()
                return LessonWriteResult(
                    LessonWriteOutcome.DEDUPED, "substring_covered", tuple(superseded)
                )
            if existing_lower in rule_lower:
                # This branch was the only one of the four here that deleted a row
                # WITHOUT saying so at any level: its three siblings each log, and
                # this one went straight to delete_semantic. So the deletion left no
                # trace a user or an operator could find -- not in the result, not in
                # the log, and not in the store, since the row is tombstoned and
                # every read path filters it. Log like the siblings do.
                #
                # IDENTITIES, never content, and that is the point of this whole scan's
                # logging rather than a limitation of this line. A lesson holds whatever
                # the user once told the agent -- credentials, paths, names -- so a log
                # line carrying its text turns a silent-deletion bug into a disclosure
                # bug, on a sink that persists to disk and may reach a notification
                # channel. Both keys ARE the store's own row ids (``lesson.<digest>``),
                # so an operator can join this line to the tombstoned row, to the
                # delete_semantic audit record, and to the matching ``superseded`` entry
                # in the result -- which is the read path where the text belongs, and
                # where it is redacted at every surface.
                #
                # The id is logged rather than a fresh digest deliberately: a
                # newly-computed hash would correlate with nothing. Nothing here HASHES
                # anything, so this adds no weak-hashing exposure -- ``_lesson_key``
                # already derived these ids, and CodeQL flags that derivation at its own
                # site, not at a line that merely logs the result.
                if may_retire_existing:
                    deferred_supersedes.append(
                        (existing["key"], existing_report, existing["value_json"], "contains")
                    )
                continue

            # Topic overlap dedup
            if rule_words:
                existing_words = self._lesson_keywords(existing_lower)
                if existing_words:
                    overlap = rule_words & existing_words
                    # Divided by the LARGER keyword set, not the smaller one. Against
                    # the smaller set the ratio measures "how much of the shorter rule
                    # the longer one covers", so a two-word rule whose words both
                    # appear in a nineteen-word rule scores 100% and DELETES it —
                    # detailed guidance destroyed by a terse near-truism. Against the
                    # larger set the score is symmetric, and reaching 0.5 requires the
                    # two rules to genuinely be about the same thing.
                    ratio = len(overlap) / max(len(rule_words), len(existing_words))
                    if ratio >= 0.5:
                        if may_retire_existing:
                            deferred_supersedes.append(
                                (
                                    existing["key"],
                                    existing_report,
                                    existing["value_json"],
                                    "%.0f%% keyword overlap" % (ratio * 100),
                                )
                            )
                        continue

            # Semantic dedup via embeddings (use stored embedding when available)
            if similarity is not None:
                existing_emb_blob = existing.get("embedding")
                row_blob = None
                if (
                    existing_emb_blob
                    and isinstance(existing_emb_blob, bytes)
                    and len(existing_emb_blob) >= 4
                ):
                    row_blob = existing_emb_blob
                elif (
                    self.embed_fn
                    and not defer_backfills
                    and backfills_done < _MAX_BACKFILLS_PER_CALL
                    and not existing.get("_authority_prepass_embed_failed")
                ):
                    # Lazy backfill: compute embedding for legacy lessons (count even on failure)
                    # Sampled BEFORE the embed: _try_embed returns None when a swap
                    # spanned its own call, so this value is the blob's true space.
                    # Sampling after it returns would tag an old blob with the new
                    # generation and the flush check would wave it through.
                    # A row the authority pre-pass already attempted is skipped:
                    # the pre-pass memoized a successful blob onto the row dict
                    # (so this branch is not reached) and marked a failure, so
                    # every row is attempted at most once per call, exactly as
                    # before the pre-pass existed.
                    backfill_generation = self._space_generation
                    # Embed the canonical rule text (matching write_lesson), not
                    # the display rendering -- the vector must live in the same
                    # space as the query vectors it is compared against.
                    existing_emb = self._try_embed(
                        _lessons._lesson_embed_text(json.loads(existing["value_json"])),
                        PRIORITY_BULK,
                    )
                    if existing_emb:
                        row_blob = struct.pack(f"{len(existing_emb)}f", *existing_emb)
                        pending_backfills.append(
                            (
                                row_blob,
                                existing["key"],
                                backfill_generation,
                                existing["value_json"],
                                existing_emb,
                            )
                        )
                    backfills_done += 1
                if row_blob is not None:
                    sim = similarity({"embedding": row_blob})
                    if sim > 0.85:
                        # Newest wins, matching the substring and topic-overlap
                        # branches above -- both supersede the stored row
                        # unconditionally. A length tie-break on
                        # ``len(rule) > len(existing_text)`` would DROP the
                        # submission when it loses, making character count
                        # decide which of two near-identical rules is current.
                        # A correction is frequently SHORTER than the stale
                        # lesson it corrects (a retracted claim collapses to a
                        # one-line "not installed"), so the losing case landed
                        # exactly on corrections -- and left the stale lesson in
                        # effect, the one outcome that actively misleads the
                        # agent rather than merely losing information.
                        #
                        # No authority check HERE, by construction: the
                        # non-mutating pre-pass above already returned DEDUPED
                        # if any purely-semantic row outranks the write. Like
                        # both lexical branches, this one only QUEUES its
                        # supersede -- a later row can still decline the write
                        # via ``substring_covered``, and a declined write
                        # executes no deletion at all.
                        if may_retire_existing:
                            deferred_supersedes.append(
                                (
                                    existing["key"],
                                    existing_report,
                                    existing["value_json"],
                                    "%.2f cosine" % sim,
                                )
                            )
                        continue

        # No pending backfill is dropped for a queued row. The queue is a list of
        # CANDIDATE deletions until the write commits, so discarding their vectors
        # here would cost the surviving rows their embeddings on exactly the paths
        # that delete nothing. A vector written to a row this call then retires is
        # one spent UPDATE on a tombstone.

        _flush_backfills()

        # ``repo_scope`` mirrors onto the ``scope`` carve axis when the caller named
        # no scope facet of its own. ``value_json`` stays authoritative -- the lesson
        # reader keeps reading it -- so this is an index projection, never a second
        # source of truth for what a lesson is scoped to.
        if repo_scope and (facets is None or not facets.scope):
            # ``replace`` rather than a field-by-field rebuild: naming the four other
            # axes here would silently DROP any axis added to MemoryFacets later.
            prior: memory_schema.MemoryFacets = (
                facets if facets is not None else memory_schema.MemoryFacets()
            )
            facets = dataclasses.replace(prior, scope=repo_scope)
        err = self.set_semantic(key, value, confidence, source, facets=facets)
        if err is not None:
            # Nothing was deleted: the scan only QUEUED its supersedes, and the
            # queue drains below this return. So a value the store rejects costs
            # no stored row, and ``superseded`` is empty here by construction.
            return LessonWriteResult(LessonWriteOutcome.REFUSED, err[0].value, tuple(superseded))
        # THE WRITE HAS LANDED -- drain the supersede queue. Every route that
        # declines the submission returned above, so reaching this line is what
        # makes each queued deletion a genuine replacement rather than a loss.
        #
        # Each row is deleted only while its body is still the one the scan READ, and
        # the comparison is the delete statement's own, so no writer can land between
        # checking and tombstoning. Nothing serializes this method for its whole
        # length and ``_lesson_key`` keys on the rule and scope alone, so a competing
        # write CAN reach a queued key inside this window -- a user enriching the very
        # rule being retired lands on it exactly -- and a second process on the same
        # database file is ordered by no lock this process holds. The guard decides
        # the write's OWN key too: ``set_semantic`` has committed by here, so a queued
        # row sharing that key fails it, and no separate same-key check is needed.
        #
        # A supersede is LOGGED here because this is where one happens; the scan only
        # nominates rows. IDENTITIES only, never row text -- a lesson can hold
        # whatever the user tells the agent, and this sink persists to disk.
        #
        # The attribution is passed INTO delete_semantic (winning key + reason), so
        # the tombstone's record status and audit event record it durably in the
        # database rather than only in the ``learn_add`` reply, which a
        # timed-out-but-committed write can lose. The DB event/status write is that
        # durable trace; the line below logs at WARNING so the same deletion is
        # visible to a human tailing gateway.log (a default install records WARNING+
        # to disk), not only to a query against the audit table.
        for d_key, d_report, d_body, d_reason in deferred_supersedes:
            if not self.delete_semantic(
                d_key,
                source,
                expect_value_json=d_body,
                superseded_by=key,
                supersede_reason=d_reason,
            ):
                logger.info(
                    "Lesson supersede skipped: %s changed or went while %s was written",
                    d_key,
                    key,
                )
                continue
            logger.warning(
                "Lesson supersede: %s replaces %s [%s] (%s), %d so far",
                key,
                d_key,
                category,
                d_reason,
                len(superseded) + 1,
            )
            superseded.append(d_report)

        if rule_emb:
            emb_blob = struct.pack(f"{len(rule_emb)}f", *rule_emb)
            with self._vector_commit(rule_emb, best_effort=True) as current:
                if not current or self._space_generation != lesson_embed_generation:
                    # Swap landed mid-write: leave the vector NULL for the backfill
                    # instead of persisting one from the previous space. The lesson
                    # row itself is already written.
                    logger.debug("Dropping a lesson embedding produced in a previous space")
                else:
                    # Body equality pins the actual embedding input. A later edit,
                    # tombstone or completed backfill must win this tail race.
                    # ensure_ascii=False matches the representation set_semantic
                    # persists; an escaped dump would match no row for a
                    # non-ASCII lesson, leaving its embedding NULL.
                    self.db.execute(
                        f"UPDATE {self._sem_rel} SET embedding = ? WHERE key = ? "
                        f"AND value_json = ? AND embedding IS NULL AND is_deleted = 0"
                        f"{self._sem_guard}",
                        (emb_blob, key, json.dumps(value, ensure_ascii=False)),
                    )
        # ``matched`` is pass 1's verdict: it rewrote an EXISTING row under that row's
        # own key to attach a clause, which is an enrichment. Every other route here
        # wrote a new row under the submitted rule's key -- including the ones that
        # superseded an older row first, since the caller's lesson did not exist under
        # this key before. Same two words the JSONL store uses for the same events.
        return LessonWriteResult(
            LessonWriteOutcome.ENRICHED if matched else LessonWriteOutcome.INSERTED,
            superseded=tuple(superseded),
            applies=effective_applies,
        )

    @staticmethod
    def _lesson_keywords(text: str) -> set[str]:
        """Extract significant words from a lesson rule, ignoring stop words."""
        return _lessons.lesson_keywords(text)

    def embed_lesson(self, rule: str) -> list[float] | None:
        """Embed a lesson rule once for reuse across dedup passes.

        Synchronous (performs a blocking embed); callers on an event loop
        should wrap this in ``asyncio.to_thread()``.
        """
        return self._try_embed(rule) if self.embed_fn else None

    def find_contradiction_candidates(
        self,
        rule: str,
        threshold_low: float = 0.4,
        threshold_high: float = 0.85,
        rule_emb: list[float] | None = None,
        repo_scope: str | None = None,
    ) -> list[dict]:
        """Find lessons related to rule but not caught by standard dedup."""
        return _lessons.find_contradiction_candidates(
            self, rule, threshold_low, threshold_high, rule_emb, repo_scope
        )

    def has_any_lesson(self) -> bool:
        """Whether any active row decodes to RENDERABLE lesson data, ignoring scope."""
        return _lessons.has_any_lesson(self)

    def get_lessons(self, limit: int | None = None, offset: int = 0) -> list[dict]:
        """Return lesson.* entries ordered by most recently updated."""
        return _lessons.get_lessons(self, limit, offset)

    def count_lessons(self) -> int:
        """Return the number of live lessons without materializing them."""
        return _lessons.count_lessons(self)

    def has_any_decodable_lesson(self) -> bool:
        """Whether any active ``lesson.*`` row holds JSON that decodes at all."""
        return _lessons.has_any_decodable_lesson(self)

    def delete_lesson(
        self, rule_substring: str, repo_scope: str | None = None, *, exact: bool = False
    ) -> bool:
        """Delete lessons whose value contains rule_substring."""
        return _lessons.delete_lesson(self, rule_substring, repo_scope, exact=exact)

    def get_lessons_context(
        self,
        query_text: str = "",
        cap: int = 0,
        project_dir: str | Path | None = None,
        *,
        recall_query: _RecallQuery | None = None,
        background: bool = False,
        hard_cap: int = 0,
        directive_budget: int = 0,
        experience_budget: int = 0,
    ) -> str:
        """Format lessons for prompt injection, most relevant first."""
        return _lessons.get_lessons_context(
            self,
            query_text,
            cap,
            project_dir,
            recall_query=recall_query,
            background=background,
            hard_cap=hard_cap,
            directive_budget=directive_budget,
            experience_budget=experience_budget,
        )

    def turn_lessons(
        self,
        query_text: str,
        *,
        shown: Callable[[str], bool],
        project_dir: str | Path | None = None,
        max_rows: int,
        max_chars: int,
        render_lesson: Callable[[str], str] | None = None,
    ) -> list[tuple[str, str]]:
        """``(key, text)`` of the lessons a follow-up message should add, best first."""
        return _lessons.turn_lessons(
            self,
            query_text,
            shown=shown,
            project_dir=project_dir,
            max_rows=max_rows,
            max_chars=max_chars,
            render_lesson=render_lesson,
        )

    def _rank_lessons(
        self,
        entries: list[tuple[dict, str]],
        query_text: str,
        *,
        recall_query: _RecallQuery | None = None,
    ) -> list[tuple[dict, str]]:
        """Order *entries* by hybrid relevance to *query_text*, most relevant first."""
        return _lessons.rank_lessons(self, entries, query_text, recall_query=recall_query)

    def _any_lesson_overlap(self, entries: list[tuple[dict, str]], query_text: str) -> bool:
        """Whether ANY entry shares a stemmed word with *query_text*."""
        return _lessons.any_lesson_overlap(self, entries, query_text)

    @staticmethod
    def _stored_similarity_scorer(
        query_emb: list[float] | None,
    ) -> Callable[[dict], float]:
        """Build a cosine scorer for one query, with query-side work done once."""
        return _embedding.stored_similarity_scorer(query_emb)

    # ── Migration & Import ──

    @staticmethod
    def _cosine_sim(a: list[float], b: list[float]) -> float:
        """Cosine similarity between two vectors.

        Vectors of different length are incomparable and score 0.0 rather
        than being silently truncated to the shorter one by ``zip`` — a row
        embedded at a different dimensionality (e.g. an old embedding-model
        generation) would otherwise return a plausible-looking partial-overlap
        score instead of being rejected. Matches the dimension guard already
        enforced by ``_stored_similarity_scorer`` (byte-length check) and
        ``HybridRetriever._cosine_similarity``.
        """
        if len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(y * y for y in b))
        return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0

    @staticmethod
    def _parse_preference(text: str) -> tuple[str, str] | None:
        """Extract key-value from preference text with better heuristics."""
        return _migration.parse_preference(text)

    def _embed_bulk_row(self, text: str, *, pace: bool) -> "list[float] | None":
        """Embed one row of a corpus sweep, then optionally pace the loop."""
        return _embedding.embed_bulk_row(self, text, pace=pace)

    def _embedding_token(self) -> tuple[str | None, str]:
        with self._db_lock:
            return self.recorded_embedding_space(), self.recorded_rebuild_generation()

    def _embedding_current(self, vector: list[float] | None) -> bool:
        if not isinstance(vector, _EmbeddingVector):
            return True
        if vector.space_token != self._embedding_token():
            return False
        if vector.managed:
            from kiro_crew.embeddings import embedding_rebuild_generation

            requested = embedding_rebuild_generation()
            if requested and requested != vector.space_token[1]:
                return False
        return True

    @contextmanager
    def _embedding_config_guard(self, vector: list[float] | None):
        """Serialize managed vector publication against explicit config apply."""
        from kiro_crew.config.loader import _config_write_lock, _lock_target, config_path

        if isinstance(vector, _EmbeddingVector) and vector.managed:
            with _config_write_lock(_lock_target(config_path())):
                yield
        else:
            yield

    @contextmanager
    def _vector_commit(self, vector: list[float] | None, *, best_effort: bool = False):
        """Own a derived-only transaction; callers never commit inside it."""
        with ExitStack() as resources:
            started = False

            def rollback_owned():
                if not started:
                    return
                self._faiss_index = None
                self._faiss_id_map.clear()
                self._invalidate_episodic_scoring()
                try:
                    self.db.rollback()
                except Exception:
                    # Closing rolls back the uncommitted vector without touching
                    # already committed text. Do not reuse an uncertain connection.
                    logger.exception("Vector rollback failed; closing the store")
                    self.close()

            try:
                # Wait for config admission before blocking readers of this store.
                # Both locks stay owned through commit or rollback below.
                resources.enter_context(self._embedding_config_guard(vector))
                resources.enter_context(self._db_lock)
                self.db.execute("BEGIN IMMEDIATE")
                started = True
                current = self._embedding_current(vector)
            except (OSError, sqlite3.Error, StdlibSQLiteError):
                rollback_owned()
                if not best_effort:
                    raise
                logger.warning(
                    "Derived vector admission failed; saved text retained", exc_info=True
                )
                yield False
                return
            try:
                yield current
                self.db.commit()
            except (OSError, sqlite3.Error, StdlibSQLiteError):
                rollback_owned()
                if not best_effort:
                    raise
                logger.warning("Derived vector write failed; saved text retained", exc_info=True)
            except BaseException:
                rollback_owned()
                raise

    def _try_embed(self, text: str, priority: int = PRIORITY_NORMAL) -> list[float] | None:
        """Embed text using embed_fn if available."""
        return _embedding.try_embed(self, text, priority)

    def _read_meta(self, key: str) -> str | None:
        """Read a ``memory_meta`` value, or None when absent."""
        row = self._fetch_one_locked("SELECT value FROM memory_meta WHERE key = ?", (key,))
        return str(row["value"]) if row is not None else None

    def _write_meta_in_transaction(self, key: str, value: str) -> None:
        """Write metadata under the caller's database lock and transaction."""
        with self._db_lock:
            self.db.execute(
                "INSERT INTO memory_meta (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, value, _now_iso()),
            )

    def _write_meta(self, key: str, value: str) -> None:
        """Upsert a ``memory_meta`` value."""
        with self._db_lock:
            self._write_meta_in_transaction(key, value)
            self.db.commit()

    def recorded_rebuild_generation(self) -> str:
        """Explicit rebuild request whose old vectors were durably invalidated."""
        return _embedding_repair.recorded_rebuild_generation(self)

    def embedding_repair_state(self, generation: str) -> tuple[bool, int]:
        """Snapshot invalidation acknowledgment and remaining live NULL vectors."""
        return _embedding_repair.embedding_repair_state(self, generation)

    def begin_space_change(self) -> None:
        """Mark the start of a vector-space change (a live model swap)."""
        return _embedding_repair.begin_space_change(self)

    @property
    def space_generation(self) -> int:
        """The current vector-space generation, for callers that pre-embed.

        Read this BEFORE computing a vector you intend to hand to
        :meth:`write_lesson`, then pass it back as ``rule_emb_generation``.
        """
        return self._space_generation

    def set_embedding_dim(self, dim: int) -> bool:
        """Retarget the store at a new vector width. Returns True if it changed."""
        return _embedding_repair.set_embedding_dim(self, dim)

    def recorded_embedding_space(self) -> str | None:
        """Signature the stored vectors were produced under, or None if unrecorded."""
        return _embedding_repair.recorded_embedding_space(self)

    def has_stored_embeddings(self) -> bool:
        """Whether any persisted vector needs an existing space attribution."""
        return _embedding_repair.has_stored_embeddings(self)

    def reconcile_embedding_space(
        self,
        signature: str,
        *,
        clear_when_unknown: bool = False,
        force: bool = False,
        rebuild_generation: str = "",
    ) -> int:
        """Serialize the request check and invalidation across SQLite connections."""
        return _embedding_repair.reconcile_embedding_space(
            self,
            signature,
            clear_when_unknown=clear_when_unknown,
            force=force,
            rebuild_generation=rebuild_generation,
        )

    def _reconcile_embedding_space_locked(
        self,
        signature: str,
        *,
        clear_when_unknown: bool = False,
        force: bool = False,
        rebuild_generation: str = "",
    ) -> int:
        """Discard embeddings produced by a DIFFERENT model. Returns rows invalidated."""
        return _embedding_repair.reconcile_embedding_space_locked(
            self,
            signature,
            clear_when_unknown=clear_when_unknown,
            force=force,
            rebuild_generation=rebuild_generation,
        )

    def has_pending_embeddings(self) -> bool:
        """True when any row is waiting for a vector. Never loads the model."""
        return _embedding_repair.has_pending_embeddings(self)

    def _backfill_rows(
        self, sql: str, *, kind: str, identity: str, limit: int | None
    ) -> list[sqlite3.Row]:
        """Page bounded repair fairly, including past rows whose inference failed."""
        return _embedding_repair.backfill_rows(self, sql, kind=kind, identity=identity, limit=limit)

    def backfill_missing_embeddings(
        self,
        progress: "Callable[[int, int], None] | None" = None,
        *,
        pace: bool = True,
        max_rows_per_kind: int | None = None,
        should_stop: "Callable[[], bool] | None" = None,
    ) -> int:
        """Compute missing episodic embeddings and extend the resident index."""
        return _embedding_repair.backfill_missing_embeddings(
            self, progress, pace=pace, max_rows_per_kind=max_rows_per_kind, should_stop=should_stop
        )

    def _backfill_lesson_embeddings(
        self,
        progress: "Callable[[int, int], None] | None" = None,
        *,
        pace: bool = True,
        max_rows: int | None = None,
        should_stop: "Callable[[], bool] | None" = None,
    ) -> int:
        """Embed lesson rows whose vector is NULL. Returns the count embedded."""
        return _embedding_repair.backfill_lesson_embeddings(
            self, progress, pace=pace, max_rows=max_rows, should_stop=should_stop
        )

    def _backfill_semantic_kv_embeddings(
        self,
        progress: "Callable[[int, int], None] | None" = None,
        *,
        pace: bool = True,
        max_rows: int | None = None,
        should_stop: "Callable[[], bool] | None" = None,
    ) -> int:
        """Embed non-lesson semantic rows whose vector is NULL. Returns the count."""
        return _embedding_repair.backfill_semantic_kv_embeddings(
            self, progress, pace=pace, max_rows=max_rows, should_stop=should_stop
        )

    def migrate_from_markdown(self) -> dict[str, int]:
        """Migrate Global V1 Markdown and JSONL learning into its vector store."""
        if self._memory_version == 2:
            raise ValueError("Member databases do not import legacy learned files")
        # Honor KIROCREW_HOME via config_dir() so the source directory matches
        # what legacy_memory_present() detects — hardcoding Path.home() would
        # migrate a different dir than was detected under a custom home, then
        # flip migrated=True having imported nothing (silent data loss).
        home = config_dir()
        base = home / "workspace" / "memory"
        counts = {"semantic": 0, "episodic": 0, "skipped": 0}
        # The three markdown sources below are memory SOURCE text, so they are read
        # through the injected file access rather than with Path methods -- if the
        # default workspace's memory tree is mounted elsewhere, this import has to
        # read the tree that is actually in use, not an abandoned local copy of it.
        #
        # lessons.jsonl is deliberately NOT read that way: it sits at the data-home
        # ROOT, outside the memory tree, so no memory mount covers it.
        from kiro_crew.memory_files import memory_files_for
        from kiro_crew.platform.interfaces import MemoryRoots

        files = memory_files_for(
            MemoryRoots(
                workspace=home / "workspace",
                memory_dir=base,
                history_dir=base / "history",
                memory_version=1,
            )
        )

        # ── Lessons ──
        lessons_path = home / "lessons.jsonl"
        if lessons_path.is_file():
            for lesson in _migration.legacy_lessons(lessons_path):
                if lesson is _migration.SKIP:
                    counts["skipped"] += 1
                    continue
                rule, category, negative, raw_scope, applies, cites, cited_commit = lesson
                try:
                    if rule and self.write_lesson(
                        rule,
                        category,
                        negative,
                        source="migration",
                        repo_scope=raw_scope,
                        applies=applies,
                        cites=cites,
                        cited_commit=cited_commit,
                    ):
                        counts["semantic"] += 1
                    else:
                        counts["skipped"] += 1
                except (json.JSONDecodeError, KeyError):
                    counts["skipped"] += 1

        # ── Preferences ──
        prefs_path = base / "preferences.md"
        if files.exists(prefs_path):
            for text in _migration.preference_bullets(files, prefs_path):
                # Try smart key-value extraction
                parsed = self._parse_preference(text)
                if parsed:
                    key, value = parsed
                    if self.set_semantic(key, value, 0.85, "migration") is None:
                        counts["semantic"] += 1
                        continue
                # Fallback: write as episodic
                if self.write_episodic(
                    text,
                    embedding=self._try_embed(text),
                    importance=0.6,
                    source="migration",
                    tags=["preference"],
                ):
                    counts["episodic"] += 1
                else:
                    counts["skipped"] += 1

        # ── Projects ──
        proj_path = base / "projects.md"
        if files.exists(proj_path):
            for kind, text, project in _migration.project_entries(files, proj_path):
                if kind == "project":
                    if self.set_semantic("project.name", text, 0.85, "migration") is None:
                        counts["semantic"] += 1
                    else:
                        counts["skipped"] += 1
                elif text and self.write_episodic(
                    text,
                    embedding=self._try_embed(text),
                    importance=0.5,
                    source="migration",
                    tags=["project", project],
                ):
                    counts["episodic"] += 1
                else:
                    counts["skipped"] += 1

        # ── History ──
        history_dir = base / "history"
        for text in _migration.history_paragraphs(
            files, history_dir, min_chars=_EPISODIC_TEXT_MIN, max_chars=_EPISODIC_TEXT_MAX
        ):
            if self.write_episodic(
                text,
                embedding=self._try_embed(text),
                importance=0.4,
                source="migration",
                tags=["history"],
            ):
                counts["episodic"] += 1
            else:
                counts["skipped"] += 1

        embedded_row = self._fetch_one_locked(
            "SELECT COUNT(*) FROM episodic_memories WHERE is_deleted=0 AND embedding IS NOT NULL"
        )
        embedded_n = embedded_row[0] if embedded_row is not None else 0
        logger.info(
            "Migration complete: semantic=%d episodic=%d skipped=%d embedded=%d",
            counts["semantic"],
            counts["episodic"],
            counts["skipped"],
            embedded_n,
        )
        return counts

    def import_memory(self, data: dict) -> dict[str, int]:
        """Import memory from an export dict with 'semantic' and 'episodic' arrays."""
        counts = {"semantic": 0, "episodic": 0, "skipped": 0}
        for entry in data.get("semantic", []):
            try:
                val = (
                    json.loads(entry["value_json"])
                    if isinstance(entry.get("value_json"), str)
                    else entry.get("value")
                )
                conf = float(entry.get("confidence", 0.85))
                src = entry.get("source", "import")
                if self.set_semantic(entry["key"], val, conf, src) is None:
                    counts["semantic"] += 1
                else:
                    counts["skipped"] += 1
            except Exception:
                counts["skipped"] += 1
        for entry in data.get("episodic", []):
            try:
                if self.write_episodic(
                    entry["text"],
                    embedding=self._try_embed(entry["text"]),
                    importance=float(entry.get("importance", 0.5)),
                    source=entry.get("source", "import"),
                    tags=(
                        json.loads(entry["tags"])
                        if isinstance(entry.get("tags"), str)
                        else entry.get("tags", [])
                    ),
                ):
                    counts["episodic"] += 1
                else:
                    counts["skipped"] += 1
            except Exception:
                counts["skipped"] += 1
        return counts

    def _fts5_episodic_search(
        self, query: str, limit: int, tag_filter: list[str] | None = None
    ) -> list[dict]:
        """Simple LIKE-based text + tags search fallback for episodic memories."""
        return _episodic_search.fts5_episodic_search(self, query, limit, tag_filter)

    # ── Episodic Promotion ──

    def promote_episodic_patterns(self, min_count: int = 5, min_sim: float = 0.75) -> int:
        """Scan episodic memories for repeated patterns and promote to semantic facts.

        Returns count of promoted entries.
        """
        if not self.embed_fn or not _HAS_NUMPY:
            logger.info("Promotion skipped: embeddings not available")
            return 0

        promoted = 0
        skipped = 0
        rows = self._fetch_all_locked(
            "SELECT id, text, embedding FROM episodic_memories "
            "WHERE is_deleted = 0 AND embedding IS NOT NULL "
            "ORDER BY importance DESC, created_at DESC LIMIT 500"
        )

        # Cluster similar episodic memories
        clusters = _migration.cluster_episodes(rows, min_sim)

        # Promote clusters with min_count+ members
        for members in clusters.values():
            if len(members) < min_count:
                continue
            canonical = max(members, key=lambda m: len(m["text"]))
            text = canonical["text"]

            key = self._infer_semantic_key(text)
            if not key:
                continue

            value = self._extract_value_from_text(text)
            # ``derived_from`` names the episode this fact was synthesized out of.
            # The cluster's own rows are tombstoned immediately below, so without it
            # the promoted fact is the only surviving trace and nothing records what
            # it came from -- the one provenance question a reader of a promoted row
            # actually asks. The canonical member is the representative the cluster
            # was collapsed onto.
            reject = self.set_semantic(
                key,
                value,
                0.9,
                "promotion",
                facets=memory_schema.MemoryFacets(derived_from=str(canonical["id"])),
            )
            if reject is None:
                promoted += 1
                for m in members:
                    self._delete_episodic_row(m["id"])
                logger.info("Promoted %d episodic → %s: %s", len(members), key, value[:60])
            else:
                # A refused cluster keeps its rows and re-clusters identically next pass, so the
                # refusal repeats forever: count every pass, but warn only the first time per key.
                skipped += 1
                reject_code, reject_reason = reject
                # Keyed on the cause too: _infer_semantic_key returns a constant for every
                # "user prefers" cluster, so keying on key alone hides refusals of other causes.
                if (key, reject_code.value) not in self._promotion_refused:
                    self._promotion_refused[(key, reject_code.value)] = None
                    while len(self._promotion_refused) > _MAX_PROMOTION_REFUSED:
                        self._promotion_refused.popitem(last=False)
                    logger.warning(
                        "Promotion skipped %s (%s: %s): %d rows retained, retried each pass",
                        key,
                        reject_code.value,
                        reject_reason,
                        len(members),
                    )

        if skipped:
            logger.info("Promotion pass: %d promoted, %d skipped", promoted, skipped)
        return promoted

    @staticmethod
    def _infer_semantic_key(text: str) -> str | None:
        """Infer semantic key from episodic text."""
        return _migration.infer_semantic_key(text)

    @staticmethod
    def _extract_value_from_text(text: str) -> str:
        """Extract value from episodic text."""
        return _migration.extract_value_from_text(text)

    # ── Observability ──

    def get_rejection_stats(self) -> dict[str, int]:
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

    def get_context_preview(self, query_text: str = "") -> dict:
        """Preview what would be injected into context (for debugging)."""
        return _recall.get_context_preview(self, query_text)

    def _check_recall_query(self, query: _RecallQuery | None) -> None:
        """Called under the store lock before a read and before publication."""
        return _recall.check_recall_query(self, query)

    def recall(
        self,
        query_text: str,
        *,
        cap: int = 3000,
        project_dir: str | Path | None = None,
        keep: Callable[[list[dict]], list[dict] | None] | None = None,
    ) -> dict:
        """Compute once; discard mixed-space results and retry keyword-only once."""
        return _recall.recall(self, query_text, cap=cap, project_dir=project_dir, keep=keep)

    def _recall_once(
        self,
        query_text: str,
        *,
        cap: int,
        project_dir: str | Path | None,
        query: _RecallQuery,
        keep: Callable[[list[dict]], list[dict] | None] | None = None,
    ) -> dict:
        """Bounded on-demand member context with the evidence actually selected."""
        return _recall.recall_once(
            self, query_text, cap=cap, project_dir=project_dir, query=query, keep=keep
        )

"""Vector memory — structured semantic + episodic memory with audit trail.

Storage: ~/.kiro/crew/memory.db (SQLite, WAL mode)
FAISS index: ~/.kiro/crew/memory.faiss (optional, for vector search)

Semantic: key-value store with allow-list keys, confidence gating,
conflict resolution, injection detection, and event logging.
Episodic: conversation fragments with embeddings, importance scoring,
time-decay retrieval via FAISS (falls back to FTS5 without embeddings).

:class:`VectorMemoryStore` owns the one SQLite connection and its ``_db_lock``: the
instance state every collaborator reads (built in ``__init__``), the lineage binding,
the properties (``algorithm_version``, ``policy_revision``, ``db``,
``space_generation``), the locked fetch helpers and read counters, ``get_semantic``,
``reconfigure``, ``_cosine_sim`` and the derived-vector transaction ``_vector_commit``.
Every other member is a collaborator's function: a one-line delegate to a
``kiro_crew.vector_memory_runtime`` rule, or the owner's function itself, bound as the
class attribute of the same name. The write paths, the bookkeeping tables and
derived-state currency are owners in ``vector_memory_runtime``; the code that opens the
connection or names member-only relations lives beside this module
(``vector_memory_lifecycle``, ``vector_memory_member``,
``vector_memory_semantic_write``, ``vector_memory_inventory``). This module re-exports
every name it bound before those owners held it, keeps the redaction of the
injection-refusal snippet (``_injection_audit_snippet``), and is the patch surface every
owner reads its seams from at call time.
"""

from __future__ import annotations

import hashlib
import logging
import math
import struct  # noqa: F401 -- tests reach the struct module as vector_memory.struct
import threading
import time  # noqa: F401 -- patch seam: runtime modules read time through this module
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from sqlite3 import Error as StdlibSQLiteError
from typing import Callable, Literal
from uuid import uuid4  # noqa: F401 -- patch seam: owners read uuid4 through this module

from kiro_crew import memory_record_metadata as record_meta
from kiro_crew import memory_schema, memory_stores, memory_v2, platform_compat  # noqa: F401
from kiro_crew import vector_memory_inventory as _inventory
from kiro_crew import vector_memory_lifecycle as _lifecycle
from kiro_crew import vector_memory_member as _member_db
from kiro_crew import vector_memory_semantic_write as _semantic_write
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
from kiro_crew.owner_only_files import owner_only_opener, prepare_owner_only_sqlite  # noqa: F401
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
from kiro_crew.vector_memory_lifecycle import (  # noqa: F401
    _MEMORY_META_TABLE,
    _MIGRATIONS,
    _SCHEMA_V1,
    _migrate_v2,
)
from kiro_crew.vector_memory_member import (  # noqa: F401
    _member_consolidation_confidence,
    _member_consolidation_metadata,
    _member_identity,
    _member_store,
    consolidation_source_digest,
    create_member_database,
    declared_store,
    open_member_database,
    read_member_database_identity,
)
from kiro_crew.vector_memory_runtime import bulk_ingest as _bulk_ingest
from kiro_crew.vector_memory_runtime import derived_state as _derived_state
from kiro_crew.vector_memory_runtime import embedding as _embedding
from kiro_crew.vector_memory_runtime import embedding_repair as _embedding_repair
from kiro_crew.vector_memory_runtime import episodic_search as _episodic_search
from kiro_crew.vector_memory_runtime import episodic_write as _episodic_write
from kiro_crew.vector_memory_runtime import faiss_index as _faiss_index
from kiro_crew.vector_memory_runtime import lesson_write as _lesson_write
from kiro_crew.vector_memory_runtime import lessons as _lessons
from kiro_crew.vector_memory_runtime import migration as _migration
from kiro_crew.vector_memory_runtime import recall as _recall
from kiro_crew.vector_memory_runtime import retirement as _retirement
from kiro_crew.vector_memory_runtime import semantic as _semantic
from kiro_crew.vector_memory_runtime import (  # noqa: F401
    semantic_scoring_patch as _semantic_scoring_patch,
)
from kiro_crew.vector_memory_runtime import store_bookkeeping as _store_bookkeeping
from kiro_crew.vector_memory_runtime import term_index as _term_index  # noqa: F401
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
from kiro_crew.vector_memory_runtime.store_bookkeeping import _MAX_EVENTS  # noqa: F401
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
from kiro_crew.vector_memory_runtime.write_outcomes import (  # noqa: F401
    _LESSON_WROTE_OUTCOMES,
    EpisodicWriteOutcome,
    LessonWriteOutcome,
    LessonWriteResult,
    SemanticRejectCode,
)

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


_AUDITABLE_REJECT_CODES = {
    SemanticRejectCode.ALLOWLIST,
    SemanticRejectCode.CONFIDENCE,
    SemanticRejectCode.CONSOLIDATION_VALUE_SIZE,
    SemanticRejectCode.INJECTION,
    SemanticRejectCode.RESERVED_PREFIX,
    SemanticRejectCode.VALUE_EMPTY,
    SemanticRejectCode.WORK_STATE,
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
#
# This is the FLOOR of the scaled cap below, not a fixed cap. The per-call scan
# the overflow path falls back to re-stems the whole population in pure Python on
# every recall, holding the GIL that every turn's context build needs, so on a
# large store that fallback serializes turn builds against each other and against
# consolidation writes. A large store holds the resident set and keeps the term
# index instead — but only on a host with the memory to spare, so the effective
# cap scales with available memory (``_semantic_scoring_max_bytes``).
_SEMANTIC_SCORING_MAX_BYTES = 64 * 1024 * 1024
# How far above the floor the scaled cap may reach. The hard ceiling is
# ``_SEMANTIC_SCORING_CEILING_MULTIPLE * _SEMANTIC_SCORING_MAX_BYTES``, computed
# from the floor at call time so it tracks the floor rather than being a second
# fixed number that could drift from it: 16 x 64 MiB = 1 GiB. A store's resident
# scoring set is bounded user data (keys + values + embeddings); past ~1 GiB the
# memory spent to avoid the per-call scan is not a good trade even on a large
# host, and an unbounded cap lets one store's set dominate the gateway's RSS.
# 1 GiB holds a large store (a 54k-row 1024-d store's ~266 MB set) with headroom.
_SEMANTIC_SCORING_CEILING_MULTIPLE = 16
# Fraction of currently-available memory the resident semantic scoring set may
# occupy. 1/16 keeps the set a small slice of free memory: a ~266 MB set is
# admitted once a host has ~4.3 GB free, and the 1 GiB ceiling is reached at
# ~16 GB free. A small host keeps the 64 MiB floor and never loads a resident
# set it cannot hold.
_SEMANTIC_SCORING_AVAIL_FRACTION = 1 / 16


def _semantic_scoring_max_bytes() -> int:
    """Memory-scaled ceiling on the resident semantic scoring set, in bytes.

    ``clamp(floor, fraction * available, CEILING_MULTIPLE * floor)``, where the
    floor is ``_SEMANTIC_SCORING_MAX_BYTES`` read at call time (so pinning that
    constant moves both the floor and the derived ceiling). The probe is the same
    cgroup-clamped available-memory reader the dynamic sub-agent cap uses, so a
    container's limit binds. FAIL SAFE: when available memory cannot be read
    (probe returns <= 0, or raises), the 64 MiB floor is kept, so a host whose
    memory is unmeasurable never loads a resident set larger than the floor.
    """
    floor = _SEMANTIC_SCORING_MAX_BYTES
    ceiling = _SEMANTIC_SCORING_CEILING_MULTIPLE * floor
    try:
        from kiro_crew.subagent import _available_memory_gb

        available_gb = _available_memory_gb()
    except Exception:  # pragma: no cover - defensive; the probe must never raise here
        return floor
    if available_gb <= 0:  # unreadable probe -> fail safe to the floor
        return floor
    available_bytes = available_gb * 1024 * 1024 * 1024
    scaled = int(available_bytes * _SEMANTIC_SCORING_AVAIL_FRACTION)
    return max(floor, min(scaled, ceiling))


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


def _injection_audit_snippet(text: str) -> str:
    """The redacted, bounded excerpt an injection refusal records in its audit event."""
    return redact_and_truncate(text, 200)


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

# What ``VectorMemoryStore._read_pooled`` returns when a read must run on the
# shared connection under ``_db_lock`` instead of on a pooled read connection.
_ON_SHARED_CONNECTION = object()


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
    always on; only the EXPOSURE is a surface decision. Every increment and
    every snapshot takes the counters' own lock: reads on the pooled read
    connections run outside ``_db_lock``, so that lock does not cover them. A
    snapshot is never torn and no count is lost to a concurrent reader.
    """

    statements_executed: int = 0
    rows_read: int = 0
    semantic_rows_read: int = 0
    semantic_full_scans: int = 0
    episodic_rows_read: int = 0
    episodic_full_scans: int = 0
    _guard: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    def record(self, rows: int, scan: _ScanSurface | None = None) -> None:
        """Credit one materialized SELECT of *rows* rows.

        *scan* marks the read as a whole-population retrieval scan of that
        surface; leaving it None still credits the all-tables totals, which is
        the right answer for a bounded or keyed read.
        """
        with self._guard:
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
        with self._guard:
            return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "_guard"}


def _read_pooled(store: "VectorMemoryStore", sql: str, params: Sequence[object], *, one: bool):
    """Run one SELECT on a pooled read-only connection, outside ``_db_lock``.

    Returns ``_ON_SHARED_CONNECTION`` when the read must run on the shared
    connection under the lock instead:

    - this thread already holds the lock, so the read belongs to a locked
      section and must see that section's own uncommitted writes;
    - the pool is off (before ``init()`` finished, after ``close()``, or a
      file not in WAL mode);
    - the shared connection has a transaction open that no thread is
      inside (a failed write left it open), whose rows only that
      connection can see;
    - the pooled connection failed; the shared one reports the real error.

    A read that runs while another thread is writing sees the last
    committed state, the same rows it would have read just before that
    write took the lock.
    """
    db = store.db  # readiness and "not initialized" errors, unchanged
    lock = store._db_lock
    held = getattr(lock, "held_by_current_thread", None)
    if held is None or held() or not store._readers.enabled:
        return _ON_SHARED_CONNECTION
    if db.in_transaction and lock.acquire(blocking=False):
        try:
            if db.in_transaction:
                return _ON_SHARED_CONNECTION
        finally:
            lock.release()
    conn = store._readers.checkout()
    if conn is None:
        return _ON_SHARED_CONNECTION
    broken = True
    try:
        cursor = conn.execute(sql, params)
        try:
            result = cursor.fetchone() if one else cursor.fetchall()
        finally:
            cursor.close()
        broken = False
        return result
    except sqlite3.OperationalError:
        # A statement this connection cannot run (a function only the shared
        # connection registers, an interrupt from close()). The connection
        # itself is fine; close() discards it through the generation check.
        broken = False
        return _ON_SHARED_CONNECTION
    except sqlite3.Error:
        return _ON_SHARED_CONNECTION
    finally:
        store._readers.checkin(conn, broken=broken)


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
        # Whether _db's own close is kept from deleting the -wal (sqlite_wal.keep_wal_on_close).
        self._wal_kept_on_close = False
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
        #
        # Plain reads do not take it: once init() has the file open in WAL mode,
        # the fetch helpers run on read-only connections from ``_readers``, so a
        # read waits neither for another read nor for a writer.
        self._db_lock = _lifecycle.StoreDbLock()
        self._readers = _lifecycle.ReaderPool()
        # Read-volume totals, with their own small lock (see _ReadCounters):
        # reads on the pooled connections run outside _db_lock.
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
        # One rebuild of the resident set at a time; taken before ``_db_lock``, never inside it.
        self._semantic_scoring_build_lock = threading.Lock()
        # Keys patched into the resident set while their transaction was still open.
        self._semantic_scoring_pending: set[str] = set()
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

    # Members whose body lives in a collaborator module: each name below IS that
    # module's function, so a class-level patch of it reaches every caller.

    # ── bound from vector_memory_runtime/store_bookkeeping.py ──
    with_record_metadata = _store_bookkeeping.with_record_metadata
    _ineligible_ids = _store_bookkeeping._ineligible_ids
    _eligible_rows = _store_bookkeeping._eligible_rows
    _record_mutation = _store_bookkeeping._record_mutation
    _log_event = _store_bookkeeping._log_event
    get_events = _store_bookkeeping.get_events
    rotate_events = _store_bookkeeping.rotate_events
    _read_meta = _store_bookkeeping._read_meta
    _write_meta_in_transaction = _store_bookkeeping._write_meta_in_transaction
    _write_meta = _store_bookkeeping._write_meta
    get_rejection_stats = _store_bookkeeping.get_rejection_stats

    # ── bound from vector_memory_runtime/derived_state.py ──
    invalidate_semantic_content = _derived_state.invalidate_semantic_content
    _invalidate_episodic_scoring = _derived_state._invalidate_episodic_scoring
    _invalidate_semantic_scoring = _derived_state._invalidate_semantic_scoring
    _sqlite_data_version = _derived_state._sqlite_data_version
    _embedding_token = _derived_state._embedding_token
    _embedding_current = _derived_state._embedding_current
    _embedding_config_guard = _derived_state._embedding_config_guard

    # ── bound from vector_memory_runtime/episodic_write.py ──
    embed_episodic = _episodic_write.embed_episodic
    write_episodic = _episodic_write.write_episodic
    write_episodic_outcome = _episodic_write.write_episodic_outcome
    has_episodic_text = _episodic_write.has_episodic_text
    restore_episodic = _episodic_write.restore_episodic
    delete_episodic = _episodic_write.delete_episodic
    _get_episodic = _episodic_write._get_episodic
    _delete_episodic_row = _episodic_write._delete_episodic_row
    _enforce_episodic_cap = _episodic_write._enforce_episodic_cap

    # ── bound from vector_memory_runtime/lesson_write.py ──
    write_lesson = _lesson_write.write_lesson
    embed_lesson = _lesson_write.embed_lesson

    # ── bound from vector_memory_runtime/bulk_ingest.py ──
    migrate_from_markdown = _bulk_ingest.migrate_from_markdown
    import_memory = _bulk_ingest.import_memory
    promote_episodic_patterns = _bulk_ingest.promote_episodic_patterns

    # ── bound from vector_memory_lifecycle.py ──
    _secret_bearing_files = _lifecycle._secret_bearing_files
    _restrict_memory_files = _lifecycle._restrict_memory_files
    init = _lifecycle.init
    _init_database = _lifecycle._init_database
    _schema_steps = _lifecycle._schema_steps
    _apply_migration = _lifecycle._apply_migration
    _crew_stamps_missing = _lifecycle._crew_stamps_missing
    _stamp_crew_lineage = _lifecycle._stamp_crew_lineage
    close = _lifecycle.close
    _release_store_use_lock = _lifecycle._release_store_use_lock

    # ── bound from vector_memory_member.py ──
    _write_history = _member_db._write_history
    _append_history = _member_db._append_history
    append_history = _member_db.append_history
    read_history_entries = _member_db.read_history_entries
    _read_editable_history_for_day = _member_db._read_editable_history_for_day
    read_editable_history = _member_db.read_editable_history
    replace_today_history = _member_db.replace_today_history
    search_memory = _member_db.search_memory
    rebuild_memory_index = _member_db.rebuild_memory_index
    consolidation_receipt = _member_db.consolidation_receipt
    apply_consolidation = _member_db.apply_consolidation
    seed_item_if_absent = _member_db.seed_item_if_absent

    # ── bound from vector_memory_inventory.py ──
    get_episodic_list = _inventory.get_episodic_list
    _require_facets = _inventory._require_facets
    list_by_facets = _inventory.list_by_facets
    count_by_facet = _inventory.count_by_facet
    memory_stats = _inventory.memory_stats

    # ── bound from vector_memory_semantic_write.py ──
    _write_semantic = _semantic_write._write_semantic

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
    # test_vector_memory_lock_discipline.py). Route plain SELECTs through these helpers; only
    # read-modify-write sections that must be atomic should take the lock
    # explicitly. A helper called outside any locked section runs its SELECT on a
    # pooled read-only connection (``_read_pooled``) and takes no lock at all;
    # called inside one, it stays on ``self.db`` under the held lock. Both
    # helpers materialize results before returning, so callers never iterate a
    # live cursor — and per the lock's contract, never call a blocking embed
    # while holding it.

    def _fetch_all_locked(
        self,
        sql: str,
        params: Sequence[object] = (),
        *,
        scan: _ScanSurface | None = None,
    ) -> list[sqlite3.Row]:
        """Run a SELECT; return materialized rows (see ``_read_pooled`` for where).

        Pass *scan* at the few call sites that read a whole population, so the
        read-volume counters can attribute it to that surface (see
        :class:`_ReadCounters`). The default leaves the read in the all-tables
        totals only, which is correct for a bounded or keyed fetch.
        """
        pooled = _read_pooled(self, sql, params, one=False)
        if pooled is _ON_SHARED_CONNECTION:
            with self._db_lock:
                rows = self.db.execute(sql, params).fetchall()
        else:
            rows = pooled
        self._reads.record(len(rows), scan)
        return rows

    def _fetch_one_locked(self, sql: str, params: Sequence[object] = ()) -> sqlite3.Row | None:
        """Run a SELECT; return the first row or None (see ``_read_pooled``)."""
        pooled = _read_pooled(self, sql, params, one=True)
        if pooled is _ON_SHARED_CONNECTION:
            with self._db_lock:
                row = self.db.execute(sql, params).fetchone()
        else:
            row = pooled
        self._reads.record(1 if row is not None else 0)
        return row

    def read_counters(self) -> dict[str, int]:
        """Return this store's monotonic read-volume totals.

        Per store INSTANCE and per process: the counts start at zero on
        construction, only ever rise, and are not persisted, so two processes
        over one database file report their own reads independently. See
        :class:`_ReadCounters` for what each key counts.
        """
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
        self,
        limit: int | None = None,
        offset: int = 0,
        *,
        q: str = "",
        after_key: str | None = None,
        exclude_lessons: bool = False,
    ) -> list[dict]:
        """Get active semantic memory entries."""
        return _semantic.get_all_semantic(
            self,
            limit,
            offset,
            q=q,
            after_key=after_key,
            exclude_lessons=exclude_lessons,
        )

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
        from kiro_crew import recall_pool as _recall_pool  # circular import at module level

        pooled = _recall_pool.offload(
            self, "semantic", {"query_text": query_text, "cap": cap, "facts_only": facts_only}
        )
        if pooled is not None:
            return pooled
        return _semantic.get_semantic_context(self, query_text, cap, facts_only=facts_only)

    def _semantic_candidates_v2(
        self, query_text: str, *, recall_query: _RecallQuery | None = None
    ) -> list[dict]:
        """Keep member preferences; retrieve facts only with relevant evidence."""
        return _semantic.semantic_candidates_v2(self, query_text, recall_query=recall_query)

    # ── Event Log ──

    # ── FAISS Index ──

    def invalidate_episode_content(self) -> None:
        """Drop derived vectors after a content edit; SQLite stays authoritative."""
        return _faiss_index.invalidate_episode_content(self)

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

    def _semantic_scoring_set(self) -> _SemanticScoringSet | None:
        """Return cached semantic scoring rows, or ``None`` for a per-call scan."""
        return _semantic.semantic_scoring_set(self)

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

    def get_retired_episodic(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """Episodes a semantic write SUPERSEDED, newest first, with what superseded them."""
        return _retirement.get_retired_episodic(self, limit, offset)

    def get_episodic_context(
        self,
        query_embedding: list[float] | None = None,
        query_text: str = "",
        cap: int = 3000,
    ) -> str:
        """Format episodic search results for prompt injection."""
        from kiro_crew import recall_pool as _recall_pool  # circular import at module level

        pooled = _recall_pool.offload(
            self,
            "episodic",
            {
                "query_embedding": None if query_embedding is None else list(query_embedding),
                "query_text": query_text,
                "cap": cap,
            },
        )
        if pooled is not None:
            return pooled
        return _episodic_search.get_episodic_context(self, query_embedding, query_text, cap)

    # ── Facets: reading a carve back out ──

    # ── Episodic Helpers ──

    @staticmethod
    def _matches_tags(mem: dict, tag_filter: list[str]) -> bool:
        """Check if an episodic entry matches ANY of the given tags."""
        return _episodic_search.matches_tags(mem, tag_filter)

    def _decay_rate_for(self, raw_tags: str | list[str] | None) -> float:
        """Resolve the per-day recency decay rate for an episodic row."""
        return _episodic_search.decay_rate_for(self, raw_tags)

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
    #: The memory algorithm the open file runs (1 for V1, 2 for a member database), set
    #: by ``_init_database`` in ``vector_memory_lifecycle`` once the file is admitted.
    _memory_version: int

    def _touch_last_accessed(self, mem_ids: list[str]) -> None:
        """Record an access timestamp for episodic rows, debounced per row."""
        return _episodic_search.touch_last_accessed(self, mem_ids)

    # ── Lessons ──

    @staticmethod
    def _lesson_keywords(text: str) -> set[str]:
        """Extract significant words from a lesson rule, ignoring stop words."""
        return _lessons.lesson_keywords(text)

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
        self,
        rule_substring: str,
        repo_scope: str | None = None,
        *,
        exact: bool = False,
        keep_one: bool = False,
    ) -> bool:
        """Delete lessons whose value contains rule_substring."""
        return _lessons.delete_lesson(
            self, rule_substring, repo_scope, exact=exact, keep_one=keep_one
        )

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
        from kiro_crew import recall_pool as _recall_pool  # circular import at module level

        if _recall_pool.pool_enabled():
            job_args = _recall_pool.lessons_args(
                self,
                query_text,
                cap,
                project_dir,
                recall_query,
                background,
                hard_cap,
                directive_budget,
                experience_budget,
            )
            pooled = _recall_pool.offload(self, "lessons", job_args)
            if pooled is not None:
                return pooled
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

    def startup_lesson_query(self, query_text: str) -> _RecallQuery:
        """Embed a first message once for ranking its startup lessons."""
        return _lessons.startup_lesson_query(self, query_text)

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

    def _fts5_episodic_search(
        self, query: str, limit: int, tag_filter: list[str] | None = None
    ) -> list[dict]:
        """Simple LIKE-based text + tags search fallback for episodic memories."""
        return _episodic_search.fts5_episodic_search(self, query, limit, tag_filter)

    # ── Episodic Promotion ──

    @staticmethod
    def _infer_semantic_key(text: str) -> str | None:
        """Infer semantic key from episodic text."""
        return _migration.infer_semantic_key(text)

    @staticmethod
    def _extract_value_from_text(text: str) -> str:
        """Extract value from episodic text."""
        return _migration.extract_value_from_text(text)

    # ── Observability ──

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

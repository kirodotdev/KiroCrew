"""Two schema lineages for one vector-store engine.

A crew silo created from this module forward is a ``memory_items`` file — one row
table carrying the three kinds (``directive``, ``fact``, ``episode``) plus the
carve facets — and it never gets the v1 tables at all. Every other vector file,
above all ``config_dir()/"memory.db"``, stays on the v1 lineage whose migration
list is frozen at ``[1, 2, 3]``. There is no cutover or dual write: the
two lineages coexist for the life of each file. Shared extension tables in
``memory_record_metadata`` backfill record identity and revisions without
rewriting either lineage's original rows.

Why a file's own shape decides, and not a config flag
-----------------------------------------------------
:func:`detect_lineage` reads SQLite's own schema table. Every vector file that exists on
any install today holds ``semantic_memory`` as a real TABLE, so it answers
:data:`LINEAGE_V1` and :func:`lineage_for_new_file` — the only place a path is
consulted — is never reached for it. That is what makes "the existing memory
lineage is preserved" a property of the code path rather than a claim a test
asserts: there is no route from a populated file to :data:`MIGRATIONS_CREW`, so a
later edit to the path predicate cannot reach the file either.

The path predicate is POSITIVE membership
(``memory_stores.named_store_of_db``). Spelled as a negation
(``db_path != config_dir()/"memory.db"``) it would capture the eval runner's
``ws/"vector_memory.db"``, the bench ingest path, the onboarding importer's
``destination/"memory.db"`` and every ``tmp_path`` in the suite.

Third barrier, and the reason the failure direction is safe: the two version
series are DISJOINT (``{1, 2, 3}`` against ``{1001}``), and ``init()`` applies
migrations by set membership. If detection were ever bypassed on a crew file, the
v1 list's ``CREATE TABLE IF NOT EXISTS semantic_memory`` silently no-ops against
the view and then ``_migrate_v2``'s ``ALTER TABLE semantic_memory ADD COLUMN``
raises ``Cannot add a column to a view`` — loud, and before any write.

The views are READ-ONLY, deliberately
-------------------------------------
``semantic_memory`` and ``episodic_memories`` survive as views so the 36 read
statements in ``vector_memory.py`` run byte-identically, and so the four vector
scorers stay partitioned by RELATION: episodic blobs are L2-normalized at write
and semantic/lesson blobs are not, three scorers take a bare dot product, and one
undivided ``embedding`` column would put un-normalized rows in front of them. The
views also expose no facet column, so no existing ranker can read one — that is
what makes "facets are carve axes, never ranking signals" a fact about the
relation rather than a convention someone has to police.

They carry no ``INSTEAD OF`` trigger, and must never be given one:
``snapshot_redact._refuse_update_triggers_that_destroy_rows`` refuses a database
whose UPDATE trigger writes a relation other than the trigger's own, and an
``INSTEAD OF`` trigger on a view does that BY DEFINITION — its exemption requires
``target == tbl_name``, which such a trigger can never satisfy. The refusal is by
trigger name and is permanent, so the whole product database would stop being
backup-inspectable. Writes therefore name the physical table, which is what
:func:`semantic_relation` and :func:`episodic_relation` are for.

LEAF-ish module: stdlib plus ``memory_stores`` (itself stdlib-only), so
``vector_memory`` can import it without a cycle and a future snapshot component
can learn the crew table set without importing the 4,000-line engine.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Callable, Final

from kiro_crew.memory_stores import named_store_of_db

logger = logging.getLogger(__name__)

#: A file holding the v1 tables. The floor: every vector file that exists today.
LINEAGE_V1: Final = "v1"

#: A file holding ``memory_items``. Only a crew silo created from here forward.
LINEAGE_CREW: Final = "crew"

#: The crew lineage's only migration version. Offset far above the v1 series so
#: the two ``schema_version`` sets cannot overlap, which makes a file's row set
#: alone say which lineage it is — independently of :func:`detect_lineage`, and
#: without either lineage's DDL being reachable through the other's set-membership
#: loop.
CREW_SCHEMA_VERSION: Final = 1001

#: ``memory_meta`` key recording the lineage, for observability only. Structure
#: is authoritative for lineage: a lost or hand-edited stamp cannot misclassify
#: a file. The separate private marker below has a fail-closed authorization role.
LINEAGE_META_KEY: Final = "schema_lineage"

#: ``memory_meta`` key recording the store this file belongs to, written once at
#: creation. A silo restored or copied into the wrong directory becomes
#: detectable instead of silently serving another crew. The identity is the
#: STORE, not a crew: several crews may bind one store.
STORE_NAME_META_KEY: Final = "store_name"

#: Durable proof that a crew-lineage file was provisioned as private V2. Unlike
#: ``schema_lineage``, this is authorization-significant: once present, losing
#: or changing the external ownership manifest must fail closed rather than
#: reopening the same physical database as an unowned legacy V1 store.
PRIVATE_MEMORY_VERSION_META_KEY: Final = "private_memory_version"

#: Private owner's identity at the time the durable V2 marker is written.
#: Paired with :data:`STORE_NAME_META_KEY` so both halves of the external
#: manifest can be checked on every later raw database open.
OWNER_MEMBER_META_KEY: Final = "owner_member"

#: The three kinds the design collapses six memory types onto. ``directive`` is
#: behavioural preferences plus lessons, ``fact`` is projects/semantic/user
#: facts, ``episode`` is daily history plus episodic. Stamped from the key
#: prefix at write time and NOT a dispatch axis — the engine keeps discriminating
#: lessons with ``key LIKE 'lesson.%'`` exactly as it does on v1, so ``kind`` is
#: queryable without becoming load-bearing.
KIND_DIRECTIVE: Final = "directive"
KIND_FACT: Final = "fact"
KIND_EPISODE: Final = "episode"

#: The kinds the ``semantic_memory`` view spans. Both are keyed rows sharing one
#: uniqueness domain, which is why the view can present them as v1's one table.
SEMANTIC_KINDS: Final = (KIND_DIRECTIVE, KIND_FACT)

#: Every kind, in the order the ``CHECK`` constraint lists them. The closed set a
#: caller-supplied ``kind`` is validated against, so a typo is refused instead of
#: silently answering "no rows" for a kind that cannot exist.
ALL_KINDS: Final = (*SEMANTIC_KINDS, KIND_EPISODE)

_SEMANTIC_KIND_SQL: Final = "('{}', '{}')".format(*SEMANTIC_KINDS)

#: The row-type column. Named once because both the DDL and the read side splice
#: it, and it is the one groupable column that is NOT a facet.
_KIND_COLUMN: Final = "kind"


# ── The two relations that are NOT lineage-specific ──────────────────────────
#
# ``memory_events`` and ``memory_meta`` are the only product tables both lineages
# hold under the same name AND reach through the same statements: ``_log_event``,
# ``get_events``, ``rotate_events`` and ``_read_meta``/``_write_meta`` name them
# literally, with none of the per-lineage relation indirection the semantic and
# episodic writes carry. So each has ONE definition here, composed into
# ``vector_memory._SCHEMA_V1`` and into :data:`CREW_SCHEMA_SQL` alike.
#
# Two hand copies is the shape that fails silently: ``MIGRATIONS_CREW`` is one
# frozen entry, so a v1 migration adding a column to either table would reach every
# v1 file and NO silo, and the shared INSERT would then fail on silos alone — while
# every v1-backed test in the suite stayed green.
#
# Each fragment is a self-delimiting script: a leading newline so it can follow the
# statement above it, and a trailing one so the next can follow it.

#: The audit trail. Its own table on both lineages rather than folded into
#: ``memory_items``: it needs AUTOINCREMENT, it is the only per-row provenance an
#: episode has, and it is the one relation the engine hard-DELETEs from.
MEMORY_EVENTS_SQL: Final = """
CREATE TABLE IF NOT EXISTS memory_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    memory_key TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_type ON memory_events(memory_type, created_at);
CREATE INDEX IF NOT EXISTS idx_events_key ON memory_events(memory_key);
"""

#: Per-file key/value. Required on both lineages, not optional:
#: ``reconcile_embedding_space`` keys the per-FILE embedding signature here, and a
#: vector table it cannot sweep leaves incomparable vectors behind after a model
#: swap.
MEMORY_META_SQL: Final = """
CREATE TABLE IF NOT EXISTS memory_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

#: Physical row table of a crew silo. Column notes that are not obvious:
#:
#: * ``UNIQUE (key)``, not the design's ``UNIQUE (kind, key)``. v1 spells this
#:   ``key TEXT PRIMARY KEY`` — one row per key, period — and per-kind uniqueness
#:   is strictly WEAKER: it would let ``pref.color`` exist as both a directive and
#:   a fact, and then the ``semantic_memory`` view returns two rows where every
#:   statement in the engine expects at most one. SQLite treats NULLs as distinct
#:   in a unique index, so episodes (``key IS NULL``) stay unconstrained and are
#:   identified by ``id`` alone.
#: * Timestamps are TEXT ISO-8601, not the design's ``REAL``. Seven sites rank
#:   these by lexicographic string comparison and two parse them with
#:   ``datetime.fromisoformat``; one of the seven is the episodic CAP EVICTION,
#:   where a wrong order deletes the wrong memories. SQLite also sorts REAL before
#:   TEXT, so a mixed column is worse than either.
#: * No ``REFERENCES`` clause anywhere. ``PRAGMA foreign_keys`` is 0 on this
#:   connection and is per-connection, so the clauses would enforce nothing;
#:   turning it on would newly enforce constraints against rows the engine
#:   already wrote.
#: * NO ``embedding_dim`` column, despite the design asking for one. Its stated
#:   purpose -- "store the dim, do not assume 1024" -- is already met without it:
#:   the width IS ``length(embedding)/4``, derivable from the blob at any time, and
#:   the two comparability checks read the blob's byte length rather than any
#:   column. As stored data it could only DRIFT, and would: six lazy-backfill and
#:   repair statements set ``embedding`` alone, so a row would carry a vector beside
#:   a stale or NULL width. A write-only column that can disagree with the value it
#:   describes is worse than no column. (A ``GENERATED ALWAYS`` column would make
#:   drift impossible, but it needs SQLite 3.31+ and this build documents no SQLite
#:   floor and uses no generated column anywhere else.)
#: * NOT ``WITHOUT ROWID``. A rowid-less table makes the whole database
#:   unprovable to ``snapshot_redact``'s rowid-alias check, which DROPS the
#:   product database rather than skipping one table.
CREW_SCHEMA_SQL: Final = f"""
CREATE TABLE IF NOT EXISTS memory_items (
    id               TEXT PRIMARY KEY,
    kind             TEXT NOT NULL
                     CHECK (kind IN ('{KIND_DIRECTIVE}', '{KIND_FACT}', '{KIND_EPISODE}')),
    key              TEXT,
    text             TEXT NOT NULL,
    value_json       TEXT,
    embedding        BLOB,
    conversation_id  TEXT,
    tags             TEXT NOT NULL DEFAULT '[]',
    scope            TEXT NOT NULL DEFAULT '',
    surface          TEXT NOT NULL DEFAULT '',
    crew             TEXT NOT NULL DEFAULT '',
    session_key      TEXT NOT NULL DEFAULT '',
    derived_from     TEXT NOT NULL DEFAULT '',
    importance       REAL NOT NULL DEFAULT 0.5,
    confidence       REAL NOT NULL DEFAULT 0.5,
    source           TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    last_accessed_at TEXT,
    is_deleted       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (key)
);

CREATE INDEX IF NOT EXISTS idx_mi_kind_live    ON memory_items (kind, is_deleted);
CREATE INDEX IF NOT EXISTS idx_mi_key_live     ON memory_items (key, is_deleted);
CREATE INDEX IF NOT EXISTS idx_mi_created      ON memory_items (created_at);
CREATE INDEX IF NOT EXISTS idx_mi_evict        ON memory_items
    (kind, is_deleted, importance, created_at);
CREATE INDEX IF NOT EXISTS idx_mi_conversation ON memory_items (conversation_id);
CREATE INDEX IF NOT EXISTS idx_mi_scope        ON memory_items (scope, is_deleted);
CREATE INDEX IF NOT EXISTS idx_mi_crew         ON memory_items (crew, is_deleted);
CREATE INDEX IF NOT EXISTS idx_mi_surface      ON memory_items (surface, is_deleted);

-- The v1 relation NAMES survive as READ-ONLY views, in the v1 COLUMN ORDER,
-- because ~36 read statements name them and `SELECT *` feeds `sqlite3.Row`.
-- Splitting by `kind` is also what keeps the vector scorers sound, and omitting
-- every facet column is what makes facets unreachable from every ranker.
-- Never add an `INSTEAD OF` trigger here -- see the module docstring.
CREATE VIEW IF NOT EXISTS semantic_memory AS
    SELECT key, value_json, confidence, source, created_at, updated_at, is_deleted,
           embedding
      FROM memory_items WHERE kind IN {_SEMANTIC_KIND_SQL};

CREATE VIEW IF NOT EXISTS episodic_memories AS
    SELECT id, conversation_id, text, embedding, tags, importance, created_at,
           last_accessed_at, is_deleted
      FROM memory_items WHERE kind = '{KIND_EPISODE}';
{MEMORY_EVENTS_SQL}{MEMORY_META_SQL}"""

MIGRATIONS_CREW: Final[list[tuple[int, str, "Callable[[sqlite3.Connection], None] | None"]]] = [
    (CREW_SCHEMA_VERSION, CREW_SCHEMA_SQL, None),
]

#: The semantic id namespace. Deterministic from the key, so a writer never needs
#: ``last_insert_rowid()`` (the engine has no such call) and a facet stamp can
#: address the row it just wrote. Episodes keep their own uuid as ``id``, and the
#: prefix is what keeps the two namespaces from colliding.
_SEMANTIC_ID_PREFIX: Final = "key:"


def semantic_item_id(key: str) -> str:
    """The ``memory_items.id`` of the semantic row under *key*."""
    return _SEMANTIC_ID_PREFIX + key


def kind_for_key(key: str) -> str:
    """The kind stamped on a semantic row under *key*.

    A lesson is a rule the agent must follow, so it is a ``directive``; every
    other semantic key is a ``fact``. This mirrors the ``key LIKE 'lesson.%'``
    discrimination the engine already applies, rather than introducing a second,
    divergent notion of what a lesson is.
    """
    return KIND_DIRECTIVE if key.startswith("lesson.") else KIND_FACT


def semantic_relation(lineage: str) -> str:
    """The WRITABLE relation holding semantic rows in *lineage*."""
    return "memory_items" if lineage == LINEAGE_CREW else "semantic_memory"


def episodic_relation(lineage: str) -> str:
    """The WRITABLE relation holding episodic rows in *lineage*."""
    return "memory_items" if lineage == LINEAGE_CREW else "episodic_memories"


def semantic_guard(lineage: str) -> str:
    """A trailing ``AND kind IN (...)`` clause for a semantic write, or ``""``.

    Empty on v1, so an interpolated statement renders byte-identically to the
    literal it replaced. On the crew lineage it is what keeps a semantic write
    off an episode sharing the physical table.
    """
    return f" AND kind IN {_SEMANTIC_KIND_SQL}" if lineage == LINEAGE_CREW else ""


def episodic_guard(lineage: str) -> str:
    """A trailing ``AND kind = 'episode'`` clause for an episodic write, or ``""``.

    Redundant against an ``id``-bound statement, since the two id namespaces are
    disjoint, and kept anyway: it is the assertion that makes each interpolated
    statement readable on its own, and it costs an indexed equality.
    """
    return f" AND kind = '{KIND_EPISODE}'" if lineage == LINEAGE_CREW else ""


# ── The four statements a relation swap cannot express ──
#
# Fifteen of the engine's nineteen write statements differ between the lineages
# only in which relation they name, so they interpolate ``semantic_relation`` /
# ``episodic_guard`` and render byte-identically on v1. These four differ in their
# COLUMN LIST: ``memory_items`` requires ``id``, ``kind``, ``text`` and
# ``updated_at``, none of which v1 supplies. Each therefore carries two spellings
# and a param builder, kept side by side so a change to one is visibly a change to
# the other.

#: v1's semantic insert, byte-identical to the literal it replaced.
_SEMANTIC_INSERT_V1: Final = (
    "INSERT INTO semantic_memory "
    "(key, value_json, confidence, source, created_at, updated_at, is_deleted) "
    "VALUES (?, ?, ?, ?, ?, ?, 0)"
)

_SEMANTIC_INSERT_CREW: Final = (
    "INSERT INTO memory_items "
    "(id, kind, key, text, value_json, confidence, source, created_at, updated_at, "
    "is_deleted) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)"
)

#: v1's semantic upsert, byte-identical to the literal it replaced.
_SEMANTIC_UPSERT_V1: Final = (
    "INSERT INTO semantic_memory (key, value_json, confidence, source, created_at, "
    "updated_at, is_deleted) "
    "VALUES (?, ?, ?, ?, ?, ?, 0) "
    "ON CONFLICT(key) DO UPDATE SET value_json=?, confidence=?, source=?, updated_at=?, "
    "is_deleted=0, "
    "embedding=CASE WHEN semantic_memory.value_json = excluded.value_json "
    "THEN semantic_memory.embedding ELSE NULL END"
)

#: SQLite refuses ``ON CONFLICT`` on a view ("cannot UPSERT a view"), so the crew
#: spelling names the physical table. It conflicts on ``key`` for the same reason
#: v1 does — one row per key.
_SEMANTIC_UPSERT_CREW: Final = (
    "INSERT INTO memory_items (id, kind, key, text, value_json, confidence, source, "
    "created_at, updated_at, is_deleted) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0) "
    "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
    "text=excluded.text, confidence=excluded.confidence, source=excluded.source, "
    "updated_at=excluded.updated_at, is_deleted=0, "
    "embedding=CASE WHEN memory_items.value_json = excluded.value_json "
    "THEN memory_items.embedding ELSE NULL END"
)

#: v1's episodic insert, byte-identical to the literal it replaced.
_EPISODIC_INSERT_V1: Final = (
    "INSERT INTO episodic_memories "
    "(id, conversation_id, text, embedding, tags, "
    "importance, created_at, is_deleted) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, 0)"
)

#: ``updated_at`` mirrors ``created_at``: v1's ``episodic_memories`` has no such
#: column and nothing updates an episode's text, but the column is NOT NULL here.
_EPISODIC_INSERT_CREW: Final = (
    "INSERT INTO memory_items "
    "(id, kind, conversation_id, text, embedding, tags, "
    "importance, source, created_at, updated_at, is_deleted) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)"
)

#: Fallback ``source`` for an episode whose caller named none. v1's
#: ``episodic_memories`` has no source column at all, so the value is genuinely new
#: information here -- and it must be the CALLER's, not a constant. Six writers pass
#: something else (``import``, ``benchmark``, ``migration``, an ops-app label, and
#: ``consolidation:<session_key>``), and ``source`` is an AUTHORITY level that four
#: rules key on: stamping every episode ``consolidation`` would have an
#: operator-imported row claim consolidation authority, and would disagree with the
#: value ``memory_events`` records for the very same insert.
_EPISODE_SOURCE_DEFAULT: Final = "consolidation"


def semantic_insert(lineage: str) -> str:
    """The plain semantic INSERT for *lineage* (the import path's no-clobber write)."""
    return _SEMANTIC_INSERT_CREW if lineage == LINEAGE_CREW else _SEMANTIC_INSERT_V1


def semantic_insert_params(
    lineage: str, key: str, value_json: str, confidence: float, source: str, now: str
) -> tuple[object, ...]:
    """Params for :func:`semantic_insert`. The v1 tuple is unchanged."""
    if lineage != LINEAGE_CREW:
        return (key, value_json, confidence, source, now, now)
    return (
        semantic_item_id(key),
        kind_for_key(key),
        key,
        value_json,
        value_json,
        confidence,
        source,
        now,
        now,
    )


def semantic_upsert(lineage: str) -> str:
    """The semantic UPSERT for *lineage*."""
    return _SEMANTIC_UPSERT_CREW if lineage == LINEAGE_CREW else _SEMANTIC_UPSERT_V1


def semantic_upsert_params(
    lineage: str, key: str, value_json: str, confidence: float, source: str, now: str
) -> tuple[object, ...]:
    """Params for :func:`semantic_upsert`.

    The v1 tuple is the original ten values, unchanged. The crew spelling needs no
    trailing conflict values because it reads them from ``excluded``, which is what
    lets one statement carry the same keep-the-vector-when-unchanged rule.
    """
    if lineage != LINEAGE_CREW:
        return (key, value_json, confidence, source, now, now, value_json, confidence, source, now)
    return (
        semantic_item_id(key),
        kind_for_key(key),
        key,
        value_json,
        value_json,
        confidence,
        source,
        now,
        now,
    )


def episodic_insert(lineage: str) -> str:
    """The episodic INSERT for *lineage*."""
    return _EPISODIC_INSERT_CREW if lineage == LINEAGE_CREW else _EPISODIC_INSERT_V1


def episodic_insert_params(
    lineage: str,
    mem_id: str,
    conversation_id: str | None,
    text: str,
    embedding_blob: bytes | None,
    tags_json: str,
    importance: float,
    now: str,
    source: str = "",
) -> tuple[object, ...]:
    """Params for :func:`episodic_insert`. The v1 tuple is unchanged.

    *source* is the CALLER's value, threaded rather than assumed: v1 discards it,
    but recording a known-wrong one is strictly worse than recording none.
    """
    if lineage != LINEAGE_CREW:
        return (mem_id, conversation_id, text, embedding_blob, tags_json, importance, now)
    return (
        mem_id,
        KIND_EPISODE,
        conversation_id,
        text,
        embedding_blob,
        tags_json,
        importance,
        source or _EPISODE_SOURCE_DEFAULT,
        now,
        now,
    )


# ── Facets: the carve axes, stamped at write time ────────────────────────────


@dataclass(frozen=True)
class MemoryFacets:
    """The deterministic carve axes stamped onto a crew row at write time.

    Every field is a FILTER axis and never a ranking signal, which the schema
    enforces rather than asks for: none of these columns is exposed by the
    ``semantic_memory`` or ``episodic_memories`` views, so no statement in the
    engine can read one into a score.

    All fields default to ``""`` rather than ``None`` because the columns are
    ``NOT NULL DEFAULT ''``: absence and "not applicable" are the same answer for
    a carve, and a nullable axis would make every future filter write
    ``IS NULL OR = ''``.

    Deliberately NOT the doc's ``repo_url`` / ``code_path`` / ``package`` trio.
    No deterministic source for those exists in this fork — the git-origin probe
    answers ``None`` for every worktree and spawns a subprocess per call, the
    branch helper returns an egress-redacted value that will not compare equal
    later, and the manifest the doc reads for ``package`` is not part of this
    build. ``scope`` carries the one repository axis that has a live evaluator
    and a live carve instead.
    """

    #: The repository/project fragment, mirroring the ``repo_scope`` a lesson
    #: already carries inside ``value_json``. An INDEX-ONLY PROJECTION:
    #: ``value_json`` stays authoritative and the lesson reader keeps reading it,
    #: because two sources of truth for a scope is how a carve silently widens.
    scope: str = ""
    #: Where the memory came from, as a BOUNDED label from
    #: ``messaging.link.telemetry_channel_of`` — never a raw session key, whose
    #: cardinality is unbounded. Not ``sel._infer_source``, which fails OPEN to
    #: ``"slack"`` for an unrecognised key and would file foreign rows inside a
    #: slack carve.
    surface: str = ""
    #: The CREW alias that produced the row — the ``cfg.agents`` key, never the
    #: kiro-cli agent template. The two namespaces are disjoint and resolving a
    #: store from the wrong one answers ``default`` for exactly the crew that
    #: configured otherwise, silently and toward the operator's own memory.
    crew: str = ""
    #: The conversation this row came from. Its own column rather than smuggled
    #: into ``source``, which is an AUTHORITY level that four rules key on.
    session_key: str = ""
    #: The ``memory_items.id`` this row was synthesized from, when it was.
    derived_from: str = ""

    def is_empty(self) -> bool:
        """True when there is nothing to stamp, so the writer can skip the UPDATE."""
        return not (
            self.scope or self.surface or self.crew or self.session_key or self.derived_from
        )


#: Stamps the carve axes on one row. Addressed by ``id`` because both id
#: namespaces are deterministic (``key:<key>`` for a semantic row, the uuid for an
#: episode), so no writer needs ``last_insert_rowid()`` — the engine has no such
#: call, and a lastrowid read would be wrong through a view anyway.
#:
#: The per-axis CASE keeps a stamp ADDITIVE: an empty value leaves the stored one
#: alone, so a second write that knows only the surface cannot blank a scope an
#: earlier write established. Each axis is bound twice -- once for the test, once
#: for the value -- which is the cost of expressing that in one statement.
FACET_STAMP_SQL: Final = (
    "UPDATE memory_items SET "
    "scope = CASE WHEN ? = '' THEN scope ELSE ? END, "
    "surface = CASE WHEN ? = '' THEN surface ELSE ? END, "
    "crew = CASE WHEN ? = '' THEN crew ELSE ? END, "
    "session_key = CASE WHEN ? = '' THEN session_key ELSE ? END, "
    "derived_from = CASE WHEN ? = '' THEN derived_from ELSE ? END "
    "WHERE id = ?"
)


def facet_stamp_params(item_id: str, facets: MemoryFacets) -> tuple[object, ...]:
    """Params for :data:`FACET_STAMP_SQL`, each axis passed twice for its CASE."""
    return (
        facets.scope,
        facets.scope,
        facets.surface,
        facets.surface,
        facets.crew,
        facets.crew,
        facets.session_key,
        facets.session_key,
        facets.derived_from,
        facets.derived_from,
        item_id,
    )


# ── Facets: reading a carve back out ─────────────────────────────────────────
#
# The write side above stamps; this is the read side, and it is the ONLY place a
# facet name is ever spliced into a statement. Two rules make that safe, and both
# are structural rather than remembered:
#
# * A name reaching SQL is always one of THIS module's own literals. The builders
#   iterate :data:`FACET_NAMES`; a caller's mapping is consulted for MEMBERSHIP
#   only, so no caller string can become an identifier however it is spelled.
# * Every VALUE is a bound parameter. Nothing a caller supplies is rendered.
#
# The statements live here rather than in ``vector_memory`` because they name
# ``memory_items``, a relation only the crew lineage has: a literal naming it from
# the engine would be a statement that raises on every v1 file.


#: Every facet a caller may filter on, DERIVED from :class:`MemoryFacets` so a
#: sixth carve axis becomes filterable by adding the field and nothing else. A
#: tuple, so the identifier order in a built statement is stable and reviewable.
FACET_NAMES: Final = tuple(field.name for field in fields(MemoryFacets))

#: The columns a COUNT may be grouped by: every facet, plus ``kind``. ``kind`` is
#: deliberately NOT a facet — it is the row type, and it is absent from
#: :class:`MemoryFacets` because nothing stamps it — but "how much of this crew's
#: memory is directives" is the same question an operator asks in the same breath
#: as "which surfaces filled it", and it is a closed set of three literals rather
#: than caller text. Kept as its own name so the FILTER allowlist stays exactly
#: the dataclass's fields.
GROUPABLE_COLUMNS: Final = (*FACET_NAMES, _KIND_COLUMN)

#: The columns a carve page returns. An explicit list, never ``SELECT *``:
#:
#: * ``embedding`` is a BLOB, and a carve is a partition rather than a ranking —
#:   the one thing this read must not hand back is a vector.
#: * ``value_json`` is omitted because it is not additional information here. A
#:   semantic write sets ``text`` and ``value_json`` to the same string, and an
#:   episode leaves ``value_json`` NULL, so on this table it is either a duplicate
#:   or empty — at up to 4 KB per row.
#: * ``is_deleted`` is omitted because every carve read filters it to 0.
FACET_PAGE_COLUMNS: Final = (
    "id",
    _KIND_COLUMN,
    "key",
    "text",
    "tags",
    "importance",
    "confidence",
    "source",
    "created_at",
    "updated_at",
    *FACET_NAMES,
)

#: Rows one carve page may return, clamped in the builder so no surface can widen
#: it. ``memory_items`` is unbounded and continuously written, so an uncapped page
#: is the same unbounded-serialization exposure ``get_all_semantic``'s cap exists
#: for (CWE-770); 500 rows is well past what an operator reads in one screen.
MAX_FACET_PAGE: Final = 500

#: Default page size, matching ``get_episodic_list``'s so the two list surfaces
#: page alike.
DEFAULT_FACET_PAGE: Final = 50

#: Distinct values one grouped count may report. ``session_key`` cardinality is
#: unbounded — one value per conversation, forever — so the count is ordered by
#: population and truncated here. The bound is the honest shape of the answer: it
#: reports the most populous values, not all of them.
MAX_FACET_GROUPS: Final = 200


class UnknownFacet(ValueError):
    """A facet name, group axis or ``kind`` value outside its closed set.

    Raised rather than ignored, because a silently dropped filter WIDENS a carve:
    a caller asking for one crew's rows would be handed every crew's, under a
    heading naming theirs.
    """


class FacetsUnsupported(RuntimeError):
    """A facet query aimed at a file on the v1 lineage, which has no facets.

    The refusal, and never an empty result. On v1 the columns do not exist, so
    "how many rows carry ``crew = finance``" has no answer there — while an empty
    page or a ``{}`` count says "none", which for a populated default store is
    simply false. An operator asking what is in a crew's memory would read that
    as "nothing", which is the one wrong answer this seam can give.
    """


def _reject_unknown_facets(filters: Mapping[str, str]) -> None:
    """Raise :class:`UnknownFacet` for any key of *filters* that is not a facet."""
    unknown = sorted(set(filters) - set(FACET_NAMES))
    if unknown:
        raise UnknownFacet(
            f"unknown memory facet(s) {unknown}; the carve axes are {list(FACET_NAMES)}"
        )


def _facet_predicate(filters: Mapping[str, str], kind: str) -> tuple[str, list[object]]:
    """A trailing ``AND col = ?`` chain for *filters* plus *kind*, and its params.

    The loop walks :data:`FACET_NAMES` rather than *filters*, so every identifier
    in the returned SQL is one of this module's literals and the caller's mapping
    decides only WHICH axes appear. Values are returned separately, to be bound.

    A blank value is a real filter, not an absent one: an unstamped row stores
    ``''`` on every axis, so ``{"crew": ""}`` selects exactly the rows no writer
    attributed. That is why the filters are a mapping instead of a
    :class:`MemoryFacets` — the dataclass spells absence and "not applicable" the
    same way, which is right for a stamp and would make "unattributed" unaskable
    here.
    """
    _reject_unknown_facets(filters)
    if kind and kind not in ALL_KINDS:
        raise UnknownFacet(f"unknown memory kind {kind!r}; the kinds are {list(ALL_KINDS)}")
    clauses: list[str] = []
    params: list[object] = []
    for name in FACET_NAMES:
        if name in filters:
            clauses.append(f" AND {name} = ?")
            params.append(str(filters[name]))
    if kind:
        clauses.append(f" AND {_KIND_COLUMN} = ?")
        params.append(kind)
    return "".join(clauses), params


def facet_page_query(
    filters: Mapping[str, str],
    kind: str = "",
    limit: int = DEFAULT_FACET_PAGE,
    offset: int = 0,
) -> tuple[str, tuple[object, ...]]:
    """``(sql, params)`` for one page of the carve *filters* + *kind* select.

    Which index the filter rides, measured with ``EXPLAIN QUERY PLAN``:

    * ``scope`` → ``idx_mi_scope``, ``crew`` → ``idx_mi_crew``,
      ``surface`` → ``idx_mi_surface``. Each is ``(column, is_deleted)``, and the
      live-rows-only predicate is the index's second column, so a one-axis carve
      is a pure index seek.
    * ``kind`` alone → ``idx_mi_kind_live``.
    * ``session_key`` and ``derived_from`` have NO index of their own and scan the
      table. Deliberately left that way: both are high-cardinality identifiers
      reached from a row an operator already has in hand (a session they are
      looking at, an episode a promotion named), so the query is a needle lookup
      rather than a dashboard-wide aggregate — and two more indexes on a
      write-heavy table cost every consolidation pass.
    * Combined axes seek on whichever indexed column comes first and filter the
      rest, so pairing an unindexed axis with an indexed one recovers the seek.

    The ORDER BY needs a temporary B-tree in every shape: ``idx_mi_created``
    covers ``created_at`` alone, and this orders by ``id`` after it. That tie
    break is what makes paging STABLE — ``created_at`` is a microsecond ISO
    string, so ties are rare rather than impossible, and one tie is enough to
    show a row on two pages and hide another entirely.
    """
    predicate, params = _facet_predicate(filters, kind)
    # Clamped HERE rather than at each surface, so the CLI and the HTTP route
    # inherit one bound and neither can widen it.
    page = max(1, min(int(limit), MAX_FACET_PAGE))
    skip = max(0, int(offset))
    sql = (
        f"SELECT {', '.join(FACET_PAGE_COLUMNS)} FROM memory_items "
        f"WHERE is_deleted = 0{predicate} "
        "ORDER BY created_at DESC, id LIMIT ? OFFSET ?"
    )
    return sql, (*params, page, skip)


def facet_count_query(
    group_by: str,
    filters: Mapping[str, str],
    kind: str = "",
    limit: int = MAX_FACET_GROUPS,
) -> tuple[str, tuple[object, ...]]:
    """``(sql, params)`` counting live rows per distinct value of *group_by*.

    *group_by* must be one of :data:`GROUPABLE_COLUMNS`; it is compared against
    that tuple and the tuple's own literal is what reaches the statement, so a
    caller's string is never interpolated even when it matches.

    Ordered by population and then by value, so the answer is deterministic and
    the truncation at :data:`MAX_FACET_GROUPS` drops the least populous tail
    rather than an arbitrary slice. Grouping by ``crew``, ``scope``, ``surface``
    or ``kind`` is a covering-index scan; ``session_key`` and ``derived_from``
    scan the table for the reason :func:`facet_page_query` gives.
    """
    if group_by not in GROUPABLE_COLUMNS:
        raise UnknownFacet(
            f"cannot group memory by {group_by!r}; the group axes are {list(GROUPABLE_COLUMNS)}"
        )
    # The allowlist's OWN literal, not the argument, so the identifier spliced
    # below provably comes from this module even if the comparison above ever
    # loosened.
    column = GROUPABLE_COLUMNS[GROUPABLE_COLUMNS.index(group_by)]
    predicate, params = _facet_predicate(filters, kind)
    groups = max(1, min(int(limit), MAX_FACET_GROUPS))
    sql = (
        f"SELECT {column} AS value, COUNT(*) AS total FROM memory_items "
        f"WHERE is_deleted = 0{predicate} "
        f"GROUP BY {column} ORDER BY total DESC, value ASC LIMIT ?"
    )
    return sql, (*params, groups)


def detect_lineage(db: sqlite3.Connection) -> str | None:
    """Which lineage *db* already IS, or ``None`` when it holds no product table.

    Structural rather than a stamp, so a lost or hand-edited ``memory_meta`` row
    cannot misclassify a file — and so every file that exists today answers
    :data:`LINEAGE_V1` without carrying a stamp at all.

    ``type='table'`` deliberately: on the crew lineage ``semantic_memory`` exists
    as a VIEW, so a check that accepted either would answer v1 for a crew file and
    run the v1 migrations against views.
    """
    names = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "  # wokeignore:rule=master
            "AND name IN ('memory_items', 'semantic_memory', 'episodic_memories')"
        ).fetchall()
    }
    if "memory_items" in names:
        return LINEAGE_CREW
    if names:
        return LINEAGE_V1
    return None


def lineage_for_new_file(db_path: Path) -> str:
    """The lineage a file created at *db_path* is born with.

    Only reached when :func:`detect_lineage` answered ``None`` — i.e. for a file
    with no product tables yet. Positive membership only; see the module
    docstring for the four real paths a negation would wrongly capture.
    """
    return LINEAGE_CREW if named_store_of_db(db_path) else LINEAGE_V1

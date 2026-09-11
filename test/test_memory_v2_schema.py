"""Two schema lineages over one engine, with each file's object set pinned by VALUE.

``memory_schema`` gives a crew silo its own physical row table, while every other
vector file — above all ``config_dir()/"memory.db"`` — stays on the v1 lineage. The
operator constraint is one-directional: memory v2 is for crew members only, so a silo
may look like whatever it needs to and the DEFAULT store must look like exactly what
it looked like before.

Nothing else in the tree can falsify the second half. The whole memory suite's only
the schema table read is a SUBSET assertion (``assert "semantic_memory" in tables``),
nothing asserts on ``vector_memory._MIGRATIONS``, and ``PRAGMA user_version`` is never
written — so a second ``CREATE TABLE`` landing in the default store's file is invisible
to every existing test, including the 21 golden payload tests. That is why the
assertions here are EXACT SETS rather than membership checks.

The v1 object names, table set and version set are spelled as LITERALS. Deriving them
from the production DDL would make the pin circular: it would move with the thing it
exists to hold still. The production migration LIST is pinned separately, against the
literal tuples, so a rename shows up as a failure here rather than passing quietly on
both sides.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import logging
import re
import shutil
import sqlite3
import struct
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock

import pytest

from conftest import make_dir_link
from kiro_crew import memory_schema, vector_memory
from kiro_crew.config import loader as loader_mod
from kiro_crew.config.loader import config_dir
from kiro_crew.memory_stores import (
    DEFAULT_MEMORY_STORE,
    MEMORY_DB_FILE,
    UnknownMemoryStore,
    memory_stores_root,
    named_store_of_db,
    resolve_store_path,
)
from kiro_crew.vector_memory import (
    _HAS_NUMPY,
    _MEMORY_META_TABLE,
    _MIGRATIONS,
    _SCHEMA_V1,
    VectorMemoryStore,
    _migrate_v2,
)

# Every test writes ``config.json`` into its own data home and drops the process-wide
# config cache, so two workers racing that global is a flake. One xdist group for the
# whole module, the way ``test_memory_v1_golden.py`` does it.
pytestmark = pytest.mark.xdist_group("memory_v2_schema")


# ── The declared silos ────────────────────────────────────────────────────────

#: Two silos, not one. A lineage resolved into a module-level global rather than onto
#: the store would answer for whichever file was built first, and one silo cannot
#: tell those two shapes apart.
_FINANCE = "finance"
_WORK = "work"

#: A third declared name, used only to alias another store's directory.
_ACME = "acme"


# ── The v1 lineage, as literals ───────────────────────────────────────────────

#: Every ``type='table'`` name a v1 vector file holds. ``sqlite_sequence`` is
#: SQLite's own, created the moment an ``AUTOINCREMENT`` table is (``memory_events``),
#: so it belongs to the measured set on both lineages.
_V1_TABLES = frozenset(
    {
        "schema_version",
        "semantic_memory",
        "episodic_memories",
        "memory_events",
        "memory_meta",
        "memory_record_meta",
        "memory_revisions",
        "sqlite_sequence",
    }
)

#: The v1 migration series, closed. Two disjoint version series is the third barrier
#: behind detection: neither lineage's DDL is reachable through the other's
#: set-membership loop.
_V1_SCHEMA_VERSIONS = frozenset({1, 2, 3})

#: Every ``type='table'`` name a crew silo holds. No ``semantic_memory`` and no
#: ``episodic_memories``: on this lineage those two names are VIEWS.
_CREW_TABLES = frozenset(
    {
        "memory_items",
        "memory_events",
        "memory_meta",
        "memory_record_meta",
        "memory_revisions",
        "schema_version",
        "sqlite_sequence",
    }
)

#: The v1 relation names, which survive on the crew lineage as read-only views.
_V1_RELATIONS = ("semantic_memory", "episodic_memories")

#: The carve axes. Present on ``memory_items``, absent from both views — which is what
#: makes "a facet is never a ranking signal" a fact about the relation rather than a
#: convention someone has to police. There is deliberately no ``embedding_dim``
#: column to list here: a vector's width IS ``length(embedding)/4``, so storing it
#: could only drift from the blob it describes.
_FACET_COLUMNS = frozenset({"scope", "surface", "crew", "session_key", "derived_from"})

#: The five columns a writer can STAMP, in the order ``FACET_STAMP_SQL`` sets them.
_CARVE_AXES = ("scope", "surface", "crew", "session_key", "derived_from")


# ── Fixed inputs. Nothing here reads the clock or the host. ───────────────────

_SEMANTIC_KEY = "pref.editor"
_THEME_KEY = "pref.theme"
_LESSON_RULE = "Rebase before merging."
_EPISODE_TEXT = "The finance crew rotated the deploy key on the release host."

#: A stored vector, seeded by hand. Four floats, so ``embedding_dim`` is 4 and the
#: projection is checkable rather than merely non-NULL.
_VECTOR = struct.pack("4f", 0.1, 0.2, 0.3, 0.4)
_VECTOR_DIM = len(_VECTOR) // 4

#: A repository scope in the shape the injection gate compares, so ``canonical_scope``
#: returns it unchanged and ``scope_is_admissible`` accepts it. The mirror tests are
#: then about the mirror rather than about the scope validator.
_REPO_SCOPE = "acme/ledger"

#: A DIFFERENT scope, so "the explicit facet wins over ``repo_scope``" cannot pass by
#: the two values coinciding.
_EXPLICIT_SCOPE = "acme/portal"

#: A BOUNDED surface label, the kind ``messaging.link.telemetry_channel_of`` answers.
_SURFACE = "slack"

#: A conversation, in its own axis rather than smuggled into ``source``.
_SESSION_KEY = "slack:C0LEDGER"

#: The four axes a consolidation writer can know. ``derived_from`` is deliberately
#: absent: only promotion sets it, and leaving it empty here is what makes the
#: NULL-vs-``''`` asymmetry (see :func:`_stamped`) observable on an ordinary write.
_WRITER_AXES = memory_schema.MemoryFacets(
    scope=_REPO_SCOPE, surface=_SURFACE, crew=_FINANCE, session_key=_SESSION_KEY
)

#: All five. Used where the claim is about the KWARG rather than about a writer —
#: the v1 lineage must ignore every axis, not merely the four a writer supplies.
_EVERY_AXIS = dataclasses.replace(
    _WRITER_AXES, derived_from=memory_schema.semantic_item_id(_SEMANTIC_KEY)
)

#: Columns whose value cannot compare across two files: a uuid or a clock reading.
_VOLATILE_COLUMNS = frozenset({"id", "created_at", "updated_at", "last_accessed_at"})


def _declare_silos(*names: str) -> None:
    """Declare *names* as memory stores in the per-test data home.

    ``memory_stores`` is an OBJECT keyed by store name. Spelled as an ARRAY
    (``{"memory_stores": ["finance"]}``) the schema drops the whole entry, every name
    degrades to the default with a warning, and ``resolve_store_path("finance")``
    answers ``config_dir()/"memory.db"`` — the DEFAULT store's own file. A test
    written that way builds the default store twice, calls one of them a silo, and
    asserts nothing at all.
    """
    payload = {
        "memory_stores": {DEFAULT_MEMORY_STORE: {}, **{name: {} for name in names}},
        "default_memory_store": DEFAULT_MEMORY_STORE,
    }
    (config_dir() / "config.json").write_text(json.dumps(payload), encoding="utf-8")
    loader_mod._invalidate_config_cache()


def _objects(db: sqlite3.Connection, kind: str) -> set[str]:
    """Every the schema table object of *kind* in *db*, by name."""
    return {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type = ?", (kind,))}


def _columns(db: sqlite3.Connection, relation: str) -> list[str]:
    """The column names of *relation*, IN DECLARATION ORDER.

    A list, not a set: ``SELECT *`` feeds ``sqlite3.Row``, and ~36 read statements in
    the engine name these relations, so a view that presented v1's columns in a
    different order would still satisfy a set comparison.
    """
    return [row[1] for row in db.execute(f"PRAGMA table_info({relation})")]


def _versions(db: sqlite3.Connection) -> set[int]:
    """The applied migration versions recorded in *db*."""
    return {int(row[0]) for row in db.execute("SELECT version FROM schema_version")}


def _ddl(db: sqlite3.Connection, name: str) -> str:
    """The ``CREATE`` statement *db* stores for the object *name*."""
    row = db.execute("SELECT sql FROM sqlite_schema WHERE name = ?", (name,)).fetchone()
    assert row is not None, f"{name} does not exist"
    return str(row[0])


@pytest.fixture
def stores():
    """A builder that opens vector stores and closes every one at teardown.

    Each ``VectorMemoryStore`` holds an open SQLite connection, and an open handle
    blocks ``tmp_path`` teardown on Windows — so the close belongs to the fixture
    rather than to each test's happy path, where a failed assertion would skip it.
    Registration happens BEFORE ``init()`` so a store whose init raises is closed too.
    """
    opened: list[VectorMemoryStore] = []

    def build(db_path: Path) -> VectorMemoryStore:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = VectorMemoryStore(db_path=db_path)
        opened.append(store)
        store.init()
        return store

    try:
        yield build
    finally:
        for store in opened:
            store.close()


@pytest.fixture
def silo(stores):
    """One declared crew silo, opened.

    Module-level rather than repeated per class: six classes below want exactly
    this, and the ORDER matters — declare first, resolve second, or
    ``resolve_store_path`` answers the DEFAULT store's own file and the test
    silently asserts against the operator-shaped file instead of a silo.
    """
    _declare_silos(_FINANCE)
    store = stores(resolve_store_path(_FINANCE))
    assert store._lineage == memory_schema.LINEAGE_CREW
    return store


def _axes(store: VectorMemoryStore, item_id: str) -> dict[str, object]:
    """*item_id*'s five carve axes, read from ``memory_items`` DIRECTLY.

    Never through a view. Neither view exposes one of these columns — that is what
    section 6 is about — so a read through ``semantic_memory`` reports nothing and
    every assertion about a stamp would pass with no stamp having happened.
    """
    columns = ", ".join(_CARVE_AXES)
    row = store.db.execute(
        f"SELECT {columns} FROM memory_items WHERE id = ?", (item_id,)
    ).fetchone()
    assert row is not None, f"no memory_items row with id {item_id!r}"
    return {axis: row[axis] for axis in _CARVE_AXES}


def _stamped(**axes: object) -> dict[str, object]:
    """The axis dict a row carries when only *axes* were stamped.

    All five axes are ``NOT NULL DEFAULT ''``, so an unstamped one reads back as
    ``''`` and never NULL. That uniformity is the point: a nullable axis would make
    every carve filter spell ``IS NULL OR = ''``, and a filter written the documented
    way (``derived_from = ''``) would silently match no unpromoted row at all.
    """
    base: dict[str, object] = {axis: "" for axis in _CARVE_AXES}
    base.update(axes)
    return base


def _the_lesson_row(store: VectorMemoryStore) -> sqlite3.Row:
    """The crew silo's ONE lesson row, asserting there is exactly one.

    Addressed by the ``lesson.`` key prefix rather than by ``kind``, which is the
    same discrimination the engine itself makes, and it keeps the helper usable
    without importing the private key hasher.
    """
    rows = store.db.execute("SELECT * FROM memory_items WHERE key LIKE 'lesson.%'").fetchall()
    assert len(rows) == 1, [row["key"] for row in rows]
    return rows[0]


def _episode_ids(store: VectorMemoryStore) -> list[str]:
    """Every episode id in *store*, crew lineage only."""
    return [
        row["id"]
        for row in store.db.execute(
            "SELECT id FROM memory_items WHERE kind = ? ORDER BY created_at, id",
            (memory_schema.KIND_EPISODE,),
        )
    ]


def _stable_rows(store: VectorMemoryStore, relation: str) -> list[dict[str, object]]:
    """Every row of *relation*, minus the columns that cannot compare across files."""
    return [
        {name: value for name, value in dict(row).items() if name not in _VOLATILE_COLUMNS}
        for row in store.db.execute(f"SELECT * FROM {relation} ORDER BY rowid")
    ]


# ── 1. The predicate is POSITIVE ──────────────────────────────────────────────


#: Every path a NEGATION would wrongly call a silo. ``db_path != config_dir()/"memory.db"``
#: is true of all seven, and each is a shape that really occurs: the default store's own
#: file, the eval runner's workspace vector file, the onboarding importer's destination,
#: a bare temp path, a file nested BELOW a silo, a store directory whose name fails the
#: shape rule, and the literal ``memory_stores/default/`` (unreachable through
#: ``resolve_store_path``, and it must still not be told the GLOBAL store's name).
_NOT_A_SILO: list[tuple[str, Callable[[Path, Path], Path]]] = [
    ("the-default-stores-own-file", lambda root, tmp: resolve_store_path(DEFAULT_MEMORY_STORE)),
    ("an-eval-runner-workspace", lambda root, tmp: tmp / "ws" / "vector_memory.db"),
    ("an-onboarding-destination", lambda root, tmp: tmp / "destination" / MEMORY_DB_FILE),
    ("a-bare-temp-path", lambda root, tmp: tmp / "mem.db"),
    ("nested-below-a-silo", lambda root, tmp: root / _FINANCE / "sub" / MEMORY_DB_FILE),
    ("an-uppercase-store-dir", lambda root, tmp: root / "Finance" / MEMORY_DB_FILE),
    ("the-literal-default-dir", lambda root, tmp: root / DEFAULT_MEMORY_STORE / MEMORY_DB_FILE),
]


class TestTheSiloPredicateIsPositive:
    """``named_store_of_db`` names a store or says nothing; it never says "not default".

    This is the inverse of ``resolve_store_path``, and the only spelling of "this file
    is a crew silo" that :func:`memory_schema.lineage_for_new_file` is allowed to
    consult. The negation a caller would otherwise reach for captures four real
    non-silo paths, so it would hand each of them the crew lineage.
    """

    @pytest.mark.parametrize(
        "make_path",
        [factory for _, factory in _NOT_A_SILO],
        ids=[name for name, _ in _NOT_A_SILO],
    )
    def test_a_non_silo_path_names_no_store(self, tmp_path: Path, make_path) -> None:
        _declare_silos(_FINANCE, _WORK)
        assert named_store_of_db(make_path(memory_stores_root(), tmp_path)) == ""

    def test_a_declared_stores_own_vector_file_names_that_store(self) -> None:
        """The positive half. Without it every assertion above passes on ``return ""``."""
        _declare_silos(_FINANCE, _WORK)
        assert named_store_of_db(resolve_store_path(_FINANCE)) == _FINANCE
        assert named_store_of_db(resolve_store_path(_WORK)) == _WORK

    def test_an_aliased_store_directory_names_neither_store(self, tmp_path: Path) -> None:
        """A link INSIDE the root redirects rather than escapes, and must be refused.

        With ``memory_stores/acme`` pointing at ``memory_stores/finance``, a
        resolved-PARENT check still sees the fenced root and would answer ``"acme"``
        for a file that physically belongs to ``finance`` — handing two crews one
        silo with every path check reporting success. The containment test is
        therefore IDENTITY, and the answer for the alias is neither name.
        """
        _declare_silos(_FINANCE, _ACME)
        root = memory_stores_root()
        (root / _FINANCE).mkdir(parents=True)
        make_dir_link(root / _ACME, root / _FINANCE)

        assert named_store_of_db(root / _ACME / MEMORY_DB_FILE) == ""
        # The store the link points AT is untouched: it is a real store directory.
        assert named_store_of_db(root / _FINANCE / MEMORY_DB_FILE) == _FINANCE
        # And the forward direction agrees, so the two cannot disagree about which
        # directory is whose.
        with pytest.raises(UnknownMemoryStore):
            resolve_store_path(_ACME)


# ── 2. The regression test ────────────────────────────────────────────────────


class TestTheDefaultStoreNeverGetsTheCrewLineage:
    """The operator's own ``memory.db`` is byte-shaped exactly as it was.

    Asserted as EXACT SETS, and that is the point of the class. The design doc's
    verbatim v4 schema is five additional tables; applied to
    ``config_dir()/"memory.db"`` it leaves all 21 golden-payload tests green, every
    behavioural memory test green, and the suite's one the schema table read green —
    that read is ``assert "semantic_memory" in tables``, which an added table cannot
    falsify. A subset assertion here would have caught nothing. An added table, an
    added view, an added trigger, and an added ``schema_version`` row each fail here
    and nowhere else.
    """

    def test_the_default_store_holds_exactly_the_v1_objects(self, stores, tmp_path: Path) -> None:
        _declare_silos(_FINANCE, _WORK)
        # Silos FIRST, the default store SECOND. A lineage cached anywhere but on the
        # store instance would have been set to "crew" by the time the default file
        # is opened, which is the ordering that makes this test able to fail.
        for name in (_FINANCE, _WORK):
            assert stores(resolve_store_path(name))._lineage == memory_schema.LINEAGE_CREW

        default_path = config_dir() / MEMORY_DB_FILE
        assert resolve_store_path(DEFAULT_MEMORY_STORE) == default_path
        # Three distinct files. If the declaration above ever stopped taking effect,
        # both silo paths WOULD be this file and everything below would assert against
        # a file this test itself created as a silo.
        assert len({resolve_store_path(_FINANCE), resolve_store_path(_WORK), default_path}) == 3

        default = stores(default_path)
        assert default._lineage == memory_schema.LINEAGE_V1

        tables = _objects(default.db, "table")
        assert tables == set(_V1_TABLES)
        assert "memory_items" not in tables
        assert _objects(default.db, "view") == set()
        assert _objects(default.db, "trigger") == set()
        assert _versions(default.db) == set(_V1_SCHEMA_VERSIONS)
        # v1's two relations are real TABLES here, which is also what makes
        # ``detect_lineage`` answer v1 for every file that exists on any install.
        assert memory_schema.detect_lineage(default.db) == memory_schema.LINEAGE_V1

        # Neither advisory stamp is written on the v1 lineage: doing so would add rows
        # to the operator's own file, which is the one thing this seam exists to avoid.
        for key in (memory_schema.LINEAGE_META_KEY, memory_schema.STORE_NAME_META_KEY):
            assert default._read_meta(key) is None, key

    def test_the_v1_migration_list_is_frozen(self) -> None:
        """The v1 series is closed at three. A fourth entry is a default-store change.

        Pinned against the module's own DDL objects rather than their text, so this
        stays a statement about the LIST — which migration runs, in which order, with
        which callable — and not a second copy of the schema that has to be edited
        whenever a comment in it moves.
        """
        assert _MIGRATIONS == [
            (1, _SCHEMA_V1, None),
            (2, "", _migrate_v2),
            (3, _MEMORY_META_TABLE, None),
        ]
        assert {version for version, _, _ in _MIGRATIONS} == set(_V1_SCHEMA_VERSIONS)


# ── 3. The positive twin ──────────────────────────────────────────────────────


class TestASiloIsBornOnTheCrewLineage:
    """A newly created silo is a ``memory_items`` file that PRESENTS v1's relations."""

    def test_a_new_silo_holds_exactly_the_crew_objects(self, stores) -> None:
        _declare_silos(_FINANCE)
        silo = stores(resolve_store_path(_FINANCE))

        assert silo._lineage == memory_schema.LINEAGE_CREW
        assert _objects(silo.db, "table") == set(_CREW_TABLES)
        assert _objects(silo.db, "view") == {*_V1_RELATIONS}
        # ZERO triggers, permanently — see ``test_the_schema_carries_no_trigger``.
        assert _objects(silo.db, "trigger") == set()
        assert _versions(silo.db) == {memory_schema.CREW_SCHEMA_VERSION}
        # Neither v1 relation exists as a TABLE, which is what stops ``detect_lineage``
        # from running the v1 migrations against a view.
        assert set(_V1_RELATIONS).isdisjoint(_objects(silo.db, "table"))
        assert memory_schema.detect_lineage(silo.db) == memory_schema.LINEAGE_CREW

    def test_the_two_version_series_are_disjoint(self) -> None:
        """The barrier that holds even if detection were bypassed on a crew file.

        ``init()`` applies migrations by SET membership, so overlapping series would
        let one lineage's DDL run inside the other's loop.
        """
        assert memory_schema.CREW_SCHEMA_VERSION not in _V1_SCHEMA_VERSIONS
        assert {memory_schema.CREW_SCHEMA_VERSION}.isdisjoint(
            {version for version, _, _ in _MIGRATIONS}
        )

    def test_the_views_present_v1s_columns_in_v1s_order(self, stores, tmp_path: Path) -> None:
        """Compared against a REAL v1 file, not a hardcoded list.

        A hardcoded expectation would have to be edited alongside any v1 column
        change and would then agree with itself while the two lineages diverged.
        """
        _declare_silos(_FINANCE)
        silo = stores(resolve_store_path(_FINANCE))
        reference = stores(tmp_path / "v1-reference" / MEMORY_DB_FILE)
        assert reference._lineage == memory_schema.LINEAGE_V1

        for relation in _V1_RELATIONS:
            assert _columns(silo.db, relation) == _columns(reference.db, relation), relation

    def test_a_new_silo_stamps_its_lineage_and_its_store(self, stores) -> None:
        """Advisory rows: a silo restored into the wrong directory becomes detectable.

        Structure stays authoritative — ``detect_lineage`` reads the schema table —
        so a lost or hand-edited stamp cannot misclassify a file.
        """
        _declare_silos(_FINANCE)
        silo = stores(resolve_store_path(_FINANCE))
        assert silo._read_meta(memory_schema.LINEAGE_META_KEY) == memory_schema.LINEAGE_CREW
        assert silo._read_meta(memory_schema.STORE_NAME_META_KEY) == _FINANCE
        # This declared, unowned crew-schema file is the supported legacy V1
        # case. Only a positively validated private manifest may add the durable
        # authorization marker.
        assert silo._read_meta(memory_schema.PRIVATE_MEMORY_VERSION_META_KEY) is None
        assert silo._read_meta(memory_schema.OWNER_MEMBER_META_KEY) is None

    def test_no_crew_table_is_without_rowid(self, stores) -> None:
        """``WITHOUT ROWID`` would cost the whole product database its backup.

        ``snapshot_redact._rowid_alias`` needs a unique row handle to rewrite a row
        by, and a table it cannot update that way is refused — which DROPS the
        database from the archive rather than skipping the one table. So the
        requirement is on every relation in the file, not only on ``memory_items``.
        """
        _declare_silos(_FINANCE)
        silo = stores(resolve_store_path(_FINANCE))
        tables = {name for name in _objects(silo.db, "table") if not name.startswith("sqlite_")}
        assert "memory_items" in tables

        for table in sorted(tables):
            assert "WITHOUT ROWID" not in _ddl(silo.db, table).upper(), table
            # And by behaviour: a rowid-less table answers "no such column: rowid".
            silo.db.execute(f"SELECT rowid FROM {table} LIMIT 1").fetchall()


# ── 4. Inertness for an install already running a silo ────────────────────────


class TestThePreExistingSiloKeepsItsLineage:
    """A file that already holds the v1 tables stays v1 wherever it sits.

    ``detect_lineage`` reads the file, so the path predicate is never consulted for
    it. There is no cutover, no dual write and no backfill: the two lineages coexist
    for the life of each file, and this is the claim that the change is inert for
    anyone already running a named store.
    """

    def test_a_v1_file_inside_a_silo_directory_stays_v1_across_two_inits(
        self, stores, tmp_path: Path
    ) -> None:
        _declare_silos(_WORK)

        # Seeded by running the v1 migrations for real, at a path that is not a silo,
        # then copying the file in — which is what a restored backup looks like.
        # Writing the DDL by hand instead would skip v2's ``embedding`` column and
        # v3's ``memory_meta`` and seed a file no install has.
        seed = stores(tmp_path / "already-running" / MEMORY_DB_FILE)
        assert seed._lineage == memory_schema.LINEAGE_V1
        assert seed.set_semantic(_SEMANTIC_KEY, "vim", 1.0, "user_explicit") is None
        seed.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        seed.close()

        silo_path = resolve_store_path(_WORK)
        silo_path.parent.mkdir(parents=True, exist_ok=True)
        # The sidecars too: a committed row lives in the ``-wal`` until a checkpoint
        # moves it, so copying the ``.db`` alone can copy a file with no rows in it.
        for suffix in ("", "-wal", "-shm"):
            sidecar = Path(f"{seed._db_path}{suffix}")
            if sidecar.exists():
                shutil.copy2(sidecar, Path(f"{silo_path}{suffix}"))

        # Twice, because the second init is the one that runs against a file the first
        # init already touched — and it is the reachable shape, on every gateway start.
        for attempt in (1, 2):
            silo = stores(silo_path)
            assert silo._lineage == memory_schema.LINEAGE_V1, attempt
            assert _objects(silo.db, "table") == set(_V1_TABLES), attempt
            assert _objects(silo.db, "view") == set(), attempt
            assert "memory_items" not in _objects(silo.db, "table"), attempt
            assert _versions(silo.db) == set(_V1_SCHEMA_VERSIONS), attempt
            # No stamp: the crew branch is what writes those, and it is unreachable
            # from a file that already holds product tables.
            assert silo._read_meta(memory_schema.LINEAGE_META_KEY) is None, attempt
            # The seeded row is still readable, so this is a live store rather than a
            # file that merely kept its schema.
            assert [row["key"] for row in silo.get_all_semantic()] == [_SEMANTIC_KEY], attempt
            silo.close()


# ── 5. Behavioural parity, through the public API ─────────────────────────────


def _seed_vector(store: VectorMemoryStore, key: str) -> None:
    """Put a stored vector on *key*'s row, in the shape its lineage has for one.

    Written through SQL rather than through an ``embed_fn`` so the seed is exact and
    the assertion is about the upsert's conflict rule alone, not about whatever width
    a live embedder happens to produce.

    Named relation per lineage: a crew silo's writable relation is the physical
    ``memory_items``, and both views refuse a write outright.
    """
    rel = "memory_items" if store._lineage == memory_schema.LINEAGE_CREW else "semantic_memory"
    store.db.execute(f"UPDATE {rel} SET embedding = ? WHERE key = ?", (_VECTOR, key))
    store.db.commit()


def _stored_vector(store: VectorMemoryStore, key: str) -> tuple[bytes | None, int | None]:
    """*key*'s ``(embedding, width)``, read from the PHYSICAL relation.

    The width is MEASURED from the blob rather than read from a column, which is the
    whole reason no such column exists: a stored width can disagree with the vector
    it describes, and a measured one cannot. Read from the physical relation because
    the crew lineage's ``semantic_memory`` is a view, and a future column added to
    one lineage only would otherwise be invisible here.
    """
    rel = "memory_items" if store._lineage == memory_schema.LINEAGE_CREW else "semantic_memory"
    row = store.db.execute(f"SELECT embedding FROM {rel} WHERE key = ?", (key,)).fetchone()
    blob = row["embedding"]
    return (blob, len(blob) // 4 if blob else None)


class TestTheCrewLineageAnswersTheV1Contract:
    """The same public calls, on both lineages, asserted to AGREE.

    Fifteen of the engine's nineteen write statements differ between the lineages only
    in which relation they name; four differ in their column list. None of that may be
    observable from outside, so each test here drives the PUBLIC store API and compares
    the two answers rather than inspecting either schema.
    """

    @pytest.fixture
    def both(self, stores, tmp_path: Path) -> dict[str, VectorMemoryStore]:
        """One store per lineage. The v1 reference lives under ``tmp_path``.

        Deliberately not ``config_dir()/"memory.db"``: this class WRITES, and the file
        whose bytes must not move is pinned by value in
        :class:`TestTheDefaultStoreNeverGetsTheCrewLineage`.
        """
        _declare_silos(_FINANCE)
        crew = stores(resolve_store_path(_FINANCE))
        reference = stores(tmp_path / "v1-reference" / MEMORY_DB_FILE)
        assert crew._lineage == memory_schema.LINEAGE_CREW
        assert reference._lineage == memory_schema.LINEAGE_V1
        # No embedder on either store, so nothing refills a vector behind an
        # assertion about one being cleared.
        assert crew.embed_fn is None and reference.embed_fn is None
        return {memory_schema.LINEAGE_CREW: crew, memory_schema.LINEAGE_V1: reference}

    @staticmethod
    def _agree(answers: dict[str, object]) -> object:
        """Assert both lineages answered the same thing, and return that answer."""
        crew = answers[memory_schema.LINEAGE_CREW]
        v1 = answers[memory_schema.LINEAGE_V1]
        assert crew == v1, f"crew={crew!r} v1={v1!r}"
        return crew

    def test_a_semantic_row_reads_back_the_same_on_both(self, both) -> None:
        answers: dict[str, object] = {}
        for lineage, store in both.items():
            assert store.set_semantic(_SEMANTIC_KEY, "vim", 1.0, "user_explicit") is None
            # Projected onto the stable fields: ``created_at`` reads the clock and
            # ``embedding`` is NULL here, so neither can be compared across files.
            answers[lineage] = [
                (row["key"], row["value_json"], row["confidence"], row["source"])
                for row in store.get_all_semantic()
            ]
            assert store.get_semantic(_SEMANTIC_KEY)["value_json"] == '"vim"'
        assert self._agree(answers) == [(_SEMANTIC_KEY, '"vim"', 1.0, "user_explicit")]

    def test_an_episodic_row_reads_back_the_same_on_both(self, both) -> None:
        listed: dict[str, object] = {}
        contexts: dict[str, object] = {}
        for lineage, store in both.items():
            assert store.write_episodic(_EPISODE_TEXT, source="test") is True
            listed[lineage] = [row["text"] for row in store.get_episodic_list(limit=10)]
            contexts[lineage] = store.get_episodic_context(query_text="deploy key")
        assert self._agree(listed) == [_EPISODE_TEXT]
        assert _EPISODE_TEXT in str(self._agree(contexts))

    def test_a_lesson_reads_back_the_same_on_both(self, both) -> None:
        answers: dict[str, object] = {}
        for lineage, store in both.items():
            assert store.write_lesson(_LESSON_RULE, category="preference")
            answers[lineage] = [(row["key"], row["value_json"]) for row in store.get_lessons()]
            assert store.count_lessons() == 1
        rows = self._agree(answers)
        assert isinstance(rows, list) and len(rows) == 1
        assert _LESSON_RULE in rows[0][1]

    def test_delete_semantic_tombstones_rather_than_removing_on_both(self, both) -> None:
        """Soft delete, so the row survives with ``is_deleted = 1``.

        Read back from the store's own PHYSICAL relation (``_sem_rel``), because that
        is the seam the write went through and a hard delete would be invisible to
        ``get_semantic`` either way.
        """
        surviving: dict[str, object] = {}
        for lineage, store in both.items():
            assert store.set_semantic(_SEMANTIC_KEY, "vim", 1.0, "user_explicit") is None
            assert store.delete_semantic(_SEMANTIC_KEY, "user_explicit") is True
            assert store.get_semantic(_SEMANTIC_KEY) is None
            assert store.get_all_semantic() == []
            row = store.db.execute(
                f"SELECT key, is_deleted FROM {store._sem_rel} WHERE key = ?", (_SEMANTIC_KEY,)
            ).fetchone()
            surviving[lineage] = (row["key"], row["is_deleted"])
        assert self._agree(surviving) == (_SEMANTIC_KEY, 1)

    def test_the_kind_stamp_follows_the_key(self, both) -> None:
        """``kind`` is stamped from the key prefix and is NOT a dispatch axis.

        Crew-only: v1 has no such column. The engine keeps discriminating lessons with
        ``key LIKE 'lesson.%'`` exactly as it does on v1, so this asserts the stamp
        agrees with :func:`memory_schema.kind_for_key` rather than that anything reads it.
        """
        crew = both[memory_schema.LINEAGE_CREW]
        assert crew.set_semantic(_SEMANTIC_KEY, "vim", 1.0, "user_explicit") is None
        assert crew.write_lesson(_LESSON_RULE, category="preference")
        assert crew.write_episodic(_EPISODE_TEXT, source="test") is True

        keyed = {
            row["key"]: (row["kind"], row["id"])
            for row in crew.db.execute(
                "SELECT id, key, kind FROM memory_items WHERE key IS NOT NULL"
            )
        }
        lesson_keys = [key for key in keyed if key.startswith("lesson.")]
        assert len(lesson_keys) == 1

        for key, (kind, item_id) in keyed.items():
            assert kind == memory_schema.kind_for_key(key), key
            # The id namespace is deterministic from the key, which is what lets a
            # writer address the row it just wrote without ``last_insert_rowid()``.
            assert item_id == memory_schema.semantic_item_id(key), key
        assert keyed[_SEMANTIC_KEY][0] == memory_schema.KIND_FACT
        assert keyed[lesson_keys[0]][0] == memory_schema.KIND_DIRECTIVE

        # An episode is keyless, so it is identified by ``id`` alone.
        assert {
            row["kind"]
            for row in crew.db.execute("SELECT kind FROM memory_items WHERE key IS NULL")
        } == {memory_schema.KIND_EPISODE}

    def test_an_unchanged_rewrite_keeps_the_vector_and_a_changed_one_clears_it(self, both) -> None:
        """The upsert's conflict clause, which is the one statement with real logic.

        A row must never keep ranking by a vector computed from different text,
        and it must not pay a re-embed for a consolidation pass that rewrote
        the same value. Both halves are asserted, on both lineages.

        Confidence 1.0 deliberately, and every return value is asserted ``is None``:
        ``set_semantic`` REJECTS a write below ``_confidence_threshold`` (from any
        source but ``user_explicit``) as ``low_confidence`` and returns before the
        upsert runs at all — so the same test written with a low confidence passes
        while asserting nothing about the conflict clause.
        """
        for store in both.values():
            assert store.set_semantic(_THEME_KEY, "dark", 1.0, "user_explicit") is None
            _seed_vector(store, _THEME_KEY)
            # The width is measured from the blob, so both lineages report it the
            # same -- there is no stored width that could differ between them.
            assert _stored_vector(store, _THEME_KEY) == (_VECTOR, _VECTOR_DIM)

            # Same value, re-affirmed: the stored vector still describes this text.
            assert store.set_semantic(_THEME_KEY, "dark", 1.0, "user_explicit") is None
            assert _stored_vector(store, _THEME_KEY) == (_VECTOR, _VECTOR_DIM)

            # Changed value: the vector goes, and on the crew lineage the projection
            # goes with it — a surviving ``embedding_dim`` would outlive the blob it
            # describes.
            assert store.set_semantic(_THEME_KEY, "light", 1.0, "user_explicit") is None
            assert _stored_vector(store, _THEME_KEY) == (None, None)
            assert store.get_semantic(_THEME_KEY)["value_json"] == '"light"'


# ── 6. Facets are carve axes, never ranking signals ───────────────────────────


#: SQL verbs, so the walk below collects STATEMENTS rather than every string in a
#: 4,000-line module.
_SQL_VERB = re.compile(r"\b(SELECT|INSERT INTO|UPDATE|DELETE FROM)\b")

#: A facet column named as a WORD. ``scope``, ``surface`` and ``crew`` are ordinary
#: English, and ``_`` is a word character — so this matches a bare ``scope`` column
#: and never ``repo_scope``, which is a different thing that legitimately appears.
_FACET_WORD = re.compile(r"\b(" + "|".join(sorted(_FACET_COLUMNS)) + r")\b")


def _render_fstring(node: ast.JoinedStr) -> str:
    """*node* rendered with every interpolation replaced by ``{}``."""
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        else:
            parts.append("{}")
    return "".join(parts)


def _sql_statements(module: ModuleType) -> list[str]:
    """Every SQL-shaped string literal in *module*'s source, docstrings excluded.

    An f-string is rendered with each interpolation as ``{}``, so a statement whose
    relation is ``self._sem_rel`` is collected as the statement it is rather than
    dropped for not being one constant; its literal fragments are then skipped, so
    the same statement is not also collected in pieces.

    Docstrings are excluded because the claim being checked is about what the engine
    EXECUTES. A sentence that names a facet and a view together is how the rule gets
    explained — the crew DDL's own comments do exactly that — and prose describing a
    prohibition must not read as a breach of it.
    """
    tree = ast.parse(inspect.getsource(module))
    fragments = {
        id(part)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for part in node.values
    }
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            found.append(_render_fstring(node))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in fragments
            and id(node) not in docstrings
        ):
            found.append(node.value)
    return [text for text in found if _SQL_VERB.search(text)]


class TestFacetsAreUnreachableFromEveryRanker:
    """No existing ranker CAN read a facet, because no view exposes one.

    The four vector scorers and every keyword scan read ``semantic_memory`` or
    ``episodic_memories``. Omitting the facet columns from both views is what turns
    "facets are carve axes" from a convention into a property of the relation.
    """

    def test_neither_view_exposes_a_facet_column(self, silo) -> None:
        for relation in _V1_RELATIONS:
            exposed = set(_columns(silo.db, relation))
            assert _FACET_COLUMNS.isdisjoint(exposed), (relation, sorted(_FACET_COLUMNS & exposed))
        # The columns really are on the physical table, so the disjointness above is a
        # statement about the RELATION rather than about columns nobody added.
        assert _FACET_COLUMNS <= set(_columns(silo.db, "memory_items"))

    def test_no_statement_naming_a_view_names_a_facet_column(self) -> None:
        """The same claim, made against the ENGINE's statements rather than the DDL.

        The views not exposing the columns is the enforcement; this is the half a
        reader can check without opening a database, and the half CI reports. A
        statement that reached for a facet through ``semantic_memory`` would raise
        ``no such column`` at runtime, on the crew lineage only, from whichever code
        path happened to run it — a failure shape worth catching at review time.

        Bounded to statements naming a VIEW on purpose. A carve FILTER on
        ``memory_items`` is what these columns exist for, so a walk that refused
        every mention of one anywhere in the engine would go red on the next feature
        that used them correctly. What must never happen is a facet reaching the
        RANKED READ path, and that path is exactly the one that goes through a view.

        The walk cannot see a column list assembled at runtime (one statement builds
        one), which is precisely why the views, and not this test, are the barrier.
        """
        through_a_view = [
            statement
            for statement in _sql_statements(vector_memory)
            if any(relation in statement for relation in _V1_RELATIONS)
        ]

        # Two positive controls, one per collection path — a plain literal and a
        # rendered f-string. Without them a walk that collected nothing would pass,
        # and this whole test would be an expensive no-op. Not a COUNT: the number of
        # read statements moves with any ordinary refactor.
        assert "SELECT * FROM semantic_memory WHERE key = ? AND is_deleted = 0" in through_a_view
        assert [s for s in through_a_view if "{}" in s], "no interpolated statement was collected"

        for statement in through_a_view:
            named = _FACET_WORD.findall(statement)
            assert not named, (named, statement)


# ── 7. Every kind carries the axes its writer named ───────────────────────────


class TestFacetsAreStampedOnEveryKind:
    """One kwarg, three kinds, and the axes land on all three.

    Driven through the PUBLIC writers, never through ``_stamp_facets`` itself: the
    stamp runs from the tail of each writer, so a direct call would keep passing
    while a writer quietly stopped threading its facets through. Read back with
    :func:`_axes`, which names ``memory_items`` — the views cannot show a facet, and
    that is the point rather than an inconvenience.
    """

    def test_every_declared_axis_is_a_column_and_is_asserted_here(self) -> None:
        """The dataclass, the table and this module's axis list are ONE set.

        A sixth field added to :class:`memory_schema.MemoryFacets` without a column
        is silently dropped by the stamp; added without a line here it is stamped and
        never asserted on. Both directions fail this, which is what keeps every test
        below honest as the carve grows.
        """
        declared = {field.name for field in dataclasses.fields(memory_schema.MemoryFacets)}
        assert declared == set(_CARVE_AXES)
        # Equal, not a subset: every column hidden from the views is a carve axis a
        # writer can name. A column hidden for some OTHER reason would be a third
        # category, and this is where that has to be argued rather than assumed.
        assert declared == _FACET_COLUMNS

    def test_a_fact_carries_every_axis_the_writer_named(self, silo) -> None:
        assert (
            silo.set_semantic(_SEMANTIC_KEY, "vim", 1.0, "user_explicit", facets=_WRITER_AXES)
            is None
        )
        item_id = memory_schema.semantic_item_id(_SEMANTIC_KEY)
        row = silo.db.execute("SELECT * FROM memory_items WHERE id = ?", (item_id,)).fetchone()
        assert row["kind"] == memory_schema.KIND_FACT
        assert _axes(silo, item_id) == _stamped(
            scope=_REPO_SCOPE, surface=_SURFACE, crew=_FINANCE, session_key=_SESSION_KEY
        )

    def test_a_lesson_key_carries_the_axes_as_a_directive(self, silo) -> None:
        """A lesson is the same physical row shape under a ``lesson.`` key.

        Written through ``write_lesson`` rather than ``set_semantic`` with a hand-made
        key, because ``write_lesson`` is the caller consolidation actually uses and it
        reaches the stamp through its own tail.
        """
        assert silo.write_lesson(_LESSON_RULE, category="preference", facets=_WRITER_AXES)
        row = _the_lesson_row(silo)
        assert row["kind"] == memory_schema.KIND_DIRECTIVE
        assert _axes(silo, row["id"]) == _stamped(
            scope=_REPO_SCOPE, surface=_SURFACE, crew=_FINANCE, session_key=_SESSION_KEY
        )

    def test_an_episode_carries_every_axis_the_writer_named(self, silo) -> None:
        """The kind that most needs a carve: an episode is delivered only by search.

        Its id is a uuid rather than a projection of a key, so this also covers the
        other half of the id namespace the stamp addresses rows by.
        """
        assert silo.write_episodic(_EPISODE_TEXT, source="test", facets=_WRITER_AXES) is True
        ids = _episode_ids(silo)
        assert len(ids) == 1
        row = silo.db.execute("SELECT * FROM memory_items WHERE id = ?", (ids[0],)).fetchone()
        assert row["kind"] == memory_schema.KIND_EPISODE
        assert row["key"] is None
        assert _axes(silo, ids[0]) == _stamped(
            scope=_REPO_SCOPE, surface=_SURFACE, crew=_FINANCE, session_key=_SESSION_KEY
        )

    def test_an_unfaceted_row_is_blank_on_every_axis(self, silo) -> None:
        """The absence shape, and it is UNIFORM across all five axes.

        Every axis is ``NOT NULL DEFAULT ''``, so an unstamped one reads back blank and
        never NULL. Pinned because the uniformity is what a carve filter depends on: a
        single nullable axis would force every filter to spell ``IS NULL OR = ''``, and
        one written the documented way would then silently match nothing.

        Both spellings of "no facets" are covered. ``facets=None`` never enters the
        stamp; an all-empty ``MemoryFacets`` enters it and is turned away by
        ``is_empty()``. They must be indistinguishable in the row, or the writer that
        builds a facet object unconditionally behaves differently from the one that
        passes nothing.
        """
        assert silo.set_semantic(_SEMANTIC_KEY, "vim", 1.0, "user_explicit") is None
        assert (
            silo.set_semantic(
                _THEME_KEY, "dark", 1.0, "user_explicit", facets=memory_schema.MemoryFacets()
            )
            is None
        )
        assert memory_schema.MemoryFacets().is_empty()

        blank = _stamped()
        assert list(blank.values()) == [""] * len(_CARVE_AXES)
        for key in (_SEMANTIC_KEY, _THEME_KEY):
            assert _axes(silo, memory_schema.semantic_item_id(key)) == blank, key


# ── 8. A stamp contributes; it does not overwrite what it does not know ───────


class TestAFacetStampIsAdditive:
    """``FACET_STAMP_SQL``'s per-axis CASE, which is what lets two writers cooperate.

    Consolidation knows the crew, the surface and the session; ``write_lesson`` knows
    the scope. They reach the same row through different calls, so a stamp that wrote
    all five axes unconditionally would blank whatever the other writer established —
    and a blanked carve axis is a row that silently rejoins the unfiltered pool.

    Observable on a KEYED row only: an episode gets a fresh uuid per write, so there
    is no second write to the same episode to be additive about.
    """

    def test_a_second_write_that_knows_only_the_surface_keeps_the_scope(self, silo) -> None:
        item_id = memory_schema.semantic_item_id(_SEMANTIC_KEY)
        facets_scope = memory_schema.MemoryFacets(scope=_REPO_SCOPE)
        assert (
            silo.set_semantic(_SEMANTIC_KEY, "vim", 1.0, "user_explicit", facets=facets_scope)
            is None
        )
        assert _axes(silo, item_id) == _stamped(scope=_REPO_SCOPE)

        facets_surface = memory_schema.MemoryFacets(surface=_SURFACE)
        assert (
            silo.set_semantic(_SEMANTIC_KEY, "vim", 1.0, "user_explicit", facets=facets_surface)
            is None
        )
        assert _axes(silo, item_id) == _stamped(scope=_REPO_SCOPE, surface=_SURFACE)

    def test_an_empty_facet_object_leaves_every_stored_axis_alone(self, silo) -> None:
        """The degenerate second write, which a background path takes constantly."""
        item_id = memory_schema.semantic_item_id(_SEMANTIC_KEY)
        assert (
            silo.set_semantic(_SEMANTIC_KEY, "vim", 1.0, "user_explicit", facets=_WRITER_AXES)
            is None
        )
        stamped = _axes(silo, item_id)

        for facets in (None, memory_schema.MemoryFacets()):
            assert (
                silo.set_semantic(_SEMANTIC_KEY, "vim", 1.0, "user_explicit", facets=facets) is None
            )
            assert _axes(silo, item_id) == stamped, facets

    def test_a_non_empty_axis_still_replaces_the_stored_one(self, silo) -> None:
        """Additive is not append-only: a writer that KNOWS an axis is authoritative.

        Without this the CASE reads as "first writer wins", and a row whose scope
        genuinely moved would keep pointing at the old carve forever.
        """
        item_id = memory_schema.semantic_item_id(_SEMANTIC_KEY)
        first = memory_schema.MemoryFacets(scope=_REPO_SCOPE)
        second = memory_schema.MemoryFacets(scope=_EXPLICIT_SCOPE)
        assert silo.set_semantic(_SEMANTIC_KEY, "vim", 1.0, "user_explicit", facets=first) is None
        assert silo.set_semantic(_SEMANTIC_KEY, "vim", 1.0, "user_explicit", facets=second) is None
        assert _axes(silo, item_id) == _stamped(scope=_EXPLICIT_SCOPE)


# ── 9. repo_scope projects onto the scope axis ─────────────────────────────────


class TestRepoScopeMirrorsOntoTheScopeAxis:
    """A scoped lesson gets its carve for free, and ``value_json`` stays the truth.

    ``write_lesson`` already carries ``repo_scope`` INSIDE the stored value, and every
    reader — the injection gate included — keeps reading it there. The column is an
    index-only projection, so the two can never disagree about what a lesson is
    scoped to: one of them is written from the other.
    """

    def test_repo_scope_lands_on_the_scope_axis(self, silo) -> None:
        assert silo.write_lesson(_LESSON_RULE, category="preference", repo_scope=_REPO_SCOPE)
        row = _the_lesson_row(silo)
        assert _axes(silo, row["id"]) == _stamped(scope=_REPO_SCOPE)
        # Still authoritative in the value, which is where the gate reads it.
        assert json.loads(row["value_json"])["repo_scope"] == _REPO_SCOPE

    def test_an_explicit_scope_facet_wins_over_repo_scope(self, silo) -> None:
        """The caller that named a scope facet was the more specific of the two."""
        explicit = memory_schema.MemoryFacets(scope=_EXPLICIT_SCOPE)
        assert silo.write_lesson(
            _LESSON_RULE, category="preference", repo_scope=_REPO_SCOPE, facets=explicit
        )
        row = _the_lesson_row(silo)
        assert _axes(silo, row["id"]) == _stamped(scope=_EXPLICIT_SCOPE)
        # And the value still carries the lesson's own scope: the projection lost the
        # argument, the STORED lesson did not change meaning.
        assert json.loads(row["value_json"])["repo_scope"] == _REPO_SCOPE

    def test_the_mirror_keeps_the_other_axes_the_caller_named(self, silo) -> None:
        """The mirror rebuilds the facet object, so it must carry the rest forward.

        ``MemoryFacets`` is frozen, so filling ``scope`` means constructing a new one
        — and a construction that named only ``scope`` would drop the crew, surface
        and session the caller threaded in, silently, on exactly the writes that have
        both a scope and an identity.
        """
        partial = memory_schema.MemoryFacets(
            surface=_SURFACE, crew=_FINANCE, session_key=_SESSION_KEY
        )
        assert silo.write_lesson(
            _LESSON_RULE, category="preference", repo_scope=_REPO_SCOPE, facets=partial
        )
        row = _the_lesson_row(silo)
        assert _axes(silo, row["id"]) == _stamped(
            scope=_REPO_SCOPE, surface=_SURFACE, crew=_FINANCE, session_key=_SESSION_KEY
        )


# ── 10. Inertness of the kwarg on the v1 lineage ───────────────────────────────


def _write_a_fact(store: VectorMemoryStore, facets: memory_schema.MemoryFacets | None) -> None:
    assert store.set_semantic(_SEMANTIC_KEY, "vim", 1.0, "user_explicit", facets=facets) is None


def _write_a_lesson(store: VectorMemoryStore, facets: memory_schema.MemoryFacets | None) -> None:
    assert store.write_lesson(_LESSON_RULE, category="preference", facets=facets)


def _write_an_episode(store: VectorMemoryStore, facets: memory_schema.MemoryFacets | None) -> None:
    assert store.write_episodic(_EPISODE_TEXT, source="test", facets=facets) is True


#: (writer, the v1 relation the row lands in). All three public writers take the
#: kwarg, so all three are the surface a v1 install can reach it through.
_V1_WRITERS: list[
    tuple[str, Callable[[VectorMemoryStore, memory_schema.MemoryFacets | None], None], str]
] = [
    ("set_semantic", _write_a_fact, "semantic_memory"),
    ("write_lesson", _write_a_lesson, "semantic_memory"),
    ("write_episodic", _write_an_episode, "episodic_memories"),
]


class TestTheV1LineageIgnoresFacets:
    """The kwarg is additive-with-a-safe-default, so a caller threads identity ONCE.

    ``history_consolidation`` builds one facet object and passes it to whichever
    store the session resolved to. If a v1 store raised, or stored, or grew a column
    for it, then every operator on the default store would pay for a feature scoped
    to crew silos — and the payment would land on ``config_dir()/"memory.db"``, the
    one file this whole lineage split exists to leave alone.

    Compared against a SECOND v1 store written without the kwarg, rather than against
    a hardcoded row: "ignored" means the two files hold the same row, which is a
    claim no expectation written by hand can make.
    """

    @pytest.mark.parametrize(
        ("write", "relation"),
        [(write, relation) for _, write, relation in _V1_WRITERS],
        ids=[name for name, _, _ in _V1_WRITERS],
    )
    def test_the_same_kwarg_is_accepted_and_ignored(
        self, stores, tmp_path: Path, write, relation: str
    ) -> None:
        faceted = stores(tmp_path / "with-facets" / MEMORY_DB_FILE)
        plain = stores(tmp_path / "without-facets" / MEMORY_DB_FILE)
        for store in (faceted, plain):
            assert store._lineage == memory_schema.LINEAGE_V1
        before = {name: _columns(faceted.db, name) for name in _V1_RELATIONS}

        # Every axis, including the one only promotion sets: the claim is about the
        # kwarg, not about what any particular writer happens to know.
        write(faceted, _EVERY_AXIS)
        write(plain, None)

        # No exception, and no schema movement: same tables, same columns, in order.
        assert _objects(faceted.db, "table") == set(_V1_TABLES)
        assert _objects(faceted.db, "view") == set()
        assert {name: _columns(faceted.db, name) for name in _V1_RELATIONS} == before
        for name in _V1_RELATIONS:
            assert _FACET_COLUMNS.isdisjoint(set(before[name])), name
        assert _versions(faceted.db) == set(_V1_SCHEMA_VERSIONS)

        rows = _stable_rows(faceted, relation)
        # Non-empty FIRST: two empty stores also hold equal row sets, so the comparison
        # below says nothing until the faceted write is known to have landed.
        assert len(rows) == 1, rows
        assert rows == _stable_rows(plain, relation)


# ── 11. A lost facet must never cost the row, or the run ──────────────────────


class _RaisesOnTheFacetStamp:
    """A connection stand-in that fails ONLY the facet stamp and delegates the rest.

    Matched on the statement TEXT rather than on a call ordinal, so the forced
    failure stays attached to the stamp when the writes around it change. Everything
    else — including ``commit`` — is the real connection, because the point of the
    test is that the write itself completed.
    """

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self.refusals = 0

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        if sql == memory_schema.FACET_STAMP_SQL:
            self.refusals += 1
            raise sqlite3.OperationalError("forced: only the facet stamp fails")
        return self._real.execute(sql, parameters)

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


class TestAFacetStampNeverFailsAWrite:
    """``_stamp_facets`` swallows every exception, and the reason is not tidiness.

    ``HistoryConsolidator._consolidate`` calls its writers inside a try whose
    ``billed`` flag is still False while they run. An exception escaping the stamp is
    therefore recorded as "the consolidation attempt did not happen", so all four
    consolidation entry points re-arm on the next 60s idle tick — and keep re-arming,
    forever, with no backoff, because the next attempt fails identically. The cost of
    the opposite choice is one carve filter on one row, and nothing else reads the
    column.

    Forced for real rather than asserted from the source: the connection is replaced
    with a stand-in that fails exactly the stamp statement.
    """

    def test_the_row_survives_a_stamp_that_raises(self, silo, caplog) -> None:
        connection = _RaisesOnTheFacetStamp(silo.db)
        # ``db`` is a read-only property over ``_db``, so the patch goes on the
        # attribute. A context manager, and never ``monkeypatch.undo()``, which
        # reverts the conftest's KIROCREW_HOME pin along with it and aims the next
        # write at the operator's real data home.
        with (
            mock.patch.object(silo, "_db", connection),
            caplog.at_level(logging.WARNING, logger="kiro_crew.vector_memory"),
        ):
            assert (
                silo.set_semantic(_SEMANTIC_KEY, "vim", 1.0, "user_explicit", facets=_WRITER_AXES)
                is None
            )
        # The stamp really ran and really failed — otherwise the assertions below
        # describe a store that never attempted one.
        assert connection.refusals == 1

        item_id = memory_schema.semantic_item_id(_SEMANTIC_KEY)
        row = silo.db.execute("SELECT * FROM memory_items WHERE id = ?", (item_id,)).fetchone()
        assert row is not None, "the row was rolled back with the facet stamp"
        assert (row["text"], row["value_json"], row["kind"]) == (
            '"vim"',
            '"vim"',
            memory_schema.KIND_FACT,
        )
        # Live, not merely present: a reader still gets the memory.
        assert silo.get_semantic(_SEMANTIC_KEY)["value_json"] == '"vim"'
        # And the axes are simply absent. Losing them costs a carve filter.
        assert _axes(silo, item_id) == _stamped()

        # The one operator-visible trace of a dropped carve, so a silo whose stamps
        # are all failing is diagnosable rather than mysteriously unfiltered.
        logged = [
            record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING
        ]
        assert [m for m in logged if "Facet stamp failed" in m and item_id in m], logged


# ── 12. Promotion's only surviving provenance ─────────────────────────────────


#: A promotable cluster. Five episodes whose text infers ``pref.general``, whose
#: 80-char prefixes all differ (so the text-prefix dedup admits every one), and of
#: which exactly ONE is the longest — which makes ``max(members, key=len)``, the
#: canonical member the cluster is collapsed onto, deterministic rather than a tie
#: broken by whatever order the rows came back in.
_CLUSTER = tuple(
    f"Sample {index}: the user prefers the dark dashboard theme on every surface"
    for index in range(4)
) + ("Sample 4: the user prefers the dark dashboard theme on every surface, said at length",)

#: The key ``_infer_semantic_key`` returns for a "user prefers" cluster.
_PROMOTED_KEY = "pref.general"

#: Any width does: comparability is decided by blob byte length WITHIN one file, and
#: nothing checks a returned vector against the store's declared ``embedding_dim``.
#: Small keeps the clustering pass cheap.
_CLUSTER_EMBED_DIM = 8


def _one_direction_embed(_text: str) -> list[float]:
    """Every text onto ONE vector, so the cluster is self-similar at exactly 1.0.

    The clustering pass takes a dot product of L2-normalized episodic blobs, so an
    identical vector puts every member in one cluster for any ``min_sim`` — which
    makes the promotion a property of the code rather than of what an embedding model
    happens to think of five sentences.
    """
    return [0.5] * _CLUSTER_EMBED_DIM


@pytest.mark.skipif(not _HAS_NUMPY, reason="episodic clustering needs numpy")
class TestPromotionRecordsWhatItWasDerivedFrom:
    """A promoted fact names the episode it was synthesized out of, or nothing does.

    ``promote_episodic_patterns`` collapses a cluster onto one semantic row and then
    TOMBSTONES every member, so no read can reach them again. ``derived_from`` is the
    only surviving trace of where the fact came from — the one provenance question a
    reader of a promoted row actually asks.
    """

    def test_the_promoted_fact_names_the_canonical_episode(self, silo) -> None:
        silo.embed_fn = _one_direction_embed
        # FAISS similarity dedup would collapse five identical vectors into a single
        # row and the cluster would never reach ``min_count``. It is not a dependency
        # of this project, so patching it away is a no-op where the suite runs and
        # keeps the test deterministic on a machine that happens to have it.
        with mock.patch.object(silo, "_faiss_index", None):
            for text in _CLUSTER:
                assert silo.write_episodic(text, source="test") is True

        episodes = {
            row["text"]: row["id"]
            for row in silo.db.execute(
                "SELECT id, text FROM memory_items WHERE kind = ?",
                (memory_schema.KIND_EPISODE,),
            )
        }
        assert len(episodes) == len(_CLUSTER)
        canonical = max(_CLUSTER, key=len)
        assert len([text for text in _CLUSTER if len(text) == len(canonical)]) == 1

        assert silo.promote_episodic_patterns(min_count=len(_CLUSTER)) == 1

        promoted = memory_schema.semantic_item_id(_PROMOTED_KEY)
        # ``derived_from`` and NOTHING else: promotion knows the provenance and has no
        # business inventing a crew, a surface or a session for a synthesized row.
        assert _axes(silo, promoted) == _stamped(derived_from=episodes[canonical])

        # Every member is retired, which is what makes the column the only trace: the
        # rows survive as tombstones and no read path returns one.
        live = silo.db.execute(
            "SELECT COUNT(*) FROM memory_items WHERE kind = ? AND is_deleted = 0",
            (memory_schema.KIND_EPISODE,),
        ).fetchone()[0]
        assert live == 0
        assert silo.get_episodic_list(limit=10) == []
        assert len(_episode_ids(silo)) == len(_CLUSTER)


# ── 13. The crew DDL, on its own ───────────────────────────────────────────────


class TestTheCrewSchemaIsSelfConsistent:
    """Properties of ``CREW_SCHEMA_SQL`` itself, checked against a bare connection."""

    @pytest.fixture
    def db(self):
        """An in-memory database carrying the crew DDL and nothing else."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(memory_schema.CREW_SCHEMA_SQL)
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _insert(db: sqlite3.Connection, item_id: str, kind: str, key: str | None) -> None:
        db.execute(
            "INSERT INTO memory_items (id, kind, key, text, source, created_at, updated_at) "
            "VALUES (?, ?, ?, 'body', 'test', '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T00:00:00+00:00')",
            (item_id, kind, key),
        )

    def test_applying_the_ddl_twice_creates_nothing_twice(self, db) -> None:
        """Every migration must be idempotent, because a version row can be refilled.

        ``init()`` applies migrations by SET membership (``if ver not in applied``), so
        a ``schema_version`` row lost to a hand edit or a partial restore makes the
        whole DDL run again against a populated file.
        """
        first = sorted(
            (row["type"], row["name"]) for row in db.execute("SELECT * FROM sqlite_schema")
        )
        db.executescript(memory_schema.CREW_SCHEMA_SQL)
        second = sorted(
            (row["type"], row["name"]) for row in db.execute("SELECT * FROM sqlite_schema")
        )
        assert second == first
        assert len(first) == len(set(first)), "an object was created twice"

    def test_a_punched_version_row_refills_without_changing_the_schema(self, stores) -> None:
        """The same idempotency, driven through ``init()`` on a real silo file."""
        _declare_silos(_FINANCE)
        path = resolve_store_path(_FINANCE)
        first = stores(path)
        before = {kind: _objects(first.db, kind) for kind in ("table", "view", "trigger", "index")}
        first.db.execute("DELETE FROM schema_version")
        first.db.commit()
        first.close()

        again = stores(path)
        assert again._lineage == memory_schema.LINEAGE_CREW
        assert _versions(again.db) == {memory_schema.CREW_SCHEMA_VERSION}
        assert {
            kind: _objects(again.db, kind) for kind in ("table", "view", "trigger", "index")
        } == before

    def test_the_check_constraint_refuses_an_unknown_kind(self, db) -> None:
        """Three kinds, closed. ``preference`` is a lesson CATEGORY, not a kind."""
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            self._insert(db, "x", "preference", "pref.color")
        for kind in (*memory_schema.SEMANTIC_KINDS, memory_schema.KIND_EPISODE):
            assert kind in memory_schema.CREW_SCHEMA_SQL

    def test_one_key_cannot_exist_under_two_kinds(self, db) -> None:
        """``UNIQUE (key)``, not the design's per-kind uniqueness.

        v1 spells this ``key TEXT PRIMARY KEY`` — one row per key, period. Per-kind
        uniqueness is strictly WEAKER: ``pref.color`` could exist as both a directive
        and a fact, and then ``semantic_memory`` returns two rows where every statement
        in the engine expects at most one.
        """
        self._insert(db, "a", memory_schema.KIND_FACT, "pref.color")
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            self._insert(db, "b", memory_schema.KIND_DIRECTIVE, "pref.color")

    def test_many_episodes_share_a_null_key(self, db) -> None:
        """SQLite treats NULLs as distinct in a unique index, so episodes are free.

        An episode has no key and is identified by ``id`` alone, which is what lets
        one uniqueness domain serve both the keyed kinds and the keyless one.
        """
        for index in range(3):
            self._insert(db, f"e{index}", memory_schema.KIND_EPISODE, None)
        assert db.execute("SELECT COUNT(*) FROM memory_items WHERE key IS NULL").fetchone()[0] == 3

    def test_both_views_refuse_every_write(self, db) -> None:
        """READ-ONLY by construction, which is why writes name the physical table."""
        self._insert(db, "a", memory_schema.KIND_FACT, "pref.color")
        self._insert(db, "e0", memory_schema.KIND_EPISODE, None)
        for relation in _V1_RELATIONS:
            for statement in (
                f"UPDATE {relation} SET is_deleted = 1",
                f"DELETE FROM {relation}",
                f"INSERT INTO {relation} (is_deleted) VALUES (0)",
            ):
                with pytest.raises(sqlite3.OperationalError, match="because it is a view"):
                    db.execute(statement)

    def test_the_schema_carries_no_trigger(self, db) -> None:
        """And must never be given one, least of all an ``INSTEAD OF`` on a view.

        ``snapshot_redact._refuse_update_triggers_that_destroy_rows`` refuses a
        database whose UPDATE trigger writes a relation other than the trigger's own;
        its exemption requires ``target == tbl_name``, which an ``INSTEAD OF`` trigger
        on a view can never satisfy. The refusal keys on the trigger NAME and is
        permanent, so adding one would stop the whole product database from being
        backup-inspectable — not one table, the database.
        """
        assert _objects(db, "trigger") == set()
        # The STATEMENT, not the word: the DDL's own comment names the prohibition,
        # so a bare "TRIGGER" search would fail on the documentation of the rule.
        assert "CREATE TRIGGER" not in memory_schema.CREW_SCHEMA_SQL.upper()


# ── 14. The lineage-agnostic tables have ONE definition ────────────────────────


#: The two product tables that are NOT lineage-specific. Both lineages hold them
#: under the same name AND reach them through the same statements: ``_log_event``,
#: ``get_events``, ``rotate_events`` and ``_read_meta``/``_write_meta`` name them
#: literally, with none of the ``_sem_rel``/``_epi_rel`` indirection the semantic and
#: episodic writes carry.
_SHARED_TABLES = ("memory_events", "memory_meta")

#: The one fragment each shared table's DDL comes from. Keyed by table so the
#: source-level assertions below name a fragment rather than re-spelling its text —
#: which is what keeps this test a statement about the SHARING and not a third copy
#: of the schema.
_SHARED_DDL = {
    "memory_events": memory_schema.MEMORY_EVENTS_SQL,
    "memory_meta": memory_schema.MEMORY_META_SQL,
}


def _table_shape(db: sqlite3.Connection, table: str) -> dict[str, object]:
    """Everything about *table* that a statement written against it binds to.

    Columns as an ORDERED list of ``(name, type, notnull, default, pk)`` — what an
    INSERT's column list and a ``SELECT *`` row both depend on — plus every index
    with its uniqueness, its origin and its own columns. SQLite's implicit
    ``sqlite_autoindex_*`` counts: it is how a PRIMARY KEY on a TEXT column is
    backed, so dropping that key would move this set and nothing else would.
    """
    columns = [tuple(row[1:6]) for row in db.execute(f"PRAGMA table_info({table})")]
    indexes = {
        row[1]: (row[2], row[3], [info[2] for info in db.execute(f"PRAGMA index_info({row[1]})")])
        for row in db.execute(f"PRAGMA index_list({table})")
    }
    return {"columns": columns, "indexes": indexes}


class TestTheSharedTablesHaveOneDefinition:
    """``memory_events`` and ``memory_meta`` are one definition, composed twice.

    These two are the only product tables the engine reaches with NO per-lineage
    indirection: ``_log_event``'s INSERT, ``get_events``' and ``rotate_events``'
    statements, and the ``_read_meta``/``_write_meta`` pair that
    ``reconcile_embedding_space`` keys the per-file vector signature through, all
    name them literally on both lineages.

    So a column on one lineage and not the other is a statement that works on v1 and
    raises on a silo — and nothing else in the tree would report it.
    ``MIGRATIONS_CREW`` is a single frozen entry at
    :data:`memory_schema.CREW_SCHEMA_VERSION`, so a fourth ``_MIGRATIONS`` entry
    adding a column would reach every v1 file and NO silo. v1 is what the rest of the
    suite exercises, so the break would surface on an operator's silo, at runtime,
    from whichever write path happened to run first.
    """

    def test_both_lineages_compose_the_same_fragment(self) -> None:
        """The source-level half, and the half that makes divergence impossible.

        A shape comparison alone can only observe what someone already wrote twice;
        this observes that neither lineage spells the DDL at all. Containment plus a
        count of one, so a hand copy re-added BESIDE the shared fragment fails here
        instead of passing because the shared text is still present somewhere.
        """
        assert _SCHEMA_V1.endswith(memory_schema.MEMORY_EVENTS_SQL)
        assert _MEMORY_META_TABLE == memory_schema.MEMORY_META_SQL
        assert memory_schema.CREW_SCHEMA_SQL.endswith(
            memory_schema.MEMORY_EVENTS_SQL + memory_schema.MEMORY_META_SQL
        )

        # ``_SCHEMA_V1`` and ``_MEMORY_META_TABLE`` are v1's first and third
        # migrations; concatenated they are every byte of DDL a v1 file is built from.
        v1_script = _SCHEMA_V1 + _MEMORY_META_TABLE
        for table in _SHARED_TABLES:
            create = f"CREATE TABLE IF NOT EXISTS {table} ("
            assert _SHARED_DDL[table].count(create) == 1, table
            assert v1_script.count(create) == 1, table
            assert memory_schema.CREW_SCHEMA_SQL.count(create) == 1, table

    def test_both_lineages_present_the_same_shape(self, stores, tmp_path: Path) -> None:
        """Identical column names, order, types, nullability and index sets.

        Built through ``init()`` rather than by executing the constants, so it covers
        the composition as the ENGINE applies it — v1 reaches ``memory_meta`` through
        its third migration and a silo through its first, and the two must still land
        on one shape.
        """
        _declare_silos(_FINANCE)
        crew = stores(resolve_store_path(_FINANCE))
        v1 = stores(tmp_path / "v1-reference" / MEMORY_DB_FILE)
        assert crew._lineage == memory_schema.LINEAGE_CREW
        assert v1._lineage == memory_schema.LINEAGE_V1

        for table in _SHARED_TABLES:
            shape = _table_shape(v1.db, table)
            assert shape["columns"], f"{table} has no columns on v1 — the DDL build is broken"
            assert _table_shape(crew.db, table) == shape, table
            # The stored CREATE text too, which only ONE definition can make equal:
            # two hand copies differed by their column alignment alone, and that is
            # precisely the difference a shape comparison cannot see.
            assert _ddl(crew.db, table) == _ddl(v1.db, table), table

        # Non-vacuity, per table: two empty index sets also compare equal. Spelled as
        # literals for the reason the module docstring gives — deriving them from the
        # DDL would move the pin along with the thing it holds still.
        assert set(_table_shape(v1.db, "memory_events")["indexes"]) == {
            "idx_events_type",
            "idx_events_key",
        }
        # ``key TEXT PRIMARY KEY`` on a TEXT column, which SQLite backs with an
        # implicit index rather than with the rowid.
        assert set(_table_shape(v1.db, "memory_meta")["indexes"]) == {
            "sqlite_autoindex_memory_meta_1"
        }

    def test_the_shared_statements_round_trip_on_both_lineages(
        self, stores, tmp_path: Path
    ) -> None:
        """The failure the shape identity exists to prevent, driven end to end.

        ``_log_event`` is reached from every write path and ``memory_meta`` from
        ``reconcile_embedding_space``, so a column present on one lineage only shows
        up here as ``sqlite3.OperationalError`` from an ordinary write — on the silo
        alone, since v1 is the lineage every other test exercises.
        """
        _declare_silos(_FINANCE)
        crew = stores(resolve_store_path(_FINANCE))
        v1 = stores(tmp_path / "v1-reference" / MEMORY_DB_FILE)

        signature = "shared-tables-round-trip"
        for store in (crew, v1):
            assert store.set_semantic(_SEMANTIC_KEY, "vim", 1.0, "user_explicit") is None
            # ``_log_event`` runs from the tail of every write and swallows its own
            # exceptions, so the row it left behind is the only proof it worked.
            assert [event["memory_key"] for event in store.get_events(limit=10)] == [
                _SEMANTIC_KEY
            ], store._lineage
            # The real ``memory_meta`` caller, not ``_write_meta`` directly: a first
            # call on a file with no recorded space stamps it and clears nothing.
            assert store.reconcile_embedding_space(signature) == 0
            assert store.recorded_embedding_space() == signature, store._lineage

"""The carve READ side: filtering, counting, paging, and the two refusals.

``memory_schema`` stamps five facets onto every crew row and indexes three of them;
until there is a reader, the columns and their indexes are write-only. This module
covers the reader, and its subject is the three ways a filter API goes wrong:

* **A dropped filter WIDENS the carve.** A facet the query silently ignores hands
  back every crew's rows under a heading naming one crew, and nothing raises. So
  each axis is asserted to EXCLUDE — a negative row is planted for every test that
  claims a filter works, and an unknown axis name is refused rather than dropped.
* **A wrong answer beats no answer.** On the v1 lineage the columns do not exist,
  and an empty page there says "this crew has no memories" about a store holding
  thousands of unfaceted rows. The contract is a refusal, and the same refusal at
  every surface.
* **A filter API invites string-built SQL.** Every identifier that reaches a
  statement is one of ``memory_schema``'s own literals, iterated from
  :data:`memory_schema.FACET_NAMES`; a caller's mapping is consulted for
  membership only. The hostile-name tests assert the refusal AND that the table
  survives, because "it raised" and "it did not execute" are different claims.

The store fixtures follow ``test_memory_v2_schema.py``: declare the silos first,
resolve second, and close every connection at teardown.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from aiohttp import web

from kiro_crew import memory_schema
from kiro_crew.config import loader as loader_mod
from kiro_crew.config.loader import config_dir
from kiro_crew.dashboard.handlers import _shared
from kiro_crew.memory_stores import (
    DEFAULT_MEMORY_STORE,
    MEMORY_DB_FILE,
    resolve_store_path,
)
from kiro_crew.vector_memory import VectorMemoryStore

# Each test writes ``config.json`` into its own data home and drops the process-wide
# config cache, so two workers racing that global is a flake. One xdist group for the
# whole module, the way ``test_memory_v2_schema.py`` does it.
pytestmark = pytest.mark.xdist_group("memory_v2_facet_read")


_FINANCE = "finance"
_OPS = "ops"

#: Two surfaces, both bounded labels of the kind ``telemetry_channel_of`` answers.
_SLACK = "slack"
_DISCORD = "discord"

_SESSION_A = "slack:C0LEDGER"
_SESSION_B = "discord:987"

_SCOPE_A = "acme/ledger"
_SCOPE_B = "acme/portal"

_EPISODE_TEXT = "The finance crew rotated the deploy key on the release host."

#: A facet name shaped like an injection. Any of these reaching a statement as an
#: IDENTIFIER would either drop the ``WHERE`` or destroy the table, so they are the
#: right inputs to prove the allowlist is what composes SQL.
_HOSTILE_NAMES = [
    "crew; DROP TABLE memory_items",
    "crew) = '' OR 1=1 --",
    "crew, (SELECT embedding FROM memory_items)",
    "1=1",
    "",
    "CREW",
]


def _declare_silos(*names: str) -> None:
    """Declare *names* as memory stores in the per-test data home.

    ``memory_stores`` is an OBJECT keyed by store name; spelled as an ARRAY the
    schema drops the whole entry and every name degrades to the default, so
    ``resolve_store_path(name)`` would answer the DEFAULT store's own file and a
    test written that way asserts against the operator-shaped file.
    """
    payload = {
        "memory_stores": {DEFAULT_MEMORY_STORE: {}, **{name: {} for name in names}},
        "default_memory_store": DEFAULT_MEMORY_STORE,
    }
    (config_dir() / "config.json").write_text(json.dumps(payload), encoding="utf-8")
    loader_mod._invalidate_config_cache()


@pytest.fixture
def stores():
    """A builder that opens vector stores and closes every one at teardown.

    Each store holds an open SQLite connection, and an open handle blocks
    ``tmp_path`` teardown on Windows — so the close belongs to the fixture rather
    than to each test's happy path, where a failed assertion would skip it.
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


def _facets(**axes: str) -> memory_schema.MemoryFacets:
    return memory_schema.MemoryFacets(**axes)


@pytest.fixture
def silo(stores) -> VectorMemoryStore:
    """A crew silo carrying a deliberately MIXED population.

    Every test that asserts a filter selects needs a row the filter must exclude,
    so the seed spans two crews, two surfaces, two sessions, two scopes, all three
    kinds, and rows with nothing stamped at all. Without the negative rows a query
    that ignored its filters entirely would satisfy every membership assertion.
    """
    _declare_silos(_FINANCE, _OPS)
    store = stores(resolve_store_path(_FINANCE))
    assert store._lineage == memory_schema.LINEAGE_CREW
    seed = [
        ("pref.editor", _facets(crew=_FINANCE, surface=_SLACK, session_key=_SESSION_A)),
        ("pref.theme", _facets(crew=_FINANCE, surface=_DISCORD, session_key=_SESSION_B)),
        ("pref.lang", _facets(crew=_OPS, surface=_SLACK, session_key=_SESSION_A)),
        ("project.ledger", _facets(crew=_FINANCE, scope=_SCOPE_A)),
        ("project.portal", _facets(crew=_OPS, scope=_SCOPE_B)),
        # Nothing stamped: the ``''`` population every axis has, and the rows a
        # filter for a named value must leave out.
        ("user.name", None),
    ]
    for key, facets in seed:
        assert store.set_semantic(key, key, 1.0, "user_explicit", facets=facets) is None
    assert store.write_lesson(
        "Rebase before merging.",
        category="preference",
        facets=_facets(crew=_FINANCE, surface=_SLACK),
    )
    assert (
        store.write_episodic(
            _EPISODE_TEXT, source="test", facets=_facets(crew=_FINANCE, surface=_SLACK)
        )
        is True
    )
    return store


def _handles(rows: list[dict]) -> set[str]:
    """Each row's key, or its id when it has none (an episode)."""
    return {row["key"] or row["id"] for row in rows}


def _kinds(rows: list[dict]) -> set[str]:
    return {row["kind"] for row in rows}


# ── 1. The allowlist is the dataclass, and the CLI reaches all of it ──────────


class TestTheAllowlistIsDerivedNotRestated:
    """One source for "what is a facet": :class:`memory_schema.MemoryFacets`.

    Three copies of the axis list exist by necessity — the dataclass, the query
    allowlist, and the CLI's flags, which argparse cannot build from a runtime
    tuple without paying an import on every invocation. The first two are derived
    from the dataclass; this class is what keeps the third from drifting into a
    silently unreachable sixth axis.
    """

    def test_the_query_allowlist_is_exactly_the_dataclass_fields(self) -> None:
        declared = tuple(field.name for field in dataclasses.fields(memory_schema.MemoryFacets))
        assert memory_schema.FACET_NAMES == declared

    def test_the_group_axes_are_the_facets_plus_kind(self) -> None:
        """``kind`` is groupable and NOT a facet, which is a deliberate asymmetry.

        It is the row type rather than a stamped attribution, so it must stay out
        of the FILTER allowlist's derivation — but "how much of this is directives"
        is the same question an operator asks in the same breath.
        """
        assert memory_schema.GROUPABLE_COLUMNS == (*memory_schema.FACET_NAMES, "kind")
        assert "kind" not in memory_schema.FACET_NAMES

    def test_the_kinds_tuple_matches_the_check_constraint(self) -> None:
        assert memory_schema.ALL_KINDS == (
            memory_schema.KIND_DIRECTIVE,
            memory_schema.KIND_FACT,
            memory_schema.KIND_EPISODE,
        )
        for kind in memory_schema.ALL_KINDS:
            assert f"'{kind}'" in memory_schema.CREW_SCHEMA_SQL

    def test_the_cli_exposes_one_flag_per_facet_and_the_closed_choice_sets(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
    ) -> None:
        """The CLI's literals, checked against the derived tuples.

        A sixth facet added to the dataclass without a flag here is filterable
        everywhere except the surface an operator actually types, and nothing else
        in the tree reports it.

        Read from the RENDERED help, the way ``test_cli_help.py`` reaches the
        parser: argparse's ``_actions`` is private, and the help text is what an
        operator discovering the verb actually sees.
        """
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr("sys.argv", ["kirocrew", "memory", "carve", "--help"])
        from kiro_crew.cli import main

        with pytest.raises(SystemExit):
            main()
        rendered = capsys.readouterr().out
        for facet in memory_schema.FACET_NAMES:
            assert f"--{facet.replace('_', '-')}" in rendered, facet
        # argparse renders a closed choice set inline, so the two orders are pinned
        # as well as the membership.
        assert "{" + ",".join(memory_schema.ALL_KINDS) + "}" in rendered
        assert "{" + ",".join(memory_schema.GROUPABLE_COLUMNS) + "}" in rendered


# ── 2. Every axis filters, and each one EXCLUDES ──────────────────────────────


#: ``(facet, value, the keys that carry it)``. Spelled out rather than derived from
#: the seed, so a seed edit that quietly changed what a filter should return fails
#: here instead of agreeing with itself.
_ONE_AXIS = [
    ("crew", _FINANCE, {"pref.editor", "pref.theme", "project.ledger"}),
    ("crew", _OPS, {"pref.lang", "project.portal"}),
    ("surface", _DISCORD, {"pref.theme"}),
    ("session_key", _SESSION_B, {"pref.theme"}),
    ("scope", _SCOPE_A, {"project.ledger"}),
    ("scope", _SCOPE_B, {"project.portal"}),
]


class TestEachAxisFiltersAndExcludes:
    """One axis at a time, asserted by EQUALITY against the expected key set.

    Equality rather than containment: a query that ignored its filter would
    contain the expected rows and every other row too, so a membership assertion
    is exactly the shape that passes with no filtering happening at all.
    """

    @pytest.mark.parametrize(
        ("facet", "value", "expected"),
        [(f, v, e) for f, v, e in _ONE_AXIS],
        ids=[f"{f}={v}" for f, v, _ in _ONE_AXIS],
    )
    def test_a_single_axis_selects_exactly_its_rows(
        self, silo, facet: str, value: str, expected: set[str]
    ) -> None:
        rows = silo.list_by_facets({facet: value}, limit=100)
        # The lesson and the episode carry a crew and a surface but no key, so the
        # crew/surface cases are compared on the KEYED rows plus a count of the rest.
        keyed = {row["key"] for row in rows if row["key"] and not row["key"].startswith("lesson.")}
        assert keyed == expected
        for row in rows:
            assert row[facet] == value, (facet, row["id"])

    def test_a_named_value_never_returns_an_unattributed_row(self, silo) -> None:
        """The negative half of every case above, made explicit.

        ``user.name`` is stamped with nothing, so it must be absent from every
        named-value carve — and present in the blank one.
        """
        for facet in memory_schema.FACET_NAMES:
            named = silo.list_by_facets({facet: _FINANCE}, limit=100)
            assert "user.name" not in _handles(named), facet

    def test_an_empty_value_selects_the_unattributed_rows(self, silo) -> None:
        """``{"crew": ""}`` is a filter, not an omission — and the two differ.

        Every axis is ``NOT NULL DEFAULT ''``, so "no writer attributed this" is a
        VALUE. A mapping keyed by facet name is what makes the question askable at
        all: :class:`memory_schema.MemoryFacets` spells absence and "not
        applicable" identically, which is right for a stamp and would collapse
        these two carves into one.
        """
        blank = _handles(silo.list_by_facets({"crew": ""}, limit=100))
        assert blank == {"user.name"}
        # Omitting the axis is the OTHER question, and it answers with everything.
        assert len(silo.list_by_facets({}, limit=100)) == 8

    def test_the_kind_axis_narrows_to_one_row_type(self, silo) -> None:
        assert _kinds(silo.list_by_facets({}, kind=memory_schema.KIND_EPISODE, limit=100)) == {
            memory_schema.KIND_EPISODE
        }
        directives = silo.list_by_facets({}, kind=memory_schema.KIND_DIRECTIVE, limit=100)
        assert [row["key"].startswith("lesson.") for row in directives] == [True]
        facts = silo.list_by_facets({}, kind=memory_schema.KIND_FACT, limit=100)
        assert len(facts) == 6

    def test_a_tombstoned_row_leaves_the_carve(self, silo) -> None:
        """Live rows only, which is also what lets the query ride ``(col, is_deleted)``."""
        before = _handles(silo.list_by_facets({"crew": _FINANCE}, limit=100))
        assert "pref.editor" in before
        assert silo.delete_semantic("pref.editor", "user_explicit") is True
        after = _handles(silo.list_by_facets({"crew": _FINANCE}, limit=100))
        assert after == before - {"pref.editor"}


class TestCombinationsAndTogether:
    """Two axes are an intersection, never a union.

    A builder that joined its clauses with ``OR``, or that kept only the last one,
    passes every single-axis test above. These are the cases that separate them:
    each pair has rows matching one half and not the other.
    """

    def test_crew_and_surface_intersect(self, silo) -> None:
        rows = silo.list_by_facets({"crew": _FINANCE, "surface": _SLACK}, limit=100)
        keyed = {row["key"] for row in rows if row["key"] and not row["key"].startswith("lesson.")}
        assert keyed == {"pref.editor"}
        # ``pref.lang`` is slack but ops; ``pref.theme`` is finance but discord.
        # Either one appearing means the two clauses did not AND.
        assert {"pref.lang", "pref.theme"}.isdisjoint(_handles(rows))

    def test_a_facet_and_a_kind_intersect(self, silo) -> None:
        rows = silo.list_by_facets(
            {"crew": _FINANCE, "surface": _SLACK}, kind=memory_schema.KIND_FACT, limit=100
        )
        assert _handles(rows) == {"pref.editor"}

    def test_three_axes_narrow_to_one_row(self, silo) -> None:
        rows = silo.list_by_facets(
            {"crew": _FINANCE, "surface": _DISCORD, "session_key": _SESSION_B}, limit=100
        )
        assert _handles(rows) == {"pref.theme"}

    def test_a_contradictory_combination_is_empty(self, silo) -> None:
        """Both values exist; no row carries both. An OR join answers five rows."""
        assert silo.list_by_facets({"crew": _FINANCE, "scope": _SCOPE_B}, limit=100) == []


# ── 3. Counts ─────────────────────────────────────────────────────────────────


class TestCountsAreRight:
    """The "what is actually in this store" question, by value and by total."""

    def test_counting_by_crew_partitions_every_live_row(self, silo) -> None:
        counts = silo.count_by_facet("crew")
        # The lesson and the episode are finance too, so finance carries five.
        assert counts == {_FINANCE: 5, _OPS: 2, "": 1}
        # A partition, not a sample: the totals add up to the whole live store.
        assert sum(counts.values()) == len(silo.list_by_facets({}, limit=100))

    def test_counting_by_surface_and_by_kind(self, silo) -> None:
        # slack: two facts, the lesson and the episode. blank: the two scoped
        # projects and the row nothing stamped.
        assert silo.count_by_facet("surface") == {_SLACK: 4, "": 3, _DISCORD: 1}
        assert silo.count_by_facet("kind") == {
            memory_schema.KIND_FACT: 6,
            memory_schema.KIND_DIRECTIVE: 1,
            memory_schema.KIND_EPISODE: 1,
        }

    def test_a_count_can_be_asked_within_a_carve(self, silo) -> None:
        """Filters narrow the population BEFORE the grouping.

        A builder that grouped the whole table and then filtered the result would
        answer the unfiltered totals here.
        """
        assert silo.count_by_facet("kind", {"crew": _FINANCE}) == {
            memory_schema.KIND_FACT: 3,
            memory_schema.KIND_DIRECTIVE: 1,
            memory_schema.KIND_EPISODE: 1,
        }
        assert silo.count_by_facet("surface", {"crew": _OPS}) == {_SLACK: 1, "": 1}
        assert silo.count_by_facet("crew", kind=memory_schema.KIND_FACT) == {
            _FINANCE: 3,
            _OPS: 2,
            "": 1,
        }

    def test_the_blank_bucket_is_reported_and_not_dropped(self, silo) -> None:
        """The unattributed rows are an ANSWER, not noise.

        A count that filtered ``!= ''`` would hide exactly the rows an operator is
        looking for when they ask why a carve is short.
        """
        assert silo.count_by_facet("derived_from") == {"": 8}
        assert silo.count_by_facet("scope")[""] == 6

    def test_counts_are_ordered_most_populous_first(self, silo) -> None:
        """The order is what makes the group truncation the least populous tail."""
        totals = list(silo.count_by_facet("crew").values())
        assert totals == sorted(totals, reverse=True)

    def test_a_count_within_an_empty_carve_is_empty(self, silo) -> None:
        assert silo.count_by_facet("kind", {"crew": "nobody"}) == {}


# ── 4. The v1 lineage refuses, identically, at every surface ──────────────────


class TestTheV1LineageRefusesRatherThanAnsweringEmpty:
    """The chosen contract, and the reason it is not "empty result".

    On v1 the facet columns do not exist, so "how many rows carry
    ``crew = finance``" has no answer there. An empty page or a ``{}`` count says
    "none" — which, for the default store holding thousands of unfaceted rows, is
    the one wrong answer this seam can give: an operator reads it as "this crew
    remembers nothing" and goes looking for a bug in the writer.

    Asserted on the DEFAULT store's own path as well as a bare temp file, because
    the default store is the file the whole lineage split exists to leave alone and
    is the one a CLI invocation with no ``--store`` reaches.
    """

    @pytest.fixture
    def v1(self, stores, tmp_path: Path) -> VectorMemoryStore:
        _declare_silos(_FINANCE)
        store = stores(tmp_path / "v1-reference" / MEMORY_DB_FILE)
        assert store._lineage == memory_schema.LINEAGE_V1
        assert store.set_semantic("pref.editor", "vim", 1.0, "user_explicit") is None
        # Populated, which is the whole point: an empty store cannot tell "no rows
        # match" apart from "this store cannot answer".
        assert len(store.get_all_semantic()) == 1
        return store

    def test_listing_refuses(self, v1) -> None:
        with pytest.raises(memory_schema.FacetsUnsupported):
            v1.list_by_facets({"crew": _FINANCE})

    def test_counting_refuses(self, v1) -> None:
        with pytest.raises(memory_schema.FacetsUnsupported):
            v1.count_by_facet("crew")

    def test_a_filterless_call_refuses_too(self, v1) -> None:
        """The refusal is about the FILE, not about what was asked of it.

        A guard placed on the predicate instead of on the lineage would let an
        unfiltered call through and answer from the v1 tables — reporting rows with
        no facet columns as though they had been carved.
        """
        with pytest.raises(memory_schema.FacetsUnsupported):
            v1.list_by_facets({})

    def test_the_default_store_refuses(self, stores) -> None:
        _declare_silos(_FINANCE)
        default = stores(resolve_store_path(DEFAULT_MEMORY_STORE))
        assert default._lineage == memory_schema.LINEAGE_V1
        with pytest.raises(memory_schema.FacetsUnsupported):
            default.count_by_facet("crew")

    def test_the_refusal_names_the_lineage_and_not_a_path(self, v1) -> None:
        """The message reaches an HTTP body, so it must carry no filesystem path."""
        with pytest.raises(memory_schema.FacetsUnsupported) as excinfo:
            v1.count_by_facet("crew")
        message = str(excinfo.value)
        assert "v1" in message and "crew memory store" in message
        assert str(v1._db_path) not in message

    def test_the_refusal_is_not_an_ordinary_lookup_error(self) -> None:
        """A distinct type, so a caller cannot swallow it with a bare ``except``.

        ``FacetsUnsupported`` and ``UnknownFacet`` mean different things — one is
        the store, the other is the request — and the two surfaces map them to
        different statuses, so they must not share a base a handler would catch
        together by accident.
        """
        assert not issubclass(memory_schema.FacetsUnsupported, memory_schema.UnknownFacet)
        assert not issubclass(memory_schema.UnknownFacet, memory_schema.FacetsUnsupported)
        assert issubclass(memory_schema.UnknownFacet, ValueError)


# ── 5. A hostile name is refused, never interpolated ─────────────────────────


class TestAHostileFacetNameIsRefused:
    """Names come from the allowlist; values are bound. Both halves, asserted.

    Every case checks TWO things: the call raised, and the table is still there
    with its rows. "It raised" alone would also be true of a statement that
    executed a ``DROP`` and then failed on the next line.
    """

    @staticmethod
    def _intact(store: VectorMemoryStore) -> None:
        """``memory_items`` still exists and still holds the whole seed."""
        assert store.db.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 8
        assert len(store.list_by_facets({}, limit=100)) == 8

    @pytest.mark.parametrize("name", _HOSTILE_NAMES)
    def test_a_hostile_filter_name_is_refused(self, silo, name: str) -> None:
        with pytest.raises(memory_schema.UnknownFacet) as excinfo:
            silo.list_by_facets({name: "x"})
        # The message names the closed set, so whoever trips it can fix it.
        assert "carve axes" in str(excinfo.value)
        self._intact(silo)

    @pytest.mark.parametrize("name", _HOSTILE_NAMES)
    def test_a_hostile_group_axis_is_refused(self, silo, name: str) -> None:
        with pytest.raises(memory_schema.UnknownFacet) as excinfo:
            silo.count_by_facet(name)
        assert "group axes" in str(excinfo.value)
        self._intact(silo)

    def test_a_valid_axis_beside_a_hostile_one_is_refused_as_a_whole(self, silo) -> None:
        """No partial acceptance: the refusal precedes any SQL composition.

        A builder that skipped unknown keys and honoured the rest would pass every
        single-axis test in this module while quietly widening any carve whose
        caller typo'd one axis.
        """
        with pytest.raises(memory_schema.UnknownFacet):
            silo.list_by_facets({"crew": _FINANCE, "cREw": _OPS})
        self._intact(silo)

    def test_an_unknown_kind_is_refused_rather_than_answered_empty(self, silo) -> None:
        """A typo'd kind that returned no rows is indistinguishable from an answer."""
        with pytest.raises(memory_schema.UnknownFacet):
            silo.list_by_facets({}, kind="preference")
        with pytest.raises(memory_schema.UnknownFacet):
            silo.count_by_facet("crew", kind="lesson")
        self._intact(silo)

    def test_a_hostile_VALUE_is_bound_and_simply_matches_nothing(self, silo) -> None:  # noqa: N802
        """The other half of the rule: a VALUE is never a syntax hazard.

        A value is bound, so the nastiest string is just a string that no row
        carries. This is what makes refusing NAMES sufficient rather than needing
        a value sanitizer as well.
        """
        for value in ("' OR 1=1 --", "'; DROP TABLE memory_items; --", "%", "_"):
            assert silo.list_by_facets({"crew": value}, limit=100) == [], value
            assert silo.count_by_facet("kind", {"crew": value}) == {}, value
        self._intact(silo)

    def test_no_built_statement_carries_a_caller_value(self, silo) -> None:
        """Structural: the SQL text holds identifiers only, the params hold values."""
        sql, params = memory_schema.facet_page_query({"crew": _FINANCE}, memory_schema.KIND_FACT)
        assert _FINANCE not in sql and memory_schema.KIND_FACT not in sql
        assert _FINANCE in params and memory_schema.KIND_FACT in params
        # And the read names the PHYSICAL table, never a view: the views expose no
        # facet column, so a facet read through one raises ``no such column``.
        assert "memory_items" in sql
        assert "semantic_memory" not in sql and "episodic_memories" not in sql

    def test_the_query_never_reads_or_returns_a_vector(self, silo) -> None:
        """A facet partitions; it never scores. So no ``embedding`` anywhere."""
        for sql, _params in (
            memory_schema.facet_page_query({}, ""),
            memory_schema.facet_count_query("crew", {}),
        ):
            assert "embedding" not in sql
        assert "embedding" not in memory_schema.FACET_PAGE_COLUMNS
        row = silo.list_by_facets({}, limit=1)[0]
        assert "embedding" not in row
        assert not any(isinstance(value, (bytes, memoryview)) for value in row.values())


# ── 6. Paging ─────────────────────────────────────────────────────────────────


class TestPagingIsStable:
    """Every row exactly once across pages, even when timestamps tie.

    ``created_at`` is a microsecond ISO string, so a tie is rare rather than
    impossible — and one tie is enough for ``ORDER BY created_at DESC`` alone to
    show a row on two pages and hide another entirely. The tie-break on the primary
    key is what makes the order total.
    """

    def test_pages_partition_the_result(self, silo) -> None:
        everything = [row["id"] for row in silo.list_by_facets({}, limit=100)]
        assert len(everything) == 8
        paged: list[str] = []
        for offset in range(0, len(everything), 3):
            paged.extend(row["id"] for row in silo.list_by_facets({}, limit=3, offset=offset))
        assert paged == everything

    def test_paging_survives_identical_timestamps(self, silo) -> None:
        """The tie case, forced rather than hoped for.

        Written against ``memory_items`` directly: it is the physical table, and
        both v1 relation names are read-only views on this lineage.
        """
        silo.db.execute("UPDATE memory_items SET created_at = '2026-01-01T00:00:00+00:00'")
        silo.db.commit()
        everything = [row["id"] for row in silo.list_by_facets({}, limit=100)]
        assert len(set(everything)) == 8
        paged: list[str] = []
        for offset in range(0, 8, 2):
            paged.extend(row["id"] for row in silo.list_by_facets({}, limit=2, offset=offset))
        assert paged == everything

    def test_a_filtered_page_pages_within_its_carve(self, silo) -> None:
        expected = [row["id"] for row in silo.list_by_facets({"crew": _FINANCE}, limit=100)]
        assert len(expected) == 5
        first = silo.list_by_facets({"crew": _FINANCE}, limit=2)
        second = silo.list_by_facets({"crew": _FINANCE}, limit=2, offset=2)
        assert [row["id"] for row in first + second] == expected[:4]

    def test_an_offset_past_the_end_is_empty(self, silo) -> None:
        assert silo.list_by_facets({}, limit=10, offset=500) == []

    def test_the_page_size_is_clamped_by_the_builder(self) -> None:
        """One bound, owned by the builder, so no surface can widen it.

        ``memory_items`` is unbounded and continuously written, so an uncapped page
        is the unbounded-serialization exposure ``get_all_semantic``'s own cap
        exists for.
        """
        _sql, params = memory_schema.facet_page_query({}, "", limit=10_000, offset=-5)
        assert params[-2:] == (memory_schema.MAX_FACET_PAGE, 0)
        _sql, params = memory_schema.facet_page_query({}, "", limit=0)
        assert params[-2] == 1

    def test_the_group_count_is_clamped_by_the_builder(self) -> None:
        """``session_key`` cardinality is unbounded — one value per conversation."""
        _sql, params = memory_schema.facet_count_query("session_key", {}, limit=10_000)
        assert params[-1] == memory_schema.MAX_FACET_GROUPS


# ── 7. Every filter rides the index the docstring claims ──────────────────────


class TestTheFilterRidesTheIndexItClaims:
    """The plan, measured, so the comment cannot rot into a wrong promise.

    Three of the five facets have an index and two do not. Pinning it here is what
    keeps the docstring honest: a reader deciding whether a carve is cheap needs
    the real answer, and a claim nothing checks becomes false the first time an
    index is renamed.
    """

    @pytest.fixture
    def crew_db(self) -> Any:
        db = sqlite3.connect(":memory:")
        try:
            db.executescript(memory_schema.CREW_SCHEMA_SQL)
            yield db
        finally:
            db.close()

    @staticmethod
    def _plan(db: sqlite3.Connection, sql: str, params: tuple) -> str:
        return " | ".join(row[-1] for row in db.execute("EXPLAIN QUERY PLAN " + sql, params))

    @pytest.mark.parametrize(
        ("facet", "index"),
        [("scope", "idx_mi_scope"), ("crew", "idx_mi_crew"), ("surface", "idx_mi_surface")],
    )
    def test_the_three_indexed_axes_seek(self, crew_db, facet: str, index: str) -> None:
        sql, params = memory_schema.facet_page_query({facet: "x"})
        assert index in self._plan(crew_db, sql, params)

    def test_a_kind_only_filter_rides_the_kind_index(self, crew_db) -> None:
        sql, params = memory_schema.facet_page_query({}, memory_schema.KIND_FACT)
        assert "idx_mi_kind_live" in self._plan(crew_db, sql, params)

    @pytest.mark.parametrize("facet", ["session_key", "derived_from"])
    def test_the_two_unindexed_axes_scan_and_the_docstring_says_so(
        self, crew_db, facet: str
    ) -> None:
        sql, params = memory_schema.facet_page_query({facet: "x"})
        plan = self._plan(crew_db, sql, params)
        # SQLite may scan the creation-time index to satisfy ORDER BY. That is
        # still a full scan, not a selective lookup on this unindexed facet.
        assert "SCAN memory_items" in plan
        assert "SEARCH memory_items" not in plan
        assert facet in (memory_schema.facet_page_query.__doc__ or "")

    def test_pairing_an_unindexed_axis_with_an_indexed_one_recovers_the_seek(self, crew_db) -> None:
        sql, params = memory_schema.facet_page_query({"crew": "x", "session_key": "y"})
        assert "idx_mi_crew" in self._plan(crew_db, sql, params)


# ── 8. The HTTP route ─────────────────────────────────────────────────────────


def _request(
    state: Any, query: dict[str, str], session_key: str = "dashboard:ui", *, owner: bool = False
) -> Any:
    """A minimal aiohttp request stand-in, the style ``test_memory_graph.py`` uses."""
    from unittest.mock import MagicMock

    req = MagicMock()
    req.app = {"state": state}
    req.method = "GET"
    req.query = query
    req.match_info = {}
    req.headers = {"X-Session-Key": session_key}
    identity = {"user": "local-app", "app": ""} if owner else {}
    req.get.side_effect = identity.get
    req.__contains__.side_effect = identity.__contains__
    req.__getitem__.side_effect = identity.__getitem__
    return req


def _payload(resp: web.Response) -> Any:
    return json.loads(resp.text or "")


class TestTheCarveRoute:
    """``GET /api/memory/carve`` reads an explicitly selected store behind the owner gate."""

    @pytest.fixture
    def handler(self):
        import kiro_crew.dashboard.handlers.memory as mem_mod

        return mem_mod

    @staticmethod
    def _state(store: Any) -> Any:
        from unittest.mock import MagicMock

        state = MagicMock()
        state.context_builder = MagicMock(memory=MagicMock(vector_store=store))
        state._slots = {}
        state._restricted_keys = set()
        state.owner_id = ""
        return state

    async def _call(
        self, handler_mod, store, query, *, silo: str, owner: bool = True
    ) -> web.Response:
        """Exercise the real owner gate and resolver with a selected silo."""
        from kiro_crew.context import ContextBuilder

        async def _ensure(name: str):
            assert name == silo
            return store

        with mock.patch.object(ContextBuilder, "ensure_store", staticmethod(_ensure)):
            return await handler_mod.api_memory_carve(
                _request(self._state(store), {"store": silo, **query}, owner=owner)
            )

    @pytest.mark.asyncio
    async def test_it_lists_the_owner_selected_silo(self, handler, silo) -> None:
        resp = await self._call(handler, silo, {"crew": _FINANCE}, silo=_FINANCE)
        assert resp.status == 200
        body = _payload(resp)
        assert body["store"] == _FINANCE
        assert {entry["key"] for entry in body["entries"] if entry["key"]} >= {"pref.editor"}
        assert all(entry["crew"] == _FINANCE for entry in body["entries"])

    @pytest.mark.asyncio
    async def test_count_by_switches_the_response_shape(self, handler, silo) -> None:
        resp = await self._call(handler, silo, {"count_by": "crew"}, silo=_FINANCE)
        assert resp.status == 200
        assert _payload(resp)["counts"] == {_FINANCE: 5, _OPS: 2, "": 1}
        assert "entries" not in _payload(resp)

    @pytest.mark.asyncio
    async def test_an_empty_query_value_filters_for_the_unattributed(self, handler, silo) -> None:
        """``?crew=`` must reach the store as a filter, not be normalized away."""
        resp = await self._call(handler, silo, {"crew": ""}, silo=_FINANCE)
        assert [entry["key"] for entry in _payload(resp)["entries"]] == ["user.name"]

    @pytest.mark.asyncio
    async def test_a_v1_store_answers_409_with_a_machine_readable_code(
        self, handler, stores, tmp_path: Path
    ) -> None:
        """The same refusal the CLI prints, as a status plus a ``code``.

        A backend string has no i18n catalog path, so every non-2xx body here
        carries a code a client can branch on.
        """
        _declare_silos(_FINANCE)
        v1 = stores(tmp_path / "v1-route" / MEMORY_DB_FILE)
        assert v1._lineage == memory_schema.LINEAGE_V1
        resp = await self._call(handler, v1, {"crew": _FINANCE}, silo=_FINANCE)
        assert resp.status == 409
        assert _payload(resp)["code"] == "facets_unsupported"

    @pytest.mark.asyncio
    async def test_a_session_bound_to_no_silo_reaches_the_global_store_and_is_refused(
        self, handler, stores, tmp_path: Path
    ) -> None:
        """The fall-through, and it lands on the SAME refusal.

        A session naming no silo must not be handed a crew's rows, and the global
        store is on v1 — so "you are not in a crew store" and "this store has no
        facets" are one answer with one code.
        """
        _declare_silos(_FINANCE)
        global_store = stores(tmp_path / "global" / MEMORY_DB_FILE)
        with mock.patch.object(_shared, "_session_memory_store", lambda *_a: ""):
            resp = await handler.api_memory_carve(
                _request(self._state(global_store), {"crew": _FINANCE})
            )
        assert resp.status == 409
        assert _payload(resp)["code"] == "facets_unsupported"

    @pytest.mark.asyncio
    async def test_an_unrecognized_query_key_never_becomes_a_filter(self, handler, silo) -> None:
        """A query key is not a facet name, and the route never treats one as one.

        The filter mapping is keyed from :data:`memory_schema.FACET_NAMES` and the
        query is consulted for membership, so no caller string can reach the
        builder as a name — which is why this route cannot produce the
        hostile-name refusal the store tests drive directly.

        Refusing unknown query keys instead is not available here: a request
        legitimately carries keys that are not filters, ``?token=`` among them, and
        a route that 400'd on those would break query-token auth. The cost is that
        a typo'd ``?crews=`` is an unfiltered carve rather than an error, which is
        why the enumerable inputs — ``count_by`` and ``kind`` — are validated.
        """
        unfiltered = await self._call(handler, silo, {"count_by": "crew"}, silo=_FINANCE)
        resp = await self._call(
            handler,
            silo,
            {"crew; DROP TABLE memory_items": "x", "crews": _OPS, "count_by": "crew"},
            silo=_FINANCE,
        )
        assert resp.status == 200
        assert _payload(resp) == _payload(unfiltered)
        assert silo.db.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 8

    @pytest.mark.asyncio
    @pytest.mark.parametrize("axis", ["embedding", "crew; DROP TABLE memory_items", "1=1"])
    async def test_a_hostile_group_axis_answers_400_and_leaves_the_table(
        self, handler, silo, axis: str
    ) -> None:
        """``count_by`` IS caller text reaching a name position, so it is validated."""
        resp = await self._call(handler, silo, {"count_by": axis}, silo=_FINANCE)
        assert resp.status == 400
        assert _payload(resp)["code"] == "unknown_facet"
        assert silo.db.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 8

    @pytest.mark.asyncio
    async def test_an_unknown_kind_answers_400(self, handler, silo) -> None:
        resp = await self._call(handler, silo, {"kind": "preference"}, silo=_FINANCE)
        assert resp.status == 400
        assert _payload(resp)["code"] == "unknown_facet"

    @pytest.mark.asyncio
    async def test_non_integer_pagination_answers_400_with_a_code(self, handler, silo) -> None:
        resp = await self._call(handler, silo, {"limit": "many"}, silo=_FINANCE)
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_pagination"

    @pytest.mark.asyncio
    async def test_a_silo_whose_vectors_cannot_be_stood_up_answers_503(self, handler) -> None:
        """Reported, never answered from the global store.

        Falling back there would serve another population under this crew's name.
        """
        _declare_silos(_FINANCE)
        resp = await self._call(handler, None, {}, silo=_FINANCE)
        assert resp.status == 503
        assert _payload(resp)["code"] == "store_unavailable"

    @pytest.mark.asyncio
    async def test_a_store_query_parameter_is_refused_not_quietly_ignored(
        self, handler, silo
    ) -> None:
        """``?store=`` never redirects the read for a caller that is not the operator.

        The route now HAS the parameter, and this is the property that replaced
        "the parameter does not exist": a caller holding no dashboard identity is
        REFUSED rather than silently served its own silo. The refusal is the
        stronger of the two, because quietly ignoring the name tells a caller that
        asked for another crew's rows that it received them.

        ``_request`` builds a mapping with no ``user`` key, which is exactly the
        shape ``token_auth_middleware``'s ``X-Internal-Secret`` branch leaves
        behind -- so this drives the agent case rather than a synthetic one.
        """
        resp = await self._call(
            handler, silo, {"store": _OPS, "count_by": "crew"}, silo=_FINANCE, owner=False
        )
        assert resp.status == 403
        assert _payload(resp)["code"] == "owner_only"
        # And nothing from the requested store leaked into the refusal body.
        assert _OPS not in resp.body.decode()

    def test_the_route_is_registered_as_a_get(self) -> None:
        from aiohttp import web as aiohttp_web

        from kiro_crew.dashboard.routes import memory as memory_routes

        app = aiohttp_web.Application()
        app["state"] = None
        memory_routes.register(app)
        registered = {
            (resource.canonical, route.method)
            for resource in app.router.resources()
            for route in resource
        }
        # Read-only: GET and the HEAD aiohttp's ``add_get`` pairs with it, and
        # nothing that mutates. A facet is an index projection over rows another
        # writer stamped; there is nothing here for a caller to change.
        assert {method for path, method in registered if path == "/api/memory/carve"} == {
            "GET",
            "HEAD",
        }


# ── 9. The CLI verb ───────────────────────────────────────────────────────────


def _carve_args(**overrides: Any) -> Any:
    """The namespace ``kirocrew memory carve`` hands its handler.

    Every facet defaults to ``None`` — an ABSENT flag — because that is what
    argparse produces, and the difference between ``None`` and ``""`` is the whole
    distinction between "unconstrained" and "unattributed".
    """
    from types import SimpleNamespace

    base: dict[str, Any] = {facet: None for facet in memory_schema.FACET_NAMES}
    base.update(store=None, kind=None, count_by=None, limit=50, offset=0)
    base.update(overrides)
    return SimpleNamespace(**base)


class TestTheCarveCliVerb:
    """The operator surface, driven through the handler the dispatcher calls.

    Each test lets ``_memory_carve`` open the store itself: one
    ``VectorMemoryStore`` per file is an invariant (two instances do not share
    ``_db_lock``), so the seed is written and CLOSED before the verb runs.
    """

    @pytest.fixture
    def seeded(self, silo) -> str:
        """A populated ``finance`` silo with no live handle on it."""
        silo.close()
        return _FINANCE

    @staticmethod
    def _run(**overrides: Any) -> None:
        from kiro_crew.cli_commands import _memory_carve

        _memory_carve(_carve_args(**overrides))

    def test_it_counts_a_named_silo(self, seeded, capsys) -> None:
        self._run(store=seeded, count_by="crew")
        printed = capsys.readouterr().out
        assert f"{_FINANCE}: 5" in printed
        assert f"{_OPS}: 2" in printed
        # The unattributed bucket is LABELLED rather than printed as a blank line,
        # which is the difference between an answer and a rendering artifact.
        assert "(unattributed): 1" in printed

    def test_it_lists_a_carve_with_its_axes(self, seeded, capsys) -> None:
        self._run(store=seeded, crew=_FINANCE, kind=memory_schema.KIND_FACT)
        printed = capsys.readouterr().out
        assert "pref.editor" in printed
        assert f"crew={_FINANCE}" in printed
        # ``pref.lang`` is the ops row; its presence would mean the flag was lost
        # between the namespace and the store.
        assert "pref.lang" not in printed

    def test_the_default_store_prints_the_refusal_not_an_empty_list(self, capsys) -> None:
        """The same contract the store raises and the route returns as 409.

        An operator who typed no ``--store`` must not be told their crew has no
        memories; they must be told this store cannot answer.
        """
        _declare_silos(_FINANCE)
        self._run(count_by="crew")
        printed = capsys.readouterr().out
        assert "Cannot carve" in printed and "v1 schema lineage" in printed
        assert "No live rows" not in printed

    def test_an_undeclared_store_is_refused_without_reading_global_memory(self, capsys) -> None:
        """A mistyped private store never authorizes a global memory read."""
        _declare_silos(_FINANCE)
        self._run(store="nope", count_by="crew")
        printed = capsys.readouterr().out
        assert "'nope' is not declared" in printed
        assert printed.startswith("Error:")
        assert f"Cannot carve {DEFAULT_MEMORY_STORE!r}" not in printed

    def test_a_malformed_store_name_is_one_line_and_not_a_traceback(self, capsys) -> None:
        """A shape defect RAISES rather than degrading, and the CLI reports it.

        Repairing ``../work`` onto ``work`` is the one case that would silently
        point two crews at one directory, so the resolver refuses — and an operator
        gets a sentence instead of a stack.
        """
        _declare_silos(_FINANCE)
        self._run(store="../work")
        printed = capsys.readouterr().out
        assert printed.startswith("Error: invalid memory store name")

    def test_an_unknown_kind_is_reported_rather_than_answered(self, seeded, capsys) -> None:
        """Reachable despite argparse's ``choices``: the store validates too.

        The choice set is UX; the refusal is the contract, and it must not depend
        on which surface the call came through.
        """
        self._run(store=seeded, kind="preference")
        assert "Error: unknown memory kind" in capsys.readouterr().out

    def test_an_empty_carve_says_so_without_claiming_the_store_cannot_answer(
        self, seeded, capsys
    ) -> None:
        """The third outcome, distinct from both refusals.

        "No row matches" and "this store has no facets" are different facts, and
        the two messages must not be confusable.
        """
        self._run(store=seeded, crew="nobody")
        printed = capsys.readouterr().out
        assert "No live rows" in printed
        assert "Cannot carve" not in printed

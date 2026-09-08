"""Read-only knowledge stats: the store aggregate, the CLI verb, the MCP twin.

Every case runs against ONE seeded library whose numbers are fixed by
:func:`_seed_library` -- the fixture contract the CLI, the MCP tool and the pod
check are all asserted against:

===================  =========  =====
source               documents  items
===================  =========  =====
alpha                        2      4
beta                         1      2
gamma (registered)           0      0
(no source)                  1      2
-------------------  ---------  -----
TOTAL                        4      8
===================  =========  =====

``alpha`` also holds one non-active row, which no total counts. The sourceless
bucket holds one hashless item, which counts as an item and as no document.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.knowledge import store as store_module
from kiro_crew.knowledge.store import KnowledgeStore

# The fixture contract, named once so a drifting seed fails as a contract
# mismatch rather than as an arithmetic surprise in each assertion.
EXPECTED_SOURCES = 3
EXPECTED_DOCUMENTS = 4
EXPECTED_ITEMS = 8


def _library_fingerprint(db_path) -> tuple:
    """Every row identity the stats surfaces read, via a read-only connection.

    Opened ``mode=ro`` so taking the fingerprint cannot itself be the write a
    read-only assertion is looking for. Byte-comparing the file would instead
    measure SQLite bookkeeping -- opening a store runs ``CREATE TABLE IF NOT
    EXISTS`` and the migrations -- which is not the claim being made.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        items = conn.execute(
            "SELECT id, status, source_id, content_hash FROM items ORDER BY id"
        ).fetchall()
        sources = conn.execute("SELECT id, name FROM sources ORDER BY id").fetchall()
    finally:
        conn.close()
    return (tuple(items), tuple(sources))


def _add_document(store: KnowledgeStore, source_id: str | None, title: str, chunks: int) -> str:
    """Write one document as *chunks* item rows sharing its whole-text hash."""
    content_hash = hashlib.sha256(title.encode()).hexdigest()
    for index in range(chunks):
        store.add_item(
            title=title,
            content=f"{title} part {index}",
            item_type="document",
            source_id=source_id,
            chunk_index=index,
            content_hash=content_hash,
        )
    return content_hash


def _seed_library(db_path) -> KnowledgeStore:
    store = KnowledgeStore(str(db_path))
    # local_folder, not an invented type: the store's constructor reaps an
    # itemless source of any other type, so `gamma` would not survive the next
    # open and the zero-item row this contract pins would vanish.
    alpha = store.add_source("alpha", "local_folder", "/tmp/alpha")
    beta = store.add_source("beta", "local_folder", "/tmp/beta")
    store.add_source("gamma", "local_folder", "/tmp/gamma")
    _add_document(store, alpha, "alpha-one", chunks=3)
    _add_document(store, alpha, "alpha-two", chunks=1)
    _add_document(store, beta, "beta-one", chunks=2)
    _add_document(store, None, "orphan-one", chunks=1)
    store.add_item(
        title="hashless",
        content="no document identity",
        item_type="document",
        source_id=None,
        chunk_index=0,
    )
    superseded = store.add_item(
        title="alpha-old",
        content="replaced",
        item_type="document",
        source_id=alpha,
        chunk_index=0,
        content_hash=hashlib.sha256(b"alpha-old").hexdigest(),
    )
    store.db.execute("UPDATE items SET status = 'superseded' WHERE id = ?", (superseded,))
    store.db.commit()
    return store


@pytest.fixture
def seeded_home(tmp_path):
    """A config dir holding the seeded library at its canonical path."""
    db_path = tmp_path / "workspace" / "knowledge" / "knowledge.db"
    db_path.parent.mkdir(parents=True)
    store = _seed_library(db_path)
    store.db.close()
    return tmp_path


@pytest.fixture
def seeded_store(tmp_path):
    store = _seed_library(tmp_path / "knowledge.db")
    yield store
    store.db.close()


class TestStoreAggregate:
    def test_totals_match_the_fixture_contract(self, seeded_store):
        stats = seeded_store.aggregate_stats()
        assert (stats.sources, stats.documents, stats.items) == (
            EXPECTED_SOURCES,
            EXPECTED_DOCUMENTS,
            EXPECTED_ITEMS,
        )

    def test_per_source_reconciles_with_the_totals(self, seeded_store):
        stats = seeded_store.aggregate_stats()
        assert sum(s.items for s in stats.per_source) == stats.items
        assert sum(s.documents for s in stats.per_source) == stats.documents

    def test_per_source_rows_carry_the_seeded_split(self, seeded_store):
        stats = seeded_store.aggregate_stats()
        by_name = {s.name: (s.documents, s.items) for s in stats.per_source}
        assert by_name == {
            "alpha": (2, 4),
            "beta": (1, 2),
            "gamma": (0, 0),
            "(no source)": (1, 2),
        }

    def test_registered_source_with_no_items_is_listed_at_zero(self, seeded_store):
        stats = seeded_store.aggregate_stats()
        gamma = next(s for s in stats.per_source if s.name == "gamma")
        assert (gamma.documents, gamma.items) == (0, 0)
        assert gamma.source_id is not None

    def test_sourceless_bucket_reports_no_source_id(self, seeded_store):
        stats = seeded_store.aggregate_stats()
        orphan = next(s for s in stats.per_source if s.source_id is None)
        assert orphan.items == 2, "one hashed document chunk plus one hashless item"
        assert orphan.documents == 1, "the hashless item belongs to no document"

    def test_sourceless_bucket_is_absent_when_every_item_has_a_source(self, tmp_path):
        store = KnowledgeStore(str(tmp_path / "owned.db"))
        try:
            source_id = store.add_source("only", "folder", "/tmp/only")
            _add_document(store, source_id, "doc", chunks=2)
            stats = store.aggregate_stats()
        finally:
            store.db.close()
        assert [s.source_id for s in stats.per_source] == [source_id]

    def test_non_active_items_are_excluded(self, seeded_store):
        raw = seeded_store.db.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
        assert raw == EXPECTED_ITEMS + 1
        assert seeded_store.aggregate_stats().items == EXPECTED_ITEMS

    def test_empty_library_reports_zeroes(self, tmp_path):
        store = KnowledgeStore(str(tmp_path / "empty.db"))
        try:
            stats = store.aggregate_stats()
        finally:
            store.db.close()
        assert (stats.sources, stats.documents, stats.items, stats.per_source) == (0, 0, 0, ())

    def test_reading_the_aggregate_leaves_every_row_untouched(self, tmp_path):
        db_path = tmp_path / "knowledge.db"
        store = _seed_library(db_path)
        store.db.close()
        before = _library_fingerprint(db_path)
        reader = KnowledgeStore(str(db_path))
        try:
            reader.aggregate_stats()
        finally:
            reader.db.close()
        assert _library_fingerprint(db_path) == before

    def test_read_only_open_serves_the_aggregate_and_refuses_every_write(self, tmp_path):
        db_path = tmp_path / "knowledge.db"
        _seed_library(db_path).db.close()
        reader = KnowledgeStore.open_read_only(str(db_path))
        try:
            assert reader.aggregate_stats().items == EXPECTED_ITEMS
            # Refused by SQLite (`mode=ro`), not by a convention a caller can skip.
            with pytest.raises(store_module.sqlite3.OperationalError, match="readonly"):
                reader.add_source("late", "web", "https://example.invalid/late")
        finally:
            reader.db.close()

    def test_item_owned_by_a_vanished_source_lands_in_the_sourceless_bucket(self, seeded_store):
        # Unreachable through this store: items.source_id REFERENCES sources(id)
        # under foreign_keys=ON. Planted with the check off, the way a database
        # written before the constraint was enforced can still hold one.
        raw = sqlite3.connect(seeded_store._db_path)
        try:
            raw.execute("PRAGMA foreign_keys=OFF")
            raw.execute(
                "INSERT INTO items (id, title, content, item_type, source_id, status, "
                "content_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "ghost-1",
                    "ghost",
                    "owner vanished",
                    "document",
                    "no-such-source",
                    "active",
                    "ghosthash",
                    "2026-01-01",
                    "2026-01-01",
                ),
            )
            raw.commit()
        finally:
            raw.close()
        stats = seeded_store.aggregate_stats()
        assert (stats.documents, stats.items) == (EXPECTED_DOCUMENTS + 1, EXPECTED_ITEMS + 1)
        assert sum(s.items for s in stats.per_source) == stats.items
        assert sum(s.documents for s in stats.per_source) == stats.documents
        bucket = next(s for s in stats.per_source if s.source_id is None)
        assert (bucket.documents, bucket.items) == (2, 3)


class TestCliStatsVerb:
    def _run(self, home, as_json: bool = False):
        from kiro_crew import cli

        args = SimpleNamespace(knowledge_action="stats", json=as_json)
        with (
            patch("kiro_crew.cli.config_dir", return_value=home),
            patch("kiro_crew.cli.sel", return_value=MagicMock()),
        ):
            cli._knowledge(args)

    def test_human_output_carries_totals_and_a_row_per_source(self, seeded_home, capsys):
        self._run(seeded_home)
        out = capsys.readouterr().out
        assert f"{EXPECTED_SOURCES} source(s)" in out
        assert f"{EXPECTED_DOCUMENTS} document(s)" in out
        assert f"{EXPECTED_ITEMS} item(s)" in out
        for name in ("alpha", "beta", "gamma", "(no source)"):
            assert name in out

    def test_json_output_matches_the_fixture_contract(self, seeded_home, capsys):
        self._run(seeded_home, as_json=True)
        payload = json.loads(capsys.readouterr().out)
        assert payload["sources"] == EXPECTED_SOURCES
        assert payload["documents"] == EXPECTED_DOCUMENTS
        assert payload["items"] == EXPECTED_ITEMS
        assert sum(row["items"] for row in payload["per_source"]) == EXPECTED_ITEMS
        orphan = next(row for row in payload["per_source"] if row["id"] is None)
        assert orphan["items"] == 2

    def test_unconfigured_library_is_reported_not_crashed(self, tmp_path, capsys):
        self._run(tmp_path)
        assert "not configured" in capsys.readouterr().out

    def test_unconfigured_library_stays_machine_readable_under_json(self, tmp_path, capsys):
        self._run(tmp_path, as_json=True)
        assert json.loads(capsys.readouterr().out) == {"error": "not_configured"}

    def test_stats_leaves_every_library_row_untouched(self, seeded_home):
        db_path = seeded_home / "workspace" / "knowledge" / "knowledge.db"
        before = _library_fingerprint(db_path)
        self._run(seeded_home)
        assert _library_fingerprint(db_path) == before

    def test_usage_names_both_verbs_and_nothing_else(self, seeded_home, capsys):
        from kiro_crew import cli

        with (
            patch("kiro_crew.cli.config_dir", return_value=seeded_home),
            patch("kiro_crew.cli.sel", return_value=MagicMock()),
        ):
            cli._knowledge(SimpleNamespace(knowledge_action=None))
        usage = capsys.readouterr().out
        assert "knowledge dedup" in usage and "knowledge stats" in usage

    @pytest.mark.parametrize("refused", ["flush", "rebuild", "repair", "reindex"])
    def test_a_repair_action_reaches_no_library(self, seeded_home, capsys, refused):
        from kiro_crew import cli

        db_path = seeded_home / "workspace" / "knowledge" / "knowledge.db"
        before = _library_fingerprint(db_path)
        with (
            patch("kiro_crew.cli.config_dir", return_value=seeded_home),
            patch("kiro_crew.cli.sel", return_value=MagicMock()),
        ):
            cli._knowledge(SimpleNamespace(knowledge_action=refused))
        assert "Usage:" in capsys.readouterr().out
        assert _library_fingerprint(db_path) == before

    def test_stats_keeps_the_source_row_a_migrating_open_reaps(self, seeded_home):
        # An itemless source outside the sweep exclusions that nothing references:
        # the row `_migrate()` deletes on every ordinary open, and the write the
        # word read-only has to exclude.
        db_path = seeded_home / "workspace" / "knowledge" / "knowledge.db"
        writer = KnowledgeStore(str(db_path))
        writer.add_source("ghost", "web", "https://example.invalid/ghost")
        writer.db.close()
        before = _library_fingerprint(db_path)
        assert "ghost" in {name for _sid, name in before[1]}
        self._run(seeded_home)
        assert _library_fingerprint(db_path) == before
        # The contrast that gives the assertion above its meaning: an ordinary
        # open does reap the row.
        KnowledgeStore(str(db_path)).db.close()
        assert "ghost" not in {name for _sid, name in _library_fingerprint(db_path)[1]}

    def test_library_behind_the_schema_is_reported_not_migrated(self, tmp_path, capsys):
        db_path = tmp_path / "workspace" / "knowledge" / "knowledge.db"
        db_path.parent.mkdir(parents=True)
        stale = sqlite3.connect(db_path)
        try:
            stale.execute("CREATE TABLE sources (id TEXT PRIMARY KEY, name TEXT NOT NULL)")
            stale.execute("CREATE TABLE items (id TEXT PRIMARY KEY, source_id TEXT, status TEXT)")
            stale.commit()
        finally:
            stale.close()
        self._run(tmp_path)
        out = capsys.readouterr().out
        assert "behind this schema" in out and "does not migrate" in out
        check = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cols = {row[1] for row in check.execute("PRAGMA table_info(items)").fetchall()}
        finally:
            check.close()
        assert cols == {"id", "source_id", "status"}, "the verb added no column"


class TestCliDedupDryRun:
    """``kirocrew knowledge dedup`` without ``--apply`` previews and writes nothing."""

    def _run(self, home):
        from kiro_crew import cli

        args = SimpleNamespace(knowledge_action="dedup", apply=False)
        with (
            patch("kiro_crew.cli.config_dir", return_value=home),
            patch("kiro_crew.cli.sel", return_value=MagicMock()),
        ):
            cli._knowledge(args)

    def test_dry_run_keeps_the_source_row_a_migrating_open_reaps(self, seeded_home, capsys):
        # The itemless source row `_migrate()` deletes on every ordinary open is
        # exactly the write a preview printing "no changes" must not make.
        db_path = seeded_home / "workspace" / "knowledge" / "knowledge.db"
        writer = KnowledgeStore(str(db_path))
        writer.add_source("ghost", "web", "https://example.invalid/ghost")
        writer.db.close()
        before = _library_fingerprint(db_path)
        assert "ghost" in {name for _sid, name in before[1]}
        self._run(seeded_home)
        assert "DRY RUN" in capsys.readouterr().out
        assert _library_fingerprint(db_path) == before
        # The contrast that gives the assertion above its meaning: an ordinary
        # open -- the one --apply keeps -- does reap the row.
        KnowledgeStore(str(db_path)).db.close()
        assert "ghost" not in {name for _sid, name in _library_fingerprint(db_path)[1]}


class TestMcpParity:
    """The MCP twin answers the same question from the same aggregate."""

    def _call(self, home):
        from kiro_crew.mcp_core import _call_tool_inner

        with (
            patch("kiro_crew.mcp_core.config_dir", return_value=home),
            patch("kiro_crew.mcp_core.sel", return_value=MagicMock()),
        ):
            return _call_tool_inner("knowledge_list_sources", {})

    def test_tool_reports_the_fixture_contract_totals(self, seeded_home):
        result = self._call(seeded_home)
        assert (
            f"Knowledge library: {EXPECTED_SOURCES} source(s), "
            f"{EXPECTED_DOCUMENTS} document(s), {EXPECTED_ITEMS} item(s)."
        ) in result

    def test_tool_totals_equal_the_cli_json(self, seeded_home, capsys):
        from kiro_crew import cli

        with (
            patch("kiro_crew.cli.config_dir", return_value=seeded_home),
            patch("kiro_crew.cli.sel", return_value=MagicMock()),
        ):
            cli._knowledge(SimpleNamespace(knowledge_action="stats", json=True))
        payload = json.loads(capsys.readouterr().out)
        result = self._call(seeded_home)
        assert (
            f"{payload['sources']} source(s), {payload['documents']} document(s), "
            f"{payload['items']} item(s)."
        ) in result

    def test_tool_still_lists_every_source_id_for_scoping(self, seeded_home):
        result = self._call(seeded_home)
        assert "Sources (3):" in result
        for name in ("alpha", "beta", "gamma"):
            assert f"- {name} — id: " in result

    def test_descriptor_advertises_the_counts_and_the_read_only_boundary(self):
        from kiro_crew.mcp_tools.knowledge import schemas

        descriptor = next(d for d in schemas() if d["name"] == "knowledge_list_sources")
        description = descriptor["description"]
        assert "documents" in description and "items" in description
        assert "never rebuilds" in description

    def test_tool_leaves_every_library_row_untouched(self, seeded_home):
        db_path = seeded_home / "workspace" / "knowledge" / "knowledge.db"
        before = _library_fingerprint(db_path)
        self._call(seeded_home)
        assert _library_fingerprint(db_path) == before

    def test_unconfigured_library_is_reported_not_crashed(self, tmp_path):
        assert "not configured" in self._call(tmp_path)

    def test_tool_names_the_items_no_line_carries(self, seeded_home):
        # The two sourceless items: in the total, on no per-source line.
        result = self._call(seeded_home)
        assert "2 item(s) are owned by no registered source" in result
        assert "more membership(s)" not in result

    def test_tool_counts_the_memberships_a_collapse_adds(self, seeded_home):
        db_path = seeded_home / "workspace" / "knowledge" / "knowledge.db"
        store = KnowledgeStore(str(db_path))
        try:
            alpha, beta = (
                store.db.execute("SELECT id FROM sources WHERE name = ?", (name,)).fetchone()["id"]
                for name in ("alpha", "beta")
            )
            item = store.db.execute(
                "SELECT id FROM items WHERE source_id = ? AND status = ? LIMIT 1", (alpha, "active")
            ).fetchone()["id"]
            store.db.execute(
                "INSERT INTO source_locations (id, item_id, source_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("loc-1", item, beta, "2026-01-01"),
            )
        finally:
            store.db.close()
        result = self._call(seeded_home)
        assert "count 1 more membership(s)" in result
        assert "2 item(s) are owned by no registered source" in result

    def test_tool_keeps_the_source_row_a_migrating_open_reaps(self, seeded_home):
        # The itemless source row `_migrate()` deletes on every ordinary open:
        # a tool advertised as "only counts" must not be the open that reaps it.
        db_path = seeded_home / "workspace" / "knowledge" / "knowledge.db"
        writer = KnowledgeStore(str(db_path))
        writer.add_source("ghost", "web", "https://example.invalid/ghost")
        writer.db.close()
        before = _library_fingerprint(db_path)
        assert "ghost" in {name for _sid, name in before[1]}
        result = self._call(seeded_home)
        assert f"Knowledge library: {EXPECTED_SOURCES + 1} source(s)" in result
        assert "- ghost — id: " in result
        assert _library_fingerprint(db_path) == before
        # The contrast that gives the assertion above its meaning: an ordinary
        # open does reap the row.
        KnowledgeStore(str(db_path)).db.close()
        assert "ghost" not in {name for _sid, name in _library_fingerprint(db_path)[1]}

    def test_library_behind_the_schema_is_reported_not_migrated(self, tmp_path):
        db_path = tmp_path / "workspace" / "knowledge" / "knowledge.db"
        db_path.parent.mkdir(parents=True)
        stale = sqlite3.connect(db_path)
        try:
            stale.execute("CREATE TABLE sources (id TEXT PRIMARY KEY, name TEXT NOT NULL)")
            stale.execute("CREATE TABLE items (id TEXT PRIMARY KEY, source_id TEXT, status TEXT)")
            stale.commit()
        finally:
            stale.close()
        result = self._call(tmp_path)
        assert "behind this schema" in result and "does not migrate" in result
        check = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cols = {row[1] for row in check.execute("PRAGMA table_info(items)").fetchall()}
        finally:
            check.close()
        assert cols == {"id", "source_id", "status"}, "the tool added no column"

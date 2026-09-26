"""Knowledge bundle round-trip: per-document ownership survives export/import.

Ownership lives in three per-document state tables (``folder_file_state``,
``artifact_item_state``, ``agent_item_state``). An item without its row is
unmanaged: nothing claims it for de-duplication, the Sources UI has no group label
for it, and a later ingest of the same document adds a second copy instead of
replacing the first.

A source's identity across two stores is its ``uri``, not its id -- ``sources.uri``
carries the UNIQUE constraint and each store mints its own ids -- so every row
pointing at a source is rewritten through a uri-keyed map on import.
"""

import ast
import inspect
import json
import time
from pathlib import Path

import pytest

from kiro_crew.dashboard.handlers.knowledge import _validate_knowledge_bundle
from kiro_crew.knowledge import store as store_module
from kiro_crew.knowledge.agent_source import remove_document
from kiro_crew.knowledge.store import (
    BUNDLE_STATE_KEY_COL,
    KnowledgeBundleError,
    KnowledgeStore,
)

#: The aggregate source these pins use. It is the AGENT aggregate because that is the
#: one table a bundle restores: nothing reaps agent state by absence, so a restored row
#: is a claim rather than an order to delete the items the import just brought.
AGGREGATE_URI = "agent://"


@pytest.fixture()
def exporter(tmp_path):
    s = KnowledgeStore(str(tmp_path / "exporter.db"))
    yield s
    s.close()


@pytest.fixture()
def importer(tmp_path):
    s = KnowledgeStore(str(tmp_path / "importer.db"))
    yield s
    s.close()


def _owned_doc(store, *, slug="doc", body="artifact body", name="Doc", uri=AGGREGATE_URI):
    """One owned aggregate document: a source, an item, and the ownership row.

    Written to `agent_item_state`, the one table a bundle restores, so the rules below
    are exercised where they actually apply. Every rule these pins cover -- the source
    remap, key collisions, claimed ids, withholding -- is table-agnostic; the choice of
    table decides only whether a restored row is a claim or a deletion order.
    """
    sid = store.get_source_by_uri(uri)
    sid = sid["id"] if sid else store.add_source(name="Auto-added", source_type="agent", uri=uri)
    item_id = store.add_item(title=name, content=body, item_type="document", source_id=sid)
    store.db.execute(
        "INSERT OR REPLACE INTO agent_item_state "
        "(source_id, slug, content_hash, item_ids, updated_at, name, status, source_uri) "
        "VALUES (?, ?, ?, ?, ?, ?, 'active', 'https://x/y')",
        (sid, slug, "hash-" + slug, json.dumps([item_id]), "2026-01-01T00:00:00", name),
    )
    store.db.commit()
    return sid, item_id


def _artifact_doc(store, *, slug="doc", body="artifact body", name="Doc", uri="artifact://"):
    """One owned ARTIFACT document, for the pins about that table specifically."""
    """One owned artifact document: a source, an item, and the ownership row."""
    sid = store.get_source_by_uri(uri)
    sid = sid["id"] if sid else store.add_source(name="Artifacts", source_type="artifact", uri=uri)
    item_id = store.add_item(title=name, content=body, item_type="document", source_id=sid)
    store.db.execute(
        "INSERT OR REPLACE INTO artifact_item_state "
        "(source_id, slug, content_hash, item_ids, updated_at, name, status, kind) "
        "VALUES (?, ?, ?, ?, ?, ?, 'active', 'markdown')",
        (sid, slug, "hash-" + slug, json.dumps([item_id]), "2026-01-01T00:00:00", name),
    )
    store.db.commit()
    return sid, item_id


def _folder_doc(store, *, uri="/remote/notes", file_path=None, body="folder body"):
    """One owned folder document, backed by a REAL file on this host.

    The folder scan detects deletions by walking every state row and dropping the ones
    whose path the walk does not find, without consulting `status` -- so a row
    restored for a file this host does not have is a deletion order for the items the
    import just brought. The file lives beside the store's own database, which pytest
    gives us per test. Pass *file_path* explicitly to model an absent one.
    """
    if file_path is None:
        root = Path(store._db_path).parent / "notes"
        root.mkdir(parents=True, exist_ok=True)
        real = root / "a.md"
        real.write_text(body)
        file_path = str(real)
    sid = store.add_source(name="Notes", source_type="local_folder", uri=uri)
    item_id = store.add_item(title="a.md", content=body, item_type="document", source_id=sid)
    store.db.execute(
        "INSERT INTO folder_file_state (source_id, file_path, content_hash, "
        "text_hash, mtime, item_ids, last_seen, status, attempts) "
        "VALUES (?, ?, 'raw-hash', 'text-hash', 123.5, ?, ?, 'done', 0)",
        (sid, file_path, json.dumps([item_id]), "2026-01-01T00:00:00"),
    )
    store.db.commit()
    return sid, item_id


def _agent_doc(
    store,
    *,
    slug="note",
    body="agent body",
    name="Note",
    source_uri="https://x/y",
    uri=AGGREGATE_URI,
):
    """One owned agent document: a source, an item, and the ownership row.

    This is the table the pins below use for every rule that is not about a specific
    table, because it is the only one a bundle restores -- nothing reaps agent state by
    absence, so a restored row is a claim and not a deletion order.
    """
    sid = store.get_source_by_uri(uri)
    sid = sid["id"] if sid else store.add_source(name="Auto-added", source_type="agent", uri=uri)
    item_id = store.add_item(title=name, content=body, item_type="document", source_id=sid)
    store.db.execute(
        "INSERT OR REPLACE INTO agent_item_state (source_id, slug, content_hash, "
        "item_ids, updated_at, name, status, source_uri) "
        "VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
        (sid, slug, "hash-" + slug, json.dumps([item_id]), "2026-01-01T00:00:00", name, source_uri),
    )
    store.db.commit()
    return sid, item_id


def _owner_of(store, table, item_id):
    """The (source_id, key) whose group holds *item_id*, or None."""
    key_col = BUNDLE_STATE_KEY_COL[table]
    for row in store.db.execute(
        f"SELECT source_id, {key_col} AS k, item_ids FROM {table}"
    ):  # noqa: S608
        if item_id in json.loads(row["item_ids"] or "[]"):
            return row["source_id"], row["k"]
    return None


def _item_count(store, body):
    return sum(1 for r in store.db.execute("SELECT content FROM items") if body in r["content"])


class TestBundleCarriesOwnership:
    """export_all ships the three state tables and import restores them."""

    def test_export_lists_every_state_table(self, exporter):
        _owned_doc(exporter)
        bundle = exporter.export_all()
        for table in BUNDLE_STATE_KEY_COL:
            assert table in bundle, f"{table} missing from the bundle"

    def test_artifact_ownership_survives_round_trip(self, exporter, importer):
        _, item_id = _owned_doc(exporter)
        result = importer.import_bundle(exporter.export_all())
        assert result["items_imported"] == 1
        assert result["ownership_rows_imported"] == 1
        owner = _owner_of(importer, "agent_item_state", item_id)
        assert owner is not None
        local_sid = importer.get_source_by_uri(AGGREGATE_URI)["id"]
        assert owner == (local_sid, "doc")

    def test_agent_ownership_survives_round_trip(self, exporter, importer):
        _, agent_item = _agent_doc(exporter)
        result = importer.import_bundle(exporter.export_all())
        assert result["ownership_rows_imported"] == 1
        assert _owner_of(importer, "agent_item_state", agent_item) is not None

    def test_a_folder_row_is_never_restored(self, exporter, importer):
        """A `file_path` does not mean the same thing on two hosts: the scan decides
        what it means by walking the MAPPED source's own live filters, then deletes
        every state row whose path the walk did not yield. Restoring one would be an
        order to delete the items the import just brought."""
        _, folder_item = _folder_doc(exporter)
        result = importer.import_bundle(exporter.export_all())
        assert result["items_imported"] == 1, "the content itself still arrives"
        assert result["ownership_rows_imported"] == 0
        assert _owner_of(importer, "folder_file_state", folder_item) is None

    def test_a_folder_row_present_on_this_host_is_refused_too(self, exporter, importer):
        """Eligibility is not a property of the file existing. The walk also applies
        the receiving source's extension allowlist, byte floor, ignore patterns and a
        root `.kiroignore` it re-reads every sweep -- and the answer can change after
        the import, so no check made here stays true."""
        _folder_doc(exporter)
        assert importer.import_bundle(exporter.export_all())["ownership_rows_imported"] == 0

    def test_the_bundle_still_carries_the_folder_table(self, exporter, importer):
        """The export stays a faithful record of the store. What changes is that the
        import does not act on this table."""
        _folder_doc(exporter)
        assert len(exporter.export_all()["folder_file_state"]) == 1

    def test_the_restored_row_keeps_its_hash_name_and_status(self, exporter, importer):
        _owned_doc(exporter)
        importer.import_bundle(exporter.export_all())
        row = importer.db.execute(
            "SELECT content_hash, name, status, merged_into_source_id " "FROM agent_item_state"
        ).fetchone()
        assert (row["content_hash"], row["name"]) == ("hash-doc", "Doc")
        assert row["status"] == "active"
        assert row["merged_into_source_id"] is None

    def test_an_artifact_row_is_not_restored(self, exporter, importer):
        """`reconcile_artifacts` runs on every `ArtifactKnowledgeSync.start`, takes
        `known.keys() - live`, and removes each provably absent slug's group -- so a row
        restored on a host whose artifact store lacks the slug deletes the imported
        documents on the next start. `_known_kinds` reads every row regardless of
        `status`, so no status value hides it from that pass."""
        _, item_id = _artifact_doc(exporter)
        result = importer.import_bundle(exporter.export_all())
        assert result["items_imported"] == 1, "the content itself still arrives"
        assert result["ownership_rows_imported"] == 0
        assert (
            importer.db.execute("SELECT COUNT(*) AS n FROM artifact_item_state").fetchone()["n"]
            == 0
        )
        assert (
            importer.db.execute(
                "SELECT COUNT(*) AS n FROM items WHERE id = ?", (item_id,)
            ).fetchone()["n"]
            == 1
        )

    def test_agent_row_keeps_its_document_locator(self, exporter, importer):
        _agent_doc(exporter, source_uri="https://example.test/page")
        importer.import_bundle(exporter.export_all())
        row = importer.db.execute("SELECT source_uri FROM agent_item_state").fetchone()
        assert row["source_uri"] == "https://example.test/page"


class TestSourceUriCollision:
    """A uri already present locally holds a DIFFERENT id, and the import has to
    survive that: the source row cannot be inserted, so every pointer into it has
    to be rewritten or the foreign key refuses the whole write."""

    def test_import_succeeds_when_the_uri_is_already_local(self, exporter, importer):
        _owned_doc(exporter, slug="remote", body="remote body", name="Remote")
        _owned_doc(importer, slug="local", body="local body", name="Local")
        remote_sid = exporter.get_source_by_uri(AGGREGATE_URI)["id"]
        local_sid = importer.get_source_by_uri(AGGREGATE_URI)["id"]
        assert remote_sid != local_sid

        result = importer.import_bundle(exporter.export_all())

        assert result["items_imported"] == 1
        assert _item_count(importer, "remote body") == 1
        assert _item_count(importer, "local body") == 1

    def test_collision_import_files_items_under_the_local_source(self, exporter, importer):
        _owned_doc(exporter, slug="remote", body="remote body", name="Remote")
        _owned_doc(importer, slug="local", body="local body", name="Local")
        local_sid = importer.get_source_by_uri(AGGREGATE_URI)["id"]

        importer.import_bundle(exporter.export_all())

        source_ids = {r["source_id"] for r in importer.db.execute("SELECT source_id FROM items")}
        assert source_ids == {local_sid}
        known = {r["id"] for r in importer.db.execute("SELECT id FROM sources")}
        assert source_ids <= known, "an item points at a source row that is absent"

    def test_collision_import_restores_ownership_for_the_imported_document(
        self, exporter, importer
    ):
        _, remote_item = _owned_doc(exporter, slug="remote", body="remote body", name="Remote")
        _owned_doc(importer, slug="local", body="local body", name="Local")
        local_sid = importer.get_source_by_uri(AGGREGATE_URI)["id"]

        result = importer.import_bundle(exporter.export_all())

        assert result["ownership_rows_imported"] == 1
        assert _owner_of(importer, "agent_item_state", remote_item) == (local_sid, "remote")
        slugs = {r["slug"] for r in importer.db.execute("SELECT slug FROM agent_item_state")}
        assert slugs == {"local", "remote"}

    def test_reimporting_the_same_bundle_changes_nothing(self, exporter, importer):
        _owned_doc(exporter)
        bundle = exporter.export_all()
        importer.import_bundle(bundle)
        second = importer.import_bundle(bundle)
        assert second["items_imported"] == 0
        assert second["ownership_rows_imported"] == 0
        assert _item_count(importer, "artifact body") == 1
        assert (
            importer.db.execute("SELECT COUNT(*) AS n FROM agent_item_state").fetchone()["n"] == 1
        )

    def test_exporting_store_can_reimport_its_own_bundle(self, exporter):
        _, item_id = _owned_doc(exporter)
        result = exporter.import_bundle(exporter.export_all())
        assert result["items_imported"] == 0
        assert _item_count(exporter, "artifact body") == 1
        assert _owner_of(exporter, "agent_item_state", item_id) is not None


class TestUriAbsentLocally:
    """A bundle uri this store does not hold is CREATED, so its documents arrive
    owned rather than being dropped."""

    def test_absent_uri_creates_the_source(self, exporter, importer):
        _folder_doc(exporter, uri="/remote/notes")
        importer.import_bundle(exporter.export_all())
        created = importer.get_source_by_uri("/remote/notes")
        assert created is not None
        assert created["source_type"] == "local_folder"

    def test_absent_uri_keeps_the_bundle_source_id(self, exporter, importer):
        remote_sid, _ = _folder_doc(exporter, uri="/remote/notes")
        importer.import_bundle(exporter.export_all())
        assert importer.get_source_by_uri("/remote/notes")["id"] == remote_sid

    def test_absent_uri_whose_id_is_taken_gets_a_fresh_id(self, exporter, importer):
        remote_sid, _ = _folder_doc(exporter, uri="/remote/notes")
        # Same id locally, different uri: the id cannot be reused and the uri is
        # still absent, so neither reusing nor skipping is right.
        importer.db.execute(
            "INSERT INTO sources (id, name, source_type, uri, properties, "
            "sync_status, created_at, updated_at) "
            "VALUES (?, 'Other', 'local_folder', '/local/other', '{}', 'paused', "
            "'2026-01-01T00:00:00', '2026-01-01T00:00:00')",
            (remote_sid,),
        )
        importer.db.commit()

        result = importer.import_bundle(exporter.export_all())

        created = importer.get_source_by_uri("/remote/notes")
        assert created is not None
        assert created["id"] != remote_sid
        assert result["items_imported"] == 1
        assert importer.get_source_by_uri("/local/other")["name"] == "Other"

    def test_walking_source_still_waits_for_confirmation(self, exporter, importer):
        _folder_doc(exporter, uri="/remote/notes")
        importer.import_bundle(exporter.export_all())
        row = importer.get_source_by_uri("/remote/notes")
        assert row["sync_status"] == "pending_confirmation"


def _imported_item_ids(store, body):
    return {
        r["id"] for r in store.db.execute("SELECT id, content FROM items") if body in r["content"]
    }


class TestOwnershipIsOnlyRestoredForItemsThatArrived:
    """A state row names a group. Restoring one whose group is not fully here would
    let a later ingest replace part of the document and duplicate the rest."""

    def test_group_naming_an_item_outside_the_bundle_is_dropped(self, exporter, importer):
        _owned_doc(exporter)
        bundle = exporter.export_all()
        bundle["agent_item_state"][0]["item_ids"] = json.dumps(["not-in-this-bundle"])
        result = importer.import_bundle(bundle)
        assert result["items_imported"] == 1
        assert result["ownership_rows_imported"] == 0

    def test_a_row_cannot_own_an_item_filed_under_a_different_source(self, exporter, importer):
        """Both the row's source and the item's source are in the bundle, and they
        differ. The item IS inserted by this import, so only the same-source check
        refuses it -- a row may own items filed under itself and nothing else."""
        _, artifact_item = _owned_doc(exporter, slug="doc", name="Doc")
        other_sid = exporter.add_source(
            name="Elsewhere", source_type="local_file", uri="/elsewhere.md"
        )
        stray = exporter.add_item(
            title="Stray", content="stray text", item_type="document", source_id=other_sid
        )
        bundle = exporter.export_all()
        bundle["agent_item_state"][0]["item_ids"] = json.dumps([artifact_item, stray])

        result = importer.import_bundle(bundle)

        assert result["items_imported"] == 2
        assert result["ownership_rows_imported"] == 0

    def test_an_item_landing_under_another_source_does_not_strand_its_sibling(
        self, exporter, importer
    ):
        """An item id already present here keeps its LOCAL source, so the row may not
        name it -- but the sibling this import DID write is the row's, and dropping the
        whole row would leave that sibling searchable and owned by nothing."""
        other = importer.add_source(name="Other", source_type="local_file", uri="/local/other.md")
        importer.db.execute(
            "INSERT INTO items (id, title, content, item_type, source_id, "
            "created_at, updated_at) VALUES ('shared-id', 'Local', 'local text', "
            "'document', ?, '2026-01-01T00:00:00', '2026-01-01T00:00:00')",
            (other,),
        )
        importer.db.commit()

        _, first = _owned_doc(exporter)
        bundle = exporter.export_all()
        remote_sid = exporter.get_source_by_uri(AGGREGATE_URI)["id"]
        bundle["items"].append(
            {
                "id": "shared-id",
                "title": "Second",
                "content": "second chunk",
                "item_type": "document",
                "source_id": remote_sid,
            }
        )
        bundle["agent_item_state"][0]["item_ids"] = json.dumps([first, "shared-id"])

        result = importer.import_bundle(bundle)

        # Only the first item lands; 'shared-id' is already taken locally.
        assert result["items_imported"] == 1
        assert result["ownership_rows_imported"] == 1
        assert _owner_of(importer, "agent_item_state", first) is not None
        group = json.loads(
            importer.db.execute(
                "SELECT item_ids FROM agent_item_state WHERE slug = 'doc'"
            ).fetchone()["item_ids"]
        )
        assert group == [first], "the row claimed an id this import did not write"
        assert (
            importer.db.execute("SELECT source_id FROM items WHERE id = 'shared-id'").fetchone()[
                "source_id"
            ]
            == other
        )
        named = [
            w for w in result["withheld"] if w.get("reason") == "ownership_row_names_absent_items"
        ]
        assert named, "the id left out of the group was not named"
        assert "shared-id" in named[0]["items"]

    def test_group_cannot_claim_items_the_bundle_did_not_ship(self, exporter, importer):
        """A bundle naming ids that exist LOCALLY must not take them over: the
        local document's own row would then be one of two naming the same items."""
        _, local_item = _owned_doc(importer, slug="local", body="local body", name="Local")
        _owned_doc(exporter, slug="remote", body="remote body", name="Remote")
        bundle = exporter.export_all()
        bundle["agent_item_state"][0]["item_ids"] = json.dumps([local_item])

        result = importer.import_bundle(bundle)

        assert result["ownership_rows_imported"] == 0
        assert _owner_of(importer, "agent_item_state", local_item) == (
            importer.get_source_by_uri(AGGREGATE_URI)["id"],
            "local",
        )

    def test_a_group_cannot_adopt_unowned_local_content(self, exporter, importer):
        """Residue -- an item under the mapped source that no state row owns -- is
        exactly what a bundle must not be able to claim. It is content this store
        already holds and the bundle never shipped, and adopting it would hand a
        foreign document the power to replace or delete it."""
        local_sid = importer.add_source(name="Auto-added", source_type="agent", uri=AGGREGATE_URI)
        importer.db.execute(
            "INSERT INTO items (id, title, content, item_type, source_id, "
            "created_at, updated_at) VALUES ('residue-id', 'Residue', "
            "'residue text', 'document', ?, '2026-01-01T00:00:00', "
            "'2026-01-01T00:00:00')",
            (local_sid,),
        )
        importer.db.commit()
        _owned_doc(exporter, slug="theirs", name="Theirs")
        bundle = exporter.export_all()
        bundle["agent_item_state"][0]["item_ids"] = json.dumps(["residue-id"])

        result = importer.import_bundle(bundle)

        assert result["ownership_rows_imported"] == 0
        assert (
            importer.db.execute("SELECT COUNT(*) AS n FROM agent_item_state").fetchone()["n"] == 0
        )

    def test_empty_group_is_dropped(self, exporter, importer):
        _owned_doc(exporter)
        bundle = exporter.export_all()
        bundle["agent_item_state"][0]["item_ids"] = "[]"
        assert importer.import_bundle(bundle)["ownership_rows_imported"] == 0

    def test_unparsable_group_is_dropped(self, exporter, importer):
        _owned_doc(exporter)
        bundle = exporter.export_all()
        bundle["agent_item_state"][0]["item_ids"] = "{not json"
        assert importer.import_bundle(bundle)["ownership_rows_imported"] == 0

    def test_local_row_for_the_same_document_wins(self, exporter, importer):
        """Same (source uri, slug) on both sides: replacing the local row would
        leave the items it owns with nothing naming them."""
        _, local_item = _owned_doc(importer, slug="doc", body="local body", name="Local")
        _owned_doc(exporter, slug="doc", body="remote body", name="Remote")

        result = importer.import_bundle(exporter.export_all())

        assert result["ownership_rows_imported"] == 0
        assert _owner_of(importer, "agent_item_state", local_item) is not None
        row = importer.db.execute("SELECT name FROM agent_item_state WHERE slug = 'doc'").fetchone()
        assert row["name"] == "Local"


def _two_chunk_doc(store, *, slug="doc", bodies=("chunk one", "chunk two"), name="Doc"):
    """One owned document whose group names TWO items, as a chunked file produces."""
    sid = store.get_source_by_uri(AGGREGATE_URI)
    sid = (
        sid["id"]
        if sid
        else store.add_source(name="Auto-added", source_type="agent", uri=AGGREGATE_URI)
    )
    ids = [
        store.add_item(title=name, content=body, item_type="document", source_id=sid)
        for body in bodies
    ]
    store.db.execute(
        "INSERT OR REPLACE INTO agent_item_state "
        "(source_id, slug, content_hash, item_ids, updated_at, name, status, source_uri) "
        "VALUES (?, ?, ?, ?, ?, ?, 'active', 'https://x/y')",
        (sid, slug, "hash-" + slug, json.dumps(ids), "2026-01-01T00:00:00", name),
    )
    store.db.commit()
    return sid, ids


class TestOwnershipSurvivesAPartlyDeletedDocument:
    """No delete path prunes a state row's group, by design -- so a document that lost one
    chunk has a row naming a dead id. The accepting pass keeps only the ids the import
    wrote, so shipping that row verbatim leaves the dead id named in the account of what
    nobody owns, on every restore of that document. The export prunes instead, which
    keeps the account for the ids that really are unowned here."""

    def test_the_export_ships_only_ids_it_carries(self, exporter):
        sid, ids = _two_chunk_doc(exporter)
        exporter.delete_item(ids[0])

        bundle = exporter.export_all()

        row = next(r for r in bundle["agent_item_state"] if r["slug"] == "doc")
        assert json.loads(row["item_ids"]) == [ids[1]], "the export shipped a dead id"

    def test_the_surviving_chunk_arrives_owned(self, exporter, importer):
        sid, ids = _two_chunk_doc(exporter)
        exporter.delete_item(ids[0])

        result = importer.import_bundle(exporter.export_all())

        assert result["withheld"] == [], f"ownership was withheld: {result['withheld']}"
        assert _item_count(importer, "chunk two") == 1
        assert (
            _owner_of(importer, "agent_item_state", ids[1]) is not None
        ), "the surviving chunk arrived unowned"

    def test_an_absent_item_is_named_and_left_out_of_the_group(self, exporter, importer):
        """A bundle built elsewhere, or before this pruning, can still name an item no
        import wrote. That id stays out of the group -- partial ownership would claim
        items another document holds -- and the caller is told which document and which
        ids, because its content is here owned by nothing and nothing else says so."""
        sid, ids = _two_chunk_doc(exporter)
        bundle = exporter.export_all()
        bundle["items"] = [i for i in bundle["items"] if i["id"] != ids[0]]

        result = importer.import_bundle(bundle)

        named = [
            w for w in result["withheld"] if w.get("reason") == "ownership_row_names_absent_items"
        ]
        assert named, f"the absent id was silent: {result['withheld']}"
        assert named[0]["key"] == "doc"
        assert ids[0] in named[0]["items"]
        group = json.loads(
            importer.db.execute(
                "SELECT item_ids FROM agent_item_state WHERE slug = 'doc'"
            ).fetchone()["item_ids"]
        )
        assert group == [ids[1]], "the row claimed an id no import wrote"


class TestTheGroupParserIsBoundedAndLinear:
    """A group's member count and each member's length are both bounded, and the bound
    refuses in the open rather than trimming a group down in silence."""

    def test_an_oversize_group_is_refused_by_name(self, importer):
        """Trimming would under-claim silently: the accepting pass would read a shorter
        group than the bundle states and hand the missing chunks to no one."""
        bundle = {
            "agent_item_state": [
                {
                    "source_id": "s1",
                    "slug": "doc",
                    "item_ids": json.dumps(
                        [f"i{n}" for n in range(store_module._MAX_ITEM_GROUP_MEMBERS + 1)]
                    ),
                    "updated_at": "2026-01-01T00:00:00",
                    "last_seen": "2026-01-01T00:00:00",
                }
            ]
        }

        with pytest.raises(KnowledgeBundleError) as caught:
            importer.import_bundle(bundle)

        assert "agent_item_state.item_ids" in str(caught.value)
        assert str(store_module._MAX_ITEM_GROUP_MEMBERS) in str(caught.value)

    def test_an_overlong_id_is_refused_by_name(self, importer):
        bundle = {
            "agent_item_state": [
                {
                    "source_id": "s1",
                    "slug": "doc",
                    "item_ids": json.dumps(["x" * (store_module._MAX_ITEM_ID_CHARS + 1)]),
                    "updated_at": "2026-01-01T00:00:00",
                    "last_seen": "2026-01-01T00:00:00",
                }
            ]
        }

        with pytest.raises(KnowledgeBundleError) as caught:
            importer.import_bundle(bundle)

        assert "character id" in str(caught.value)

    def test_the_parser_does_not_rescan_the_group_it_is_building(self):
        """The membership test must hash, not scan. At the member bound a list scan costs
        millions of comparisons while holding the knowledge write lock, so this asserts the
        shape rather than a wall-clock time that a loaded runner would make flaky."""
        source = inspect.getsource(store_module._bundle_item_group)
        code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
        assert "seen: set[str] = set()" in code, "the parser scans a list again"
        assert "entry not in group" not in code, "the quadratic membership test is back"

    def test_a_normal_group_round_trips_unchanged(self):
        ids = [f"item-{n}" for n in range(12)]
        assert store_module._bundle_item_group(json.dumps(ids)) == ids

    def test_duplicates_still_collapse_in_order(self):
        assert store_module._bundle_item_group(json.dumps(["b", "a", "b"])) == ["b", "a"]


class TestTheExemptionIsPerIdNotPerKey:
    """The exemption names the IDS the bundle's own row for a key claims. Keying it on the
    (table, source, key) pair alone fails in both directions at once, and two reviewers
    found one direction each."""

    def test_an_empty_group_marker_does_not_vouch_for_an_id(self, exporter, importer):
        """A full export ships empty-group marker rows verbatim. Such a row carries a key
        while claiming nothing, so it must not exempt that key's local row -- which here
        holds a stale claim on an id the bundle ships. Exempting it hands the imported item
        to that unrelated row, and the next delete of that document takes it.

        The marker's key must MATCH the stale row's key: that is the only shape where a
        key-level exemption and an id-level one differ."""
        _, remote_item = _owned_doc(exporter, slug="doc", body="remote body", name="Remote")
        exporter_sid = exporter.db.execute(
            "SELECT source_id FROM agent_item_state WHERE slug = 'doc'"
        ).fetchone()["source_id"]
        exporter.db.commit()
        local_sid = importer.add_source(name="Auto-added", source_type="agent", uri=AGGREGATE_URI)
        importer.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, item_ids, "
            "updated_at, name, status, source_uri) VALUES (?, 'unrelated', 'stale-hash', "
            "?, '2026-01-01T00:00:00', 'Stale', 'active', 'https://x/y')",
            (local_sid, json.dumps([remote_item])),
        )
        importer.db.commit()

        bundle = exporter.export_all()
        # Injected rather than exported: this store's export now prunes an empty group away,
        # so the shape can only reach the gate from an older or hand-built bundle -- which is
        # exactly why the gate, not the export, has to refuse it.
        bundle["agent_item_state"].append(
            {
                "source_id": exporter_sid,
                "slug": "unrelated",
                "content_hash": "marker-hash",
                "item_ids": "[]",
                "updated_at": "2026-01-01T00:00:00",
                "name": "Marker",
                "status": "active",
                "source_uri": "https://x/y",
                "last_seen": "2026-01-01T00:00:00",
            }
        )

        result = importer.import_bundle(bundle)

        assert result["items_withheld"] >= 1, "an empty-group marker vouched for a contested id"
        assert _item_count(importer, "remote body") == 0, "a stale claim captured the import"

    def test_a_bundle_with_no_state_rows_still_restores_a_deleted_item(self, importer):
        """The single-item export endpoint ships no state table, and neither does any
        bundle written before this change. Such a bundle states no ownership, so it cannot
        be judged against a local claim -- and the claim it would meet is the deleter's
        own row, which no exemption can ever cover because nothing was shipped to cover
        it. Judging it there withholds the item with no exit at all."""
        _, item_id = _owned_doc(importer, slug="doc", body="saved body", name="Doc")
        bundle = importer.export_all()
        for table in BUNDLE_STATE_KEY_COL:
            bundle.pop(table, None)
        importer.delete_item(item_id)
        assert _item_count(importer, "saved body") == 0

        result = importer.import_bundle(bundle)

        assert result["items_withheld"] == 0, "a state-table-less bundle was gated anyway"
        assert _item_count(importer, "saved body") == 1, "the item never came back"


class TestTheSameDocumentExceptionCoversEverySite:
    """The invariant, not the one line that was reported: wherever a LOCAL state row's
    liveness or claim decides that the bundle's content does not come in, the document
    the bundle is restoring is exempt. Enumerated structurally because this defect
    reappeared three times at three different sites -- fixing the reported point and
    stopping is what let the next one through, so a NEW site has to fail this test and
    force a decision rather than inherit the old behaviour."""

    @staticmethod
    def _store_source():
        return Path(store_module.__file__).read_text(encoding="utf-8")

    def _gate_sites(self):
        tree = ast.parse(self._store_source())
        functions = [
            f for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]

        def enclosing(node):
            best = None
            for f in functions:
                if f.lineno <= node.lineno <= (f.end_lineno or f.lineno):
                    if best is None or f.lineno > best.lineno:
                        best = f
            return best.name if best else "<module>"

        return {
            enclosing(n)
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "_state_row_owns_items"
        }

    def test_every_liveness_gate_is_a_known_site(self):
        """A liveness gate in a function not listed here has not been considered against
        the same-document exception. Extend the list only together with a behavioural pin
        for that site."""
        assert self._gate_sites() == {"_bundle_blocked_items", "_import_bundle_state"}

    def test_the_withholding_site_compares_the_local_group(self):
        """`_bundle_blocked_items` must decide on the groups OVERLAPPING, which is what
        tells the same document from a different one holding the key."""
        source = self._store_source()
        body = source[
            source.index("def _bundle_blocked_items") : source.index("def _contested_claims")
        ]
        assert (
            "group & local_group" in body
        ), "the collision arm no longer distinguishes the same document"

    def test_the_claim_site_exempts_ids_not_keys(self):
        """The exemption must be per ID. Keying it on (table, source, key) alone lets an
        empty-group marker row vouch for ids it never claimed, and it leaves a bundle that
        ships no state row for a table judged against that table's claims anyway -- the
        two directions two independent reviewers found."""
        source = self._store_source()
        body = source[source.index("def _contested_claims") : source.index("def _items_exist")]
        assert (
            "restored_groups.get(" in body
        ), "the contested-claim arm exempts a whole key again, not the ids it names"
        assert (
            "if table not in stated" in body
        ), "a bundle stating no ownership for a table is judged against it again"


class TestAPartialDeleteStillRestores:
    """A chunk deleted through the ordinary route leaves the group naming it while a
    surviving sibling keeps the row live. The row therefore holds the key AND is the
    document the bundle restores, and the restored chunk must come back owned. Withholding
    it leaves one exit -- delete the document's remaining chunks so the row owns nothing
    live, then re-import -- which is not a recovery path, because it asks the user to
    destroy more content to get content back."""

    def test_the_deleted_chunk_comes_back(self, importer):
        _, ids = _two_chunk_doc(importer)
        bundle = importer.export_all()
        importer.delete_item(ids[0])
        assert _item_count(importer, "chunk one") == 0, "the chunk was not deleted"
        assert _item_count(importer, "chunk two") == 1, "the sibling should survive"

        result = importer.import_bundle(bundle)

        assert result["items_withheld"] == 0, "the live sibling withheld the whole group"
        assert _item_count(importer, "chunk one") == 1, "the deleted chunk did not return"

    def test_the_restored_chunk_is_owned_again(self, importer):
        """Arriving is not enough -- nothing else would ever name it. No merge is needed
        to achieve that, and the reason is worth stating: the delete paths never rewrite a
        group (`_delete_item_cascade` leaves it alone and `_adopt_reassigned_item` only
        appends), so the row still names the deleted id and simply becomes accurate again
        once the chunk returns. Ownership is what this asserts, not the mechanism."""
        _, ids = _two_chunk_doc(importer)
        bundle = importer.export_all()
        importer.delete_item(ids[0])

        importer.import_bundle(bundle)

        assert _owner_of(importer, "agent_item_state", ids[0]) is not None
        group = json.loads(
            importer.db.execute(
                "SELECT item_ids FROM agent_item_state WHERE slug = 'doc'"
            ).fetchone()["item_ids"]
        )
        assert set(group) == set(ids), "the row does not name both chunks"

    def test_the_surviving_sibling_is_not_disturbed(self, importer):
        _, ids = _two_chunk_doc(importer)
        bundle = importer.export_all()
        importer.delete_item(ids[0])

        importer.import_bundle(bundle)

        assert _item_count(importer, "chunk two") == 1
        assert _owner_of(importer, "agent_item_state", ids[1]) is not None


def _newer_pruned_then_older_complete(store):
    """``(ids, newer_pruned, older_complete)`` for one two-chunk document.

    The complete export is taken first; a chunk is then deleted and the document
    exported again. `export_all` prunes a group to the items it carries, so the second
    bundle is a NEWER backup naming one chunk and the first an OLDER one naming both.
    Restoring the newer backup and then the older one is an ordinary restore order --
    the newest backup first, an older one to recover what it had already lost.
    """
    _, ids = _two_chunk_doc(store)
    older_complete = store.export_all()
    store.delete_item(ids[1])
    newer_pruned = store.export_all()
    return ids, newer_pruned, older_complete


def _restore_both(exporter, importer):
    """Restore the newer pruned backup, then the older complete one."""
    ids, newer, older = _newer_pruned_then_older_complete(exporter)
    importer.import_bundle(newer)
    result = importer.import_bundle(older)
    return ids, result


def _group_of(store, slug="doc"):
    row = store.db.execute(
        "SELECT item_ids FROM agent_item_state WHERE slug = ?", (slug,)
    ).fetchone()
    return set(json.loads(row["item_ids"] or "[]")) if row else None


class TestASecondBundleOwnsTheChunksItBrings:
    """A second bundle for a document already here brings chunks the local row does
    not name, and the row that names them is refused because the rest of its group was
    already present. Nothing else re-derives ownership for the ids that DID arrive:
    `agent_item_state` is the one table a bundle restores and no pass reaps it, so the
    document's own delete takes the chunks its row names and leaves the arrived one
    behind -- searchable, and owned by nothing that could ever remove it. This is the
    shape the blocking pass already describes as "its absent ids arrive, to be merged
    into that row's group by the accepting pass"."""

    def test_the_older_bundle_chunk_arrives(self, exporter, importer):
        """The control. Every pin below is vacuous if the chunk never lands."""
        ids, result = _restore_both(exporter, importer)

        assert result["items_imported"] == 1, "the older bundle brought no chunk"
        assert _item_count(importer, "chunk two") == 1

    def test_the_arriving_chunk_is_owned(self, exporter, importer):
        ids, _ = _restore_both(exporter, importer)

        local_sid = importer.get_source_by_uri(AGGREGATE_URI)["id"]
        assert _owner_of(importer, "agent_item_state", ids[1]) == (
            local_sid,
            "doc",
        ), "the chunk the older bundle brought is owned by nothing"

    def test_the_row_names_both_chunks(self, exporter, importer):
        ids, _ = _restore_both(exporter, importer)

        assert _group_of(importer) == set(ids), "the row does not name both chunks"

    def test_deleting_the_document_takes_both_chunks(self, exporter, importer):
        ids, _ = _restore_both(exporter, importer)
        local_sid = importer.get_source_by_uri(AGGREGATE_URI)["id"]

        remove_document(importer, local_sid, "doc")

        assert _item_count(importer, "chunk one") == 0
        assert (
            _item_count(importer, "chunk two") == 0
        ), "a chunk outlived the document that brought it"

    def test_nothing_the_document_brought_stays_searchable(self, exporter, importer):
        ids, _ = _restore_both(exporter, importer)
        local_sid = importer.get_source_by_uri(AGGREGATE_URI)["id"]
        assert importer.search_items_fts("chunk two"), "fixture must index the chunk"

        remove_document(importer, local_sid, "doc")

        assert (
            importer.search_items_fts("chunk two") == []
        ), "a deleted document still answers searches"

    def test_no_item_is_left_without_an_owner(self, exporter, importer):
        _restore_both(exporter, importer)

        owned = set()
        for table in BUNDLE_STATE_KEY_COL:
            for row in importer.db.execute(f"SELECT item_ids FROM {table}"):  # noqa: S608
                owned.update(json.loads(row["item_ids"] or "[]"))
        present = {r["id"] for r in importer.db.execute("SELECT id FROM items")}
        assert present == owned, "an item arrived that no ownership row names"


class TestTheMergeTakesOnlyWhatThisImportWrote:
    """The other direction. A merge that takes more than the arriving ids is a data bug
    of its own: it hands a bundle's document the authority to delete local content, or
    puts one chunk in two groups so the first delete strands the second row's items."""

    def test_local_content_the_bundle_names_is_not_merged_in(self, exporter, importer):
        """Residue under the same source that no row owns. The bundle's row names it,
        the live local row is being extended, and it must still not be captured."""
        ids, newer, older = _newer_pruned_then_older_complete(exporter)
        importer.import_bundle(newer)
        local_sid = importer.get_source_by_uri(AGGREGATE_URI)["id"]
        importer.db.execute(
            "INSERT INTO items (id, title, content, item_type, source_id, "
            "created_at, updated_at) VALUES ('residue-id', 'Residue', "
            "'residue text', 'document', ?, '2026-01-01T00:00:00', "
            "'2026-01-01T00:00:00')",
            (local_sid,),
        )
        importer.db.commit()
        next(r for r in older["agent_item_state"] if r["slug"] == "doc")["item_ids"] = json.dumps(
            [*ids, "residue-id"]
        )

        importer.import_bundle(older)

        assert _group_of(importer) == set(ids), "the merge adopted unowned local content"
        assert _item_count(importer, "residue text") == 1, "the residue must be left alone"

    def test_an_id_another_document_owns_is_not_merged_in(self, exporter, importer):
        """A sibling row in the same bundle claims the arriving id first, so the row
        being extended may not name it too -- one item in two groups means the first
        document-level delete removes content the second row still names."""
        ids, newer, older = _newer_pruned_then_older_complete(exporter)
        importer.import_bundle(newer)
        sibling = {
            **next(r for r in older["agent_item_state"] if r["slug"] == "doc"),
            "slug": "sibling",
            "name": "Sibling",
            "item_ids": json.dumps([ids[1]]),
        }
        # The sibling is accepted first, so it owns the arriving chunk.
        older["agent_item_state"] = [sibling, *older["agent_item_state"]]

        importer.import_bundle(older)

        owners = [
            row["slug"]
            for row in importer.db.execute("SELECT slug, item_ids FROM agent_item_state")
            if ids[1] in json.loads(row["item_ids"] or "[]")
        ]
        assert owners == ["sibling"], f"the chunk ended up in two groups: {owners}"

    def test_a_different_document_on_the_same_key_is_not_merged_into(self, exporter, importer):
        """Disjoint groups on one key are two different documents, not one coming back.
        The bundle's items are withheld, and the local row is left exactly as it was."""
        _, local_item = _owned_doc(importer, slug="doc", body="local body", name="Local")
        _owned_doc(exporter, slug="doc", body="remote body", name="Remote")

        importer.import_bundle(exporter.export_all())

        assert _group_of(importer) == {local_item}, "a foreign document was merged in"
        assert _item_count(importer, "remote body") == 0

    def test_the_merge_leaves_the_local_rows_identity_alone(self, exporter, importer):
        """The bundle reaching this branch can be OLDER than the local copy, so its
        hash and name must not replace the ones describing what this store holds."""
        ids, newer, older = _newer_pruned_then_older_complete(exporter)
        importer.import_bundle(newer)
        importer.db.execute(
            "UPDATE agent_item_state SET content_hash = 'local-hash', name = 'Local name' "
            "WHERE slug = 'doc'"
        )
        importer.db.commit()

        importer.import_bundle(older)

        row = importer.db.execute(
            "SELECT content_hash, name FROM agent_item_state WHERE slug = 'doc'"
        ).fetchone()
        assert row["content_hash"] == "local-hash", "an older bundle rewrote the local hash"
        assert row["name"] == "Local name"
        assert _group_of(importer) == set(ids), "the group did not grow"


class TestWithholdingIsNeverSilent:
    """Withholding still happens for a DIFFERENT document holding the key, and that is
    correct -- but the caller has to be able to say WHICH content did not arrive, not
    just how much."""

    def test_a_different_document_on_the_same_key_is_still_withheld(self, exporter, importer):
        _owned_doc(importer, slug="doc", body="local body", name="Local")
        _owned_doc(exporter, slug="doc", body="remote body", name="Remote")

        result = importer.import_bundle(exporter.export_all())

        assert result["items_withheld"] == 1
        assert _item_count(importer, "remote body") == 0
        assert _item_count(importer, "local body") == 1

    def test_the_withheld_document_is_named(self, exporter, importer):
        _owned_doc(importer, slug="doc", body="local body", name="Local")
        _owned_doc(exporter, slug="doc", body="remote body", name="Remote")

        result = importer.import_bundle(exporter.export_all())

        named = [w for w in result["withheld"] if w.get("reason") == "document_key_held_locally"]
        assert named, "a withheld document was not named in the result"
        assert named[0]["key"] == "doc"
        assert named[0]["table"] == "agent_item_state"

    def test_nothing_is_named_when_nothing_is_withheld(self, exporter, importer):
        """The control: the account is empty on a clean import, so a non-empty account
        means something really was held back."""
        _owned_doc(exporter, slug="doc", body="remote body", name="Remote")

        result = importer.import_bundle(exporter.export_all())

        assert result["items_withheld"] == 0
        assert result["withheld"] == []


class TestAKeyThisStoreAlreadyHoldsBlocksTheItems:
    """A `(source, key)` pair IS a document's identity within a source, so two
    documents cannot share one. When a live local row holds the pair, the bundle's
    copy is not a document here and its items must not arrive: nothing would ever
    own them, and the text would answer searches beside the local copy forever."""

    def test_the_colliding_items_do_not_arrive(self, exporter, importer):
        _owned_doc(importer, slug="doc", body="local body", name="Local")
        _owned_doc(exporter, slug="doc", body="remote body", name="Remote")

        result = importer.import_bundle(exporter.export_all())

        assert result["items_imported"] == 0
        assert _item_count(importer, "remote body") == 0
        assert _item_count(importer, "local body") == 1

    def test_no_item_is_left_without_an_owner(self, exporter, importer):
        _owned_doc(importer, slug="doc", body="local body", name="Local")
        _owned_doc(exporter, slug="doc", body="remote body", name="Remote")

        importer.import_bundle(exporter.export_all())

        owned = set()
        for table in BUNDLE_STATE_KEY_COL:
            for row in importer.db.execute(f"SELECT item_ids FROM {table}"):  # noqa: S608
                owned.update(json.loads(row["item_ids"] or "[]"))
        present = {r["id"] for r in importer.db.execute("SELECT id FROM items")}
        assert present == owned, "an item arrived that no ownership row names"

    def test_an_empty_local_row_does_not_block_its_items(self, exporter, importer):
        """Only a LIVE local row blocks. An empty group is a marker, and the
        imported document takes the key instead."""
        _owned_doc(exporter, slug="doc", body="remote body", name="Remote")
        local_sid = importer.add_source(name="Auto-added", source_type="agent", uri=AGGREGATE_URI)
        importer.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, "
            "item_ids, updated_at, name, status) VALUES (?, 'doc', 'loser-hash', "
            "'[]', '2026-01-01T00:00:00', 'Local', 'deduped')",
            (local_sid,),
        )
        importer.db.commit()

        result = importer.import_bundle(exporter.export_all())

        assert result["items_imported"] == 1
        assert _item_count(importer, "remote body") == 1

    def test_a_backup_restores_an_item_its_own_stale_row_still_names(self, exporter, importer):
        """The canonical restore: export a document, delete its item, re-import the export.
        `_delete_item_cascade` leaves the doc-state group naming the deleted id, so the
        deleter's own row claims it -- and that row is the same (source, key) the bundle
        restores and owns nothing live, so the accepting pass repoints it. Withholding here
        would leave the item deleted in the one case where the bundle is the only copy."""
        _, item_id = _owned_doc(importer, slug="doc", body="saved body", name="Doc")
        bundle = importer.export_all()
        importer.delete_item(item_id)
        assert _item_count(importer, "saved body") == 0, "the item was not deleted"
        stale = importer.db.execute(
            "SELECT item_ids FROM agent_item_state WHERE slug = 'doc'"
        ).fetchone()
        assert stale is not None and item_id in json.loads(
            stale["item_ids"] or "[]"
        ), "the delete cleared the group, so this pin no longer covers a stale claim"

        result = importer.import_bundle(bundle)

        assert result["items_withheld"] == 0, "a stale same-document claim blocked a restore"
        assert _item_count(importer, "saved body") == 1
        assert _owner_of(importer, "agent_item_state", item_id) is not None

    def test_a_stale_claim_on_an_id_the_bundle_ships_withholds_it(self, exporter, importer):
        """Claims are read BEFORE the item loop, so an id a stale local group names and
        the bundle also ships does not exist yet. Inserting it would make the stale claim
        live over content it never owned, and the bundle's own row for that id is then
        skipped as claimed -- leaving the item held only by the stale row, so deleting
        that unrelated document would destroy imported content."""
        _, remote_item = _owned_doc(exporter, slug="doc", body="remote body", name="Remote")
        local_sid = importer.add_source(name="Auto-added", source_type="agent", uri=AGGREGATE_URI)
        importer.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, item_ids, "
            "updated_at, name, status, source_uri) VALUES (?, 'unrelated', 'stale-hash', "
            "?, '2026-01-01T00:00:00', 'Stale', 'active', 'https://x/y')",
            (local_sid, json.dumps([remote_item])),
        )
        importer.db.commit()

        result = importer.import_bundle(exporter.export_all())

        assert result["items_withheld"] == 1
        assert _item_count(importer, "remote body") == 0, "a stale claim captured the import"
        assert _owner_of(importer, "agent_item_state", remote_item) is None or (
            importer.db.execute(
                "SELECT COUNT(*) AS n FROM items WHERE id = ?", (remote_item,)
            ).fetchone()["n"]
            == 0
        ), "the item arrived under a stale claim"

    def test_a_stale_claim_on_another_sources_item_does_not_block(self, exporter, importer):
        """A row can name LIVE items that belong to a different source:
        `_adopt_reassigned_item` leaves exactly that stale claim, by design, when a
        content hash is ambiguous. Existence is not ownership -- the row describing
        those items is the recipient's, so this row does not hold the key and the
        bundle's document must arrive rather than be withheld."""
        _owned_doc(exporter, slug="doc", body="remote body", name="Remote")
        other_sid, other_item = _owned_doc(
            importer, slug="elsewhere", body="other body", name="Other", uri="agent://other"
        )
        local_sid = importer.add_source(name="Auto-added", source_type="agent", uri=AGGREGATE_URI)
        importer.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, item_ids, "
            "updated_at, name, status, source_uri) VALUES (?, 'doc', 'stale-hash', ?, "
            "'2026-01-01T00:00:00', 'Local', 'active', 'https://x/y')",
            (local_sid, json.dumps([other_item])),
        )
        importer.db.commit()

        result = importer.import_bundle(exporter.export_all())

        assert result["items_imported"] == 1, "a stale claim withheld the bundle's content"
        assert _item_count(importer, "remote body") == 1
        assert (
            importer.db.execute(
                "SELECT source_id FROM items WHERE id = ?",
                (other_item,),
            ).fetchone()["source_id"]
            == other_sid
        ), "the claimed item moved"

    def test_an_artifact_slug_this_store_holds_withholds_its_items(self, exporter, importer):
        """The rule covers every state table, not just the one whose rows travel. An
        artifact slug this store already holds IS that document here, so the bundle's
        copy must not land as a second unowned copy of the same text."""
        _artifact_doc(importer, slug="doc", body="local body", name="Local")
        _artifact_doc(exporter, slug="doc", body="remote body", name="Remote")

        result = importer.import_bundle(exporter.export_all())

        assert result["items_withheld"] == 1
        assert _item_count(importer, "remote body") == 0, "a duplicate artifact copy arrived"
        assert _item_count(importer, "local body") == 1, "the local copy was disturbed"

    def test_a_document_this_store_does_not_have_still_arrives(self, exporter, importer):
        _owned_doc(importer, slug="local", body="local body", name="Local")
        _owned_doc(exporter, slug="remote", body="remote body", name="Remote")

        result = importer.import_bundle(exporter.export_all())

        assert result["items_imported"] == 1
        assert result["ownership_rows_imported"] == 1


class TestOneItemIsNeverOwnedByTwoRows:
    """The next document-level delete hands `delete_items_batch` an item whose only
    other holder is a state row it does not consult, finds nothing else holding it,
    and removes it -- leaving the second row naming deleted content."""

    def test_overlapping_groups_within_one_bundle_yield_one_owner(self, exporter, importer):
        _, item_id = _owned_doc(exporter, slug="first", name="First")
        bundle = exporter.export_all()
        first = bundle["agent_item_state"][0]
        bundle["agent_item_state"].append({**first, "slug": "second", "name": "Second"})

        result = importer.import_bundle(bundle)

        assert result["ownership_rows_imported"] == 1
        owners = [
            row["slug"]
            for row in importer.db.execute("SELECT slug, item_ids FROM agent_item_state")
            if item_id in json.loads(row["item_ids"] or "[]")
        ]
        assert owners == ["first"]

    def test_a_group_an_existing_row_already_owns_is_refused(self, exporter, importer):
        """The same item id under a DIFFERENT document key: the local row owns it,
        so the imported row may not name it as well."""
        local_sid = importer.add_source(name="Auto-added", source_type="agent", uri=AGGREGATE_URI)
        importer.db.execute(
            "INSERT INTO items (id, title, content, item_type, source_id, "
            "created_at, updated_at) VALUES ('shared-id', 'Local', 'shared text', "
            "'document', ?, '2026-01-01T00:00:00', '2026-01-01T00:00:00')",
            (local_sid,),
        )
        importer.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, "
            "item_ids, updated_at, name, status) VALUES (?, 'mine', 'h', "
            "'[\"shared-id\"]', '2026-01-01T00:00:00', 'Mine', 'active')",
            (local_sid,),
        )
        importer.db.commit()
        _owned_doc(exporter, slug="theirs", name="Theirs")
        bundle = exporter.export_all()
        bundle["items"].append(
            {
                "id": "shared-id",
                "title": "Theirs",
                "content": "shared text",
                "item_type": "document",
                "source_id": exporter.get_source_by_uri(AGGREGATE_URI)["id"],
            }
        )
        bundle["agent_item_state"][0]["item_ids"] = json.dumps(["shared-id"])

        result = importer.import_bundle(bundle)

        assert result["ownership_rows_imported"] == 0
        owners = [
            row["slug"]
            for row in importer.db.execute("SELECT slug, item_ids FROM agent_item_state")
            if "shared-id" in json.loads(row["item_ids"] or "[]")
        ]
        assert owners == ["mine"]


class TestOwnershipCoversOnlyWhatThisImportInserted:
    """An id the bundle ships that already exists here was skipped by
    `INSERT OR IGNORE`, so the row in the store is LOCAL content. Naming it would
    hand a foreign document the authority to replace or delete something this store
    owns -- and unowned local residue is exactly the shape that makes it reachable."""

    def test_a_group_cannot_capture_an_unowned_local_item_of_the_same_id(self, exporter, importer):
        _, item_id = _owned_doc(exporter, slug="doc", body="remote body", name="Remote")
        local_sid = importer.add_source(name="Auto-added", source_type="agent", uri=AGGREGATE_URI)
        # Residue: the SAME item id already here, under the same source, owned by
        # nothing. INSERT OR IGNORE will skip the bundle's copy.
        importer.db.execute(
            "INSERT INTO items (id, title, content, item_type, source_id, "
            "created_at, updated_at) VALUES (?, 'Residue', 'residue text', "
            "'document', ?, '2026-01-01T00:00:00', '2026-01-01T00:00:00')",
            (item_id, local_sid),
        )
        importer.db.commit()

        result = importer.import_bundle(exporter.export_all())

        assert result["items_imported"] == 0
        assert result["ownership_rows_imported"] == 0
        assert _item_count(importer, "residue text") == 1

    def test_a_group_of_freshly_inserted_items_is_still_restored(self, exporter, importer):
        _owned_doc(exporter, slug="doc", body="remote body", name="Remote")
        result = importer.import_bundle(exporter.export_all())
        assert result["items_imported"] == 1
        assert result["ownership_rows_imported"] == 1


class TestACollidingRowDoesNotSuppressASibling:
    """Blocking is decided before the rows are validated, so a malformed row must not
    get to withhold a SIBLING document's items."""

    def test_an_id_two_rows_both_claim_is_not_blocked(self, exporter, importer):
        _, item_id = _owned_doc(exporter, slug="doc", body="remote body", name="Remote")
        # Local row owns the key 'doc', so the bundle's 'doc' row collides.
        _owned_doc(importer, slug="doc", body="local body", name="Local")
        bundle = exporter.export_all()
        first = bundle["agent_item_state"][0]
        # A sibling row claims the same item, so the bundle does not agree who owns it.
        bundle["agent_item_state"].append({**first, "slug": "sibling", "name": "Sibling"})

        result = importer.import_bundle(bundle)

        assert result["items_imported"] == 1, "the sibling's content was withheld"
        assert _item_count(importer, "remote body") == 1


class TestDependentRowsFollowTheWithheldItem:
    """Locations, mentions and relations point at an item by foreign key, so one
    naming a withheld item would refuse the whole import."""

    def test_a_bundle_with_locations_for_a_colliding_document_still_imports(
        self, exporter, importer
    ):
        _, remote_item = _owned_doc(exporter, slug="doc", body="remote body", name="Remote")
        remote_sid = exporter.get_source_by_uri(AGGREGATE_URI)["id"]
        exporter.add_source_location(remote_item, remote_sid)
        _owned_doc(importer, slug="doc", body="local body", name="Local")
        bundle = exporter.export_all()
        assert bundle["source_locations"], "fixture must ship a location to be a test"

        result = importer.import_bundle(bundle)

        assert result["items_imported"] == 0
        assert _item_count(importer, "local body") == 1

    def test_a_mention_and_a_relation_on_a_withheld_item_do_not_abort(self, exporter, importer):
        _, remote_item = _owned_doc(exporter, slug="doc", body="remote body", name="Remote")
        _owned_doc(importer, slug="doc", body="local body", name="Local")
        bundle = exporter.export_all()
        bundle["entities"] = [
            {
                "id": "e1",
                "name": "Thing",
                "entity_type": "concept",
                "created_at": "2026-01-01T00:00:00",
            }
        ]
        bundle["mentions"] = [{"item_id": remote_item, "entity_id": "e1"}]
        bundle["relations"] = [
            {
                "id": "r1",
                "source_id": "e1",
                "target_id": "e1",
                "relation_type": "rel",
                "source_item_id": remote_item,
            }
        ]

        result = importer.import_bundle(bundle)

        # Both dependent rows skipped the withheld item, so nothing reaches `e1`. An
        # entity referenced by neither a mention nor a relation is a graph node no
        # document supports, so it is dropped and not counted as created.
        assert result["entities_created"] == 0
        assert result["relations_rebuilt"] == 0
        assert importer.db.execute("SELECT COUNT(*) AS n FROM mentions").fetchone()["n"] == 0
        assert (
            importer.db.execute("SELECT COUNT(*) AS n FROM entities WHERE id = 'e1'").fetchone()[
                "n"
            ]
            == 0
        )

    def test_an_entity_a_surviving_item_mentions_is_kept(self, exporter, importer):
        """The prune is scoped to what is unreachable, not to what arrived beside a
        withheld document."""
        _, remote_item = _owned_doc(exporter, slug="fresh", body="remote body", name="Remote")
        bundle = exporter.export_all()
        bundle["entities"] = [
            {
                "id": "e1",
                "name": "Thing",
                "entity_type": "concept",
                "created_at": "2026-01-01T00:00:00",
            }
        ]
        bundle["mentions"] = [{"item_id": remote_item, "entity_id": "e1"}]

        result = importer.import_bundle(bundle)

        assert result["entities_created"] == 1
        assert (
            importer.db.execute("SELECT COUNT(*) AS n FROM entities WHERE id = 'e1'").fetchone()[
                "n"
            ]
            == 1
        )

    def test_an_entity_the_bundle_carries_alone_is_kept(self, exporter, importer):
        """A bundle with an entity and no mentions means that entity deliberately -- a
        hand-built bundle does exactly this. Nothing stranded it, so nothing prunes it.
        This is the case the DB-reference check cannot save: the entity has no reference
        anywhere, so only scoping the prune to what the bundle itself stranded keeps it."""
        _owned_doc(exporter, slug="fresh", body="remote body", name="Remote")
        bundle = exporter.export_all()
        bundle["entities"] = [
            {
                "id": "standalone",
                "name": "Thing",
                "entity_type": "concept",
                "created_at": "2026-01-01T00:00:00",
            }
        ]
        bundle["mentions"] = []
        bundle["relations"] = []

        result = importer.import_bundle(bundle)

        assert result["entities_created"] == 1
        assert (
            importer.db.execute(
                "SELECT COUNT(*) AS n FROM entities WHERE id = 'standalone'"
            ).fetchone()["n"]
            == 1
        )

    def test_a_local_orphan_entity_is_left_alone(self, exporter, importer):
        """The import deletes only orphans IT created. A local entity that was already
        unreferenced is not this call's business."""
        importer.db.execute(
            "INSERT INTO entities (id, name, entity_type, created_at, updated_at) "
            "VALUES ('local-orphan', 'Old', 'concept', ?, ?)",
            ("2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        importer.db.commit()
        _owned_doc(exporter, slug="fresh", body="remote body", name="Remote")

        importer.import_bundle(exporter.export_all())

        assert (
            importer.db.execute(
                "SELECT COUNT(*) AS n FROM entities WHERE id = 'local-orphan'"
            ).fetchone()["n"]
            == 1
        )


class TestAScopedExportDoesNotLeakOtherNamespaces:
    """A namespace bundle's out-of-namespace state rows are refused on import anyway,
    so shipping them would carry another namespace's file paths, document names and
    content hashes out of the machine for nothing. `source_locations` is already
    scoped for the same reason."""

    def _two_namespaces(self, store):
        sid = store.add_source(name="Auto-added", source_type="agent", uri=AGGREGATE_URI)
        keep = store.add_item(
            title="Keep",
            content="keep body",
            item_type="document",
            source_id=sid,
            namespace="shared",
        )
        hide = store.add_item(
            title="Hide",
            content="hide body",
            item_type="document",
            source_id=sid,
            namespace="private",
        )
        for slug, item_id in (("keep", keep), ("hide", hide)):
            store.db.execute(
                "INSERT INTO agent_item_state (source_id, slug, content_hash, "
                "item_ids, updated_at, name, status) "
                "VALUES (?, ?, ?, ?, '2026-01-01T00:00:00', ?, 'active')",
                (sid, slug, "hash-" + slug, json.dumps([item_id]), slug.title()),
            )
        store.db.commit()
        return sid

    def test_a_scoped_export_ships_only_the_scoped_documents_rows(self, exporter):
        self._two_namespaces(exporter)
        bundle = exporter.export_all(namespace="shared")
        slugs = {row["slug"] for row in bundle["agent_item_state"]}
        assert slugs == {"keep"}

    def test_a_scoped_export_carries_no_other_namespace_document_name(self, exporter):
        self._two_namespaces(exporter)
        blob = json.dumps(exporter.export_all(namespace="shared"))
        assert "hash-hide" not in blob
        assert "Hide" not in blob

    def test_an_unscoped_export_still_ships_everything(self, exporter):
        self._two_namespaces(exporter)
        bundle = exporter.export_all()
        slugs = {row["slug"] for row in bundle["agent_item_state"]}
        assert slugs == {"keep", "hide"}


class TestTheImportReportsWhatItWithheld:
    """A withheld document lowers `items_imported` with nothing saying why."""

    def test_a_collision_reports_the_withheld_items(self, exporter, importer):
        _owned_doc(importer, slug="doc", body="local body", name="Local")
        _owned_doc(exporter, slug="doc", body="remote body", name="Remote")
        result = importer.import_bundle(exporter.export_all())
        assert result["items_withheld"] == 1
        assert result["items_imported"] == 0

    def test_an_ordinary_import_withholds_nothing(self, exporter, importer):
        _owned_doc(exporter)
        result = importer.import_bundle(exporter.export_all())
        assert result["items_withheld"] == 0
        assert result["items_imported"] == 1

    def test_an_id_the_bundle_files_under_another_source_is_not_blocked(self, exporter, importer):
        """A colliding row may withhold only ITS OWN document's items. An id the
        bundle files elsewhere belongs to another document, and the store's own writer
        can produce that shape when a reassigned item leaves one row naming an item
        its new owner's row never names."""
        _owned_doc(importer, slug="doc", body="local body", name="Local")
        _, artifact_item = _owned_doc(exporter, slug="doc", body="remote body", name="Remote")
        other_sid = exporter.add_source(
            name="Elsewhere", source_type="local_file", uri="/elsewhere.md"
        )
        stray = exporter.add_item(
            title="Stray", content="stray text", item_type="document", source_id=other_sid
        )
        bundle = exporter.export_all()
        # The colliding row names an item the bundle files under a different source.
        bundle["agent_item_state"][0]["item_ids"] = json.dumps([artifact_item, stray])

        result = importer.import_bundle(bundle)

        assert _item_count(importer, "stray text") == 1, "another source's item was lost"
        assert result["items_withheld"] == 1


class TestStateTextMustBeBindable:
    """`isinstance(value, str)` is not enough: a lone surrogate is a `str` SQLite
    cannot encode, and the `UnicodeEncodeError` sits outside every arm the import
    endpoint catches, so it surfaces as a 500 instead of the malformed-bundle 400."""

    def test_a_lone_surrogate_key_is_a_typed_error(self, exporter, importer):
        _owned_doc(exporter)
        bundle = exporter.export_all()
        bundle["agent_item_state"][0]["slug"] = "bad\ud800key"
        with pytest.raises(KnowledgeBundleError, match="valid UTF-8"):
            importer.import_bundle(bundle)

    def test_a_lone_surrogate_carried_column_is_a_typed_error(self, exporter, importer):
        _owned_doc(exporter)
        bundle = exporter.export_all()
        bundle["agent_item_state"][0]["name"] = "bad\ud800name"
        with pytest.raises(KnowledgeBundleError, match="valid UTF-8"):
            importer.import_bundle(bundle)

    def test_a_non_string_key_is_a_typed_error(self, exporter, importer):
        _owned_doc(exporter)
        bundle = exporter.export_all()
        bundle["agent_item_state"][0]["slug"] = 7
        with pytest.raises(KnowledgeBundleError, match="must be a string or null"):
            importer.import_bundle(bundle)


class TestIdentityColumnsMustBeText:
    """Both become dictionary keys while the bundle's source ids are rewritten, so a
    list or dict raises an unhashable-type TypeError that no handler arm catches --
    a 500 in place of the typed 400. Enforced at the writer, like the properties and
    aliases columns, so a caller that is not the dashboard endpoint is safe too."""

    @pytest.mark.parametrize("bad", [[], {}, 7, None, ""])
    def test_a_source_id_that_is_not_text_is_refused(self, importer, bad):
        bundle = {
            "sources": [{"id": bad, "name": "A", "source_type": "local_file", "uri": "/a.md"}]
        }
        with pytest.raises(KnowledgeBundleError, match="sources.id"):
            importer.import_bundle(bundle)

    @pytest.mark.parametrize("bad", [[], {}, 7, None, ""])
    def test_a_source_uri_that_is_not_text_is_refused(self, importer, bad):
        bundle = {"sources": [{"id": "s1", "name": "A", "source_type": "local_file", "uri": bad}]}
        with pytest.raises(KnowledgeBundleError, match="sources.uri"):
            importer.import_bundle(bundle)

    @pytest.mark.parametrize("field", ["id", "uri"])
    def test_a_lone_surrogate_identity_is_a_typed_error(self, importer, field):
        """A lone surrogate is a `str` SQLite cannot encode, and the `UnicodeEncodeError`
        it raises at bind time is outside every arm the endpoint catches -- a 500 where
        the typed 400 belongs. Both identity values reach a bind, so both take the gate
        every other bundle string takes."""
        src = {"id": "s1", "name": "A", "source_type": "local_file", "uri": "/a.md"}
        src[field] = "bad\ud800value"
        with pytest.raises(KnowledgeBundleError, match=f"sources.{field}"):
            importer.import_bundle({"sources": [src]})

    @pytest.mark.parametrize("field", ["id", "uri"])
    def test_the_surrogate_refusal_names_utf8(self, importer, field):
        src = {"id": "s1", "name": "A", "source_type": "local_file", "uri": "/a.md"}
        src[field] = "bad\ud800value"
        with pytest.raises(KnowledgeBundleError, match="valid UTF-8"):
            importer.import_bundle({"sources": [src]})

    def test_a_lone_surrogate_created_at_is_a_typed_error(self, importer):
        """`created_at` is checked for type and emptiness, and a lone surrogate passes
        both: it is a non-empty `str`. It reaches the same bind as the identity values,
        so it takes the same encodability gate, or the `UnicodeEncodeError` escapes every
        arm the endpoint catches and answers 500 where the typed 400 belongs."""
        src = {
            "id": "s1",
            "name": "A",
            "source_type": "local_file",
            "uri": "/a.md",
            "created_at": "bad\ud800value",
        }
        with pytest.raises(KnowledgeBundleError, match="sources.created_at"):
            importer.import_bundle({"sources": [src]})

    def test_the_created_at_surrogate_refusal_names_utf8(self, importer):
        src = {
            "id": "s1",
            "name": "A",
            "source_type": "local_file",
            "uri": "/a.md",
            "created_at": "bad\ud800value",
        }
        with pytest.raises(KnowledgeBundleError, match="valid UTF-8"):
            importer.import_bundle({"sources": [src]})

    @pytest.mark.parametrize("field", ["name", "source_type"])
    def test_a_null_not_null_column_is_refused_not_dropped(self, importer, field):
        """The schema declares these NOT NULL. A suppressed insert is worse than a loud
        one: the row never lands, the uri stays absent, nothing maps this bundle's source
        id, and the map's id fallback then resolves the UNRELATED local source whose id
        happens to match -- filing the documents under it and granting ownership, with no
        error. That is the collision the remap already detects, arriving by the back
        door."""
        src = {"id": "s1", "name": "A", "source_type": "local_file", "uri": "/a.md"}
        src[field] = None
        with pytest.raises(KnowledgeBundleError, match=f"sources.{field}"):
            importer.import_bundle({"sources": [src]})

    def test_a_null_name_does_not_misfile_items_onto_a_colliding_local_source(self, importer):
        """The whole point: the items must not land on the unrelated local source."""
        local_sid = importer.add_source(
            name="Local", source_type="local_folder", uri="/local/other"
        )
        bundle = {
            "sources": [
                {"id": local_sid, "name": None, "source_type": "local_file", "uri": "/a.md"}
            ],
            "items": [
                {
                    "id": "i1",
                    "title": "T",
                    "content": "body",
                    "item_type": "document",
                    "source_id": local_sid,
                }
            ],
        }
        with pytest.raises(KnowledgeBundleError):
            importer.import_bundle(bundle)
        assert (
            importer.db.execute(
                "SELECT COUNT(*) AS n FROM items WHERE source_id = ?", (local_sid,)
            ).fetchone()["n"]
            == 0
        )

    def test_a_null_created_at_is_refused_not_dropped(self, importer):
        """`created_at` is the last NOT NULL column a bundle supplies: a MISSING key falls
        back to the import clock, but an explicit null does not. Validated so no
        bundle-supplied null can reach the constraint, which is what keeps the answer a
        typed rejection instead of a driver error the endpoint's arms do not name."""
        local_sid = importer.add_source(
            name="Local", source_type="local_folder", uri="/local/other"
        )
        bundle = {
            "sources": [
                {
                    "id": local_sid,
                    "name": "A",
                    "source_type": "local_file",
                    "uri": "/a.md",
                    "created_at": None,
                }
            ],
            "items": [
                {
                    "id": "i1",
                    "title": "T",
                    "content": "body",
                    "item_type": "document",
                    "source_id": local_sid,
                }
            ],
        }
        with pytest.raises(KnowledgeBundleError, match="sources.created_at"):
            importer.import_bundle(bundle)
        assert (
            importer.db.execute(
                "SELECT COUNT(*) AS n FROM items WHERE source_id = ?", (local_sid,)
            ).fetchone()["n"]
            == 0
        )

    def test_a_missing_created_at_still_defaults_to_the_import_clock(self, importer):
        """Absence is not nullity: an export that omits the field must still import."""
        bundle = {
            "sources": [{"id": "s1", "name": "A", "source_type": "local_file", "uri": "/a.md"}]
        }
        importer.import_bundle(bundle)
        row = importer.db.execute("SELECT created_at FROM sources WHERE uri = '/a.md'").fetchone()
        assert row["created_at"]

    def test_a_state_row_source_id_that_is_not_text_is_skipped_not_raised(self, exporter, importer):
        _owned_doc(exporter)
        bundle = exporter.export_all()
        bundle["agent_item_state"][0]["source_id"] = []

        result = importer.import_bundle(bundle)

        assert result["items_imported"] == 1
        assert result["ownership_rows_imported"] == 0


class TestMembershipIdentifiersMustBeText:
    """Every one of these is tested against a `set[str]` of withheld items. Set
    membership hashes the key, so a list or dict raises a `TypeError` no handler arm
    catches -- a 500 where the typed 400 belongs -- and a number hashes cleanly while
    matching nothing, which writes a withheld item's dependent row as though the item
    had arrived. Enforced at the writer, beside the gate on the source identity
    columns."""

    @pytest.mark.parametrize("bad", [[], {}, 7])
    def test_an_item_id_that_is_not_text_is_refused(self, importer, bad):
        with pytest.raises(KnowledgeBundleError, match="items.id"):
            importer.import_bundle({"items": [{"id": bad, "title": "T", "content": "c"}]})

    @pytest.mark.parametrize("bad", [[], {}, 7])
    def test_a_relation_source_item_id_that_is_not_text_is_refused(self, importer, bad):
        with pytest.raises(KnowledgeBundleError, match="relations.source_item_id"):
            importer.import_bundle(
                {
                    "relations": [
                        {
                            "id": "r1",
                            "source_id": "e1",
                            "target_id": "e2",
                            "relation_type": "rel",
                            "source_item_id": bad,
                        }
                    ]
                }
            )

    @pytest.mark.parametrize("bad", [[], {}, 7])
    def test_a_location_item_id_that_is_not_text_is_refused(self, importer, bad):
        with pytest.raises(KnowledgeBundleError, match="source_locations.item_id"):
            importer.import_bundle(
                {"source_locations": [{"id": "l1", "item_id": bad, "source_id": "s1"}]}
            )

    @pytest.mark.parametrize("bad", [[], {}, 7])
    def test_a_mention_item_id_that_is_not_text_is_refused(self, importer, bad):
        with pytest.raises(KnowledgeBundleError, match="mentions.item_id"):
            importer.import_bundle({"mentions": [{"entity_id": "e1", "item_id": bad}]})

    @pytest.mark.parametrize("bad", [[], {}, 7])
    def test_an_entity_id_that_is_not_text_is_refused(self, importer, bad):
        """`entities.id` reaches BOTH halves of the problem: the entity insert binds it,
        and the created-entities set holds it. The driver's own bind error is not an
        `sqlite3.Error` subclass here, so neither half is caught downstream."""
        with pytest.raises(KnowledgeBundleError, match="entities.id"):
            importer.import_bundle(
                {"entities": [{"id": bad, "name": "E", "entity_type": "concept"}]}
            )

    def test_a_repeated_item_id_is_refused(self, importer):
        """`item_source` is keyed on `items.id`, so a repeat overwrites the earlier row's
        mapped source -- and because the withheld set holds the ID rather than the row, a
        collision resolved against the surviving mapping skips BOTH rows while the import
        reports success. `items.id` is a PRIMARY KEY, so only one could ever land."""
        bundle = {
            "items": [
                {"id": "i1", "title": "A", "content": "a"},
                {"id": "i1", "title": "B", "content": "b"},
            ]
        }
        with pytest.raises(KnowledgeBundleError, match="items"):
            importer.import_bundle(bundle)

    def test_the_repeated_id_refusal_says_it_repeats(self, importer):
        bundle = {
            "items": [
                {"id": "i1", "title": "A", "content": "a"},
                {"id": "i1", "title": "B", "content": "b"},
            ]
        }
        with pytest.raises(KnowledgeBundleError, match="repeats an id"):
            importer.import_bundle(bundle)

    def test_distinct_item_ids_are_not_refused(self, importer):
        """The rule is about a REPEAT, so two ordinary items must still pass the gate."""
        importer._bundle_membership_ids({"items": [{"id": "i1"}, {"id": "i2"}, {"title": "no id"}]})

    def test_a_null_or_absent_source_item_id_passes_the_gate(self, importer):
        """A relation need not name a source item, and an absent key reads the same way,
        so neither may be refused. Exercised at the gate itself to keep the case free of
        the foreign-key checks a whole import would also apply."""
        importer._bundle_membership_ids({"relations": [{"source_item_id": None}, {}]})


class TestAnUndeclaredSourceReferenceIsRefused:
    """A bundle's source ids are local to the store that minted them, so the import
    resolves them through the `sources` entry's uri. A reference the bundle does not
    declare has no uri to resolve, leaving only the id -- and filing content under
    whichever local row happens to share an id is what the DECLARED path already
    refuses, where a bundle id held by a different uri gets a freshly minted one
    instead. The undeclared case cannot be repaired that way, because nothing names
    the source its content belongs to, so it is refused."""

    def test_item_naming_an_unknown_source_is_refused(self, importer):
        bundle = {
            "sources": [],
            "items": [
                {
                    "id": "i1",
                    "title": "T",
                    "content": "orphan text",
                    "item_type": "document",
                    "source_id": "no-such-source",
                }
            ],
        }
        with pytest.raises(KnowledgeBundleError, match="items.source_id"):
            importer.import_bundle(bundle)
        assert importer.db.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 0

    def test_location_naming_an_unknown_source_is_refused(self, importer):
        bundle = {
            "source_locations": [
                {"id": "loc1", "item_id": "no-such-item", "source_id": "no-such-source"}
            ]
        }
        with pytest.raises(KnowledgeBundleError, match="source_locations.source_id"):
            importer.import_bundle(bundle)

    def test_an_id_this_store_holds_is_refused_just_the_same(self, importer):
        """The case the foreign key cannot catch, and the only one that loses data. The
        id exists HERE, under a source the bundle never names, so the constraint is
        satisfied and the content would be filed under an unrelated local source with
        nothing naming it."""
        other = importer.add_source(name="Other", source_type="local_file", uri="/local/other.md")
        bundle = {
            "items": [
                {
                    "id": "i1",
                    "title": "T",
                    "content": "foreign text",
                    "item_type": "document",
                    "source_id": other,
                }
            ]
        }

        with pytest.raises(KnowledgeBundleError, match="items.source_id"):
            importer.import_bundle(bundle)

        assert (
            _item_count(importer, "foreign text") == 0
        ), "foreign content was filed under a local source the bundle never named"

    def test_a_declared_source_still_resolves(self, exporter, importer):
        """The control. The refusal must not touch a bundle that carries its sources,
        which is every bundle any exporter here produces."""
        _owned_doc(exporter, slug="doc", body="remote body", name="Remote")

        result = importer.import_bundle(exporter.export_all())

        assert result["items_imported"] == 1
        assert result["ownership_rows_imported"] == 1

    def test_item_with_no_source_at_all_still_imports(self, importer):
        """A null reference is not a reference: an item filed under no source is
        legitimate and must not be caught by the refusal."""
        bundle = {
            "items": [{"id": "i1", "title": "T", "content": "loose text", "item_type": "document"}]
        }
        assert importer.import_bundle(bundle)["items_imported"] == 1
        assert (
            importer.db.execute("SELECT source_id FROM items WHERE id = 'i1'").fetchone()[
                "source_id"
            ]
            is None
        )


class TestBundleValidation:
    """The new lists go through the same shape checks as the existing ones."""

    @pytest.mark.parametrize("table", sorted(BUNDLE_STATE_KEY_COL))
    def test_state_table_must_be_a_list(self, table):
        assert _validate_knowledge_bundle({table: {}}) == f"'{table}' must be a list"

    @pytest.mark.parametrize("table", sorted(BUNDLE_STATE_KEY_COL))
    def test_state_entries_must_be_objects(self, table):
        assert _validate_knowledge_bundle({table: ["nope"]}) == f"'{table}' entries must be objects"

    def test_key_column_must_be_text(self):
        assert (
            _validate_knowledge_bundle({"agent_item_state": [{"slug": 7}]})
            == "'agent_item_state.slug' must be a string or null"
        )

    def test_display_name_must_be_text(self):
        assert (
            _validate_knowledge_bundle({"agent_item_state": [{"name": 7}]})
            == "'agent_item_state.name' must be a string or null"
        )

    def test_mtime_shape_is_not_validated_because_it_does_not_travel(self):
        assert _validate_knowledge_bundle({"folder_file_state": [{"mtime": "soon"}]}) is None

    def test_numeric_mtime_and_absent_tables_pass(self):
        assert _validate_knowledge_bundle({"folder_file_state": [{"mtime": 1.5}]}) is None
        assert _validate_knowledge_bundle({"items": []}) is None


class TestAFolderRowCarriesNothingOntoThisHost:
    """A carried `mtime` is one of the things that makes the row dangerous: the scan
    skips hashing a live row whose recorded mtime is at or above the file's, so an
    exporting host's newer value stops the local file being hashed and leaves the
    imported text indexed in its place. Refusing the whole row settles that and the
    deletion order together."""

    def test_no_folder_row_reaches_the_database(self, exporter, importer):
        exporter.db.execute("UPDATE folder_file_state SET mtime = 99999999999.0")
        _folder_doc(exporter)
        exporter.db.commit()

        importer.import_bundle(exporter.export_all())

        assert (
            importer.db.execute("SELECT COUNT(*) AS n FROM folder_file_state").fetchone()["n"] == 0
        )

    def test_no_host_mtime_can_suppress_a_local_hash(self, exporter, importer):
        """With no row at all, every real mtime fails the scan's `mtime <= recorded`
        gate, so the local file is always hashed on its own terms."""
        _folder_doc(exporter)
        importer.import_bundle(exporter.export_all())
        rows = importer.db.execute("SELECT mtime FROM folder_file_state").fetchall()
        assert rows == []
        for real_mtime in (0.0, -1.0, -2208988800.0, 1.0, time.time()):
            assert real_mtime is not None  # nothing recorded to compare against

    def test_the_folder_items_arrive_unowned_not_missing(self, exporter, importer):
        """The state they are in when a bundle carries no state tables at all."""
        _, folder_item = _folder_doc(exporter)
        result = importer.import_bundle(exporter.export_all())
        assert result["items_imported"] == 1
        assert result["items_withheld"] == 0
        assert _owner_of(importer, "folder_file_state", folder_item) is None


class TestLocalRowWinsOnlyWhileItOwnsItems:
    """A local row with an empty or stale group holds a marker, not ownership, and
    nothing else will ever name the items the bundle brought."""

    def test_empty_local_group_yields_to_the_imported_row(self, exporter, importer):
        _, remote_item = _owned_doc(exporter, slug="doc", body="remote body", name="Remote")
        local_sid = importer.add_source(name="Auto-added", source_type="agent", uri=AGGREGATE_URI)
        # A document that lost a de-duplication: a live row with no group.
        importer.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, "
            "item_ids, updated_at, name, status) VALUES (?, 'doc', 'loser-hash', "
            "'[]', '2026-01-01T00:00:00', 'Local', 'deduped')",
            (local_sid,),
        )
        importer.db.commit()

        result = importer.import_bundle(exporter.export_all())

        assert result["ownership_rows_imported"] == 1
        assert _owner_of(importer, "agent_item_state", remote_item) == (local_sid, "doc")

    def test_stale_local_group_yields_to_the_imported_row(self, exporter, importer):
        _, remote_item = _owned_doc(exporter, slug="doc", body="remote body", name="Remote")
        local_sid = importer.add_source(name="Auto-added", source_type="agent", uri=AGGREGATE_URI)
        # A group naming only items that have since been deleted.
        importer.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, "
            "item_ids, updated_at, name, status) VALUES (?, 'doc', 'gone-hash', "
            "'[\"deleted-item\"]', '2026-01-01T00:00:00', 'Local', 'active')",
            (local_sid,),
        )
        importer.db.commit()

        result = importer.import_bundle(exporter.export_all())

        assert result["ownership_rows_imported"] == 1
        assert _owner_of(importer, "agent_item_state", remote_item) is not None

    def test_displaced_marker_releases_its_claim_on_another_source_items(self, exporter, importer):
        """Leaving the claim behind under a hash no row names means a later deletion
        of the holder reassigns an item here with nothing to adopt it into."""
        _owned_doc(exporter, slug="doc", body="remote body", name="Remote")
        winner = importer.add_source(
            name="Winner", source_type="local_file", uri="/local/winner.md"
        )
        held = importer.add_item(
            title="Winner",
            content="shared text",
            item_type="document",
            source_id=winner,
            content_hash="loser-hash",
        )
        local_sid = importer.add_source(name="Auto-added", source_type="agent", uri=AGGREGATE_URI)
        importer.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, "
            "item_ids, updated_at, name, status) VALUES (?, 'doc', 'loser-hash', "
            "'[]', '2026-01-01T00:00:00', 'Local', 'deduped')",
            (local_sid,),
        )
        importer.add_source_location(held, local_sid)
        importer.db.commit()
        assert (
            importer.db.execute(
                "SELECT COUNT(*) AS n FROM source_locations WHERE source_id = ?", (local_sid,)
            ).fetchone()["n"]
            == 1
        )

        importer.import_bundle(exporter.export_all())

        assert (
            importer.db.execute(
                "SELECT COUNT(*) AS n FROM source_locations WHERE source_id = ?", (local_sid,)
            ).fetchone()["n"]
            == 0
        )
        assert (
            importer.db.execute("SELECT COUNT(*) AS n FROM items WHERE id = ?", (held,)).fetchone()[
                "n"
            ]
            == 1
        ), "the winner's item must not be deleted"


class TestDuplicateSourceIdIsMalformed:
    """One id may name only one uri. ``sources.id`` is a PRIMARY KEY, so no export
    produces a repeat; accepting one would let the later entry overwrite the earlier
    one's place in the map and file items under a source never named for them."""

    def test_repeated_id_under_two_uris_is_refused(self, importer):
        bundle = {
            "sources": [
                {"id": "s1", "name": "A", "source_type": "local_file", "uri": "/a.md"},
                {"id": "s1", "name": "B", "source_type": "local_file", "uri": "/b.md"},
            ]
        }
        with pytest.raises(KnowledgeBundleError, match="repeats an id"):
            importer.import_bundle(bundle)
        assert importer.db.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"] == 0

    def test_repeated_id_under_the_same_uri_is_fine(self, importer):
        bundle = {
            "sources": [
                {"id": "s1", "name": "A", "source_type": "local_file", "uri": "/a.md"},
                {"id": "s1", "name": "A", "source_type": "local_file", "uri": "/a.md"},
            ]
        }
        importer.import_bundle(bundle)
        assert importer.db.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"] == 1


class TestLegacyBundle:
    """A bundle written without the state tables imports exactly as before."""

    def test_missing_state_tables_import_cleanly(self, exporter, importer):
        _owned_doc(exporter)
        bundle = exporter.export_all()
        for table in BUNDLE_STATE_KEY_COL:
            bundle.pop(table)
        result = importer.import_bundle(bundle)
        assert result["items_imported"] == 1
        assert result["ownership_rows_imported"] == 0

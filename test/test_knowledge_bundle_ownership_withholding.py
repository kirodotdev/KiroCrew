"""What a key or claim this store already holds withholds from a bundle, and why.

A live local row on the same key withholds another document's items, and a local
claim withholds a shipped id unless it is that document's own row; every withheld
document and unowned arrival is named in the import's report. No item is owned by
two rows or by a row that did not insert it, dependent rows follow the withheld
item, no ``IN`` list passes the bind ceiling, and a scoped export ships no other
namespace's state rows. Shared stores, builders and probes live in
``knowledge_bundle_ownership_helpers.py``.
"""

import json

from knowledge_bundle_ownership_helpers import (  # noqa: F401 -- fixtures are requested by name
    AGGREGATE_URI,
    _artifact_doc,
    _folder_doc,
    _group_of,
    _item_count,
    _owned_doc,
    _owner_of,
    exporter_fixture,
    importer_fixture,
)

from kiro_crew.knowledge import store as store_module
from kiro_crew.knowledge.store import BUNDLE_STATE_KEY_COL


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


class TestEveryUnownedArrivalIsAccountedFor:
    """Three accepting-pass row skips leave their items owned by nothing. Two of them
    name the ids this import wrote for the refused row -- an id another document claims,
    and an id filed under a different source -- so a caller can say WHICH content arrived
    unowned. The third, an empty or non-text document key, stays silent on purpose: it
    names no document key a caller could search by. Each item below is INSERTED (the
    content is here) yet owned by nothing, which is exactly what the account must flag.
    """

    def test_an_id_another_document_claims_is_named(self, exporter, importer):
        """Two bundle rows name the SAME imported id. The first row owns it; the second
        hits the already-claimed check and is refused whole, so the id it also named
        arrives owned by nothing and must be named in the account."""
        _, item_id = _owned_doc(exporter, slug="first", name="First")
        bundle = exporter.export_all()
        first = bundle["agent_item_state"][0]
        bundle["agent_item_state"].append({**first, "slug": "second", "name": "Second"})

        result = importer.import_bundle(bundle)

        # The item arrived; only the second row's ownership of it was refused.
        assert result["ownership_rows_imported"] == 1
        assert _item_count(importer, "artifact body") == 1
        named = [
            w
            for w in result["withheld"]
            if w.get("reason") == "ownership_row_item_claimed_elsewhere"
        ]
        assert named, f"the claimed-elsewhere skip was silent: {result['withheld']}"
        assert named[0]["key"] == "second"
        assert named[0]["table"] == "agent_item_state"
        assert item_id in named[0]["items"]

    def test_an_id_filed_under_a_different_source_is_named(self, exporter, importer):
        """A row names an id this import filed under a DIFFERENT source. The row is an
        inconsistent ownership statement and refused whole, so the ids it named that
        this import wrote arrive owned by nothing and must be named."""
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

        # Both items arrived; the row that claimed them across two sources did not.
        assert result["items_imported"] == 2
        assert result["ownership_rows_imported"] == 0
        named = [
            w for w in result["withheld"] if w.get("reason") == "ownership_row_item_source_mismatch"
        ]
        assert named, f"the source-mismatch skip was silent: {result['withheld']}"
        assert named[0]["key"] == "doc"
        assert named[0]["table"] == "agent_item_state"
        assert artifact_item in named[0]["items"]

    def test_an_empty_document_key_stays_silent(self, exporter, importer):
        """The row names no key a caller could look for, so a withheld entry would have
        nothing to key on. This skip is the one unowned-arrival path left silent."""
        _owned_doc(exporter, slug="doc", name="Doc")
        bundle = exporter.export_all()
        bundle["agent_item_state"][0]["slug"] = ""

        result = importer.import_bundle(bundle)

        assert result["ownership_rows_imported"] == 0
        assert result["withheld"] == [], f"an empty-key skip spoke up: {result['withheld']}"


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


class TestTheSameRowExemptionCoversEveryStatedTable:
    """The claimed-id rule's one exception is this document meeting its OWN row, keyed on
    the bundle's row at the same `(table, source, key)`. That holds for a folder file or
    artifact slug this host still has, even though no row of those tables is written:
    `_delete_item_cascade` never prunes a group, so a per-chunk delete leaves the row
    naming the chunk, and nothing re-derives an id withheld here -- `FolderWatcher._do_scan`
    step 4 reaps only a row whose path the walk MISSED, and step 5 skips a `done` row whose
    mtime and content hash did not change."""

    def test_a_folder_rows_own_group_exempts_the_chunk_it_names(self, exporter, importer):
        _, folder_item = _folder_doc(exporter)
        bundle = exporter.export_all()
        shipped = bundle["folder_file_state"][0]
        assert folder_item in json.loads(
            shipped["item_ids"]
        ), "fixture must ship the id the local row claims"
        # The deleter's own row: same (source, file_path) the bundle's row names, its group
        # still naming the chunk `DELETE /api/knowledge/items/{id}` removed.
        local_sid = importer.add_source(
            name="Notes", source_type="local_folder", uri="/remote/notes"
        )
        importer.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, content_hash, "
            "text_hash, mtime, item_ids, last_seen, status, attempts) "
            "VALUES (?, ?, 'raw-hash', 'text-hash', 123.5, ?, "
            "'2026-01-01T00:00:00', 'done', 0)",
            (local_sid, shipped["file_path"], json.dumps([folder_item])),
        )
        importer.db.commit()

        result = importer.import_bundle(bundle)

        assert result["items_withheld"] == 0, "the row's own chunk was withheld from it"
        assert _item_count(importer, "folder body") == 1, "the deleted chunk did not come back"

    def test_another_documents_claim_is_still_contested(self, exporter, importer):
        """The rule the exception is carved out of. A local row at a DIFFERENT key claiming
        the arriving id is not this document meeting itself, so the id stays contested --
        otherwise the insert makes that claim live over content it never owned."""
        _, folder_item = _folder_doc(exporter)
        bundle = exporter.export_all()
        shipped = bundle["folder_file_state"][0]
        local_sid = importer.add_source(
            name="Notes", source_type="local_folder", uri="/remote/notes"
        )
        importer.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, content_hash, "
            "text_hash, mtime, item_ids, last_seen, status, attempts) "
            "VALUES (?, ?, 'other-hash', 'other-text', 9.0, ?, "
            "'2026-01-01T00:00:00', 'done', 0)",
            (local_sid, shipped["file_path"] + ".other", json.dumps([folder_item])),
        )
        importer.db.commit()

        result = importer.import_bundle(bundle)

        assert result["items_withheld"] == 1
        assert _item_count(importer, "folder body") == 0, "a foreign claim captured the import"
        assert [
            w["item_id"]
            for w in result["withheld"]
            if w.get("reason") == "item_id_claimed_by_another_document"
        ] == [folder_item]

    def test_an_agent_row_still_exempts_the_document_it_restores(self, exporter, importer):
        """The restored table's half, unchanged: a row meeting its own stale group -- what
        `_delete_item_cascade` leaves behind -- must not withhold the only copy of the
        chunk it brings."""
        _, item_id = _owned_doc(exporter, slug="doc", body="saved body", name="Doc")
        local_sid = importer.add_source(name="Auto-added", source_type="agent", uri=AGGREGATE_URI)
        importer.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, item_ids, "
            "updated_at, name, status, source_uri) VALUES (?, 'doc', 'hash-doc', ?, "
            "'2026-01-01T00:00:00', 'Doc', 'active', 'https://x/y')",
            (local_sid, json.dumps([item_id])),
        )
        importer.db.commit()

        result = importer.import_bundle(exporter.export_all())

        assert result["items_withheld"] == 0, "the bundle's own row withheld its own item"
        assert _item_count(importer, "saved body") == 1
        assert _owner_of(importer, "agent_item_state", item_id) == (local_sid, "doc")


class _BindSpy:
    """Forwards to the real connection and records the widest bind count it saw."""

    def __init__(self, conn):
        self._conn = conn
        self.widest = 0

    def execute(self, sql, params=()):
        self.widest = max(self.widest, len(params))
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class TestNoBundleSizedInListPassesTheBindCeiling:
    """sqlite's bind limit is 32,766 on the bundled build but only 999 on a host still
    on a pre-3.32 library, and `vector_memory` already records that floor. A bundle
    names as many sources as it likes and one state row's group runs to
    `_MAX_ITEM_GROUP_MEMBERS` (4096), so an `IN` list built from either passes 999 on
    ordinary input -- and the `OperationalError` lands mid-transaction, rolling the
    whole import back and answering 500 where it should have succeeded. Every such list
    is chunked under `store._MAX_SQL_PARAMS`, the way `_items_exist` already was."""

    def test_the_chunk_helper_covers_its_input_within_the_ceiling(self):
        values = [f"id-{i:05d}" for i in range(1100)]

        chunks = list(store_module._sql_param_chunks(values))

        assert chunks, "the helper yielded nothing for a non-empty input"
        assert max(len(c) for c in chunks) <= store_module._MAX_SQL_PARAMS
        assert [v for c in chunks for v in c] == values, "the helper dropped or reordered ids"
        assert list(store_module._sql_param_chunks([])) == []

    def test_an_import_past_the_ceiling_binds_no_statement_past_it(self, exporter, importer):
        """Three axes at once: more sources than the ceiling, a group past it on the
        bundle's row AND on the live local row the accepting pass probes, and a SECOND
        document whose whole group arrives fresh -- the local row's group is the base the
        first document's arriving ids are measured against, so only a key with no local
        row makes `arriving` itself wider than the ceiling."""
        remote_sid, first = _owned_doc(exporter, slug="doc", body="remote body", name="Remote")
        ghosts = [f"ghost-{i:05d}" for i in range(1100)]
        fresh = [f"fresh-{i:05d}" for i in range(600)]
        for gid, body in [(g, "ghost") for g in ghosts] + [(f, "fresh") for f in fresh]:
            exporter.db.execute(
                "INSERT INTO items (id, title, content, item_type, source_id, "
                "created_at, updated_at) VALUES (?, 'Extra', ?, 'document', ?, "
                "'2026-01-01T00:00:00', '2026-01-01T00:00:00')",
                (gid, f"{body} body {gid}", remote_sid),
            )
        exporter.db.execute(
            "UPDATE agent_item_state SET item_ids = ? WHERE slug = 'doc'",
            (json.dumps([first, *ghosts]),),
        )
        exporter.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, item_ids, "
            "updated_at, name, status, source_uri) VALUES (?, 'fresh', 'hash-fresh', ?, "
            "'2026-01-01T00:00:00', 'Fresh', 'active', 'https://x/y')",
            (remote_sid, json.dumps(fresh)),
        )
        exporter.db.commit()
        bundle = exporter.export_all()
        bundle["sources"].extend(
            {
                "id": f"spare-{i:05d}",
                "name": f"Spare {i}",
                "source_type": "agent",
                "uri": f"agent://spare-{i:05d}",
            }
            for i in range(1100)
        )
        # A live local row on the 'doc' key whose own group is past the ceiling, so the
        # ownership probe and the claimed-id read both see a bundle-sized list.
        _, local_item = _owned_doc(importer, slug="doc", body="local body", name="Local")
        importer.db.execute(
            "UPDATE agent_item_state SET item_ids = ? WHERE slug = 'doc'",
            (json.dumps([local_item, *ghosts]),),
        )
        importer.db.commit()

        spy = _BindSpy(importer.db)
        importer._thread_local.conn = spy
        try:
            result = importer.import_bundle(bundle)
        finally:
            importer._thread_local.conn = spy._conn

        assert result["items_imported"] > len(fresh), "the fresh group did not arrive whole"
        assert _group_of(importer, "fresh") is not None, "the fresh document was not restored"
        assert spy.widest <= store_module._MAX_SQL_PARAMS, (
            f"one statement bound {spy.widest} parameters, over the "
            f"{store_module._MAX_SQL_PARAMS} ceiling"
        )


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

"""Knowledge bundles never carry or register a ``bedrock_kb`` source.

Split from ``test_knowledge_add_source.py`` (line cap); the store fixture and
app helpers stay there and are imported here.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import test_knowledge_add_source as _add_source_tests
from aiohttp.test_utils import TestClient, TestServer
from test_knowledge_add_source import _make_app

from kiro_crew.dashboard.handlers.knowledge import import_bundle, ingest_text
from kiro_crew.knowledge.store import KnowledgeStore


@pytest.fixture()
def store(tmp_path, opened):
    """A store closed on EVERY thread at teardown (``test/conftest.py``'s ``opened``)."""
    return opened(KnowledgeStore(str(tmp_path / "test.db")))


class TestBundlesNeverCarryBedrockSources:
    """The bundle routes are the OTHER write path into ``sources``, and
    ``/api/knowledge/import`` is open to every authenticated caller. A
    ``bedrock_kb`` row in a bundle would register a KB with no consent, no
    validation and no target check, against whatever grant the owner already
    holds -- the exact registration ``add_source`` gates to the owner. So the
    store refuses such rows at import and leaves them out of exports (a live
    account binding with no items is not knowledge). Found in review."""

    _ROW = {
        "id": "src-bedrock-1",
        "name": "kb",
        "source_type": "bedrock_kb",
        "uri": "bedrock-kb://us-east-1/ABCDEFGHIJ",
        # A bundle row is a serialized SELECT * row: properties travel as text.
        "properties": json.dumps(
            {"kb_ids": "ABCDEFGHIJ", "region": "us-east-1", "profile": "team-a"}
        ),
        "sync_status": "active",
    }

    @staticmethod
    def _bundle(*sources: dict) -> dict:
        return {
            "items": [],
            "entities": [],
            "relations": [],
            "sources": list(sources),
            "source_locations": [],
            "mentions": [],
        }

    @staticmethod
    def _bedrock_rows(store) -> int:
        return store.db.execute(
            "SELECT COUNT(*) FROM sources WHERE source_type = 'bedrock_kb'"
        ).fetchone()[0]

    @staticmethod
    def _app(store):
        # The owner-shaped app the add-source tests use, plus the import route
        # (the minimal app registers only the routes under test).
        app = _add_source_tests.TestAddSourceBedrockGrantRecheck._app(store, owner="owner-1")
        app.router.add_post("/api/knowledge/import", import_bundle)
        return app

    @pytest.mark.asyncio
    async def test_an_app_token_import_registers_no_bedrock_source(self, store, monkeypatch):
        from kiro_crew import aws_consent

        # A grant for the crafted row's exact target already exists: the state
        # of any deployment whose owner uses Bedrock, and the one in which an
        # imported row would start billing retrievals.
        monkeypatch.setattr(
            aws_consent, "is_granted", lambda service, *, profile, region: (True, "")
        )
        app = self._app(store)
        local = {
            "id": "src-local-1",
            "name": "notes",
            "source_type": "local_file",
            "uri": "/tmp/notes.md",
            "properties": "{}",
            "sync_status": "active",
        }
        async with TestClient(TestServer(app)) as client:
            # An app token passes the knowledge owner gate, so it is the
            # caller class that can post a bundle without being the owner.
            resp = await client.post(
                "/api/knowledge/import",
                json=self._bundle(self._ROW, local),
                headers={"X-Test-User": "owner-1", "X-Test-App": "some-app"},
            )
            assert resp.status == 200, await resp.text()
            result = await resp.json()
        # The refused row is counted (the SEL line carries the count), the
        # rest of the bundle lands, and nothing bedrock-shaped exists to search.
        assert result["sources_refused"] == 1
        assert self._bedrock_rows(store) == 0
        assert (
            store.db.execute("SELECT COUNT(*) FROM sources WHERE id = 'src-local-1'").fetchone()[0]
            == 1
        )

    @pytest.mark.asyncio
    async def test_the_owner_is_refused_the_same_way(self, store, monkeypatch):
        """Not a caller check: the bundle is the wrong path for an account
        binding whoever posts it -- the owner registers a KB through the
        add-source form, where validation and the target check run."""
        from kiro_crew import aws_consent

        monkeypatch.setattr(
            aws_consent, "is_granted", lambda service, *, profile, region: (True, "")
        )
        app = self._app(store)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/knowledge/import",
                json=self._bundle(self._ROW),
                headers={"X-Test-User": "owner-1"},
            )
            assert resp.status == 200, await resp.text()
            assert (await resp.json())["sources_refused"] == 1
        assert self._bedrock_rows(store) == 0

    def test_export_leaves_a_registered_bedrock_source_out(self, store):
        store.add_source(
            name="kb",
            source_type="bedrock_kb",
            uri=self._ROW["uri"],
            properties=json.loads(self._ROW["properties"]),
        )
        store.add_source(name="notes", source_type="local_file", uri="/tmp/notes.md", properties={})
        assert self._bedrock_rows(store) == 1
        exported = store.export_all()["sources"]
        assert [s["source_type"] for s in exported] == ["local_file"]
        # And a round trip of that export changes nothing about the binding:
        # the row stays exactly as the owner registered it, refused count 0.
        assert store.import_bundle(self._bundle(*exported))["sources_refused"] == 0
        assert self._bedrock_rows(store) == 1

    def test_an_account_bound_uri_is_refused_under_any_type(self, store):
        """The uri is the UNIQUE handle the owner's add dedupes on, so a row of
        another type carrying it would squat the KB, and the owner's add would
        then refuse as ``source_exists`` over an unrelated row. Found in review."""
        squat = {**self._ROW, "id": "src-squat-1", "source_type": "local_folder"}
        assert store.import_bundle(self._bundle(squat))["sources_refused"] == 1
        assert store.get_source_by_uri(self._ROW["uri"]) is None

    def test_no_item_can_come_to_name_a_bedrock_source(self, store, tmp_path):
        """'Holds no items' is enforced at the writer, not asserted: the export
        drops the row, so an item naming it would be a dangling reference that
        rolls a CLEAN store's import back on the FK. Both ways an item comes to
        name a source are refused -- the insert, and a location attaching an
        existing item (the dedup path). Found in review."""
        from kiro_crew.knowledge.store import LiveSourceHoldsNoItems

        bedrock_id = store.add_source(
            name="kb",
            source_type="bedrock_kb",
            uri=self._ROW["uri"],
            properties=json.loads(self._ROW["properties"]),
        )
        local_id = store.add_source(
            name="notes", source_type="local_file", uri="/tmp/notes.md", properties={}
        )
        with pytest.raises(LiveSourceHoldsNoItems):
            store.add_item(
                title="planted", content="local text", item_type="note", source_id=bedrock_id
            )
        item_id = store.add_item(
            title="fine", content="local text", item_type="note", source_id=local_id
        )
        with pytest.raises(LiveSourceHoldsNoItems):
            store.add_source_location(item_id, bedrock_id)
        # The refused writes left nothing behind, and the store still writes.
        assert self._items_under(store, bedrock_id) == 0
        assert self._items_under(store, local_id) == 1
        assert (
            store.db.execute(
                "SELECT COUNT(*) FROM source_locations WHERE source_id = ?", (bedrock_id,)
            ).fetchone()[0]
            == 0
        )
        # The export of this store names the dropped row nowhere, so a clean
        # store -- one that never saw the bedrock row -- imports it whole.
        bundle = store.export_all()
        assert bedrock_id not in {i["source_id"] for i in bundle["items"]}
        assert bedrock_id not in {loc["source_id"] for loc in bundle["source_locations"]}
        clean = KnowledgeStore(str(tmp_path / "clean.db"))
        try:
            result = clean.import_bundle(bundle)
        finally:
            clean.close()
        assert result["items_imported"] == 1
        assert result["items_refused"] == 0
        assert result["sources_refused"] == 0

    def test_export_leaves_out_what_a_direct_write_planted_under_a_bedrock_source(
        self, store, tmp_path
    ):
        """The gateway's writers cannot put an item under a live source, but
        knowledge.db is agent-writable in-sandbox, so a row planted by a
        direct write bypasses them. The export drops the source row, so such
        an item -- and the location, mention and relation riding on it -- would
        ride out dangling and roll a clean store's import back on the FK. They
        are left out, the same set the import refuses. Found in review."""
        bedrock_id = store.add_source(
            name="kb",
            source_type="bedrock_kb",
            uri=self._ROW["uri"],
            properties=json.loads(self._ROW["properties"]),
        )
        local_id = store.add_source(
            name="notes", source_type="local_file", uri="/tmp/notes.md", properties={}
        )
        kept = store.add_item(
            title="fine", content="local text", item_type="note", source_id=local_id
        )
        now = "2026-09-22T00:00:00"
        db = store.db
        db.execute(
            "INSERT INTO items (id, title, content, item_type, source_id, created_at, updated_at) "
            "VALUES ('planted', 'planted', 'local text', 'note', ?, ?, ?)",
            (bedrock_id, now, now),
        )
        db.execute(
            "INSERT INTO entities (id, name, entity_type, created_at, updated_at) "
            "VALUES ('ent-1', 'Thing', 'concept', ?, ?)",
            (now, now),
        )
        db.execute(
            "INSERT INTO entity_relations (id, source_id, target_id, relation_type, "
            "source_item_id, created_at) VALUES ('rel-planted', 'ent-1', 'ent-1', 'self', "
            "'planted', ?)",
            (now,),
        )
        db.execute(
            "INSERT INTO entity_relations (id, source_id, target_id, relation_type, "
            "source_item_id, created_at) VALUES ('rel-kept', 'ent-1', 'ent-1', 'self', ?, ?)",
            (kept, now),
        )
        db.execute(
            "INSERT INTO source_locations (id, item_id, source_id, created_at) "
            "VALUES ('loc-planted', 'planted', ?, ?)",
            (bedrock_id, now),
        )
        db.execute(
            "INSERT INTO mentions (item_id, entity_id, created_at) "
            "VALUES ('planted', 'ent-1', ?)",
            (now,),
        )
        db.commit()
        bundle = store.export_all()
        assert {i["id"] for i in bundle["items"]} == {kept}
        assert {r["id"] for r in bundle["relations"]} == {"rel-kept"}
        assert bundle["source_locations"] == []
        assert bundle["mentions"] == []
        assert [s["source_type"] for s in bundle["sources"]] == ["local_file"]
        clean = KnowledgeStore(str(tmp_path / "clean.db"))
        try:
            result = clean.import_bundle(bundle)
        finally:
            clean.close()
        assert result["items_imported"] == 1
        assert result["relations_rebuilt"] == 1
        assert result["items_refused"] == 0

    def test_a_planted_bedrock_holder_never_becomes_an_item_owner(self, store, tmp_path):
        """Deleting a source hands each surviving item to another holder. A
        ``source_locations`` row a direct write planted under a live source must
        not make that source the new owner: the item would survive live and be
        left out of every bundle, so the next restore loses it (review finding).
        On both deletion paths the new owner comes from the holders that may
        take ownership; an item whose only other holder is the live source goes
        with its owner, and the planted row goes with it."""
        bedrock_id = store.add_source(
            name="kb",
            source_type="bedrock_kb",
            uri=self._ROW["uri"],
            properties=json.loads(self._ROW["properties"]),
        )
        local_id = store.add_source(
            name="notes", source_type="local_file", uri="/tmp/notes.md", properties={}
        )
        other_id = store.add_source(
            name="more", source_type="local_file", uri="/tmp/more.md", properties={}
        )
        shared = store.add_item(
            title="shared", content="shared text", item_type="note", source_id=local_id
        )
        lonely = store.add_item(
            title="lonely", content="lonely text", item_type="note", source_id=local_id
        )
        now = "2026-09-24T00:00:00"
        db = store.db
        # The planted rows first, so an unfiltered "first other holder" would be
        # the live source.
        db.execute(
            "INSERT INTO source_locations (id, item_id, source_id, created_at) "
            "VALUES ('loc-shared-kb', ?, ?, ?)",
            (shared, bedrock_id, now),
        )
        db.execute(
            "INSERT INTO source_locations (id, item_id, source_id, created_at) "
            "VALUES ('loc-lonely-kb', ?, ?, ?)",
            (lonely, bedrock_id, now),
        )
        db.execute(
            "INSERT INTO source_locations (id, item_id, source_id, created_at) "
            "VALUES ('loc-shared-other', ?, ?, ?)",
            (shared, other_id, now),
        )
        db.commit()

        store.delete_source_cascade(local_id)
        assert (
            db.execute("SELECT source_id FROM items WHERE id = ?", (shared,)).fetchone()[
                "source_id"
            ]
            == other_id
        )
        assert db.execute("SELECT COUNT(*) FROM items WHERE id = ?", (lonely,)).fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT COUNT(*) FROM source_locations WHERE item_id = ?", (lonely,)
            ).fetchone()[0]
            == 0
        )

        # The item-level path (a source dropping its copy of a document).
        lonely2 = store.add_item(
            title="lonely2", content="lonely text 2", item_type="note", source_id=other_id
        )
        db.execute(
            "INSERT INTO source_locations (id, item_id, source_id, created_at) "
            "VALUES ('loc-lonely2-kb', ?, ?, ?)",
            (lonely2, bedrock_id, now),
        )
        db.commit()
        store.delete_items_batch([lonely2], owner_source_id=other_id)
        assert db.execute("SELECT COUNT(*) FROM items WHERE id = ?", (lonely2,)).fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT COUNT(*) FROM source_locations WHERE item_id = ?", (lonely2,)
            ).fetchone()[0]
            == 0
        )

        bundle = store.export_all()
        assert {i["id"] for i in bundle["items"]} == {shared}
        # The deleted owner is gone and the live source is left out: one source.
        assert [s["id"] for s in bundle["sources"]] == [other_id]
        clean = KnowledgeStore(str(tmp_path / "clean.db"))
        try:
            result = clean.import_bundle(bundle)
        finally:
            clean.close()
        assert result["items_imported"] == 1
        assert result["items_refused"] == 0

    @pytest.mark.asyncio
    async def test_ingest_text_under_a_bedrock_source_is_refused_before_the_pipeline_runs(
        self, store
    ):
        """The agent's ingest-text route takes any existing source id, and a
        bedrock id is readable off the sources list. The refusal is a coded
        409 at the route, before the body is read, so the caller learns why
        instead of getting the store's refusal as a 500 after extraction."""
        bedrock_id = store.add_source(
            name="kb",
            source_type="bedrock_kb",
            uri=self._ROW["uri"],
            properties=json.loads(self._ROW["properties"]),
        )
        pipeline = MagicMock()
        pipeline.ingest_file = AsyncMock(return_value="job-1")
        app = _make_app(store, pipeline=pipeline)
        app.router.add_post("/api/knowledge/sources/{id}/ingest-text", ingest_text)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                f"/api/knowledge/sources/{bedrock_id}/ingest-text",
                json={"text": "local text", "name": "planted"},
            )
            assert resp.status == 409, await resp.text()
            assert (await resp.json())["code"] == "live_source_holds_no_items"
        pipeline.ingest_file.assert_not_called()
        assert self._items_under(store, bedrock_id) == 0

    @staticmethod
    def _item(item_id: str, source_id: str) -> dict:
        return {
            "id": item_id,
            "title": "planted",
            "content": "local text",
            "item_type": "note",
            "source_id": source_id,
            "chunk_index": 0,
            "namespace": "default",
            "summary": None,
            "tags": "[]",
            "embedding": None,
            "embedding_sig": None,
            "status": "active",
        }

    @staticmethod
    def _items_under(store, source_id: str) -> int:
        return store.db.execute(
            "SELECT COUNT(*) FROM items WHERE source_id = ?", (source_id,)
        ).fetchone()[0]

    def test_an_item_naming_a_registered_bedrock_source_is_refused(self, store):
        """A bedrock source holds no items -- it is queried live -- and its id
        and uri are readable off the sources list. A bundle names its sources
        by its own ids and the import files them by uri, so two shapes aim an
        item at the registered row. A bundle row that CLAIMS the registered
        row's uri under a local type is filed under that row, so its items and
        locations are refused and counted while the rest lands. A bundle that
        names the registered id without carrying a row for it is malformed
        like any undeclared reference, so nothing lands. Found in review."""
        from kiro_crew.knowledge.store import KnowledgeBundleError

        bedrock_id = store.add_source(
            name="kb",
            source_type="bedrock_kb",
            uri=self._ROW["uri"],
            properties=json.loads(self._ROW["properties"]),
        )
        local = {
            "id": "src-local-1",
            "name": "notes",
            "source_type": "local_file",
            "uri": "/tmp/notes.md",
            "properties": "{}",
            "sync_status": "active",
        }
        disguised = {
            "id": "src-disguised",
            "name": "kb",
            "source_type": "local_file",
            "uri": self._ROW["uri"],
            "properties": "{}",
            "sync_status": "active",
        }
        bundle = self._bundle(local, disguised)
        bundle["items"] = [
            self._item("item-planted", "src-disguised"),
            self._item("item-fine", "src-local-1"),
        ]
        bundle["source_locations"] = [
            {
                "id": "loc-1",
                "item_id": "item-fine",
                "source_id": "src-disguised",
                "chunk_range": None,
                "section_title": None,
                "anchor": None,
            }
        ]
        result = store.import_bundle(bundle)
        # The planted item is refused and counted; the rest of the bundle lands.
        assert result["items_refused"] == 1
        assert result["items_imported"] == 1
        assert self._bedrock_rows(store) == 1
        assert self._items_under(store, bedrock_id) == 0
        assert self._items_under(store, "src-local-1") == 1
        assert (
            store.db.execute(
                "SELECT COUNT(*) FROM source_locations WHERE source_id = ?", (bedrock_id,)
            ).fetchone()[0]
            == 0
        )

        undeclared = self._bundle(local)
        undeclared["items"] = [self._item("item-planted-2", bedrock_id)]
        with pytest.raises(KnowledgeBundleError):
            store.import_bundle(undeclared)
        assert self._items_under(store, bedrock_id) == 0

    def test_items_riding_on_a_refused_bundle_row_are_refused_with_it(self, store):
        """The refused row's dependents -- items, their locations and mentions,
        relations they evidence -- are dropped with the row instead of failing
        the whole bundle. An entity only those dependents referenced is
        stranded like one only withheld items referenced, so it does not
        land; a standalone entity does, which shows the bundle committed."""
        bundle = self._bundle(self._ROW)
        bundle["items"] = [self._item("item-planted", self._ROW["id"])]
        bundle["entities"] = [
            {
                "id": "ent-1",
                "name": "Thing",
                "entity_type": "concept",
                "description": None,
                "aliases": "[]",
            },
            {
                "id": "ent-2",
                "name": "Other",
                "entity_type": "concept",
                "description": None,
                "aliases": "[]",
            },
        ]
        bundle["relations"] = [
            {
                "id": "rel-1",
                "source_id": "ent-1",
                "target_id": "ent-1",
                "relation_type": "self",
                "description": None,
                "weight": 1.0,
                "source_item_id": "item-planted",
            }
        ]
        bundle["source_locations"] = [
            {
                "id": "loc-1",
                "item_id": "item-planted",
                "source_id": self._ROW["id"],
                "chunk_range": None,
                "section_title": None,
                "anchor": None,
            }
        ]
        bundle["mentions"] = [{"item_id": "item-planted", "entity_id": "ent-1", "context": None}]
        result = store.import_bundle(bundle)
        assert result["sources_refused"] == 1
        assert result["items_refused"] == 1
        assert result["items_imported"] == 0
        assert result["relations_rebuilt"] == 0
        # The bundle still committed: the standalone entity landed.
        assert result["entities_created"] == 1
        assert [r[0] for r in store.db.execute("SELECT id FROM entities")] == ["ent-2"]
        assert self._bedrock_rows(store) == 0
        assert self._items_under(store, self._ROW["id"]) == 0
        assert store.db.execute("SELECT COUNT(*) FROM mentions").fetchone()[0] == 0

    def test_numeric_ids_in_a_crafted_bundle_are_refused_the_same_way(self, store):
        """The validator does not type ids, so a crafted bundle may carry a
        number where the exporter wrote a string; the membership checks compare
        as text on both sides, else the item slips past to the FK and rolls the
        whole bundle back. Found in review."""
        row = {**self._ROW, "id": 4242}
        bundle = self._bundle(row)
        bundle["items"] = [self._item("item-planted", 4242)]
        bundle["source_locations"] = [
            {
                "id": "loc-1",
                "item_id": "item-planted",
                "source_id": 4242,
                "chunk_range": None,
                "section_title": None,
                "anchor": None,
            }
        ]
        result = store.import_bundle(bundle)
        assert result["sources_refused"] == 1
        assert result["items_refused"] == 1
        assert result["items_imported"] == 0
        assert self._bedrock_rows(store) == 0

    def test_a_refused_item_named_none_drops_no_id_less_dependents(self, store):
        """``str(None)`` is the text "None", which a bundle can also spell as
        an item id. A refused item with no id (or one literally called "None")
        must not take down a relation that names no evidencing item: that
        relation is evidence-free, not evidence of a refused item, and was
        imported before the refusal code existed. Found in review."""
        bundle = self._bundle(self._ROW)
        id_less = {**self._item("unused", self._ROW["id"])}
        del id_less["id"]
        bundle["items"] = [id_less, self._item("None", self._ROW["id"])]
        bundle["entities"] = [
            {
                "id": "ent-1",
                "name": "Thing",
                "entity_type": "concept",
                "description": None,
                "aliases": "[]",
            }
        ]
        bundle["relations"] = [
            {
                "id": "rel-free",
                "source_id": "ent-1",
                "target_id": "ent-1",
                "relation_type": "self",
                "description": None,
                "weight": 1.0,
                "source_item_id": None,
            },
            {
                "id": "rel-planted",
                "source_id": "ent-1",
                "target_id": "ent-1",
                "relation_type": "self",
                "description": None,
                "weight": 1.0,
                "source_item_id": "None",
            },
        ]
        result = store.import_bundle(bundle)
        assert result["sources_refused"] == 1
        assert result["items_refused"] == 2
        assert result["items_imported"] == 0
        # The evidence-free relation landed; the one evidenced by the refused
        # item called "None" did not.
        assert result["relations_rebuilt"] == 1
        rows = store.db.execute("SELECT id FROM entity_relations").fetchall()
        assert [row[0] for row in rows] == ["rel-free"]

    def test_an_id_less_refused_row_does_not_refuse_a_source_called_none(self, store):
        """The source-side twin: a refused ``bedrock_kb`` row with no id must
        not put the text "None" in the refused set, or a LOCAL source that a
        bundle happens to call "None" loses every item under it. Found in
        review."""
        id_less = {k: v for k, v in self._ROW.items() if k != "id"}
        local = {
            "id": "None",
            "name": "notes",
            "source_type": "local_file",
            "uri": "/tmp/notes.md",
            "properties": "{}",
            "sync_status": "active",
        }
        bundle = self._bundle(id_less, local)
        bundle["items"] = [self._item("item-fine", "None")]
        result = store.import_bundle(bundle)
        assert result["sources_refused"] == 1
        assert result["items_refused"] == 0
        assert result["items_imported"] == 1
        assert self._items_under(store, "None") == 1
        assert self._bedrock_rows(store) == 0

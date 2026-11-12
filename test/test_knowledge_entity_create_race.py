"""Two ingests that miss the same new entity name end with one entity row."""

from unittest.mock import MagicMock

from kiro_crew.knowledge import ingestion
from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.store import KnowledgeStore


def test_an_entity_added_after_the_lookup_missed_is_reused(tmp_path, monkeypatch) -> None:
    db_path = str(tmp_path / "knowledge.db")
    store = KnowledgeStore(db_path)
    other = KnowledgeStore(db_path)  # the concurrent ingest's connection
    try:
        source_id = store.add_source(name="s", source_type="manual", uri="manual://s")
        item_id = store.add_item(
            title="t", content="body", item_type="document", source_id=source_id
        )
        pipeline = IngestionPipeline(
            store=store,
            extractor=MagicMock(),
            chunker=MagicMock(),
            reader=FileReader(),
            embedder=None,
        )
        real_resolve = ingestion.resolve_entity

        def _miss_then_other_adds(*args, **kwargs):
            # The unlocked lookup misses; the other ingest adds the same name
            # before this one gets to its own add.
            found = real_resolve(*args, **kwargs)
            assert found == (None, 0)
            other.add_entity(name="Payments API", entity_type="service")
            return found

        monkeypatch.setattr(ingestion, "resolve_entity", _miss_then_other_adds)
        pipeline._store_entities(
            {"entities": [{"name": "Payments API", "type": "service"}]}, item_id
        )

        rows = store.db.execute(
            "SELECT id FROM entities WHERE name = ?", ("Payments API",)
        ).fetchall()
        assert len(rows) == 1, "the second ingest must reuse the row the first one added"
        mentioned = store.db.execute(
            "SELECT entity_id FROM mentions WHERE item_id = ?", (item_id,)
        ).fetchall()
        assert [r["entity_id"] for r in mentioned] == [rows[0]["id"]]
    finally:
        other.close()
        store.close()

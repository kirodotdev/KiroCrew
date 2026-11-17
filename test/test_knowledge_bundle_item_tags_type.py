"""A bundle item whose text columns are not strings imports instead of crashing."""

from __future__ import annotations

import pytest

from kiro_crew.knowledge.store import KnowledgeStore


@pytest.mark.parametrize(
    "field,value",
    [("tags", 5), ("tags", True), ("tags", 1.5), ("title", 7), ("content", 42)],
)
def test_a_non_string_text_column_imports_and_is_searchable(tmp_path, field, value) -> None:
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        # The current index format (what every store has once the post-boot
        # maintenance or the first search has run): it segments each column.
        store.ensure_fts_index_current()
        item = {
            "id": "i-1",
            "title": "zebracanary",
            "content": "zebracanary body",
            "item_type": "note",
            "tags": "[]",
        }
        item[field] = value
        result = store.import_bundle({"items": [item]})
        assert result["items_imported"] == 1
        row = store.db.execute(f"SELECT {field} FROM items WHERE id = 'i-1'").fetchone()
        assert isinstance(row[field], str)
        term = "zebracanary" if field != "title" else "body"
        assert [r["id"] for r in store.search_items_fts(term)] == ["i-1"]
    finally:
        store.close()

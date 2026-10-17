"""The stores, document builders and ownership probes the bundle ownership tests share.

``test_knowledge_bundle_ownership.py`` and its ``_withholding`` and ``_validation``
siblings import these by name: the ``exporter`` and ``importer`` stores (fixtures declared
``name="exporter"`` and ``name="importer"`` on ``exporter_fixture`` and
``importer_fixture``, so no test parameter shadows the import), ``AGGREGATE_URI``, the
``_owned_doc``, ``_artifact_doc`` and ``_folder_doc`` builders of one owned document each,
and the ``_owner_of``, ``_item_count`` and ``_group_of`` probes of what a store holds.
"""

import json
from pathlib import Path

import pytest

from kiro_crew.knowledge.store import BUNDLE_STATE_KEY_COL, KnowledgeStore

#: The aggregate source these pins use. It is the AGENT aggregate because that is the
#: one table a bundle restores: nothing reaps agent state by absence, so a restored row
#: is a claim rather than an order to delete the items the import just brought.
AGGREGATE_URI = "agent://"


@pytest.fixture(name="exporter")
def exporter_fixture(tmp_path):
    s = KnowledgeStore(str(tmp_path / "exporter.db"))
    yield s
    s.close()


@pytest.fixture(name="importer")
def importer_fixture(tmp_path):
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


def _group_of(store, slug="doc"):
    row = store.db.execute(
        "SELECT item_ids FROM agent_item_state WHERE slug = ?", (slug,)
    ).fetchone()
    return set(json.loads(row["item_ids"] or "[]")) if row else None

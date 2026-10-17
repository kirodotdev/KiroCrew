"""A `folder_file_state` row does not travel through a bundle.

The row is keyed on a `file_path`, which does not mean the same thing on two hosts. The
folder scan decides what that path means by walking the MAPPED source's own live
filters -- extension allowlist, `min_file_bytes`, `ignore_patterns`, and a root
`.kiroignore` it re-reads every sweep -- and then deletes every state row whose path the
walk did not yield, WITHOUT consulting `status`. A receiving store that filters the same
folder more narrowly than the sender is the ordinary case, so a restored row there is
not a stale marker: it is an order to delete the items the import just brought. The
sweep drops the row too, so a re-import re-arms it.

Checking eligibility at import time does not fix this. The walk's answer changes after
the import whenever the user edits `.kiroignore` or narrows the source's properties, so
no check made here stays true; and making the check exact would mean a second copy of
the walk's per-file rules, which is the same defect one divergence later.

The items still arrive. They arrive unowned -- the state they are in when a bundle
carries no state tables at all -- and the local scan hashes the file and writes its own
row on the next sweep. The aggregate tables are keyed on a slug, which is
host-independent, so they do travel.
"""

import json

import pytest

from kiro_crew.knowledge.store import KnowledgeStore

ARTIFACT_URI = "artifact://"


@pytest.fixture()
def exporter(tmp_path):
    store = KnowledgeStore(str(tmp_path / "exporter.db"))
    yield store
    store.close()


@pytest.fixture()
def importer(tmp_path):
    store = KnowledgeStore(str(tmp_path / "importer.db"))
    yield store
    store.close()


def _folder_row(store, *, file_path, uri="/watched/notes", body="folder body"):
    """An owned folder document whose row names *file_path*, present or not."""
    sid = store.add_source(name="Notes", source_type="local_folder", uri=uri)
    item_id = store.add_item(title="a.md", content=body, item_type="document", source_id=sid)
    store.db.execute(
        "INSERT INTO folder_file_state (source_id, file_path, content_hash, "
        "text_hash, mtime, item_ids, last_seen, status, attempts) "
        "VALUES (?, ?, 'raw-hash', 'text-hash', 123.5, ?, ?, 'done', 0)",
        (sid, str(file_path), json.dumps([item_id]), "2026-01-01T00:00:00"),
    )
    store.db.commit()
    return sid, item_id


def _real_file(tmp_path, body="folder body"):
    path = tmp_path / "watched" / "a.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _rows(store, table):
    return store.db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]  # noqa: S608


class TestTheRowIsRefusedWhateverTheHostHolds:
    def test_an_absent_file_gets_no_row(self, exporter, importer):
        _folder_row(exporter, file_path="/nonexistent/host/path/a.md")
        result = importer.import_bundle(exporter.export_all())
        assert result["ownership_rows_imported"] == 0
        assert _rows(importer, "folder_file_state") == 0

    def test_a_file_that_really_is_here_gets_no_row_either(self, exporter, importer, tmp_path):
        """Existence is not eligibility, and eligibility is not stable."""
        _folder_row(exporter, file_path=_real_file(tmp_path))
        result = importer.import_bundle(exporter.export_all())
        assert result["ownership_rows_imported"] == 0
        assert _rows(importer, "folder_file_state") == 0

    def test_a_directory_at_the_path_gets_no_row(self, exporter, importer, tmp_path):
        """A directory exists and is never a document the walk yields."""
        d = tmp_path / "watched"
        d.mkdir(parents=True, exist_ok=True)
        _folder_row(exporter, file_path=d)
        assert importer.import_bundle(exporter.export_all())["ownership_rows_imported"] == 0


class TestTheItemsStillArrive:
    def test_the_content_is_imported_unowned(self, exporter, importer):
        """Dropping the items too would lose content the receiving store has no other
        copy of. Unowned is the state a stateless bundle already leaves them in."""
        _, item_id = _folder_row(exporter, file_path="/nonexistent/host/path/a.md")
        result = importer.import_bundle(exporter.export_all())
        assert result["items_imported"] == 1
        assert result["items_withheld"] == 0
        got = importer.db.execute("SELECT title FROM items WHERE id = ?", (item_id,)).fetchone()
        assert got["title"] == "a.md"

    def test_a_folder_document_this_store_holds_withholds_its_items(
        self, exporter, importer, tmp_path
    ):
        """Withholding answers "this store already holds that document", which a folder
        path answers for itself -- so it does not depend on whether the row travels.
        Letting the items in would leave identical text answering searches beside the
        local copy, owned by nothing and removed by nothing."""
        _, item_id = _folder_row(exporter, file_path=_real_file(tmp_path))
        local_sid = importer.add_source(
            name="Notes", source_type="local_folder", uri="/watched/notes"
        )
        local_item = importer.add_item(
            title="local a.md", content="local body", item_type="document", source_id=local_sid
        )
        importer.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, content_hash, "
            "text_hash, mtime, item_ids, last_seen, status, attempts) "
            "VALUES (?, ?, 'local-hash', 'local-text', 500.0, ?, ?, 'done', 0)",
            (local_sid, str(_real_file(tmp_path)), json.dumps([local_item]), "2026-02-02T00:00:00"),
        )
        importer.db.commit()

        result = importer.import_bundle(exporter.export_all())

        assert result["items_withheld"] == 1
        assert result["items_imported"] == 0
        assert (
            importer.db.execute(
                "SELECT COUNT(*) AS n FROM items WHERE id = ?", (item_id,)
            ).fetchone()["n"]
            == 0
        ), "a duplicate of a document this store already holds arrived"
        assert (
            importer.db.execute(
                "SELECT COUNT(*) AS n FROM items WHERE id = ?", (local_item,)
            ).fetchone()["n"]
            == 1
        ), "the local copy was disturbed"

    def test_the_local_row_is_left_exactly_as_it_was(self, exporter, importer, tmp_path):
        """The import has no opinion about a folder row, so it does not touch one."""
        _folder_row(exporter, file_path=_real_file(tmp_path))
        local_sid = importer.add_source(
            name="Notes", source_type="local_folder", uri="/watched/notes"
        )
        importer.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, content_hash, "
            "text_hash, mtime, item_ids, last_seen, status, attempts) "
            "VALUES (?, ?, 'local-hash', 'local-text', 500.0, '[]', ?, 'done', 0)",
            (local_sid, str(_real_file(tmp_path)), "2026-02-02T00:00:00"),
        )
        importer.db.commit()

        importer.import_bundle(exporter.export_all())

        row = importer.db.execute(
            "SELECT content_hash, mtime, last_seen FROM folder_file_state"
        ).fetchone()
        assert (row["content_hash"], row["mtime"], row["last_seen"]) == (
            "local-hash",
            500.0,
            "2026-02-02T00:00:00",
        )


class TestOnlyAgentStateTravels:
    """The reaper test decides this, not the key's portability. `artifact_item_state` is
    keyed on a host-independent slug and still cannot travel, because
    `reconcile_artifacts` takes `known.keys() - live` on every `ArtifactKnowledgeSync
    .start` and removes each provably absent slug's group. Nothing reaps agent state."""

    def test_an_artifact_row_is_not_restored(self, exporter, importer):
        sid = exporter.add_source(name="Artifacts", source_type="artifact", uri=ARTIFACT_URI)
        item_id = exporter.add_item(
            title="Doc", content="artifact body", item_type="document", source_id=sid
        )
        exporter.db.execute(
            "INSERT INTO artifact_item_state (source_id, slug, content_hash, item_ids, "
            "updated_at, name, status, kind) VALUES (?, 'doc', 'hash-doc', ?, "
            "'2026-01-01T00:00:00', 'Doc', 'active', 'markdown')",
            (sid, json.dumps([item_id])),
        )
        exporter.db.commit()

        result = importer.import_bundle(exporter.export_all())

        assert result["items_imported"] == 1, "the content itself still arrives"
        assert result["ownership_rows_imported"] == 0
        assert _rows(importer, "artifact_item_state") == 0

    def test_an_agent_row_is_restored(self, exporter, importer):
        sid = exporter.add_source(name="Auto-added", source_type="agent", uri="agent://")
        item_id = exporter.add_item(
            title="Note", content="agent body", item_type="document", source_id=sid
        )
        exporter.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, item_ids, "
            "updated_at, name, status, source_uri) VALUES (?, 'note', 'hash-note', ?, "
            "'2026-01-01T00:00:00', 'Note', 'active', 'https://x/y')",
            (sid, json.dumps([item_id])),
        )
        exporter.db.commit()

        result = importer.import_bundle(exporter.export_all())

        assert result["ownership_rows_imported"] == 1
        assert _rows(importer, "agent_item_state") == 1

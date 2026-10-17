"""A bundle import's self-row exemption for a folder document needs its backing file.

The document builders are shared with ``test_knowledge_bundle_ownership.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_knowledge_bundle_ownership import AGGREGATE_URI, _folder_doc, _item_count, _owned_doc

from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.knowledge.store_parts import bundle_ownership


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


class TestTheFolderExemptionNeedsTheBackingFileToStillExist:
    """The ``folder_file_state`` self-row exemption is safe only while the file it names
    still exists on this host. The folder reap (``FolderWatcher._do_scan`` step 4) walks
    every state row and deletes the ones whose path the walk did not yield, without
    consulting ``status``. So a row whose file is GONE still holds the key but is a
    deletion order waiting to run: exempting its claim lets the import make the ids live,
    then the next scan reaps that path and cascades the delete over the items just
    restored, with nothing to re-ingest them. The exemption therefore has to see that the
    backing file is present before it fires -- the one slice of the owning subsystem's
    existence answer reachable inside the import transaction, because the key is a path.
    """

    def test_a_present_backing_file_keeps_the_exemption(self, exporter, importer):
        """A file the import host still holds IS that document here, so the bundle meeting
        its own row restores the chunk the local delete left behind -- unchanged."""
        _, folder_item = _folder_doc(exporter)
        bundle = exporter.export_all()
        shipped = bundle["folder_file_state"][0]
        # The deleter's own row at the same (source, file_path) the bundle names, and the
        # file still on disk: `_folder_doc` wrote it under the shared tmp_path, which both
        # the exporter and the importer host see.
        assert Path(shipped["file_path"]).exists(), "fixture must leave the file present"
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

        assert result["items_withheld"] == 0, "a present file still earned the exemption"
        assert _item_count(importer, "folder body") == 1, "the deleted chunk did not come back"

    def test_a_directory_at_the_path_does_not_earn_the_exemption(
        self, exporter, importer, tmp_path
    ):
        """The folder reap keys on what the walk yields -- FILES -- so a directory sitting
        at the path is not a backing object the walk would re-ingest. `is_file`, not
        `exists`: a same-named directory passes `exists` but the next sweep still reaps
        the row and deletes the restored chunks, so it must not earn the exemption."""
        as_dir = str(tmp_path / "nowfolder")
        _, folder_item = _folder_doc(exporter, file_path=as_dir)
        bundle = exporter.export_all()
        Path(as_dir).mkdir(parents=True, exist_ok=True)
        assert Path(as_dir).exists() and not Path(as_dir).is_file()
        local_sid = importer.add_source(
            name="Notes", source_type="local_folder", uri="/remote/notes"
        )
        importer.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, content_hash, "
            "text_hash, mtime, item_ids, last_seen, status, attempts) "
            "VALUES (?, ?, 'raw-hash', 'text-hash', 123.5, ?, "
            "'2026-01-01T00:00:00', 'done', 0)",
            (local_sid, as_dir, json.dumps([folder_item])),
        )
        importer.db.commit()

        result = importer.import_bundle(bundle)

        assert result["items_withheld"] == 1, "a directory vouched for a file the reap deletes"
        assert _item_count(importer, "folder body") == 0

    def test_a_missing_backing_file_loses_the_exemption(self, exporter, importer, tmp_path):
        """A folder row whose file is absent on this host does not meet the exemption, so
        its claim on the arriving id stays contested and the import does NOT make it live
        -- which is what keeps the reap, keyed on that same vanished path, from deleting a
        document it would otherwise have just restored."""
        gone = str(tmp_path / "deleted" / "a.md")
        assert not Path(gone).exists(), "the backing file must be absent for this scenario"
        _, folder_item = _folder_doc(exporter, file_path=gone)
        bundle = exporter.export_all()
        shipped = bundle["folder_file_state"][0]
        assert folder_item in json.loads(shipped["item_ids"])
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

        assert result["items_withheld"] == 1, "a stale folder claim still earned the exemption"
        # The claimant is the restoring document's OWN stale folder row, not a different
        # live document, so the reason names the real remedy (rescan then re-import) and
        # must NOT be the misleading "another document claims it".
        reasons = {w.get("reason") for w in result["withheld"]}
        assert (
            "item_id_claimed_by_another_document" not in reasons
        ), "the stale-file case still reports the wrong, remedy-less reason"
        assert [
            w["item_id"]
            for w in result["withheld"]
            if w.get("reason") == "folder_file_rescan_then_reimport"
        ] == [folder_item]
        assert (
            _item_count(importer, "folder body") == 0
        ), "the import made a stale claim live, which the next reap would delete"

    def test_the_other_tables_keep_the_exemption_without_a_file(self, exporter, importer):
        """The gate is scoped to the one table whose existence is testable in-transaction.
        `agent_item_state` has no path and no reap-by-absence, so its own-row exemption is
        unconditional: the bundle's only copy of a per-chunk-deleted agent document must
        still come back, never withheld over a file test that does not apply to it."""
        _, item_id = _owned_doc(exporter, slug="doc", body="agent-only body", name="Doc")
        local_sid = importer.add_source(name="Auto-added", source_type="agent", uri=AGGREGATE_URI)
        importer.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, item_ids, "
            "updated_at, name, status, source_uri) VALUES (?, 'doc', 'hash-doc', ?, "
            "'2026-01-01T00:00:00', 'Doc', 'active', 'https://x/y')",
            (local_sid, json.dumps([item_id])),
        )
        importer.db.commit()

        result = importer.import_bundle(exporter.export_all())

        assert result["items_withheld"] == 0, "an agent row's own exemption was gated on a file"
        assert _item_count(importer, "agent-only body") == 1

    def test_no_bundle_authored_path_reaches_the_existence_check(
        self, exporter, importer, monkeypatch
    ):
        """A bundle names its own `file_path`, and stat-ing one lets an uploaded bundle
        reach any host path: a UNC path leaks NTLM over SMB on Windows, a stalled mount
        holds the import's write lock. So the existence check runs ONLY against a local
        doc-state row's recorded key, never a bundle string. Here the importer holds no
        local folder row at all, so the gate must stat nothing the bundle supplied."""
        attacker = r"\\attacker.example\share\x.md"
        _, folder_item = _folder_doc(exporter, file_path=attacker)
        bundle = exporter.export_all()
        assert bundle["folder_file_state"][0]["file_path"] == attacker

        seen_keys: list[str] = []
        real = bundle_ownership._backing_object_present

        def _spy(table, key):
            seen_keys.append(key)
            return real(table, key)

        monkeypatch.setattr(bundle_ownership, "_backing_object_present", _spy)

        importer.import_bundle(bundle)

        assert attacker not in seen_keys, "a bundle-authored path reached the existence check"

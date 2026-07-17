"""Tests for cross-source Knowledge Base de-duplication (knowledge/dedup.py)."""

from __future__ import annotations

import json

from kiro_crew.knowledge.dedup import (
    DocRef,
    dedup_document,
    dedup_sweep,
    filename_near_match,
    normalize_filename,
    pick_winner,
)
from kiro_crew.knowledge.embedder import floats_to_bytes
from kiro_crew.knowledge.store import KnowledgeStore


def _mk_store(tmp_path) -> KnowledgeStore:
    return KnowledgeStore(str(tmp_path / "k.db"))


def _add_upload(store, name, content_hash, vec, sig="sig1",
                created_at="2026-01-01T00:00:00"):
    """Add a one-shot upload document (its own source)."""
    sid = store.add_source(name=name, source_type="local_file", uri=f"upload://{name}")
    iid = store.add_item(
        title=name, content="body", item_type="document", source_id=sid,
        content_hash=content_hash, embedding=floats_to_bytes(vec))
    store.db.execute(
        "UPDATE items SET embedding_sig = ?, created_at = ? WHERE id = ?",
        (sig, created_at, iid))
    store.db.execute("UPDATE sources SET updated_at = ? WHERE id = ?", (created_at, sid))
    store.db.commit()
    return sid, iid


def _folder_source(store, name="Projects"):
    row = store.db.execute(
        "SELECT id FROM sources WHERE source_type = 'local_folder' AND name = ?",
        (name,)).fetchone()
    if row:
        return row["id"]
    return store.add_source(name=name, source_type="local_folder", uri=f"/tmp/{name}")


def _add_folder_file(store, file_path, content_hash, vec, sig="sig1", mtime=1000.0,
                     created_at="2026-02-01T00:00:00", folder_name="Projects"):
    """Add a folder-file document (one folder_file_state row within a folder source)."""
    sid = _folder_source(store, folder_name)
    iid = store.add_item(
        title=file_path.rsplit("/", 1)[-1], content="body", item_type="document",
        source_id=sid, content_hash=content_hash, embedding=floats_to_bytes(vec))
    store.db.execute(
        "UPDATE items SET embedding_sig = ?, created_at = ? WHERE id = ?",
        (sig, created_at, iid))
    store.db.execute(
        "INSERT INTO folder_file_state "
        "(source_id, file_path, content_hash, mtime, item_ids, last_seen, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sid, file_path, "bytehash", mtime, json.dumps([iid]), created_at, "done"))
    store.db.commit()
    return sid, iid


def _n_uploads(store):
    return store.db.execute(
        "SELECT COUNT(*) FROM sources WHERE source_type = 'local_file'").fetchone()[0]


class TestFilenameMatch:
    def test_copy_modifiers_match(self):
        assert filename_near_match("Report.docx", "Report (1).docx")
        assert filename_near_match("Report.docx", "Report copy.docx")
        assert filename_near_match("Report.docx", "Copy of Report.docx")

    def test_close_dates_still_match(self):
        # A few days apart -- same document, a re-save / off-by-N-day revision.
        assert filename_near_match(
            "Discovery_QBR_04_14_Final.docx", "Discovery_QBR_04_21_Final.docx")
        # Same month, no day -- same instance.
        assert filename_near_match(
            "Customer 360 - Apr 2026 Update.docx", "Customer 360 - Apr 2026 Update (1).docx")
        # A few days apart across a month boundary still counts as the same doc.
        assert filename_near_match(
            "Status 2026-03-30.docx", "Status 2026-04-02.docx")
        # Underscore-delimited, a few days apart -- still the same doc.
        assert filename_near_match(
            "Weekly_Report_04_14.docx", "Weekly_Report_04_18.docx")

    def test_month_apart_dates_do_not_match(self):
        # Distinct instances of a monthly series that share an identical stem must
        # NOT collapse, even though the title is otherwise identical.
        assert not filename_near_match(
            "Customer 360 - Apr 2026 Update.docx", "Customer 360 - Dec25 Update.docx")
        assert not filename_near_match(
            "Customer 360 - Apr 2026 Update.docx", "Customer 360 - May 2026 Update.docx")
        assert not filename_near_match(
            "Weekly Report 04_14.docx", "Weekly Report 05_14.docx")
        # Underscore-delimited series must also be caught (boundary handling).
        assert not filename_near_match(
            "Status_Update_Apr_2026.docx", "Status_Update_May_2026.docx")
        assert not filename_near_match(
            "Weekly_Report_04_14.docx", "Weekly_Report_05_14.docx")

    def test_dateless_names_unaffected_by_date_gate(self):
        # No dates on either side -> the date gate never blocks a stem match.
        assert filename_near_match("Report.docx", "Report (1).docx")
        assert filename_near_match("Roadmap copy.docx", "Roadmap.docx")

    def test_distinct_names_do_not_match(self):
        assert not filename_near_match("Quarterly Sales.docx", "Engineering Roadmap.docx")

    def test_normalize_strips_extension_and_case(self):
        assert normalize_filename("My File.DOCX") == normalize_filename("my  file")


class TestPriority:
    @staticmethod
    def _doc(**kw):
        kw.setdefault("item_ids", ["x"])
        kw.setdefault("content_hash", None)
        kw.setdefault("embedding_sig", None)
        return DocRef(**kw)

    def test_persistent_beats_transient_even_if_older(self):
        folder = self._doc(source_id="f", source_type="local_folder", filename="a",
                           recency=1.0, resident_since=5.0, file_path="/a")
        upload = self._doc(source_id="u", source_type="local_file", filename="a",
                           recency=9.0, resident_since=1.0)
        winner, loser = pick_winner(folder, upload)
        assert winner.source_id == "f"
        assert loser.source_id == "u"

    def test_newest_wins_within_class(self):
        a = self._doc(source_id="a", source_type="local_file", filename="x",
                      recency=1.0, resident_since=1.0)
        b = self._doc(source_id="b", source_type="local_file", filename="x",
                      recency=2.0, resident_since=1.0)
        winner, _ = pick_winner(a, b)
        assert winner.source_id == "b"

    def test_oldest_resident_breaks_mtime_tie(self):
        a = self._doc(source_id="a", source_type="local_file", filename="x",
                      recency=1.0, resident_since=1.0)
        b = self._doc(source_id="b", source_type="local_file", filename="x",
                      recency=1.0, resident_since=5.0)
        winner, _ = pick_winner(a, b)
        assert winner.source_id == "a"

    def test_cross_format_prefers_better_recall_format(self):
        # Same doc as docx + pdf, both in folders (persistent); the pdf is NEWER.
        # The docx must still win because it extracts cleaner (better recall).
        docx = self._doc(source_id="d", source_type="local_folder",
                         filename="Report.docx", recency=1.0, resident_since=1.0,
                         file_path="/p/Report.docx")
        pdf = self._doc(source_id="p", source_type="local_folder",
                        filename="Report.pdf", recency=9.0, resident_since=1.0,
                        file_path="/q/Report.pdf")
        winner, loser = pick_winner(docx, pdf)
        assert (winner.source_id, loser.source_id) == ("d", "p")
        # order-independent
        assert pick_winner(pdf, docx)[0].source_id == "d"

    def test_same_format_winner_unchanged_by_rank_step(self):
        # Two PDFs: the rank step is skipped (same extension) and newest still wins.
        a = self._doc(source_id="a", source_type="local_folder", filename="x.pdf",
                      recency=1.0, resident_since=1.0, file_path="/a/x.pdf")
        b = self._doc(source_id="b", source_type="local_folder", filename="x.pdf",
                      recency=2.0, resident_since=1.0, file_path="/b/x.pdf")
        assert pick_winner(a, b)[0].source_id == "b"

    def test_persistence_outranks_format(self):
        # A transient docx must NOT beat a persistent pdf -- persistence is checked
        # before the format-rank step.
        upload_docx = self._doc(source_id="u", source_type="local_file",
                                filename="Report.docx", recency=9.0, resident_since=1.0)
        folder_pdf = self._doc(source_id="f", source_type="local_folder",
                               filename="Report.pdf", recency=1.0, resident_since=1.0,
                               file_path="/p/Report.pdf")
        assert pick_winner(upload_docx, folder_pdf)[0].source_id == "f"

    def test_format_rank_ordering(self):
        from kiro_crew.knowledge.dedup import _format_rank
        assert _format_rank("a.docx") > _format_rank("a.pdf")
        assert _format_rank("a.md") >= _format_rank("a.docx")
        # unknown extension and no extension both fall back to the default rank,
        # which sits above pdf so an unknown text format isn't discarded for a pdf.
        assert _format_rank("a.pdf") < _format_rank("a.weirdext")
        assert _format_rank("noext") == _format_rank("other_noext")


class TestDedupSweep:
    def test_exact_hash_collapses_upload_into_folder(self, tmp_path):
        store = _mk_store(tmp_path)
        _add_upload(store, "Doc.docx", "H1", [1.0, 0.0, 0.0, 0.0])
        _add_folder_file(store, "/p/Doc.docx", "H1", [1.0, 0.0, 0.0, 0.0])
        results = dedup_sweep(store, apply=True)
        assert len(results) == 1
        assert results[0]["reason"] == "exact"
        assert results[0]["loser"] == "Doc.docx"
        assert results[0]["winner"] == "Doc.docx"
        assert _n_uploads(store) == 0  # transient upload collapsed
        assert store.db.execute(
            "SELECT COUNT(*) FROM folder_file_state").fetchone()[0] == 1  # folder kept
        store.db.close()

    def test_fuzzy_collapses_near_duplicate(self, tmp_path):
        store = _mk_store(tmp_path)
        _add_upload(store, "Plan.docx", "H1", [1.0, 0.0, 0.0, 0.0])
        # different content hash, near-identical filename, cosine ~0.98
        _add_folder_file(store, "/p/Plan.docx", "H2", [0.98, 0.0, 0.2, 0.0])
        results = dedup_sweep(store, apply=True)
        assert len(results) == 1
        assert results[0]["reason"].startswith("fuzzy")
        assert _n_uploads(store) == 0
        store.db.close()

    def test_below_threshold_keeps_both(self, tmp_path):
        store = _mk_store(tmp_path)
        # same topic/filename but only cosine ~0.71 -- the Customer-360 Apr-vs-Dec case
        _add_upload(store, "Update.docx", "H1", [1.0, 0.0, 0.0, 0.0])
        _add_folder_file(store, "/p/Update.docx", "H2", [0.7, 0.7, 0.0, 0.0])
        results = dedup_sweep(store, apply=True)
        assert results == []
        assert _n_uploads(store) == 1
        store.db.close()

    def test_fuzzy_requires_filename_match(self, tmp_path):
        store = _mk_store(tmp_path)
        # identical embedding (cosine 1.0) but unrelated filenames -> not a duplicate
        _add_upload(store, "Apples.docx", "H1", [1.0, 0.0, 0.0, 0.0])
        _add_folder_file(store, "/p/Oranges.docx", "H2", [1.0, 0.0, 0.0, 0.0])
        results = dedup_sweep(store, apply=True)
        assert results == []
        assert _n_uploads(store) == 1
        store.db.close()

    def test_mismatched_embedding_sig_skips_fuzzy(self, tmp_path):
        store = _mk_store(tmp_path)
        _add_upload(store, "Doc.docx", "H1", [1.0, 0.0, 0.0, 0.0], sig="sigA")
        _add_folder_file(store, "/p/Doc.docx", "H2", [1.0, 0.0, 0.0, 0.0], sig="sigB")
        results = dedup_sweep(store, apply=True)
        assert results == []
        assert _n_uploads(store) == 1
        store.db.close()

    def test_dry_run_changes_nothing(self, tmp_path):
        store = _mk_store(tmp_path)
        _add_upload(store, "Doc.docx", "H1", [1.0, 0.0, 0.0, 0.0])
        _add_folder_file(store, "/p/Doc.docx", "H1", [1.0, 0.0, 0.0, 0.0])
        results = dedup_sweep(store, apply=False)
        assert len(results) == 1
        assert _n_uploads(store) == 1  # nothing deleted on a dry run
        store.db.close()

    def test_apply_is_idempotent(self, tmp_path):
        store = _mk_store(tmp_path)
        _add_upload(store, "Doc.docx", "H1", [1.0, 0.0, 0.0, 0.0])
        _add_folder_file(store, "/p/Doc.docx", "H1", [1.0, 0.0, 0.0, 0.0])
        dedup_sweep(store, apply=True)
        assert dedup_sweep(store, apply=True) == []
        store.db.close()

    def test_no_duplicates_is_noop(self, tmp_path):
        store = _mk_store(tmp_path)
        _add_upload(store, "Alpha.docx", "H1", [1.0, 0.0, 0.0, 0.0])
        _add_folder_file(store, "/p/Beta.docx", "H2", [0.0, 1.0, 0.0, 0.0])
        assert dedup_sweep(store, apply=True) == []
        assert _n_uploads(store) == 1
        store.db.close()

    def test_folder_loser_marked_deduped_not_deleted(self, tmp_path):
        store = _mk_store(tmp_path)
        # Two folders hold the same file; the newer copy wins, the older is collapsed.
        _add_folder_file(store, "/a/Doc.docx", "H1", [1.0, 0.0, 0.0, 0.0],
                         mtime=1000.0, folder_name="A")
        _add_folder_file(store, "/b/Doc.docx", "H1", [1.0, 0.0, 0.0, 0.0],
                         mtime=2000.0, folder_name="B")
        results = dedup_sweep(store, apply=True)
        assert len(results) == 1
        # The loser folder file keeps its state row as 'deduped' (so the next scan does
        # not re-ingest the still-on-disk file), with its items cleared.
        loser = store.db.execute(
            "SELECT status, item_ids FROM folder_file_state WHERE file_path = '/a/Doc.docx'"
        ).fetchone()
        assert loser["status"] == "deduped"
        assert loser["item_ids"] == "[]"
        winner = store.db.execute(
            "SELECT status FROM folder_file_state WHERE file_path = '/b/Doc.docx'"
        ).fetchone()
        assert winner["status"] == "done"
        store.db.close()


class TestDedupDocument:
    def test_new_upload_collapses_into_existing_folder(self, tmp_path):
        store = _mk_store(tmp_path)
        _add_folder_file(store, "/p/Doc.docx", "H1", [1.0, 0.0, 0.0, 0.0])
        up_sid, _ = _add_upload(store, "Doc.docx", "H1", [1.0, 0.0, 0.0, 0.0])
        results = dedup_document(store, up_sid, apply=True)
        assert len(results) == 1
        assert results[0]["loser"] == "Doc.docx"
        assert _n_uploads(store) == 0  # the new upload lost to the persistent folder copy
        store.db.close()

    def test_targeted_dedup_no_match_is_noop(self, tmp_path):
        store = _mk_store(tmp_path)
        _add_folder_file(store, "/p/Other.docx", "H2", [0.0, 1.0, 0.0, 0.0])
        up_sid, _ = _add_upload(store, "Doc.docx", "H1", [1.0, 0.0, 0.0, 0.0])
        assert dedup_document(store, up_sid, apply=True) == []
        assert _n_uploads(store) == 1
        store.db.close()

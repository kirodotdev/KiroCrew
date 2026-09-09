"""Tests for cross-source Knowledge Base de-duplication (knowledge/dedup.py)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from kiro_crew.knowledge.dedup import (
    DocRef,
    _match_reason,
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


def _upload_owns_no_items(store):
    """The upload's own copy is gone: it owns no items."""
    return store.db.execute(
        "SELECT COUNT(*) FROM items i JOIN sources s ON s.id = i.source_id "
        "WHERE s.source_type = 'local_file'").fetchone()[0] == 0


def _upload_still_holds_a_document(store):
    """...but it is a LOCATION of the surviving copy, so the document is still
    reachable from it and deleting the winner cannot destroy it."""
    return store.db.execute(
        "SELECT COUNT(*) FROM source_locations sl JOIN sources s ON s.id = sl.source_id "
        "WHERE s.source_type = 'local_file'").fetchone()[0] > 0


class TestFilenameMatch:
    @pytest.mark.parametrize("separator", ["-", "_", ".", ""])
    def test_iso_dates_keep_the_year(self, separator):
        a = f"Report 2025{separator}04{separator}14.docx"
        b = f"Report 2026{separator}04{separator}14.docx"
        assert not filename_near_match(a, b)

    def test_iso_dates_keep_close_revisions_and_separate_numeric_dates(self):
        assert filename_near_match("Report 2025-12-30.docx", "Report 2026-01-02.docx")
        assert filename_near_match(
            "Report 2025-04-14 06-10.docx", "Report 2026-04-14 06-10.docx"
        )

    def test_malformed_iso_tokens_still_feed_the_numeric_date_guard(self):
        assert not filename_near_match(
            "Report 2025-13-01.docx", "Report 2025-25-01.docx"
        )

    def test_iso_date_ranges_keep_their_explicit_years(self):
        # A numeric date (04-01-2024) whose span only partly overlaps an
        # over-matched ISO span (2024_04-30) must keep its explicit year, so two
        # date-range reports a year apart stay distinct.
        assert not filename_near_match(
            "Report 04-01-2024_04-30-2024.docx",
            "Report 04-01-2025_04-30-2025.docx",
        )

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
        # The upload keeps its source row: it is now a location of the surviving
        # copy, so the document stays reachable from it.
        assert _upload_owns_no_items(store)
        assert _upload_still_holds_a_document(store)
        assert store.db.execute(
            "SELECT COUNT(*) FROM folder_file_state").fetchone()[0] == 1  # folder kept
        store.db.close()

    def test_aggregate_source_dedups_per_document_not_wholesale(self, tmp_path):
        # An aggregate source ("Artifacts", "Auto-added") holds MANY documents
        # under one sources row. Treating it as a single dedup unit hashed only
        # its first item and made the whole library the loser, so one duplicate
        # artifact cascade-deleted every artifact. The unit is now the document:
        # only the duplicate collapses, the source row and every other document
        # in it are untouched.
        store = _mk_store(tmp_path)
        art_sid = store.add_source(
            name="Artifacts", source_type="artifact", uri="artifact://all"
        )
        dupe = store.add_item(
            title="notes", content="body", item_type="document", source_id=art_sid,
            content_hash="H1", embedding=floats_to_bytes([1.0, 0.0, 0.0, 0.0]))
        keeper = store.add_item(
            title="other", content="body2", item_type="document", source_id=art_sid,
            content_hash="H2", embedding=floats_to_bytes([0.0, 1.0, 0.0, 0.0]))
        store.db.commit()
        _add_folder_file(store, "/p/notes.md", "H1", [1.0, 0.0, 0.0, 0.0])

        results = dedup_sweep(store, apply=True)

        assert len(results) == 1
        # The aggregate source row survives.
        assert store.db.execute(
            "SELECT COUNT(*) FROM sources WHERE id = ?", (art_sid,)).fetchone()[0] == 1
        remaining = {r["id"] for r in store.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (art_sid,)).fetchall()}
        assert dupe not in remaining, "the duplicate document should be collapsed"
        assert keeper in remaining, "collapsing one document must not remove the others"
        store.db.close()

    def test_aggregate_document_is_marked_so_it_is_not_re_deduped(self, tmp_path):
        # Deleting an aggregate document's items is not enough: its item-state row
        # must record that it was collapsed, or the owning sync re-ingests it and
        # the sweep collapses it again on every pass.
        store = _mk_store(tmp_path)
        art_sid = store.add_source(
            name="Artifacts", source_type="artifact", uri="artifact://all")
        iid = store.add_item(
            title="notes", content="body", item_type="document", source_id=art_sid,
            content_hash="H1", embedding=floats_to_bytes([1.0, 0.0, 0.0, 0.0]))
        store.db.execute(
            "INSERT INTO artifact_item_state "
            "(source_id, slug, content_hash, item_ids, updated_at, name, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active')",
            (art_sid, "notes", "H1", json.dumps([iid]), "2026-01-01T00:00:00", "notes"))
        store.db.commit()
        _add_folder_file(store, "/p/notes.md", "H1", [1.0, 0.0, 0.0, 0.0])

        assert len(dedup_sweep(store, apply=True)) == 1
        row = store.db.execute(
            "SELECT status, item_ids FROM artifact_item_state WHERE slug = 'notes'"
        ).fetchone()
        assert row["status"] == "deduped"
        assert row["item_ids"] == "[]"
        # And a second pass finds nothing left to do.
        assert dedup_sweep(store, apply=True) == []
        store.db.close()

    def test_a_state_table_read_failure_cannot_abort_the_sweep(self, tmp_path):
        # The losing document's items are deleted and COMMITTED before the marker
        # tables are read. An exception from those reads would abort the sweep
        # mid-collapse, leaving the audit event unwritten -- so they degrade to
        # "no marker" instead of raising.
        store = _mk_store(tmp_path)
        _add_upload(store, "Doc.docx", "H1", [1.0, 0.0, 0.0, 0.0])
        _add_folder_file(store, "/p/Doc.docx", "H1", [1.0, 0.0, 0.0, 0.0])

        class _FailingStateReads:
            def __init__(self, real):
                self._real = real

            def execute(self, sql, *a, **kw):
                if "artifact_item_state" in sql or "agent_item_state" in sql:
                    raise sqlite3.OperationalError("no such table")
                return self._real.execute(sql, *a, **kw)

            def __getattr__(self, name):
                return getattr(self._real, name)

        class _StoreProxy:
            """The real store with item-state reads made to fail."""

            def __init__(self, real):
                self._real = real
                self.db = _FailingStateReads(real.db)

            def __getattr__(self, name):
                return getattr(self._real, name)

        results = dedup_sweep(_StoreProxy(store), apply=True)  # must not raise
        assert len(results) == 1
        # The collapse still happened: the loser's chunk rows are gone.
        n_upload_items = store.db.execute(
            "SELECT COUNT(*) FROM items WHERE source_id IN "
            "(SELECT id FROM sources WHERE source_type = 'local_file')").fetchone()[0]
        assert n_upload_items == 0
        # Its now-empty source row survives, because a read failure makes the
        # emptiness check answer False rather than guess. A lingering empty row is
        # the strictly safer outcome; the next sweep reaps it.
        assert _n_uploads(store) == 1
        store.db.close()

    def test_source_holding_other_documents_is_never_deleted(self, tmp_path):
        # A source is removed only once it is provably empty, so collapsing one
        # document can never take its siblings with it.
        store = _mk_store(tmp_path)
        sid = store.add_source(name="Agg", source_type="agent", uri="agent://")
        keeper = store.add_item(
            title="keep", content="keep", item_type="document", source_id=sid,
            content_hash="H2", embedding=floats_to_bytes([0.0, 1.0, 0.0, 0.0]))
        store.add_item(
            title="dupe", content="dupe", item_type="document", source_id=sid,
            content_hash="H1", embedding=floats_to_bytes([1.0, 0.0, 0.0, 0.0]))
        store.db.commit()
        _add_folder_file(store, "/p/dupe.md", "H1", [1.0, 0.0, 0.0, 0.0])

        dedup_sweep(store, apply=True)

        assert store.db.execute(
            "SELECT COUNT(*) FROM sources WHERE id = ?", (sid,)).fetchone()[0] == 1
        assert store.db.execute(
            "SELECT COUNT(*) FROM items WHERE id = ?", (keeper,)).fetchone()[0] == 1
        store.db.close()

    def test_fuzzy_collapses_near_duplicate(self, tmp_path):
        store = _mk_store(tmp_path)
        _add_upload(store, "Plan.docx", "H1", [1.0, 0.0, 0.0, 0.0])
        # different content hash, near-identical filename, cosine ~0.98
        _add_folder_file(store, "/p/Plan.docx", "H2", [0.98, 0.0, 0.2, 0.0])
        results = dedup_sweep(store, apply=True)
        assert len(results) == 1
        assert results[0]["reason"].startswith("fuzzy")
        assert _upload_owns_no_items(store)
        assert _upload_still_holds_a_document(store)
        store.db.close()

    def test_fuzzy_sweep_preserves_reports_from_different_iso_years(self, tmp_path):
        store = _mk_store(tmp_path)
        try:
            _, upload = _add_upload(
                store, "Report 2025-04-14.docx", "H1", [1.0, 0.0, 0.0, 0.0]
            )
            _, folder = _add_folder_file(
                store, "/p/Report 2026-04-14.docx", "H2", [0.98, 0.0, 0.2, 0.0]
            )
            assert dedup_sweep(store, apply=True) == []
            assert {
                row["id"] for row in store.db.execute("SELECT id FROM items")
            } == {upload, folder}
        finally:
            store.db.close()

    def test_fuzzy_sweep_preserves_reports_with_distinct_malformed_iso_tokens(self, tmp_path):
        store = _mk_store(tmp_path)
        try:
            _, upload = _add_upload(
                store, "Report 2025-13-01.docx", "H1", [1.0, 0.0, 0.0, 0.0]
            )
            _, folder = _add_folder_file(
                store, "/p/Report 2025-25-01.docx", "H2", [0.98, 0.0, 0.2, 0.0]
            )
            assert dedup_sweep(store, apply=True) == []
            assert {
                row["id"] for row in store.db.execute("SELECT id FROM items")
            } == {upload, folder}
        finally:
            store.db.close()

    def test_fuzzy_sweep_preserves_date_range_reports_a_year_apart(self, tmp_path):
        store = _mk_store(tmp_path)
        try:
            _, upload = _add_upload(
                store, "Report 04-01-2024_04-30-2024.docx", "H1", [1.0, 0.0, 0.0, 0.0]
            )
            _, folder = _add_folder_file(
                store,
                "/p/Report 04-01-2025_04-30-2025.docx",
                "H2",
                [0.98, 0.0, 0.2, 0.0],
            )
            assert dedup_sweep(store, apply=True) == []
            assert {
                row["id"] for row in store.db.execute("SELECT id FROM items")
            } == {upload, folder}
        finally:
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
        # The new upload lost to the persistent folder copy, but survives as a
        # location of it rather than being destroyed.
        assert _upload_owns_no_items(store)
        assert _upload_still_holds_a_document(store)
        store.db.close()

    def test_targeted_dedup_no_match_is_noop(self, tmp_path):
        store = _mk_store(tmp_path)
        _add_folder_file(store, "/p/Other.docx", "H2", [0.0, 1.0, 0.0, 0.0])
        up_sid, _ = _add_upload(store, "Doc.docx", "H1", [1.0, 0.0, 0.0, 0.0])
        assert dedup_document(store, up_sid, apply=True) == []
        assert _n_uploads(store) == 1
        store.db.close()

    def test_aggregate_ingest_path_collapses_the_duplicate_not_the_survivor(self, tmp_path):
        # dedup_document(source_id) runs on EVERY artifact save (ingestion.py's
        # per-ingest targeted dedup). _build_doc_for builds the DocRef for one
        # document, so the pair is real and the collapse is correct.
        store = _mk_store(tmp_path)
        art_sid = store.add_source(
            name="Artifacts", source_type="artifact", uri="artifact://all"
        )
        art_item = store.add_item(
            title="notes", content="body", item_type="document", source_id=art_sid,
            content_hash="H1", embedding=floats_to_bytes([1.0, 0.0, 0.0, 0.0]))
        store.add_item(
            title="unrelated", content="other", item_type="document", source_id=art_sid,
            content_hash="H9", embedding=floats_to_bytes([0.0, 0.0, 1.0, 0.0]))
        # The per-document name comes from the item-state table, not the aggregate
        # source's name -- so the two docs compare as the same format and recency
        # decides, rather than the aggregate losing on a bare source name.
        store.db.execute(
            "INSERT INTO artifact_item_state "
            "(source_id, slug, content_hash, item_ids, updated_at, name, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active')",
            (art_sid, "notes", "H1", json.dumps([art_item]),
             "2026-01-01T00:00:00", "notes.md"))
        store.db.commit()
        # A one-shot upload duplicating that artifact's content. The aggregate is
        # NEWER, so it wins and the transient upload is collapsed.
        _add_upload(store, "notes.md", "H1", [1.0, 0.0, 0.0, 0.0])
        store.db.execute("UPDATE sources SET updated_at = '2099-01-01T00:00:00' WHERE id = ?",
                         (art_sid,))
        store.db.commit()

        results = dedup_document(store, art_sid, content_hash="H1", apply=True)

        assert len(results) == 1
        # The transient upload was the loser and is gone; the artifact document it
        # duplicated survives, and so does the unrelated artifact alongside it.
        assert _upload_owns_no_items(store)
        assert _upload_still_holds_a_document(store)
        surviving = {r["id"] for r in store.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (art_sid,)).fetchall()}
        assert art_item in surviving
        assert len(surviving) == 2
        store.db.close()


class TestOneDocumentManyLocations:
    """A collapsed duplicate is a RELATIONSHIP, not a destroyed copy."""

    def _seed(self, tmp_path):
        """Two sources holding identical content: a folder file and an upload."""
        store = _mk_store(tmp_path)
        folder = store.add_source(name="docs", source_type="local_folder",
                                  uri=str(tmp_path / "docs"))
        upload = store.add_source(name="dropped.md", source_type="local_file",
                                  uri="upload://dropped.md")
        h = "c" * 64
        fid = store.add_item(title="design.md", content="the one true body",
                             item_type="document", source_id=folder,
                             content_hash=h)
        uid = store.add_item(title="dropped.md", content="the one true body",
                             item_type="document", source_id=upload,
                             content_hash=h)
        store.add_source_location(fid, folder)
        store.add_source_location(uid, upload)
        store.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, content_hash, mtime, "
            "item_ids, last_seen, status) VALUES (?, ?, ?, ?, ?, ?, 'done')",
            (folder, str(tmp_path / "docs" / "design.md"), h, 1000.0,
             json.dumps([fid]), "2024-01-01T00:00:00"))
        store.db.commit()
        return store, folder, upload, fid, uid

    def test_collapse_attaches_the_loser_source_to_the_winner_items(self, tmp_path):
        store, folder, upload, fid, uid = self._seed(tmp_path)
        try:
            actions = dedup_sweep(store, apply=True)
            assert actions, "expected a collapse"
            # The folder is persistent so it wins; the upload's item is gone.
            assert store.get_item(fid) is not None
            assert store.get_item(uid) is None
            # ...but the upload is now a LOCATION of the surviving item.
            holders = set(store.sources_holding_item(fid))
            assert holders == {folder, upload}, holders
        finally:
            store.db.close()

    def test_document_survives_deleting_the_source_that_won(self, tmp_path):
        """The property A exists for: deleting the winner must not lose the document."""
        store, folder, upload, fid, uid = self._seed(tmp_path)
        try:
            dedup_sweep(store, apply=True)
            store.delete_source_cascade(folder)
            item = store.get_item(fid)
            assert item is not None, "the document was destroyed with its winning source"
            assert item["content"] == "the one true body"
            # Ownership moved to the source that still holds it.
            assert item["source_id"] == upload
            assert store.sources_holding_item(fid) == [upload]
        finally:
            store.db.close()

    def test_deleting_the_winner_revives_the_loser_state_row(self, tmp_path):
        store, folder, upload, fid, uid = self._seed(tmp_path)
        try:
            dedup_sweep(store, apply=True)
            # Seed the mirror case: a folder file that LOST, so it owns nothing and
            # defers to the upload. Its own item is gone, which is what makes the
            # marker a strand risk -- nothing else would bring the file back.
            store.delete_items_batch([fid])
            store.db.execute(
                "UPDATE folder_file_state SET status='deduped', item_ids='[]', "
                "merged_into_source_id=? WHERE source_id=?", (upload, folder))
            store.db.commit()
            store.delete_source_cascade(upload)
            row = store.db.execute(
                "SELECT status, merged_into_source_id FROM folder_file_state "
                "WHERE source_id = ?", (folder,)).fetchone()
            assert row["merged_into_source_id"] is None
            # The folder owns no copy, so the file must be re-scanned rather than
            # adopting something that is not there.
            assert row["status"] == "pending", "a revived file must be re-scanned"
        finally:
            store.db.close()

    def test_two_refs_to_one_item_set_are_never_collapsed(self, tmp_path):
        """The self-annihilation guard: overlapping item_ids is not a duplicate pair."""
        store = _mk_store(tmp_path)
        try:
            sid = store.add_source(name="S", source_type="local_file", uri="upload://s")
            iid = store.add_item(title="a", content="body", item_type="document",
                                 source_id=sid, content_hash="d" * 64)
            a = DocRef(source_id="srcA", source_type="local_folder", filename="a.md",
                       item_ids=[iid], content_hash="d" * 64, embedding_sig=None,
                       recency=1.0, resident_since=1.0, file_path="/x/a.md")
            b = DocRef(source_id="srcB", source_type="local_file", filename="a.md",
                       item_ids=[iid], content_hash="d" * 64, embedding_sig=None,
                       recency=2.0, resident_since=2.0, file_path=None)
            assert a.key != b.key, "precondition: the key test must NOT be what saves us"
            assert _match_reason(store, a, b, 0.95) is None
        finally:
            store.db.close()

    def test_revived_row_adopts_a_document_reassigned_into_its_source(self, tmp_path):
        """Reviving must not re-ingest a document the source now owns.

        Deleting the winner reassigns the surviving copy to a source that still holds
        it -- possibly the very source whose row deferred to the winner. Clearing the
        marker to 'pending' there would re-ingest a document already present and leave
        that source holding two copies, so the row adopts the reassigned items instead.
        """
        store = _mk_store(tmp_path)
        try:
            folder = store.add_source(name="docs", source_type="local_folder",
                                      uri=str(tmp_path / "docs"))
            upload = store.add_source(name="drop.md", source_type="local_file",
                                      uri="upload://drop.md")
            h = "e" * 64
            uid = store.add_item(title="drop.md", content="body",
                                 item_type="document", source_id=upload,
                                 content_hash=h)
            # Post-collapse: the upload's item is also a location of the folder, and
            # the folder's row defers to the upload.
            store.add_source_location(uid, upload)
            store.add_source_location(uid, folder)
            store.db.execute(
                "INSERT INTO folder_file_state (source_id, file_path, content_hash, "
                "mtime, item_ids, last_seen, status, merged_into_source_id) "
                "VALUES (?, ?, ?, ?, '[]', '2024-01-01', 'deduped', ?)",
                (folder, str(tmp_path / "docs" / "drop.md"), h, 1000.0, upload))
            store.db.commit()

            store.delete_source_cascade(upload)

            item = store.get_item(uid)
            assert item is not None and item["source_id"] == folder
            row = store.db.execute(
                "SELECT status, item_ids, merged_into_source_id FROM folder_file_state "
                "WHERE source_id = ?", (folder,)).fetchone()
            assert row["merged_into_source_id"] is None
            assert row["status"] == "done", "a source that owns the document must not re-ingest"
            assert uid in json.loads(row["item_ids"])
        finally:
            store.db.close()

    def test_an_adopted_aggregate_document_stays_visible_to_the_duplicate_gate(self, tmp_path):
        """Adoption must write each table's OWN healthy status.

        ``find_document_by_hash`` matches on 'active'. An agent document adopted with
        the folder vocabulary's 'done' becomes invisible to it, and identical content
        then lands again under a second uri as a duplicate.
        """
        from kiro_crew.knowledge.agent_source import find_document_by_hash
        store = _mk_store(tmp_path)
        try:
            agg = store.add_source(name="Auto-added", source_type="agent",
                                   uri="agent://auto")
            upload = store.add_source(name="drop.md", source_type="local_file",
                                      uri="upload://drop.md")
            h = "f" * 64
            uid = store.add_item(title="drop.md", content="body",
                                 item_type="document", source_id=upload,
                                 content_hash=h)
            store.add_source_location(uid, upload)
            store.add_source_location(uid, agg)
            store.db.execute(
                "INSERT INTO agent_item_state (source_id, slug, content_hash, "
                "item_ids, updated_at, name, status, merged_into_source_id) "
                "VALUES (?, 'doc-a', ?, '[]', '2024-01-01', 'drop.md', 'deduped', ?)",
                (agg, h, upload))
            store.db.commit()

            store.delete_source_cascade(upload)

            row = store.db.execute(
                "SELECT status, item_ids FROM agent_item_state WHERE source_id = ?",
                (agg,)).fetchone()
            assert row["status"] == "active", (
                "an adopted aggregate row must use its own vocabulary, not 'done'")
            assert uid in json.loads(row["item_ids"])
            # The gate can now see it, so a second uri with the same bytes is refused.
            assert find_document_by_hash(store, agg, h, exclude_slug="doc-b") is not None
        finally:
            store.db.close()

    def test_reassigning_an_item_tells_the_new_owner_state_row(self, tmp_path):
        """A recipient must never own an item its own state row does not name.

        ``item_ids`` is the only list a source's delete path consults, so an unnamed
        item cannot be deleted: removing the document drops an empty group and the
        content stays searchable.
        """
        store = _mk_store(tmp_path)
        try:
            folder = store.add_source(name="docs", source_type="local_folder",
                                      uri=str(tmp_path / "docs"))
            agg = store.add_source(name="Auto-added", source_type="agent",
                                   uri="agent://auto")
            h = "a" * 64
            iid = store.add_item(title="shared.md", content="body",
                                 item_type="document", source_id=folder,
                                 content_hash=h)
            store.add_source_location(iid, folder)
            store.add_source_location(iid, agg)
            # The aggregate deferred to the folder, so its row owns nothing yet.
            store.db.execute(
                "INSERT INTO agent_item_state (source_id, slug, content_hash, "
                "item_ids, updated_at, name, status, merged_into_source_id) "
                "VALUES (?, 'doc-a', ?, '[]', '2024-01-01', 'shared.md', 'deduped', ?)",
                (agg, h, folder))
            store.db.commit()

            # The folder's copy goes (file deleted on disk) -- ownership moves.
            store.delete_items_batch([iid], owner_source_id=folder)

            item = store.get_item(iid)
            assert item is not None and item["source_id"] == agg
            row = store.db.execute(
                "SELECT status, item_ids FROM agent_item_state WHERE source_id = ?",
                (agg,)).fetchone()
            assert iid in json.loads(row["item_ids"]), (
                "the new owner's row must name the item it now owns")
            assert row["status"] == "active"
        finally:
            store.db.close()

    def test_deleting_a_deduped_file_releases_its_claim_on_the_winner(self, tmp_path):
        """A source that loses its copy must stop being a location of the document.

        A losing file's row is 'deduped' with an empty group, so there are no item ids
        to detach when the file is deleted. If the claim survives, deleting the winner
        later hands the document to a source whose file is gone and the content
        resurfaces there as searchable text with nothing behind it.
        """
        store = _mk_store(tmp_path)
        try:
            winner = store.add_source(name="docs-a", source_type="local_folder",
                                      uri=str(tmp_path / "a"))
            loser = store.add_source(name="docs-b", source_type="local_folder",
                                     uri=str(tmp_path / "b"))
            h = "d" * 64
            wid = store.add_item(title="dup.md", content="body", item_type="document",
                                 source_id=winner, content_hash=h)
            store.add_source_location(wid, winner)
            store.add_source_location(wid, loser)   # post-collapse claim

            assert loser in store.sources_holding_item(wid)
            dropped = store.detach_source_location_by_hash(loser, h)
            store.db.commit()

            assert dropped == 1
            holders = store.sources_holding_item(wid)
            assert loser not in holders, "the claim must be released"
            assert winner in holders, "the winner still holds its own document"
            assert store.get_item(wid) is not None, "the winner's item must survive"

            # And with the claim gone, deleting the winner cannot strand content in the
            # loser: there is no holder left, so the document goes with it.
            store.delete_source_cascade(winner)
            assert store.get_item(wid) is None
        finally:
            store.db.close()

    def test_a_gate_refused_row_adopts_the_item_the_cascade_hands_it(self, tmp_path):
        """A row can hold a location without ever carrying a deferral marker.

        The pre-ingest gate writes exactly that shape: status 'deduped', empty group,
        NO merged_into_source_id, plus a location on the holder's items. The cascade's
        revive loop only reaches marked rows, so without adoption at the reassignment
        itself the recipient owns an item its row never names -- and deleting that file
        then leaves the item searchable with no state and no locations.
        """
        store = _mk_store(tmp_path)
        try:
            holder = store.add_source(name="docs-a", source_type="local_folder",
                                      uri=str(tmp_path / "a"))
            refused = store.add_source(name="docs-b", source_type="local_folder",
                                       uri=str(tmp_path / "b"))
            h = "b" * 64
            iid = store.add_item(title="dup.md", content="body", item_type="document",
                                 source_id=holder, content_hash=h)
            store.add_source_location(iid, holder)
            store.add_source_location(iid, refused)
            # The gate's shape: no marker at all.
            store.db.execute(
                "INSERT INTO folder_file_state (source_id, file_path, content_hash, "
                "mtime, item_ids, last_seen, status) "
                "VALUES (?, ?, ?, ?, '[]', '2024-01-01', 'deduped')",
                (refused, str(tmp_path / "b" / "dup.md"), h, 1000.0))
            store.db.commit()

            store.delete_source_cascade(holder)

            item = store.get_item(iid)
            assert item is not None and item["source_id"] == refused
            row = store.db.execute(
                "SELECT status, item_ids FROM folder_file_state WHERE source_id = ?",
                (refused,)).fetchone()
            assert iid in json.loads(row["item_ids"]), (
                "an unmarked recipient row must still adopt what it inherits")
            assert row["status"] == "done"
        finally:
            store.db.close()

    def test_editing_a_deduped_document_releases_the_claim_on_the_old_content(self, tmp_path):
        """A claim is specific to the content it was made for.

        A source that lost a dedup holds the winner's items. If its own copy is then
        EDITED, that claim points at the wrong document -- deleting the holder would
        hand this source the superseded text and it would stay searchable.
        """
        store = _mk_store(tmp_path)
        try:
            holder = store.add_source(name="docs-a", source_type="local_folder",
                                      uri=str(tmp_path / "a"))
            editor = store.add_source(name="docs-b", source_type="local_folder",
                                      uri=str(tmp_path / "b"))
            old_h, new_h = "1" * 64, "2" * 64
            iid = store.add_item(title="dup.md", content="old body",
                                 item_type="document", source_id=holder,
                                 content_hash=old_h)
            store.add_source_location(iid, holder)
            store.add_source_location(iid, editor)   # the losing copy's claim

            # The editor's file changes: same document slot, different content.
            dropped = store.release_stale_claim(editor, old_h, new_h, [])
            store.db.commit()

            assert dropped == 1
            assert editor not in store.sources_holding_item(iid)
            # So deleting the holder takes the superseded text with it.
            store.delete_source_cascade(holder)
            assert store.get_item(iid) is None

        finally:
            store.db.close()

    def test_the_stale_claim_rule_leaves_a_live_group_and_an_unchanged_hash_alone(self, tmp_path):
        """The rule must fire ONLY for an owner-less row whose hash actually moved.

        A row with a live group replaces its own items through the normal
        delete-and-reingest path; releasing its claim there would drop a legitimate
        co-ownership.
        """
        store = _mk_store(tmp_path)
        try:
            holder = store.add_source(name="docs-a", source_type="local_folder",
                                      uri=str(tmp_path / "a"))
            other = store.add_source(name="docs-b", source_type="local_folder",
                                     uri=str(tmp_path / "b"))
            h = "3" * 64
            iid = store.add_item(title="dup.md", content="body", item_type="document",
                                 source_id=holder, content_hash=h)
            store.add_source_location(iid, holder)
            store.add_source_location(iid, other)

            # Owns items -> untouched, even though the hash moved.
            assert store.release_stale_claim(other, h, "4" * 64, ["some-id"]) == 0
            # Hash unchanged -> untouched.
            assert store.release_stale_claim(other, h, h, []) == 0
            # No prior hash -> nothing to release.
            assert store.release_stale_claim(other, None, h, []) == 0
            assert other in store.sources_holding_item(iid)
        finally:
            store.db.close()

    def test_two_identical_artifacts_stay_independent_documents(self, tmp_path):
        """An aggregate's unit is the DOCUMENT, not (source, content_hash).

        Two distinct artifacts can hold identical text. Grouping by hash fuses them
        into one reference spanning both documents' items, so a later collapse treats
        them as a single thing and removing one takes the other's indexed copy too.
        """
        from kiro_crew.knowledge.dedup import enumerate_docs
        store = _mk_store(tmp_path)
        try:
            agg = store.add_source(name="Artifacts", source_type="artifact",
                                   uri="artifact://library")
            h = "e" * 64
            a = store.add_item(title="spec-a", content="same text",
                               item_type="document", source_id=agg, content_hash=h)
            b = store.add_item(title="spec-b", content="same text",
                               item_type="document", source_id=agg, content_hash=h)
            for slug, iid in (("spec-a", a), ("spec-b", b)):
                store.add_source_location(iid, agg)
                store.db.execute(
                    "INSERT INTO artifact_item_state (source_id, slug, content_hash, "
                    "item_ids, updated_at, status) VALUES (?, ?, ?, ?, '2024-01-01', "
                    "'active')", (agg, slug, h, json.dumps([iid])))
            store.db.commit()

            docs = [d for d in enumerate_docs(store) if d.source_id == agg]
            assert len(docs) == 2, f"expected one unit per document, got {len(docs)}"
            groups = sorted(sorted(d.item_ids) for d in docs)
            assert groups == sorted([[a], [b]]), (
                "each document must own only its own items")
        finally:
            store.db.close()

    def test_certain_only_applies_exact_matches_and_only_reports_fuzzy(self, tmp_path):
        """An unattended pass must not delete a document on a similarity judgement."""
        from unittest.mock import patch

        from kiro_crew.knowledge.dedup import DedupAction, DocRef, dedup_sweep
        store = _mk_store(tmp_path)

        def _ref(sid: str, stype: str, name: str, iid: str, h: str) -> DocRef:
            return DocRef(source_id=sid, source_type=stype, filename=name,
                          item_ids=[iid], content_hash=h, embedding_sig=None,
                          recency=1.0, resident_since=1.0)

        try:
            collapsed: list[str] = []
            fake = [
                DedupAction(winner=_ref("w1", "local_folder", "a.md", "i1", "h1"),
                            loser=_ref("l1", "upload", "a.md", "i2", "h1"),
                            reason="exact"),
                DedupAction(winner=_ref("w2", "local_folder", "b.md", "i3", "h2"),
                            loser=_ref("l2", "upload", "b.md", "i4", "h3"),
                            reason="fuzzy:0.9700"),
            ]
            import kiro_crew.knowledge.dedup as dd
            with patch.object(dd, "enumerate_docs", return_value=[]), \
                 patch.object(dd, "find_duplicates", return_value=fake), \
                 patch.object(dd, "_audit_collapse"), \
                 patch.object(dd, "_collapse_doc",
                              side_effect=lambda st, lo, wi: collapsed.append(lo.source_id)):
                results = dedup_sweep(store, apply=True, certain_only=True)

            assert collapsed == ["l1"], "only the exact match may be applied"
            assert len(results) == 2, "the fuzzy candidate must still be reported"
        finally:
            store.db.close()

    def test_two_identical_files_in_one_folder_are_never_collapsed(self, tmp_path):
        """De-dup is cross-source. Within one source a collapse cannot be undone.

        There is no second holder to hand the survivor to, so the loser's items are
        destroyed, and the marker names the source the row already lives in -- so the
        revive hook (which fires when the MARKED source is deleted) can never run.
        With the mtime gate, removing the survivor takes the content out of the
        Library while the duplicate file is still on disk and nothing re-reads it.
        """
        from kiro_crew.knowledge.dedup import dedup_sweep
        store = _mk_store(tmp_path)
        try:
            folder = store.add_source(name="docs", source_type="local_folder",
                                      uri=str(tmp_path / "docs"))
            h = "5" * 64
            ids = {}
            for name in ("LICENSE", "vendor/LICENSE"):
                iid = store.add_item(title=name, content="MIT text",
                                     item_type="document", source_id=folder,
                                     content_hash=h)
                store.add_source_location(iid, folder)
                store.db.execute(
                    "INSERT INTO folder_file_state (source_id, file_path, "
                    "content_hash, mtime, item_ids, last_seen, status) "
                    "VALUES (?, ?, ?, 1000.0, ?, '2024-01-01', 'done')",
                    (folder, str(tmp_path / "docs" / name), h, json.dumps([iid])))
                ids[name] = iid
            store.db.commit()

            assert dedup_sweep(store, apply=True) == [], (
                "two files in ONE source must never be collapsed")
            for name, iid in ids.items():
                assert store.get_item(iid) is not None, f"{name} lost its items"
            rows = store.db.execute(
                "SELECT status, merged_into_source_id FROM folder_file_state "
                "WHERE source_id = ?", (folder,)).fetchall()
            assert all(r["status"] == "done" for r in rows)
            assert all(r["merged_into_source_id"] is None for r in rows), (
                "a marker naming its own source could never be revived")
        finally:
            store.db.close()

    def test_an_ambiguous_hash_adopts_nothing_and_keeps_the_claim(self, tmp_path):
        """A hash identifies a document only while it picks out ONE row.

        Two artifacts in one aggregate may hold identical text. Writing a reassigned
        item into both groups would put one physical item in two documents, and
        removing either would delete it and take the other's content too. So on an
        ambiguous hash adoption does nothing and the claim is kept: a stale claim is
        recoverable, a cross-wired group destroys content on the next delete.
        """
        store = _mk_store(tmp_path)
        try:
            agg = store.add_source(name="Artifacts", source_type="artifact",
                                   uri="artifact://library")
            folder = store.add_source(name="docs", source_type="local_folder",
                                      uri=str(tmp_path / "docs"))
            h = "c" * 64
            iid = store.add_item(title="spec.md", content="same text",
                                 item_type="document", source_id=folder,
                                 content_hash=h)
            store.add_source_location(iid, folder)
            store.add_source_location(iid, agg)
            # TWO distinct documents in the aggregate, identical content.
            for slug in ("spec-a", "spec-b"):
                store.db.execute(
                    "INSERT INTO artifact_item_state (source_id, slug, content_hash, "
                    "item_ids, updated_at, status) VALUES (?, ?, ?, '[]', "
                    "'2024-01-01', 'deduped')", (agg, slug, h))
            store.db.commit()

            # Adoption must refuse: the hash cannot say which document owns the item.
            store._adopt_reassigned_item(iid, agg)
            rows = store.db.execute(
                "SELECT slug, item_ids FROM artifact_item_state WHERE source_id = ?",
                (agg,)).fetchall()
            assert all(json.loads(r["item_ids"]) == [] for r in rows), (
                "one item must never be written into two documents' groups")

            # Releasing the shared claim must also refuse -- it would strand the other.
            assert store.detach_source_location_by_hash(agg, h) == 0
            assert agg in store.sources_holding_item(iid)
        finally:
            store.db.close()

    def test_a_co_owned_loser_leaves_no_second_copy(self, tmp_path):
        """Collapses chain, and the third holder must come along.

        C loses an early round to B, so C is a location of B's items. B then loses to
        A. If only B's source is moved onto A's items, C still holds B's items, the
        delete degrades to a detach, and the same text stays searchable twice while
        the sweep reports the duplicate collapsed.
        """
        store = _mk_store(tmp_path)
        try:
            from kiro_crew.knowledge.dedup import _collapse_doc
            a = store.add_source(name="A", source_type="local_folder",
                                 uri=str(tmp_path / "a"))
            b = store.add_source(name="B", source_type="local_folder",
                                 uri=str(tmp_path / "b"))
            c = store.add_source(name="C", source_type="local_folder",
                                 uri=str(tmp_path / "c"))
            h = "d" * 64
            win = store.add_item(title="spec.md", content="same text",
                                 item_type="document", source_id=a, content_hash=h)
            store.add_source_location(win, a)
            lose = store.add_item(title="spec.md", content="same text",
                                  item_type="document", source_id=b, content_hash=h)
            store.add_source_location(lose, b)
            # C deferred to B in an earlier round: it holds B's item, owning nothing.
            store.add_source_location(lose, c)
            store.db.commit()

            winner = DocRef(
                source_id=a, source_type="local_folder", filename="spec.md",
                item_ids=[win], content_hash=h, embedding_sig=None,
                recency=2000.0, resident_since=1000.0, file_path="a/spec.md")
            loser = DocRef(
                source_id=b, source_type="local_folder", filename="spec.md",
                item_ids=[lose], content_hash=h, embedding_sig=None,
                recency=1000.0, resident_since=1000.0, file_path="b/spec.md")
            _collapse_doc(store, loser, winner)

            # The redundant copy is GONE, not merely detached.
            assert store.db.execute(
                "SELECT COUNT(*) AS n FROM items WHERE id = ?", (lose,)
            ).fetchone()["n"] == 0, "the loser's duplicate item must not survive"
            # ...and C did not lose the document: it now points at the winner.
            assert c in store.sources_holding_item(win), (
                "a source that deferred to the loser must follow to the winner")
            assert b in store.sources_holding_item(win)
        finally:
            store.db.close()

    def test_ownership_lookups_work_when_text_differs_from_bytes(self, tmp_path):
        """A folder row is related to items by the TEXT hash, not the file's bytes.

        `folder_file_state.content_hash` is sha256 of the file's raw bytes -- right
        for change detection, which must not have to run an extraction pass. Items
        are keyed by sha256 of the EXTRACTED TEXT. For .md/.txt those strings are
        equal, which is why plaintext fixtures never caught this; for a PDF, DOCX or
        HTML file they differ and every state-to-item lookup silently matched nothing,
        leaving the location bookkeeping inert for exactly those documents.
        """
        store = _mk_store(tmp_path)
        try:
            a = store.add_source(name="A", source_type="local_folder",
                                 uri=str(tmp_path / "a"))
            b = store.add_source(name="B", source_type="local_folder",
                                 uri=str(tmp_path / "b"))
            bytes_hash, text_hash = "a" * 64, "b" * 64   # a transformed document
            iid = store.add_item(title="runbook.html", content="Rotate the fleet.",
                                 item_type="document", source_id=a,
                                 content_hash=text_hash)
            store.add_source_location(iid, a)
            store.add_source_location(iid, b)
            # B lost the dedup: it owns nothing, and its row carries BOTH hashes.
            store.db.execute(
                "INSERT INTO folder_file_state (source_id, file_path, content_hash, "
                "text_hash, mtime, item_ids, last_seen, status, merged_into_source_id) "
                "VALUES (?, 'b/runbook.html', ?, ?, 1.0, '[]', '2024-01-01', "
                "'deduped', ?)", (b, bytes_hash, text_hash, a))
            store.db.commit()

            # Adoption must find B's row via the item's text hash.
            store.reassign_item_source(iid, b)
            store._adopt_reassigned_item(iid, b)
            row = store.db.execute(
                "SELECT item_ids, status FROM folder_file_state WHERE source_id = ?",
                (b,)).fetchone()
            assert json.loads(row["item_ids"]) == [iid], (
                "B must record the item it inherited, or its delete path drops an "
                "empty group and the content stays searchable with no file behind it")

            # And a detach keyed on the BYTES hash must not match items at all --
            # that mismatch is the whole defect.
            assert store.detach_source_location_by_hash(a, bytes_hash) == 0
            assert store.detach_source_location_by_hash(a, text_hash) == 1
        finally:
            store.db.close()


def _find_duplicates_reference(store, docs, threshold):
    """The pre-optimization dense O(n^2) find_duplicates, kept here as the oracle.

    The optimized find_duplicates must return exactly this list, in this order, for
    every input. Copied verbatim from the original implementation so the equivalence
    tests compare against the behaviour being replaced, not against the new code.
    """
    import kiro_crew.knowledge.dedup as dd

    actions = []
    removed = set()
    survivors = set()
    for i in range(len(docs)):
        if docs[i].key in removed:
            continue
        for j in range(i + 1, len(docs)):
            if docs[j].key in removed:
                continue
            reason = dd._match_reason(store, docs[i], docs[j], threshold)
            if not reason:
                continue
            winner, loser = dd.pick_winner(docs[i], docs[j])
            if loser.key in survivors:
                continue
            actions.append(dd.DedupAction(winner=winner, loser=loser, reason=reason))
            removed.add(loser.key)
            survivors.add(winner.key)
            if docs[i].key in removed:
                break
    return actions


def _action_tuples(actions):
    return [(a.winner.key, a.loser.key, a.reason) for a in actions]


class TestSweepIsNotQuadratic:
    """The scheduled sweep must not do O(n^2) filename work on a large corpus.

    find_duplicates once called _match_reason -- and through it SequenceMatcher and
    normalize_filename -- for every one of the n*(n-1)/2 cross pairs. On a 20k-doc
    Library that is ~2e8 pairs of pure-Python string work holding the GIL in the
    gateway thread. The optimized sweep buckets candidates (a content-hash index and,
    inside each embedding_sig bucket, a filename near-match found through a chain of
    exact upper bounds on the difflib ratio), so unrelated documents are never
    compared -- and its action list is identical to the dense scan's.
    """

    @staticmethod
    def _doc(idx, name, *, content_hash=None, embedding_sig=None, item_ids=None,
             source_id=None, source_type="local_file", recency=None):
        from kiro_crew.knowledge.dedup import DocRef
        return DocRef(
            source_id=source_id if source_id is not None else f"s{idx}",
            source_type=source_type, filename=name,
            item_ids=item_ids if item_ids is not None else [f"i{idx}"],
            content_hash=content_hash, embedding_sig=embedding_sig,
            recency=float(idx) if recency is None else recency,
            resident_since=float(idx))

    # ---- equivalence with the dense reference, over randomized corpora ----

    def _random_corpus(self, rng, n):
        """Mix every feature that steers _match_reason: shared hashes, near-typo
        names, dated series, same/different sources, shared item ids, mixed sigs."""
        stems = ["Quarterly Report", "Engineering Roadmap", "Budget Plan",
                 "Onboarding Guide", "Incident Postmortem", "Release Notes"]
        suffixes = ["", " (1)", " copy", " - copy", " Apr 2026", " Dec 2025",
                    " 2026-01-15", " 2026-01-16"]
        hashes = [None] + [f"h{k}" for k in range(n // 3 + 1)]
        sigs = [None, "", "sigA", "sigB", "sigC"]
        shared_item_pool = [f"shared{k}" for k in range(n // 5 + 1)]

        docs = []
        for k in range(n):
            stem = rng.choice(stems)
            # Occasionally inject a one-character typo so some stems are near but
            # not equal (the ratio-only branch of the match).
            name = stem + rng.choice(suffixes) + ".docx"
            if rng.random() < 0.15:
                pos = rng.randrange(len(name))
                name = name[:pos] + "x" + name[pos + 1:]
            ch = rng.choice(hashes)
            sig = rng.choice(sigs)
            src = f"src{rng.randrange(1, max(2, n // 2))}"
            if rng.random() < 0.1:
                item_ids = [rng.choice(shared_item_pool)]
            else:
                item_ids = [f"it{k}"]
            stype = rng.choice(["local_file", "local_folder", "obsidian_vault"])
            docs.append(self._doc(
                k, name, content_hash=ch, embedding_sig=sig, item_ids=item_ids,
                source_id=src, source_type=stype, recency=float(rng.randrange(1000))))
        # Give fuzzy pairs a chance to clear the cosine: equal-sig docs share a vector.
        vecs = {}
        for d in docs:
            if d.embedding_sig:
                d.embedding = vecs.setdefault(d.embedding_sig, [1.0, 0.0, 0.0])
        return docs

    def test_equivalence_with_reference_over_random_corpora(self):
        import random

        import kiro_crew.knowledge.dedup as dd

        store = object()  # embeddings are cached on the DocRefs, so the store is unused
        for seed in range(40):
            rng = random.Random(seed)
            n = rng.randrange(10, 120)
            docs = self._random_corpus(rng, n)
            # Fresh copies for each run so the lazy embedding cache does not leak.
            expected = _find_duplicates_reference(store, list(docs), 0.95)
            got = dd.find_duplicates(store, list(docs), 0.95)
            assert _action_tuples(got) == _action_tuples(expected), (
                f"seed={seed} n={n}: optimized action list diverged from the dense "
                f"reference")

    def test_equivalence_with_long_autojunk_names(self):
        """Names >= 200 chars trigger SequenceMatcher autojunk. quick_ratio/ratio can
        only shrink under autojunk, so the upper-bound chain still never drops a
        matching pair -- verify the optimized output still equals the reference."""
        import kiro_crew.knowledge.dedup as dd

        base = "Engineering Roadmap " + ("alpha beta gamma delta " * 12)  # > 200 chars
        assert len(base) >= 200
        store = object()
        vec = [1.0, 0.0, 0.0]
        docs = []
        long_names = [base + ".docx", base + " (1).docx",
                      base[:-1] + "x.docx", "Short Note.docx"]
        for k, name in enumerate(long_names):
            d = self._doc(k, name, embedding_sig="s1")
            d.embedding = vec
            docs.append(d)
        docs.append(self._doc(99, "Short Note copy.docx", embedding_sig="s2"))

        expected = _find_duplicates_reference(store, list(docs), 0.95)
        got = dd.find_duplicates(store, list(docs), 0.95)
        assert _action_tuples(got) == _action_tuples(expected)
        assert any(a.reason.startswith("fuzzy:") for a in got), (
            "the long-name near-duplicates must still collapse")

    # ---- the quadratic path is gone ----

    @staticmethod
    def _distinct_names(n):
        import random
        words = ["roadmap", "budget", "onboarding", "retro", "spec", "design",
                 "notes", "runbook", "postmortem", "proposal", "charter", "review",
                 "plan", "audit", "metrics", "sprint", "backlog", "okrs", "rfc",
                 "adr", "ledger", "pipeline", "gateway", "worker", "conductor",
                 "schema", "migration", "release", "hotfix", "incident"]
        rng = random.Random(1234)
        return [f"{rng.choice(words)}-{rng.choice(words)}-{rng.choice(words)}-{k}.md"
                for k in range(n)]

    def test_ratio_calls_are_bounded_for_2000_single_sig_docs(self):
        """~2000 docs with distinct names in ONE embedding_sig bucket (the realistic
        settled-Library case) must still trigger far fewer than n*(n-1)/2
        SequenceMatcher.ratio() calls. A single bucket is the worst case: a settled
        Library re-embeds to one signature, so the fuzzy tier cannot lean on sig
        bucketing and must prune by filename alone."""
        import math as _math

        import kiro_crew.knowledge.dedup as dd

        n = 2000
        names = self._distinct_names(n)
        # One sig for every document. A well-separated embedding DIRECTION per doc
        # keeps any incidental same-stem pair below the cosine threshold without
        # reading a store.
        docs = []
        for k, nm in enumerate(names):
            d = self._doc(k, nm, embedding_sig="ONESIG")
            theta = (k % 97) * (_math.pi / 97.0)
            d.embedding = [_math.cos(theta), _math.sin(theta), 0.0]
            docs.append(d)

        calls = {"ratio": 0}
        real = dd.SequenceMatcher

        class _Counting(real):  # type: ignore[misc,valid-type]
            def ratio(self):
                calls["ratio"] += 1
                return super().ratio()

        dd.SequenceMatcher = _Counting
        try:
            dd.find_duplicates(object(), docs, 0.95)
        finally:
            dd.SequenceMatcher = real

        dense_pairs = n * (n - 1) // 2  # ~2,000,000
        # The expensive SequenceMatcher.ratio() comparisons are gated behind the
        # length band and the cheap character-multiset bound, so even in one bucket
        # they stay orders of magnitude below the dense pair count.
        assert calls["ratio"] < dense_pairs // 100, (
            f"{calls['ratio']} ratio() calls for {n} single-sig docs (dense pairs = "
            f"{dense_pairs}) -- the expensive comparison is not being pruned")

    def test_same_source_series_runs_no_ratio_calls(self):
        """A watched folder of similarly named files in ONE source -- dated meeting
        notes, numbered exports, all one embedding_sig -- is the regression case:
        _match_reason rejects same-source pairs before any filename work, so the
        candidate index must not run SequenceMatcher on them at all, nor store an
        adjacency link for them. The names deliberately near-match each other so a
        source-blind adjacency builder would run ratio() on ~n^2 pairs and store a
        quadratic link map; the source guard must drive both to zero.
        """
        import kiro_crew.knowledge.dedup as dd

        n = 1000
        docs = []
        for k in range(n):
            d = self._doc(
                k, f"weekly meeting notes {k:05d}.md",
                embedding_sig="ONESIG", source_id="one-folder",
                source_type="local_folder")
            d.embedding = [1.0, 0.0, 0.0]  # identical direction: would clear cosine
            docs.append(d)

        calls = {"ratio": 0}
        real = dd.SequenceMatcher

        class _Counting(real):  # type: ignore[misc,valid-type]
            def ratio(self):
                calls["ratio"] += 1
                return super().ratio()

        dd.SequenceMatcher = _Counting
        try:
            index = dd._CandidateIndex(docs)
        finally:
            dd.SequenceMatcher = real

        assert calls["ratio"] == 0, (
            f"{calls['ratio']} ratio() calls for {n} same-source near-named files; "
            f"the single-source guard must skip the matcher entirely")
        # The adjacency map stores no links either -- its memory stays O(n), not n^2.
        stored_links = sum(
            len(v) for m in index._sig_adjacency.values() for v in m.values())
        assert stored_links == 0, (
            f"{stored_links} adjacency links stored for same-source stems; the guard "
            f"must store none")
        # And the sweep collapses nothing (same source never matches), without stall.
        assert dd.find_duplicates(object(), docs, 0.95) == []

    def test_source_guard_keeps_cross_source_series_matching(self):
        """The same-source guard must not drop a legitimate cross-source near-match:
        the identical dated series split across TWO folders still collapses, and its
        output equals the dense reference."""
        import kiro_crew.knowledge.dedup as dd

        docs = []
        for folder in ("A", "B"):
            for k in range(3):
                d = self._doc(
                    (0 if folder == "A" else 100) + k,
                    f"weekly meeting notes {k:05d}.md",
                    embedding_sig="ONESIG", source_id=f"folder{folder}",
                    source_type="local_folder",
                    item_ids=[f"{folder}{k}"])
                d.embedding = [1.0, 0.0, 0.0]
                docs.append(d)

        got = dd.find_duplicates(object(), list(docs), 0.95)
        assert _action_tuples(got) == _action_tuples(
            _find_duplicates_reference(object(), list(docs), 0.95))
        assert got, "the cross-source dated series must still collapse"
        assert all(a.reason.startswith("fuzzy:") for a in got)

    def test_asymmetric_ratio_pair_is_not_dropped(self):
        """SequenceMatcher.ratio() is not symmetric, and _match_reason compares stems
        in doc-index order. A pair whose index-order ratio clears the floor only in
        one direction must still be surfaced -- the candidate filter takes the max of
        both orders."""
        import kiro_crew.knowledge.dedup as dd

        vec = [1.0, 0.0, 0.0]

        def mk(k, nm):
            d = self._doc(k, nm, embedding_sig="s1")
            d.embedding = vec
            return d

        # Doc 0 holds the LONGER stem: ratio(long, short) >= 0.9 but
        # ratio(short, long) < 0.9, so a length-sorted one-direction gate would drop
        # the pair that _match_reason (index order, long first) collapses.
        docs = [mk(0, "meeting tema amlplha budget.docx"),
                mk(1, "meeting team alpha budget.docx")]
        got = dd.find_duplicates(object(), docs, 0.95)
        assert _action_tuples(got) == _action_tuples(
            _find_duplicates_reference(object(), list(docs), 0.95))
        assert len(got) == 1 and got[0].reason.startswith("fuzzy:"), (
            "the asymmetric near-duplicate must still collapse")

    def test_result_matches_brute_force_on_a_mixed_db(self, tmp_path):
        """End-to-end against a real KnowledgeStore: the optimized find_duplicates
        over enumerate_docs must equal the dense reference over the same docs."""
        import kiro_crew.knowledge.dedup as dd

        store = _mk_store(tmp_path)
        try:
            vec = [1.0, 0.0, 0.0]
            _add_upload(store, "Quarterly Report.docx", "h-a", vec, sig="e1")
            _add_folder_file(store, "/f/Quarterly Report (1).docx", "h-b", vec,
                             sig="e1")
            _add_upload(store, "alpha.md", "dup-hash", [0.0, 1.0, 0.0], sig="e2")
            _add_folder_file(store, "/f/beta.md", "dup-hash", [0.0, 1.0, 0.0],
                             sig="e3")
            for k in range(30):
                _add_upload(store, f"noise-{k}.md", f"nh{k}",
                            [0.0, 0.0, float(k + 1)], sig=f"n{k}")

            expected = _find_duplicates_reference(store, dd.enumerate_docs(store), 0.95)
            got = dd.find_duplicates(store, dd.enumerate_docs(store), 0.95)
            assert _action_tuples(got) == _action_tuples(expected)
            reasons = sorted(r for *_, r in _action_tuples(got))
            assert any(r == "exact" for r in reasons)
            assert any(r.startswith("fuzzy:") for r in reasons)
        finally:
            store.db.close()

    def test_same_source_hash_bucket_does_not_store_quadratic_pairs(self):
        """A watched folder full of identical-content files (every one in a single
        source, every one sharing a content hash) is a plausible real input --
        placeholder files, scanned PDFs with no extractable text. The candidate
        generator must not emit its n*(n-1)/2 pairs, which the same-source guard would
        reject anyway, or the scheduled sweep exhausts memory."""
        import kiro_crew.knowledge.dedup as dd

        n = 4000
        docs = [dd.DocRef(
            source_id="one-folder", source_type="local_folder",
            filename=f"file{k}.pdf", item_ids=[f"i{k}"], content_hash="SAMEHASH",
            embedding_sig=None, recency=float(k), resident_since=float(k),
            file_path=f"/f/file{k}.pdf") for k in range(n)]

        index = dd._CandidateIndex(docs)
        stored = sum(len(index.candidates_after(i)) for i in range(n))
        assert stored == 0, (
            f"{stored} candidates emitted for {n} same-source identical files; "
            f"the same-source guard must drop them")
        # And the sweep returns nothing (same source never collapses), without crash.
        assert dd.find_duplicates(object(), docs, 0.95) == []

    def test_cross_source_same_stem_bucket_generates_candidates_lazily(self):
        """Thousands of same-named files across two watched folders (distinct hashes,
        one embedding signature) are legitimate cross-source candidates. The generator
        must never hold the whole n*(n-1)/2 pair graph at once: each document's
        candidate list is produced and consumed on its own, so peak storage is one
        document's candidates, not millions of pairs."""
        import kiro_crew.knowledge.dedup as dd

        per_folder = 2000
        docs = []
        for folder in ("A", "B"):
            for k in range(per_folder):
                docs.append(dd.DocRef(
                    source_id=f"folder{folder}", source_type="local_folder",
                    filename="report.md", item_ids=[f"{folder}{k}"],
                    content_hash=f"h{folder}{k}", embedding_sig="ONESIG",
                    recency=float(k), resident_since=float(k),
                    file_path=f"/{folder}/report{k}.md"))

        index = dd._CandidateIndex(docs)
        # No single document's candidate list is the whole graph: a report.md in
        # folder A can only pair with the other folder's report.md files that come
        # later in index order, never all 4,000.
        peak = max(len(index.candidates_after(i)) for i in range(len(docs)))
        assert peak <= per_folder, (
            f"peak per-document candidate list {peak} exceeds one folder's worth; "
            f"the generator is retaining too much")

    def test_cross_source_shared_item_ids_are_not_stored(self):
        """Two documents that reference the same item set are one physical item, not
        two documents; _match_reason rejects them on the shared item id, so the
        candidate generator must not emit the pair either."""
        import kiro_crew.knowledge.dedup as dd

        docs = [
            dd.DocRef(source_id="sA", source_type="local_file", filename="x.md",
                      item_ids=["shared"], content_hash="h", embedding_sig=None,
                      recency=1.0, resident_since=1.0),
            dd.DocRef(source_id="sB", source_type="local_file", filename="x.md",
                      item_ids=["shared"], content_hash="h", embedding_sig=None,
                      recency=2.0, resident_since=2.0),
        ]
        index = dd._CandidateIndex(docs)
        assert sum(len(index.candidates_after(i)) for i in range(len(docs))) == 0
        assert _action_tuples(dd.find_duplicates(object(), list(docs), 0.95)) == \
            _action_tuples(_find_duplicates_reference(object(), list(docs), 0.95))

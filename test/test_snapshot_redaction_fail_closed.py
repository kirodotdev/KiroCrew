"""A table the pass cannot read fails CLOSED, and validation matches what readers need.

Both are the same mistake in different places: a branch that could not do its job chose to
continue rather than refuse. The redaction pass exists to keep credentials off the wire, so
"could not inspect this table" cannot mean "ship it".
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import shutil
import tarfile
from pathlib import Path

import pytest
from test_snapshot import unpinnable_argv

from kiro_crew import snapshot as snap
from kiro_crew import snapshot_redact as redact

TOKEN = "8412345678:AAH9xSECRETtokenvalue_here12345"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(snap, "_mc_dir", lambda: h)
    monkeypatch.setattr(snap, "_is_gateway_running", lambda: False)
    return h


class TestATableThatCannotBeInspectedRefusesTheUpload:
    def test_a_without_rowid_table_is_not_silently_skipped(self, tmp_path):
        """`WITHOUT ROWID` has no rowid handle -- the database must go, not the table."""
        db = tmp_path / "memory.db"
        conn = redact.sqlite3.connect(str(db))
        conn.execute("CREATE TABLE norowid (k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID")
        conn.execute("INSERT INTO norowid VALUES (?, ?)", ("aws", f"token {TOKEN}"))
        conn.commit()
        conn.close()

        stage = tmp_path / "stage"
        stage.mkdir()
        (db).replace(stage / "memory.db")
        (stage / "MANIFEST.json").write_text(
            json.dumps({"version": 3, "components": {"memory": "unresolved"}}),
            encoding="utf-8",
        )

        # Refused, not dropped: deleting the database this backup exists to carry and
        # uploading the remainder yields an off-host copy that reports success and
        # restores nothing, discovered only once the machine it came from is gone.
        with pytest.raises(redact.PayloadDatabaseUnprovable) as e:
            redact.redact_bundle_for_egress(stage)
        assert "memory.db" in e.value.paths
        assert (
            stage / "memory.db"
        ).exists(), "the payload was deleted instead of the upload being refused"
        assert any("memory.db" in d for d in e.value.details), e.value.details

    def test_an_ordinary_table_is_still_redacted_in_place(self, tmp_path):
        """The refusal must not swallow the normal path."""
        db = tmp_path / "memory.db"
        conn = redact.sqlite3.connect(str(db))
        conn.execute("CREATE TABLE ok (k, v)")
        conn.execute("INSERT INTO ok VALUES (?, ?)", ("aws", f"token {TOKEN}"))
        conn.commit()
        conn.close()

        stage = tmp_path / "stage"
        stage.mkdir()
        db.replace(stage / "memory.db")
        report = redact.redact_bundle_for_egress(stage)

        assert (stage / "memory.db").is_file(), "an inspectable database was dropped"
        assert report.replacements.get("memory.db"), report.replacements
        conn = redact.sqlite3.connect(str(stage / "memory.db"))
        assert conn.execute("PRAGMA integrity_check;").fetchone()[0] == "ok"
        assert TOKEN not in conn.execute("SELECT v FROM ok").fetchone()[0]
        conn.close()

    def test_no_branch_skips_a_table_it_could_not_read(self):
        """A table that cannot be read must refuse; a DERIVED one may be skipped.

        Pinned as two properties rather than a ban on `continue`, because the FTS shadow
        tables are legitimately skipped -- they are regenerated from the content table, so
        skipping them is part of redacting rather than a hole in it. What must never come
        back is turning an unreadable table into a silent pass.
        """
        import inspect

        src = inspect.getsource(redact._redact_database)
        assert (
            "except sqlite3.DatabaseError:\n                    continue" not in src
        ), "an unreadable table is skipped instead of refusing the database"
        # Asserted as the STATES that must refuse, not as a count of occurrences in one
        # function. The count broke the moment the paged row read was extracted into a helper
        # -- the refusal had moved, not disappeared -- which is a refactor, not a regression.
        # Naming each state also says WHICH one went missing when this fails.
        source = src + inspect.getsource(redact._paged_rows)
        for state, fragment in (
            ("a table with no readable columns", "no readable columns"),
            ("a database that never settles", "did not settle"),
            ("a NUL-bearing text value", "NUL-bearing text value"),
            ("a byte-valued field that is not text", "byte-valued field carries a"),
            ("a table that cannot be read at all", "{table}: {e}"),
        ):
            assert fragment in source, (
                f"the refusal for {state} is gone; every state this pass cannot prove clean "
                "must refuse rather than skip"
            )
        assert (
            "if table in skip_tables:" in src
        ), "only positively-identified derived tables may be skipped"

    def test_an_fts_database_survives_and_loses_the_credential(self, tmp_path):
        """The fail-closed rule must not delete the Knowledge Library.

        FTS5 keeps two `WITHOUT ROWID` shadow tables, so refusing on that basis would drop
        every knowledge database from the outbound copy. Redacting the content and
        rebuilding the index is what removes the term: the inverted index holds it as
        index structure, so a redacted content table with a stale index still answers a
        search for the credential.
        """
        key = "ghp_" + "C" * 36
        stage = tmp_path / "stage"
        (stage / "workspace" / "knowledge").mkdir(parents=True)
        db = stage / "workspace" / "knowledge" / "knowledge.db"

        conn = redact.sqlite3.connect(str(db))
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, title, content)")
        conn.execute("CREATE VIRTUAL TABLE items_fts USING fts5(title, content)")
        conn.execute("INSERT INTO items (title, content) VALUES (?, ?)", ("n", f"key {key} here"))
        conn.execute(
            "INSERT INTO items_fts (rowid, title, content) VALUES (?, ?, ?)",
            (1, "n", f"key {key} here"),
        )
        conn.commit()
        conn.close()

        assert key.encode() in db.read_bytes(), "premise: the key is in the file"
        report = redact.redact_bundle_for_egress(stage)

        assert db.is_file(), f"the knowledge database was deleted: {report.dropped}"
        assert (
            key.encode() not in db.read_bytes()
        ), "the credential survived in the file, most likely in the inverted index"

        conn = redact.sqlite3.connect(str(db))
        assert conn.execute("PRAGMA integrity_check;").fetchone()[0] == "ok"
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT count(*) FROM items_fts WHERE items_fts MATCH ?", (key,)
            ).fetchone()[0]
            == 0
        ), "the index still matches the redacted credential"
        assert (
            conn.execute(
                "SELECT count(*) FROM items_fts WHERE items_fts MATCH ?", ("here",)
            ).fetchone()[0]
            == 1
        ), "redaction broke ordinary search"
        conn.close()

    def test_a_contentless_fts_index_refuses_the_upload(self, tmp_path):
        """A contentless index has no content table, so the skip-and-rebuild rule is false.

        The FTS storage tables are skipped because they are DERIVED: cleaning the content
        table and rebuilding the index is what removes the term. `content=''` has no content
        table at all, so its tokens live ONLY in that storage -- skipping it means nothing
        ever examines them and nothing regenerates them from a cleaned source.

        Measured before this guard existed: an AWS access key is a separator-free
        alphanumeric run, so FTS5 stores it as one token instead of splitting it, the scanner
        matches that shape, and a full pass reported `replacements={}` while the token sat in
        the file. Lowercasing by the tokenizer is not a control -- an `sk`-style or hex key is
        lowercase to begin with.
        """
        db = tmp_path / "memory.db"
        conn = redact.sqlite3.connect(str(db))
        conn.execute("CREATE VIRTUAL TABLE notes USING fts5(body, content='')")
        conn.execute("INSERT INTO notes(rowid, body) VALUES (1, ?)", (f"key {TOKEN} here",))
        conn.commit()
        conn.close()

        stage = tmp_path / "stage"
        stage.mkdir()
        db.replace(stage / "memory.db")

        with pytest.raises(redact.PayloadDatabaseUnprovable) as e:
            redact.redact_bundle_for_egress(stage)
        assert "memory.db" in e.value.paths
        assert any("contentless" in d for d in e.value.details), e.value.details
        # Refused, not deleted: dropping the database this backup exists to carry would
        # turn a provable-cleanliness problem into data loss.
        assert (stage / "memory.db").exists()

    def test_an_external_content_fts_database_is_redacted_and_kept(self, tmp_path):
        """The shape this product actually uses: `content=items, content_rowid=id`.

        With external content there is no `_content` shadow table and the virtual table is
        READ-ONLY for content, so the term can only leave the index by rebuilding it from
        the redacted base table. A fixture using a standard fts5 table passes without the
        rebuild for the wrong reason -- writing through the virtual table happens to fix
        both halves there -- which is why this case is pinned separately.
        """
        key = "ghp_" + "E" * 36
        stage = tmp_path / "stage"
        stage.mkdir()
        db = stage / "knowledge.db"

        conn = redact.sqlite3.connect(str(db))
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, title, content, tags)")
        conn.execute(
            "CREATE VIRTUAL TABLE items_fts USING fts5("
            "title, content, tags, content=items, content_rowid=id)"
        )
        conn.execute(
            "INSERT INTO items (title, content, tags) VALUES (?, ?, ?)",
            ("n", f"k {key} z", "t"),
        )
        conn.execute("INSERT INTO items_fts(items_fts) VALUES('rebuild')")
        conn.commit()
        assert (
            conn.execute(
                "SELECT count(*) FROM items_fts WHERE items_fts MATCH ?", (key,)
            ).fetchone()[0]
            == 1
        ), "premise: the index matches the key"
        conn.close()

        report = redact.redact_bundle_for_egress(stage)
        assert db.is_file(), f"the knowledge database was deleted: {report.dropped}"
        assert (
            key.encode() not in db.read_bytes()
        ), "the credential survived in the index -- the rebuild did not run"

        conn = redact.sqlite3.connect(str(db))
        assert conn.execute("PRAGMA integrity_check;").fetchone()[0] == "ok"
        assert (
            conn.execute(
                "SELECT count(*) FROM items_fts WHERE items_fts MATCH ?", (key,)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM items_fts WHERE items_fts MATCH ?", ("z",)
            ).fetchone()[0]
            == 1
        ), "redaction broke ordinary search"
        conn.close()

    def test_an_unrecognised_rowidless_table_still_refuses(self, tmp_path):
        """The exemption is for identified FTS shadow storage, not for rowid-less tables."""
        stage = tmp_path / "stage"
        stage.mkdir()
        db = stage / "memory.db"
        conn = redact.sqlite3.connect(str(db))
        conn.execute("CREATE TABLE odd (k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID")
        conn.execute("INSERT INTO odd VALUES ('a', 'b')")
        conn.commit()
        conn.close()

        # Refused, not dropped: deleting the database this backup exists to carry and
        # uploading the remainder yields an off-host copy that reports success and
        # restores nothing, discovered only once the machine it came from is gone.
        with pytest.raises(redact.PayloadDatabaseUnprovable) as e:
            redact.redact_bundle_for_egress(stage)
        assert "memory.db" in e.value.paths, "a rowid-less table outside FTS was silently skipped"
        assert db.exists()


class TestADeclaredRowidColumnDoesNotBecomeTheUpdateHandle:
    """`rowid` is not reserved, so a schema can take the name -- and then `WHERE rowid = ?`
    matches that COLUMN. Nothing raises; the write just lands on every row sharing the
    value. A pass that exists to protect the off-host copy must not corrupt it."""

    def _staged(self, tmp_path, schema, rows):
        db = tmp_path / "memory.db"
        conn = redact.sqlite3.connect(str(db))
        conn.execute(schema)
        conn.executemany(f"INSERT INTO shadow VALUES ({', '.join('?' * len(rows[0]))})", rows)
        conn.commit()
        conn.close()
        stage = tmp_path / "stage"
        stage.mkdir()
        db.replace(stage / "memory.db")
        return stage

    def test_a_shadowed_rowid_does_not_clobber_sibling_rows(self, tmp_path):
        """The regression: two rows share the shadowing value, only one holds a credential."""
        stage = self._staged(
            tmp_path,
            'CREATE TABLE shadow ("rowid" TEXT, secret TEXT)',
            [("dup", f"token {TOKEN}"), ("dup", "keep-me"), ("other", "keep-me-too")],
        )
        report = redact.redact_bundle_for_egress(stage)

        assert report.replacements.get("memory.db"), report.replacements
        conn = redact.sqlite3.connect(str(stage / "memory.db"))
        got = dict(
            (i, v)
            for i, (v,) in enumerate(conn.execute("SELECT secret FROM shadow ORDER BY _rowid_"))
        )
        conn.close()
        assert TOKEN not in got[0], "the credential was not removed"
        assert got[1] == "keep-me", f"an unrelated row was overwritten: {got[1]!r}"
        assert got[2] == "keep-me-too", f"an unrelated row was overwritten: {got[2]!r}"

    def test_the_shadowing_column_is_itself_still_scanned(self, tmp_path):
        """It is a column like any other -- a credential inside it must still go."""
        stage = self._staged(
            tmp_path,
            'CREATE TABLE shadow ("rowid" TEXT, v TEXT)',
            [(f"token {TOKEN}", "x")],
        )
        redact.redact_bundle_for_egress(stage)

        conn = redact.sqlite3.connect(str(stage / "memory.db"))
        held = conn.execute('SELECT "rowid" FROM shadow').fetchone()[0]
        conn.close()
        assert TOKEN not in held, "the shadowing column was skipped instead of scanned"

    def test_a_table_shadowing_every_alias_refuses_the_upload(self, tmp_path):
        """No unique handle left, so this is the WITHOUT ROWID case: the database goes."""
        stage = self._staged(
            tmp_path,
            'CREATE TABLE shadow ("rowid" TEXT, "_rowid_" TEXT, "oid" TEXT, v TEXT)',
            [("a", "b", "c", f"token {TOKEN}")],
        )
        with pytest.raises(redact.PayloadDatabaseUnprovable) as e:
            redact.redact_bundle_for_egress(stage)
        assert "memory.db" in e.value.paths
        assert (
            stage / "memory.db"
        ).exists(), "the payload was deleted instead of the upload being refused"

    def test_shadowing_is_detected_regardless_of_case(self, tmp_path):
        """SQLite identifiers are case-insensitive, so `RowID` shadows `rowid` completely."""
        stage = self._staged(
            tmp_path,
            'CREATE TABLE shadow ("RowID" TEXT, secret TEXT)',
            [("dup", f"token {TOKEN}"), ("dup", "keep-me")],
        )
        redact.redact_bundle_for_egress(stage)

        conn = redact.sqlite3.connect(str(stage / "memory.db"))
        rows = [v for (v,) in conn.execute("SELECT secret FROM shadow ORDER BY _rowid_")]
        conn.close()
        assert TOKEN not in rows[0]
        assert rows[1] == "keep-me", f"case-different shadowing was missed: {rows[1]!r}"

    def test_the_alias_resolver_prefers_rowid_and_refuses_only_when_exhausted(self):
        """Unit-level, so the ordering is pinned without building four databases."""
        assert redact._rowid_alias(["a", "b"]) == "rowid"
        assert redact._rowid_alias(["rowid"]) == "_rowid_"
        assert redact._rowid_alias(["rowid", "_rowid_"]) == "oid"
        assert redact._rowid_alias(["RowID", "_ROWID_"]) == "oid"
        with pytest.raises(redact._TableNotInspectable):
            redact._rowid_alias(["rowid", "_rowid_", "oid"])

    def test_the_select_and_the_update_cannot_drift_apart(self):
        """Two statements, one handle. If a future edit hardcodes either, the pass writes
        by a different key than it read by -- the corruption returns silently."""
        src = Path(redact.__file__).read_text(encoding="utf-8")
        body = src[src.index("def _redact_database") :]
        assert "WHERE rowid = ?" not in body, "the UPDATE hardcodes rowid again"
        assert "SELECT rowid," not in body, "the SELECT hardcodes rowid again"
        # The SELECT now lives in the paging helper, so counting `{handle}` in one function
        # no longer expresses the property -- and the property itself got STRONGER: the alias
        # is computed once in `_redact_database` and PASSED to the helper, so the two
        # statements cannot drift by construction rather than by convention. Asserted as
        # that: the helper takes the handle as a parameter and interpolates it, and the caller
        # hands it the same variable the UPDATE uses.
        paged = inspect.getsource(redact._paged_rows)
        assert "handle: str" in paged, "the paging helper no longer receives the handle"
        assert "{handle}" in paged, "the SELECT stopped interpolating the passed handle"
        assert "_paged_rows(conn, table, handle, quoted)" in body, (
            "the caller must pass the SAME `handle` local that the UPDATE interpolates, or "
            "the read and the write can key on different columns again"
        )
        assert "{handle}" in body, "the UPDATE stopped interpolating the handle"


class TestTheRollbackLedgerRecordsWhatWasTouchedNotWhatWasDeclared:
    """`installed` decides whether recovery DELETES a file, so it must mean "reached".

    It was populated with every file the component DECLARES, before the copy loop ran. A
    bundle may legitimately carry only some of them and the loop skips the rest, so those
    files were never backed up and never written -- yet recovery saw "installed, no saved
    copy, target exists", concluded the restore had created them, and deleted the operator's
    own untouched files while reporting a successful rollback.

    Both halves are asserted here, because a fix that only stops the deletion could do it by
    breaking the legitimate undo.
    """

    def _bundle_for_config(self, tmp_path, carried):
        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        payload.mkdir(parents=True)
        for name in carried:
            (payload / name).write_text("FROM ARCHIVE", encoding="utf-8")
        (payload / "MANIFEST.json").write_text(
            json.dumps({"version": 3, "components": {"config": "unresolved"}}), encoding="utf-8"
        )
        bundle = tmp_path / "b.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)
        return bundle

    def test_a_file_the_bundle_omits_is_not_deleted_by_the_rollback(self, home, tmp_path):
        declared = list(snap.CORE_FILES["config"])
        carried, omitted = declared[:1], declared[1:]
        assert omitted, "this test needs a component with more than one core file"
        for name in declared:
            (home / name).write_text(f"OPERATOR DATA {name}", encoding="utf-8")

        installed: set[str] = set()
        backup = tmp_path / "pre-restore-probe"
        backup.mkdir()
        payload = tmp_path / "payload"
        payload.mkdir()
        for name in carried:
            (payload / name).write_text("FROM ARCHIVE", encoding="utf-8")

        snap._do_replace_mutations(
            payload, home, backup, ["config"], [], installed, allow_unpinned=True
        )
        assert installed == set(carried), (
            "the ledger recorded files the copy loop skipped: "
            f"{sorted(installed - set(carried))}"
        )

        failed = snap._restore_everything_from_rollback(
            backup, home, declared, installed, allow_unpinned=bool(unpinnable_argv())
        )
        assert failed == [], failed
        for name in omitted:
            live = home / name
            assert live.is_file(), f"rollback deleted {name!r}, which this run never touched"
            assert live.read_text(encoding="utf-8") == f"OPERATOR DATA {name}"

    def test_a_file_this_run_really_created_is_still_undone(self, home, tmp_path):
        """The other half: the legitimate undo must survive the fix.

        `hooks.json` is absent live and present in the bundle, so the restore CREATES it.
        Recovery has no saved copy to put back and must remove the creation -- that is what
        "no pre-restore state" restores to. A fix that simply stopped recording would leave
        this file behind and silently keep a file the operator never had.
        """
        created = "hooks.json"
        assert not (home / created).exists()

        installed: set[str] = set()
        backup = tmp_path / "pre-restore-probe"
        backup.mkdir()
        payload = tmp_path / "payload"
        payload.mkdir()
        (payload / created).write_text("FROM ARCHIVE", encoding="utf-8")

        snap._do_replace_mutations(
            payload, home, backup, ["config"], [], installed, allow_unpinned=True
        )
        assert (home / created).is_file(), "the restore did not create the file"
        assert created in installed

        failed = snap._restore_everything_from_rollback(
            backup,
            home,
            list(snap.CORE_FILES["config"]),
            installed,
            allow_unpinned=bool(unpinnable_argv()),
        )
        assert failed == [], failed
        assert not (
            home / created
        ).exists(), "a file this run created was left behind by the rollback"


class TestAPinnedRefusalMidMutationStillRollsBack:
    """A refusal is not an OSError, and the rollback handler must still see it.

    The mutation phase reaches the pinned primitives, which REFUSE rather than raise
    OSError: `must_create=True` rejects a root recreated after its own rmtree, and a fatal
    skip reporter rejects a file it cannot copy whole. Both fire mid-mutation. If the
    handler only names OSError, the one failure class phase-two rollback exists for walks
    past it and leaves live state half replaced.
    """

    def test_a_refusal_during_the_mutation_phase_triggers_recovery(
        self, home, tmp_path, capsys, monkeypatch
    ):
        # `home` is the fixture that points `snap._mc_dir` at an isolated directory. Without
        # it this test passes VACUOUSLY: the restore writes into the ambient data home while
        # the assertions inspect an untouched tmp tree, so "ORIGINAL survived" is true because
        # nothing ever ran. Caught by mutation-testing the handler and seeing green.
        (home / "workspace" / "memory").mkdir(parents=True)
        (home / "workspace" / "memory" / "keep.md").write_text("ORIGINAL", encoding="utf-8")
        # A REAL database on both sides: the pre-flight refuses an unopenable payload
        # database before the mutation phase, so a byte-string stand-in would never let this
        # invariant be reached.
        live = redact.sqlite3.connect(str(home / "memory.db"))
        live.execute("CREATE TABLE t(v TEXT)")
        live.execute("INSERT INTO t VALUES ('LIVE')")
        live.commit()
        live.close()

        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        (payload / "workspace" / "memory").mkdir(parents=True)
        (payload / "workspace" / "memory" / "keep.md").write_text("INCOMING", encoding="utf-8")
        # Both of memory's declared trees, or the pre-flight refuses the bundle for lacking
        # one the live home has -- a refusal that would fire before the mutation phase and so
        # would not exercise this invariant at all.
        (payload / "workspace" / "knowledge").mkdir(parents=True)
        (payload / "workspace" / "knowledge" / "note.md").write_text("kb", encoding="utf-8")
        incoming = redact.sqlite3.connect(str(payload / "memory.db"))
        incoming.execute("CREATE TABLE t(v TEXT)")
        incoming.execute("INSERT INTO t VALUES ('INCOMING')")
        incoming.commit()
        incoming.close()
        (payload / "MANIFEST.json").write_text(
            json.dumps({"version": 3, "components": {"memory": "unresolved"}}), encoding="utf-8"
        )
        bundle = tmp_path / "b.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        # A refusal, not an OSError, raised from inside the mutation phase. Scoped to copies
        # out of the bundle so the recovery leg still works.
        real = snap.pinned_fs.copy_file_pinned

        def refuse_the_incoming_db(src, *a, **k):
            if "pre-restore-" not in str(src) and "memory.db" in str(src):
                raise snap.pinned_fs.PinnedPathRefusal("refusing: simulated pin failure")
            return real(src, *a, **k)

        monkeypatch.setattr(snap.pinned_fs, "copy_file_pinned", refuse_the_incoming_db)
        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
        )
        out = capsys.readouterr().out

        assert "Replace mode" in out, f"the restore never reached the mutation phase: {out}"
        assert rc == 1, out
        assert "Traceback" not in out, out
        # The point of the invariant: recovery RAN and put the original back.
        assert (home / "workspace" / "memory" / "keep.md").read_text(
            encoding="utf-8"
        ) == "ORIGINAL", "a mid-mutation refusal skipped the rollback"
        back = redact.sqlite3.connect(str(home / "memory.db"))
        rows = [r[0] for r in back.execute("SELECT v FROM t")]
        back.close()
        assert rows == ["LIVE"], f"the live database was not put back: {rows}"


class TestARewriteNeverLandsInAContainerThatRecordsItsOwnExtents:
    """The guard is the MECHANISM, not the file type.

    An earlier version keyed on `%PDF-` and its comment claimed measurement had shown the
    ASCII PDF to be the one text container that reaches the rewrite. That measurement covered
    BINARY containers (ZIP, gzip, PNG, tar, Flate-PDF, all already refused by the NUL test)
    and was over-read into a claim about all of them. A text WARC is the counterexample:
    UTF-8, NUL-free, and every record declares its own `Content-Length`.
    """

    def test_a_warc_is_refused_rather_than_shifted(self, tmp_path):
        warc = tmp_path / "crawl.warc"
        body = "key=AKIAIOSFODNN7EXAMPLE\r\n"
        warc.write_text(
            "WARC/1.0\r\nWARC-Type: response\r\n" f"Content-Length: {len(body)}\r\n\r\n{body}\r\n",
            encoding="utf-8",
        )
        before = warc.read_bytes()

        handled = redact._redact_text_file(warc, redact.RedactionReport(), "crawl.warc")

        assert handled is False, "the WARC was accepted as plain text and rewritten"
        assert warc.read_bytes() == before

    def test_a_pdf_is_still_refused_by_its_offset_table(self, tmp_path):
        """The PDF case must survive the switch from magic to mechanism."""
        pdf = tmp_path / "doc.pdf"
        pdf.write_text(
            "%PDF-1.4\ncred AKIAIOSFODNN7EXAMPLE\nxref\n0 2\nstartxref\n9\n%%EOF\n",
            encoding="utf-8",
        )
        before = pdf.read_bytes()

        handled = redact._redact_text_file(pdf, redact.RedactionReport(), "doc.pdf")

        assert handled is False
        assert pdf.read_bytes() == before

    def test_ordinary_text_is_still_redacted(self, tmp_path):
        note = tmp_path / "note.md"
        note.write_text("my key is AKIAIOSFODNN7EXAMPLE\nnext line\n", encoding="utf-8")

        assert redact._redact_text_file(note, redact.RedactionReport(), "note.md") is True
        body = note.read_text(encoding="utf-8")
        assert "AKIAIOSFODNN7EXAMPLE" not in body
        assert "next line" in body

    def test_a_file_too_large_to_hold_is_refused_before_being_read(self, tmp_path, monkeypatch):
        """Refused on its SIZE, before `read_text` -- the only point where it helps.

        `read_text` plus the scrubbed copy plus each redactor's intermediate hold several
        multiples of the file at once, so an archive-sized member ends the process with
        nothing uploaded.
        """
        big = tmp_path / "huge.log"
        big.write_text("key=AKIAIOSFODNN7EXAMPLE\n", encoding="utf-8")
        monkeypatch.setattr(redact, "_MAX_REDACTABLE_TEXT_BYTES", 4)
        opened = {"count": 0}
        real_read = type(big).read_text

        def counting_read(self, *a, **k):
            opened["count"] += 1
            return real_read(self, *a, **k)

        monkeypatch.setattr(type(big), "read_text", counting_read)

        with pytest.raises(redact._FileUnreadable):
            redact._redact_text_file(big, redact.RedactionReport(), "huge.log")
        assert opened["count"] == 0, "the file was read before its size was checked"


class TestAnFtsTriggerIsExemptOnlyForTheDeletionsThatAreMaintenance:
    """The exemption is per DELETE, not per trigger.

    The first version waved a whole trigger through as soon as any FTS name appeared anywhere
    in it, so a trigger that maintains the index AND deletes unrelated rows was exempt -- a
    rule justified for one of a thing's statements, applied to all of them.
    """

    def _db(self, path, trigger_body):
        conn = redact.sqlite3.connect(str(path))
        conn.executescript(f"""
            CREATE TABLE notes(id INTEGER PRIMARY KEY, body TEXT);
            CREATE TABLE audit(id INTEGER PRIMARY KEY, note TEXT);
            CREATE VIRTUAL TABLE notes_fts USING fts5(body);
            INSERT INTO notes(body) VALUES ('key=AKIAIOSFODNN7EXAMPLE');
            INSERT INTO audit(note) VALUES ('row the operator needs');
            CREATE TRIGGER t AFTER UPDATE ON notes BEGIN {trigger_body} END;
            """)
        conn.commit()
        conn.close()

    def test_a_trigger_that_maintains_fts_and_also_deletes_elsewhere_refuses(self, tmp_path):
        db = tmp_path / "memory.db"
        self._db(
            db,
            "DELETE FROM notes_fts WHERE rowid = OLD.id; DELETE FROM audit;",
        )

        with pytest.raises(redact._PayloadUnprovable):
            redact._redact_database(db, redact.RedactionReport(), "memory.db", product=True)

        conn = redact.sqlite3.connect(str(db))
        rows = conn.execute("SELECT count(*) FROM audit").fetchone()[0]
        conn.close()
        assert rows == 1, "the unrelated table was emptied despite the refusal"

    def test_a_trigger_whose_only_deletion_is_fts_maintenance_still_rides(self, tmp_path):
        """The must-still-work half: a full-text index is kept in step by deleting its rows.

        A plain (not external-content) fts5 table is used deliberately: `DELETE FROM` is legal
        there, whereas on an external-content table the sync idiom is
        `INSERT INTO t(t, rowid, ...) VALUES('delete', ...)` and a raw DELETE corrupts the
        index -- which is what the first version of this test did, and sqlite said so.
        """
        db = tmp_path / "memory.db"
        self._db(db, "DELETE FROM notes_fts WHERE rowid = OLD.id;")

        redact._redact_database(db, redact.RedactionReport(), "memory.db", product=True)

        conn = redact.sqlite3.connect(str(db))
        body = conn.execute("SELECT body FROM notes").fetchone()[0]
        audit = conn.execute("SELECT count(*) FROM audit").fetchone()[0]
        conn.close()
        assert "AKIA" not in body, "the credential survived"
        assert audit == 1


class TestATriggerThatDeletesRowsRefusesTheUpload:
    """The fixpoint scan proves credentials do not survive. It proves nothing about rows.

    A trigger that copies a pre-update value is cleaned by the next pass -- that is what the
    fixpoint is for and it stays. A trigger that DELETES runs once and settles immediately,
    so the fixpoint sees a quiet database while the rows are gone from the copy that leaves.
    """

    def _db(self, path, trigger_body):
        conn = redact.sqlite3.connect(str(path))
        conn.executescript(f"""
            CREATE TABLE notes(id INTEGER PRIMARY KEY, body TEXT);
            CREATE TABLE audit(id INTEGER PRIMARY KEY, note TEXT);
            INSERT INTO notes(body) VALUES ('key=AKIAIOSFODNN7EXAMPLE');
            INSERT INTO audit(note) VALUES ('row the operator needs');
            CREATE TRIGGER t AFTER UPDATE ON notes BEGIN {trigger_body} END;
            """)
        conn.commit()
        conn.close()

    def test_a_deleting_trigger_refuses_and_the_rows_survive(self, tmp_path):
        db = tmp_path / "memory.db"
        self._db(db, "DELETE FROM audit;")

        with pytest.raises(redact._PayloadUnprovable):
            redact._redact_database(db, redact.RedactionReport(), "memory.db", product=True)

        conn = redact.sqlite3.connect(str(db))
        rows = conn.execute("SELECT count(*) FROM audit").fetchone()[0]
        conn.close()
        assert rows == 1, "the trigger fired and deleted the operator's row despite refusing"

    def test_a_trigger_that_only_writes_values_is_still_handled_by_the_fixpoint(self, tmp_path):
        """The must-still-work half, and the reason this refusal is narrower than prescribed.

        Refusing every non-FTS UPDATE trigger would discard the fixpoint, which handles this
        case provably -- and would refuse an external-content full-text index, which is
        MAINTAINED by update triggers.
        """
        db = tmp_path / "memory.db"
        self._db(db, "INSERT INTO audit(note) VALUES (OLD.body);")

        redact._redact_database(db, redact.RedactionReport(), "memory.db", product=True)

        conn = redact.sqlite3.connect(str(db))
        leaked = [
            r[0]
            for r in conn.execute("SELECT note FROM audit UNION ALL SELECT body FROM notes")
            if "AKIA" in (r[0] or "")
        ]
        conn.close()
        assert leaked == [], f"the fixpoint left a credential behind: {leaked}"


class TestTheDatabaseRestageChecksAndOpensTheSameFile:
    """The screening and the SQLite open must not be two independent path walks.

    `src.resolve()` re-walked every ancestor after the loop had screened the file by name,
    so a swapped ancestor could redirect the open at a database the screen never inspected.
    The parent chain is resolved once and pinned, the final name is checked THROUGH that
    pinned parent, and the URI is built from the same resolved chain.
    """

    @pytest.mark.skipif(
        os.name == "nt",
        reason="the swapped-ancestor defence is _chain_is_link_free opening each ancestor "
        "through a directory descriptor (O_NOFOLLOW); os.supports_dir_fd is empty on "
        "Windows, so this pinned redirect-refusal guarantee does not exist to assert here",
    )
    def test_a_swapped_ancestor_cannot_redirect_the_open(self, tmp_path, monkeypatch):
        """The swap is INJECTED mid-loop, because a statically-planted link proves nothing.

        `rglob` does not descend a symlinked directory, so a link placed before the pass runs
        is never enumerated and the test passes no matter what the code does -- the first
        version of this test was exactly that, and a mutant restoring `src.resolve()` sailed
        through it. The real claim is about a swap AFTER the file was enumerated and screened,
        so the swap is performed from inside the chain check, on its first call.
        """
        real = tmp_path / "live"
        (real / "workspace").mkdir(parents=True)
        db = real / "workspace" / "notes.db"
        conn = redact.sqlite3.connect(str(db))
        conn.execute("CREATE TABLE t(v TEXT)")
        conn.execute("INSERT INTO t VALUES ('MINE')")
        conn.commit()
        conn.close()

        outside = tmp_path / "elsewhere"
        outside.mkdir()
        conn = redact.sqlite3.connect(str(outside / "notes.db"))
        conn.execute("CREATE TABLE t(v TEXT)")
        conn.execute("INSERT INTO t VALUES ('EXTERNAL-SECRET')")
        conn.commit()
        conn.close()

        stage = tmp_path / "stage"
        (stage / "workspace").mkdir(parents=True)
        shutil.copy2(str(db), str(stage / "workspace" / "notes.db"))

        real_check = snap._chain_is_link_free
        swapped = {"done": False}

        def swap_then_check(root, rel_parts):
            if not swapped["done"]:
                swapped["done"] = True
                shutil.rmtree(str(real / "workspace"))
                (real / "workspace").symlink_to(outside, target_is_directory=True)
            return real_check(root, rel_parts)

        monkeypatch.setattr(snap, "_chain_is_link_free", swap_then_check)
        snap._restage_databases(real, stage, bundle_root=stage)
        assert swapped["done"], "the swap never fired; the test proves nothing"

        staged = stage / "workspace" / "notes.db"
        conn = redact.sqlite3.connect(str(staged))
        rows = [r[0] for r in conn.execute("SELECT v FROM t")]
        conn.close()
        assert "EXTERNAL-SECRET" not in rows, (
            "the restage followed a swapped ancestor and copied an external database "
            f"into the bundle: {rows}"
        )

    def test_a_platform_without_dir_fd_still_restages(self, tmp_path, monkeypatch):
        """Windows, simulated the way this repo already simulates it.

        `os.supports_dir_fd` is EMPTY on Windows, so `os.open(..., dir_fd=fd)` raises
        `NotImplementedError` there -- not "works but weaker". The first version of the chain
        check passed `dir_fd` unconditionally and took the whole database pass down on
        Windows; `pinned_fs.supports_pinned_walk()` exists for exactly this and was not
        consulted. Both halves are simulated here: the capability report is emptied AND
        `os.open` refuses a `dir_fd`, so a missing gate fails rather than silently passing on
        a platform that happens to support it.
        """
        real = tmp_path / "live"
        real.mkdir()
        db = real / "notes.db"
        conn = redact.sqlite3.connect(str(db))
        conn.execute("CREATE TABLE t(v TEXT)")
        conn.execute("INSERT INTO t VALUES ('MINE')")
        conn.commit()
        conn.close()
        stage = tmp_path / "stage"
        stage.mkdir()
        shutil.copy2(str(db), str(stage / "notes.db"))

        monkeypatch.setattr(snap.pinned_fs.os, "supports_dir_fd", set())
        real_open = snap.os.open
        # Scoped to THIS pass, not the process: pytest's own tmp_path teardown uses
        # `os.open(..., dir_fd=)` on Linux, so an unconditional stub errors in teardown and
        # the simulation would be testing the harness instead of the code.
        active = {"on": True}

        def refuse_dir_fd(path, flags, mode=0o777, *, dir_fd=None):
            if dir_fd is not None and active["on"]:
                raise NotImplementedError("dir_fd is unavailable on this platform")
            return (
                real_open(path, flags, mode)
                if dir_fd is None
                else real_open(path, flags, mode, dir_fd=dir_fd)
            )

        monkeypatch.setattr(snap.os, "open", refuse_dir_fd)

        try:
            snap._restage_databases(real, stage, bundle_root=stage)
        finally:
            active["on"] = False

        conn = redact.sqlite3.connect(str(stage / "notes.db"))
        rows = [r[0] for r in conn.execute("SELECT v FROM t")]
        conn.close()
        assert rows == ["MINE"], f"the restage did not survive a platform without dir_fd: {rows}"

    def test_an_ordinary_database_is_still_restaged(self, tmp_path):
        """The screening must not become a blanket refusal."""
        real = tmp_path / "live"
        real.mkdir()
        db = real / "notes.db"
        conn = redact.sqlite3.connect(str(db))
        conn.execute("CREATE TABLE t(v TEXT)")
        conn.execute("INSERT INTO t VALUES ('MINE')")
        conn.commit()
        conn.close()

        stage = tmp_path / "stage"
        stage.mkdir()
        # The staged copy is a real (possibly torn) filesystem copy -- that is what this
        # pass exists to replace with a consistent one, so a non-database placeholder here
        # would be testing a state the pass never sees.
        shutil.copy2(str(db), str(stage / "notes.db"))
        conn = redact.sqlite3.connect(str(real / "notes.db"))
        conn.execute("INSERT INTO t VALUES ('ADDED-AFTER-THE-COPY')")
        conn.commit()
        conn.close()

        snap._restage_databases(real, stage, bundle_root=stage)

        conn = redact.sqlite3.connect(str(stage / "notes.db"))
        rows = [r[0] for r in conn.execute("SELECT v FROM t")]
        conn.close()
        assert rows == ["MINE", "ADDED-AFTER-THE-COPY"], f"the database was not restaged: {rows}"


class TestASavedCoreFileLinkIsPutBackAsALink:
    """`_backup_and_copy` MOVES a symlinked core file aside, so a link in the rollback
    directory is a state this code creates on purpose.

    Recovery tested `saved.is_dir()` then `saved.is_file()`, both of which dereference. A
    relative link stops resolving once it sits in the rollback directory, so it matched
    neither branch: the replacement at the live name was deleted as an undone creation, the
    link stayed behind in the rollback directory, and the recovery reported success.
    """

    def test_a_relative_symlink_survives_the_round_trip(self, home, tmp_path):
        (home / "real-crons.json").write_text('{"jobs": [{"name": "mine"}]}', encoding="utf-8")
        (home / "crons.json").symlink_to("real-crons.json")

        backup = tmp_path / "pre-restore-probe"
        backup.mkdir()
        # Exactly what the backup pass does with a symlinked core file: move the LINK.
        shutil.move(str(home / "crons.json"), str(backup / "crons.json"))
        saved = backup / "crons.json"
        # The two branch tests are both false here -- that is the whole defect.
        assert saved.is_symlink() and not saved.is_file() and not saved.is_dir()
        # The restore wrote its own replacement before something later failed.
        (home / "crons.json").write_text('{"jobs": [{"name": "from-archive"}]}', encoding="utf-8")

        failed = snap._restore_everything_from_rollback(
            backup, home, ["crons.json"], {"crons.json"}, allow_unpinned=bool(unpinnable_argv())
        )

        assert failed == [], failed
        live = home / "crons.json"
        assert live.is_symlink(), "the saved link was not put back as a link"
        assert str(live.readlink()) == "real-crons.json", "the link target changed"
        assert json.loads(live.read_text(encoding="utf-8"))["jobs"][0]["name"] == "mine"

    def test_a_saved_regular_file_still_comes_back(self, home, tmp_path):
        """The ordinary case must not be captured by the new link branch."""
        backup = tmp_path / "pre-restore-probe"
        backup.mkdir()
        (backup / "crons.json").write_text('{"jobs": [{"name": "mine"}]}', encoding="utf-8")
        (home / "crons.json").write_text('{"jobs": [{"name": "from-archive"}]}', encoding="utf-8")

        failed = snap._restore_everything_from_rollback(
            backup, home, ["crons.json"], {"crons.json"}, allow_unpinned=bool(unpinnable_argv())
        )

        assert failed == [], failed
        live = home / "crons.json"
        assert not live.is_symlink()
        assert json.loads(live.read_text(encoding="utf-8"))["jobs"][0]["name"] == "mine"


class TestAVariableLengthReplacementNeverRidesInAnOffsetDependentPayload:
    """Redaction substitutes text of a DIFFERENT length, so it may only rewrite bytes whose
    meaning does not depend on position.

    The file path already refused a NUL-bearing file for exactly this reason. Two places had
    no equivalent guard: a byte-valued database column, and a text-shaped container whose
    own layout records byte offsets. Both shipped a corrupted off-host copy and reported it
    clean, which on a backup is the worst available outcome.
    """

    def test_a_blob_carrying_a_credential_refuses_instead_of_being_rewritten(self, tmp_path):
        import struct

        db = tmp_path / "memory.db"
        payload = b"attachment holding AKIAIOSFODNN7EXAMPLE inside"
        # A length prefix that DESCRIBES the payload: the shape of every serialised record,
        # and what a shifted payload destroys.
        blob = struct.pack("<I", len(payload)) + payload + struct.pack("<I", 0xAABBCCDD)
        conn = redact.sqlite3.connect(str(db))
        conn.execute("CREATE TABLE attachments(id INTEGER PRIMARY KEY, body BLOB)")
        conn.execute("INSERT INTO attachments(body) VALUES (?)", (blob,))
        conn.commit()
        conn.close()

        with pytest.raises(redact._PayloadUnprovable):
            redact._redact_database(db, redact.RedactionReport(), "memory.db", product=True)

        # Refusing means nothing is uploaded, so the staged bytes are left exactly as they
        # were. The check that matters is that they were not SILENTLY rewritten.
        conn = redact.sqlite3.connect(str(db))
        after = bytes(conn.execute("SELECT body FROM attachments").fetchone()[0])
        conn.close()
        assert after == blob, "the blob was rewritten despite the refusal"

    def test_a_blob_with_no_credential_still_rides(self, tmp_path):
        """The refusal must key off a HIT, not off the column being binary.

        Embeddings and thumbnails are the normal contents of a byte column. Refusing every
        database that merely HAS one would replace the feature with an unconditional refusal
        -- the same failure mode measured for the refuse-on-length-change remedy.
        """
        import struct

        db = tmp_path / "memory.db"
        clean = struct.pack("<I", 8) + b"embeddin" + struct.pack("<I", 0x11223344)
        conn = redact.sqlite3.connect(str(db))
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, body BLOB)")
        conn.execute("INSERT INTO t(body) VALUES (?)", (clean,))
        conn.commit()
        conn.close()

        redact._redact_database(db, redact.RedactionReport(), "memory.db", product=True)

        conn = redact.sqlite3.connect(str(db))
        after = bytes(conn.execute("SELECT body FROM t").fetchone()[0])
        conn.close()
        assert after == clean

    def test_an_ascii_pdf_is_refused_rather_than_shifted(self, tmp_path):
        """Measured to be the ONE offset-dependent container that reaches the rewrite.

        ZIP, gzip, PNG, tar and a Flate-stream PDF are all already refused by the NUL guard
        -- each is either not UTF-8 or carries NUL. An uncompressed ASCII PDF is neither,
        and its `xref` holds byte offsets that a variable-length edit invalidates.
        """
        pdf = tmp_path / "doc.pdf"
        body = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ncred AKIAIOSFODNN7EXAMPLE\n"
        pdf.write_bytes(body + b"xref\n0 2\n0000000009 00000 n \nstartxref\n9\n%%EOF\n")
        before = pdf.read_bytes()

        handled = redact._redact_text_file(pdf, redact.RedactionReport(), "doc.pdf")

        assert handled is False, "the PDF was accepted as plain text and rewritten"
        assert pdf.read_bytes() == before, "the PDF was modified"

    def test_an_ordinary_text_file_is_still_redacted(self, tmp_path):
        """The feature has to keep working: plain text has no offsets to invalidate."""
        note = tmp_path / "note.md"
        note.write_text("my key is AKIAIOSFODNN7EXAMPLE\nnext line\n", encoding="utf-8")

        handled = redact._redact_text_file(note, redact.RedactionReport(), "note.md")

        assert handled is True
        body = note.read_text(encoding="utf-8")
        assert "AKIAIOSFODNN7EXAMPLE" not in body
        assert "next line" in body


class TestCronValidationMatchesWhatTheReaderNeeds:
    """A malformed cron shape must never reach the merge reader's field access.

    Superseded mechanism: an earlier revision pre-flighted the shape in
    `_refuse_corrupt_source_databases` and raised `SourceComponentUnsound`. The M1 base
    guards this on the MERGE side instead -- `_usable_cron_shape` classifies the shape and
    `_merge_crons` SKIPS an unusable one and continues, so the reader never calls `.get`
    on a str or iterates a non-list. The property (a bad entry is never handed to the
    reader) is unchanged; where it is enforced moved, so these pin `_usable_cron_shape`
    and the merge-skips-and-continues behaviour.
    """

    def test_a_jobs_list_of_strings_is_rejected_by_the_shape_guard(self, home, tmp_path) -> None:
        """`{"jobs": ["x"]}` is a valid object whose reader would call `.get` on a str."""
        import json as _json

        parsed = _json.loads('{"jobs": ["x"]}')
        assert (
            snap._usable_cron_shape(parsed, tmp_path / "crons.json") is False
        ), "a job that is not an object must be rejected before the reader touches it"

    def test_a_jobs_value_that_is_not_a_list_is_rejected_by_the_shape_guard(
        self, home, tmp_path
    ) -> None:
        import json as _json

        parsed = _json.loads('{"jobs": {"a": 1}}')
        assert (
            snap._usable_cron_shape(parsed, tmp_path / "crons.json") is False
        ), "a jobs value that is not a list must be rejected"

    def test_a_well_formed_crons_shape_still_passes(self, home, tmp_path) -> None:
        import json as _json

        parsed = _json.loads('{"jobs": [{"name": "a"}, {"name": "b"}]}')
        assert snap._usable_cron_shape(parsed, tmp_path / "crons.json") is True

    def test_an_absent_jobs_key_is_not_invented(self, home, tmp_path) -> None:
        """A missing `jobs` key keeps its meaning of "no jobs" -- usable, not rejected."""
        import json as _json

        parsed = _json.loads('{"other": 1}')
        assert snap._usable_cron_shape(parsed, tmp_path / "crons.json") is True

    def test_the_merge_reader_never_sees_a_bad_entry(self, home, tmp_path, capsys):
        """End to end: a malformed jobs list is skipped and live crons are left intact.

        The merge does not crash and does not corrupt the operator's crons -- it skips the
        unusable file and reports the rest complete, which is the M1 skip-and-continue
        contract (not the old whole-restore refusal).
        """
        (home / "crons.json").write_text('{"jobs": [{"name": "mine"}]}', encoding="utf-8")
        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        payload.mkdir(parents=True)
        (payload / "crons.json").write_text('{"jobs": ["x"]}', encoding="utf-8")
        (payload / "MANIFEST.json").write_text(
            json.dumps({"version": 3, "components": {"crons": "unresolved"}}),
            encoding="utf-8",
        )
        bundle = tmp_path / "b.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        rc = snap.restore_main(
            [str(bundle), "--mode", "merge", "--force", "--components", "crons"] + unpinnable_argv()
        )
        out = capsys.readouterr().out
        # 0, not 1: the merger SKIPS an unusable crons file and carries on with the rest of
        # the restore. Asserted rather than left implicit, because the return code is exactly
        # what this contract changed -- an earlier revision refused the whole restore here,
        # which turned one misshapen component into a failed restore of every other one.
        assert rc == 0, out
        assert "Traceback" not in out, out
        assert '{"jobs": [{"name": "mine"}]}' == (home / "crons.json").read_text(
            encoding="utf-8"
        ), "live crons were modified despite the incoming file being unusable"


class TestTheUploadPathDoesNotConsultConfigAtAll:
    def test_the_switch_is_read_through_the_redactor_not_the_config(self):
        """Stronger than the import-placement rule this replaces.

        That rule pinned WHERE the config import sat, to stop a hoist-back. The upload path
        no longer reads config at all: the redaction opt-out moved behind the keystone fence
        on the backup directory, because a switch in agent-writable `config.json` is one the
        agent can flip to publish live credentials through a sanctioned path. So the thing
        worth pinning is that no config read comes back here in any form.
        """
        import inspect

        src = inspect.getsource(snap._redacted_upload_copy)
        assert "outbound_redaction_enabled()" in src
        assert "KiroCrewConfig" not in src, "a config read came back to the upload path"
        assert "from kiro_crew.config.loader import" not in src
        assert not hasattr(
            snap, "KiroCrewConfig"
        ), "the module-scope config import is unused now and must not linger"

    def test_the_schema_query_uses_the_current_table_name(self):
        """The codebase already queries `sqlite_schema`; this module matches it."""
        source = Path(redact.__file__).read_text(encoding="utf-8")
        assert "sqlite_schema" in source, source[:0]
        assert argparse  # namespace kept meaningful for the upload-path callers above

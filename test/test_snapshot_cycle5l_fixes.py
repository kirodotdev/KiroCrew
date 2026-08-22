"""A table the pass cannot read fails CLOSED, and validation matches what readers need.

Both are the same mistake in different places: a branch that could not do its job chose to
continue rather than refuse. The redaction pass exists to keep credentials off the wire, so
"could not inspect this table" cannot mean "ship it".
"""

from __future__ import annotations

import argparse
import json
import tarfile
from pathlib import Path

import pytest

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
        assert src.count("_TableNotInspectable(") == 3, (
            "every state this pass cannot prove clean must refuse: a table with no rowid, "
            "a table with no columns, and a database that never settles because a trigger "
            "keeps reintroducing values"
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
        assert body.count("{handle}") == 2, "both statements must interpolate the same handle"


class TestCronValidationMatchesWhatTheReaderNeeds:
    def test_a_jobs_list_of_strings_is_refused(self, home, tmp_path):
        """`{"jobs": ["x"]}` is a valid object whose reader calls `.get` on a str."""
        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        payload.mkdir(parents=True)
        (payload / "crons.json").write_text('{"jobs": ["x"]}', encoding="utf-8")

        with pytest.raises(snap.SourceComponentUnsound) as e:
            snap._refuse_corrupt_source_databases(payload, ["crons"], mc_for_merge=home)
        assert "jobs[0]" in str(e.value), str(e.value)

    def test_a_jobs_value_that_is_not_a_list_is_refused(self, home, tmp_path):
        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        payload.mkdir(parents=True)
        (payload / "crons.json").write_text('{"jobs": {"a": 1}}', encoding="utf-8")

        with pytest.raises(snap.SourceComponentUnsound) as e:
            snap._refuse_corrupt_source_databases(payload, ["crons"], mc_for_merge=home)
        assert "not a list" in str(e.value), str(e.value)

    def test_a_well_formed_crons_file_still_passes(self, home, tmp_path):
        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        payload.mkdir(parents=True)
        (payload / "crons.json").write_text(
            '{"jobs": [{"name": "a"}, {"name": "b"}]}', encoding="utf-8"
        )
        snap._refuse_corrupt_source_databases(payload, ["crons"], mc_for_merge=home)

    def test_an_absent_jobs_key_is_not_invented(self, home, tmp_path):
        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        payload.mkdir(parents=True)
        (payload / "crons.json").write_text('{"other": 1}', encoding="utf-8")
        snap._refuse_corrupt_source_databases(payload, ["crons"], mc_for_merge=home)

    def test_the_merge_reader_never_sees_a_bad_entry(self, home, tmp_path, capsys):
        """End to end: the refusal lands before `_merge_crons` touches live state."""
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

        rc = snap.restore_main([str(bundle), "--mode", "merge", "--force", "--components", "crons"])
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "Traceback" not in out
        assert '{"jobs": [{"name": "mine"}]}' == (home / "crons.json").read_text(
            encoding="utf-8"
        ), "live crons were modified before the refusal"


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

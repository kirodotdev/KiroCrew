"""Redaction must read what a value holds, and must not delete files it does not own."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from kiro_crew import snapshot as snap
from kiro_crew import snapshot_redact as redact

KEY = "AKIAIOSFODNN7EXAMPLE"


def _stage(tmp_path: Path) -> Path:
    stage = tmp_path / "bundle"
    stage.mkdir()
    (stage / "MANIFEST.json").write_text(json.dumps({"components": {}}), encoding="utf-8")
    return stage


def _db(path: Path, body: object, vec: bytes | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with redact.sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, body TEXT, vec BLOB)")
        conn.execute("INSERT INTO items(body, vec) VALUES(?, ?)", (body, vec))
        conn.commit()
    conn.close()


class TestAByteValuedCredentialIsRedacted:
    """A column's declared type does not decide what it holds."""

    def test_a_credential_stored_as_bytes_leaves_no_trace_in_the_file(self, tmp_path: Path) -> None:
        stage = _stage(tmp_path)
        db = stage / "memory.db"
        _db(db, f"key={KEY}".encode("utf-8"))

        redact.redact_bundle_for_egress(stage)

        assert KEY.encode("utf-8") not in db.read_bytes()

    def test_the_value_keeps_its_type_so_readers_still_see_bytes(self, tmp_path: Path) -> None:
        stage = _stage(tmp_path)
        db = stage / "memory.db"
        _db(db, f"key={KEY}".encode("utf-8"))

        redact.redact_bundle_for_egress(stage)

        with redact.sqlite3.connect(str(db)) as conn:
            body = conn.execute("SELECT body FROM items").fetchone()[0]
        conn.close()
        assert isinstance(body, bytes)
        assert KEY not in body.decode("latin-1")

    def test_a_real_binary_blob_survives_byte_for_byte(self, tmp_path: Path) -> None:
        """Embeddings are blobs. Redacting must not rewrite one that holds no credential."""
        stage = _stage(tmp_path)
        db = stage / "memory.db"
        embedding = struct.pack("<8f", *[0.125 * i for i in range(8)])
        _db(db, f"key={KEY}", embedding)

        redact.redact_bundle_for_egress(stage)

        with redact.sqlite3.connect(str(db)) as conn:
            vec = conn.execute("SELECT vec FROM items").fetchone()[0]
            assert conn.execute("PRAGMA integrity_check;").fetchone()[0] == "ok"
        conn.close()
        assert vec == embedding

    def test_binary_around_a_credential_refuses_instead_of_shifting_it(
        self, tmp_path: Path
    ) -> None:
        """This used to assert the blob was REWRITTEN with its surrounding bytes intact.

        Both assertions it made were true and neither could see the defect: replacement is
        variable-length, so the head and tail bytes were still present and still at the two
        ends while everything between them had MOVED. A length prefix or an offset table in
        such a value is silently invalidated, and the corrupted database went off-host
        reported as clean. Structurally binary values are refused now.
        """
        stage = _stage(tmp_path)
        db = stage / "memory.db"
        head, tail = b"\xff\xfe\x80", b"\x00\x81\xff"
        blob = head + KEY.encode("ascii") + tail
        _db(db, "x", blob)

        with pytest.raises(redact.PayloadDatabaseUnprovable):
            redact.redact_bundle_for_egress(stage)

        with redact.sqlite3.connect(str(db)) as conn:
            vec = bytes(conn.execute("SELECT vec FROM items").fetchone()[0])
        conn.close()
        assert vec == blob, "the value was rewritten despite the refusal"

    def test_non_ascii_text_stored_as_bytes_is_still_redacted_byte_exactly(
        self, tmp_path: Path
    ) -> None:
        """The codec claim, moved to the branch that still rewrites.

        Only a byte-preserving codec can round-trip a value that is not pure ASCII: the
        patterns are ASCII, so latin-1 lets them match inside the bytes while every other
        byte comes back unchanged. This value is text-shaped -- valid UTF-8, no NUL -- so it
        is rewritten rather than refused, and the non-ASCII text around the credential has
        to survive exactly.
        """
        stage = _stage(tmp_path)
        db = stage / "memory.db"
        head = "backup note: ".encode("utf-8") + b"\xc3\xa9\xc3\xa8"
        tail = b"\xc3\xbc" + " end".encode("utf-8")
        _db(db, "x", head + KEY.encode("ascii") + tail)

        redact.redact_bundle_for_egress(stage)

        with redact.sqlite3.connect(str(db)) as conn:
            vec = bytes(conn.execute("SELECT vec FROM items").fetchone()[0])
        conn.close()
        assert KEY.encode("ascii") not in vec
        assert vec.startswith(head), "leading non-ASCII text was re-encoded"
        assert vec.endswith(tail), "trailing non-ASCII text was re-encoded"


class TestTheOperatorsOwnFilesAreNotDeleted:
    """The special names are the product's, at the product's paths — not anyone's basename."""

    def test_a_workspace_file_sharing_a_secrets_name_survives(self, tmp_path: Path) -> None:
        stage = _stage(tmp_path)
        mine = stage / "workspace" / "projects" / "telemetry_salt"
        mine.parent.mkdir(parents=True)
        mine.write_text("my own notes\n", encoding="utf-8")

        redact.redact_bundle_for_egress(stage)

        assert mine.is_file()

    def test_a_db_that_is_not_sqlite_is_refused_not_removed(self, tmp_path: Path) -> None:
        stage = _stage(tmp_path)
        mine = stage / "workspace" / "projects" / "notes.db"
        mine.parent.mkdir(parents=True)
        mine.write_bytes(b"\xff\xfe not sqlite \x00")

        with pytest.raises(redact.OpaqueFilesPresent) as caught:
            redact.redact_bundle_for_egress(stage)

        assert "workspace/projects/notes.db" in caught.value.paths
        assert mine.is_file(), "an operator's file must never be deleted to protect them"

    def test_a_workspace_file_named_like_the_derived_index_survives(self, tmp_path: Path) -> None:
        stage = _stage(tmp_path)
        mine = stage / "workspace" / "projects" / "memory_index.db"
        mine.parent.mkdir(parents=True)
        mine.write_bytes(b"\xff\xfe mine \x00")

        with pytest.raises(redact.OpaqueFilesPresent):
            redact.redact_bundle_for_egress(stage)

        assert mine.is_file()


class TestTheProductsOwnFilesStillGo:
    """Narrowing the match must not stop the real secret files from being dropped."""

    def test_the_root_secret_and_derived_index_are_still_dropped(self, tmp_path: Path) -> None:
        stage = _stage(tmp_path)
        (stage / "telemetry_salt").write_text("real\n", encoding="utf-8")
        idx = stage / "memory_index.db"
        _db(idx, "x")

        report = redact.redact_bundle_for_egress(stage)

        assert not (stage / "telemetry_salt").exists()
        assert not idx.exists()
        assert "telemetry_salt" in report.dropped
        assert "memory_index.db" in report.rebuilt_indexes

    def test_an_unreadable_product_database_refuses_the_upload(self, tmp_path: Path) -> None:
        """Refused rather than dropped.

        A database this pass cannot prove clean is REFUSED, not deleted. Removing it
        and uploading the remainder produces an off-host copy that reports success
        and restores nothing -- the failure the feature exists to prevent -- so what
        this pins is that the upload stops and the file survives to be dealt with.
        """
        stage = _stage(tmp_path)
        db = stage / "workspace" / "knowledge" / "knowledge.db"
        db.parent.mkdir(parents=True)
        db.write_bytes(b"\xff\xfe not sqlite \x00")

        with pytest.raises(redact.PayloadDatabaseUnprovable) as e:
            redact.redact_bundle_for_egress(stage)

        assert "workspace/knowledge/knowledge.db" in e.value.paths
        assert db.exists()


class TestTheSchemaTextIsScannedToo:
    """A row scan never sees the DDL, and a credential can be written into it."""

    @pytest.mark.parametrize(
        "ddl",
        [
            pytest.param(f"CREATE TABLE t(x TEXT DEFAULT '{KEY}')", id="column-default"),
            pytest.param(f"CREATE VIEW v AS SELECT '{KEY}' AS leaked", id="view-body"),
            pytest.param(
                "CREATE TRIGGER tr AFTER INSERT ON t BEGIN " f"UPDATE t SET x='{KEY}'; END",
                id="trigger-body",
            ),
        ],
    )
    def test_a_credential_in_the_schema_stops_the_product_database(
        self, tmp_path: Path, ddl: str
    ) -> None:
        stage = _stage(tmp_path)
        db = stage / "memory.db"
        with redact.sqlite3.connect(str(db)) as conn:
            conn.execute("CREATE TABLE t(x TEXT)")
            if not ddl.startswith("CREATE TABLE t("):
                conn.execute(ddl)
            else:
                conn.execute("DROP TABLE t")
                conn.execute(ddl)
            conn.commit()
        conn.close()

        with pytest.raises(redact.PayloadDatabaseUnprovable) as e:
            redact.redact_bundle_for_egress(stage)

        assert "memory.db" in e.value.paths
        assert db.exists(), "the payload was deleted instead of the upload being refused"

    def test_an_operator_database_is_refused_and_kept(self, tmp_path: Path) -> None:
        stage = _stage(tmp_path)
        mine = stage / "workspace" / "projects" / "mine.db"
        mine.parent.mkdir(parents=True)
        with redact.sqlite3.connect(str(mine)) as conn:
            conn.execute(f"CREATE TABLE t(x TEXT DEFAULT '{KEY}')")
            conn.commit()
        conn.close()

        with pytest.raises(redact.OpaqueFilesPresent) as caught:
            redact.redact_bundle_for_egress(stage)

        assert "workspace/projects/mine.db" in caught.value.paths
        assert mine.is_file()

    def test_the_products_own_schema_does_not_trip_the_scan(self, tmp_path: Path) -> None:
        """The guard against a refusal that would reject every real upload.

        The product's databases carry FTS triggers and virtual-table DDL. If any of that
        matched, this rule would drop the very databases the backup exists to carry.
        """
        stage = _stage(tmp_path)
        db = stage / "workspace" / "knowledge" / "knowledge.db"
        db.parent.mkdir(parents=True)
        with redact.sqlite3.connect(str(db)) as conn:
            conn.executescript("""
                CREATE TABLE items(rowid INTEGER PRIMARY KEY, body TEXT);
                CREATE VIRTUAL TABLE items_fts
                  USING fts5(body, content=items, content_rowid=rowid);
                CREATE TRIGGER items_ai AFTER INSERT ON items BEGIN
                  INSERT INTO items_fts(rowid, body) VALUES (new.rowid, new.body);
                END;
                """)
            conn.execute("INSERT INTO items(body) VALUES(?)", (f"key {KEY} here",))
            conn.commit()
        conn.close()

        report = redact.redact_bundle_for_egress(stage)

        assert db.is_file(), "a normal product database was dropped"
        assert report.dropped == []
        assert KEY.encode("ascii") not in db.read_bytes()
        with redact.sqlite3.connect(str(db)) as conn:
            assert conn.execute("PRAGMA integrity_check;").fetchone()[0] == "ok"
            assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1
        conn.close()


class TestTheProductPathSetCannotGoStale:
    def test_it_matches_the_databases_the_snapshot_actually_declares(self) -> None:
        declared = {"memory.db"} | set(snap.PRODUCT_TREE_DATABASES)
        assert set(redact._PRODUCT_DATABASES) == declared

    def test_the_special_files_are_matched_by_path_not_basename(self) -> None:
        src = Path(redact.__file__).read_text(encoding="utf-8")
        for group in ("_DROP_ENTIRELY", "_DERIVED_INDEXES"):
            assert f"rel in {group}" in src
            assert f"name in {group}" not in src, (
                f"{group} matched on a basename would delete an operator file that "
                "merely shares the name"
            )

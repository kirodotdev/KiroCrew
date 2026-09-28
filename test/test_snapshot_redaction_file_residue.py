"""A redacted row is not enough: the FILE must not carry the credential either."""

from __future__ import annotations

import json
from pathlib import Path

from kiro_crew import snapshot_redact as redact

KEY = "AKIAIOSFODNN7EXAMPLE"


def _stage(tmp_path: Path) -> Path:
    stage = tmp_path / "bundle"
    stage.mkdir()
    (stage / "MANIFEST.json").write_text(json.dumps({"components": {}}), encoding="utf-8")
    return stage


def _realistic_db(path: Path) -> None:
    """Several rows, only one carrying the secret -- the shape a real memory store has.

    A single-row fixture hides the defect: rewriting the only cell happens to overwrite
    the same bytes. With neighbours present the old cell content stays in the page's
    unused space.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with redact.sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE semantic(id INTEGER PRIMARY KEY, key TEXT, value TEXT)")
        conn.executemany(
            "INSERT INTO semantic(key, value) VALUES(?, ?)",
            [
                ("project.terrace", "Sydney property platform"),
                ("aws.prod", f"account key {KEY}"),
                ("pref.lang", "Chinese for discussion, English for code"),
            ],
        )
        conn.commit()
    conn.close()


class TestTheRedactedFileCarriesNoCredential:
    def test_the_plaintext_is_gone_from_the_bytes_not_just_the_rows(self, tmp_path: Path) -> None:
        stage = _stage(tmp_path)
        db = stage / "memory.db"
        _realistic_db(db)
        assert KEY.encode("ascii") in db.read_bytes(), "fixture did not plant the secret"

        redact.redact_bundle_for_egress(stage)

        assert KEY.encode("ascii") not in db.read_bytes(), (
            "every row reads redacted but the credential is still greppable in the file "
            "that leaves the host"
        )

    def test_the_rows_and_the_database_survive_the_rebuild(self, tmp_path: Path) -> None:
        stage = _stage(tmp_path)
        db = stage / "memory.db"
        _realistic_db(db)

        redact.redact_bundle_for_egress(stage)

        with redact.sqlite3.connect(str(db)) as conn:
            assert conn.execute("PRAGMA integrity_check;").fetchone()[0] == "ok"
            rows = dict(conn.execute("SELECT key, value FROM semantic").fetchall())
        conn.close()
        assert rows["project.terrace"] == "Sydney property platform"
        assert rows["pref.lang"] == "Chinese for discussion, English for code"
        assert KEY not in rows["aws.prod"]
        assert "REDACTED" in rows["aws.prod"]

    def test_a_credential_the_operator_already_deleted_does_not_ship(self, tmp_path: Path) -> None:
        """The scan finds nothing to replace, and that is exactly why this leaks.

        A DELETE does not erase the row -- SQLite frees the page and keeps the bytes. No
        live row holds the credential, so a pass that only rewrites live rows reports zero
        replacements and uploads the plaintext of a memory the operator believes is gone.
        """
        stage = _stage(tmp_path)
        db = stage / "memory.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        with redact.sqlite3.connect(str(db)) as conn:
            conn.execute("CREATE TABLE semantic(id INTEGER PRIMARY KEY, key TEXT, value TEXT)")
            conn.executemany(
                "INSERT INTO semantic(key, value) VALUES(?, ?)",
                [(f"note.{i}", f"harmless content number {i}") for i in range(40)],
            )
            conn.execute("INSERT INTO semantic(key, value) VALUES(?, ?)", ("aws.old", f"key {KEY}"))
            conn.commit()
            conn.execute("DELETE FROM semantic WHERE key = 'aws.old'")
            conn.commit()
        conn.close()
        assert KEY.encode("ascii") in db.read_bytes(), "fixture did not leave a freed page"

        report = redact.redact_bundle_for_egress(stage)

        assert report.total == 0, "no live row holds it -- there is nothing to replace"
        assert (
            KEY.encode("ascii") not in db.read_bytes()
        ), "a credential the operator deleted is still plaintext in the uploaded copy"

    def test_the_surviving_rows_are_untouched_by_the_rebuild(self, tmp_path: Path) -> None:
        stage = _stage(tmp_path)
        db = stage / "clean.db"
        with redact.sqlite3.connect(str(db)) as conn:
            conn.execute("CREATE TABLE t(x TEXT, v BLOB)")
            conn.execute("INSERT INTO t(x, v) VALUES('nothing secret here', ?)", (b"\x00\x01\x02",))
            conn.commit()
        conn.close()

        redact.redact_bundle_for_egress(stage)

        with redact.sqlite3.connect(str(db)) as conn:
            assert conn.execute("PRAGMA integrity_check;").fetchone()[0] == "ok"
            # Values survive verbatim -- this is what keeps stored embeddings usable.
            assert conn.execute("SELECT x, v FROM t").fetchall() == [
                ("nothing secret here", b"\x00\x01\x02")
            ]
        conn.close()

    def test_the_rebuild_is_not_conditional_on_a_replacement(self) -> None:
        """Gating it on `hits` is the defect: it protects only the rows this pass rewrote."""
        import inspect
        import textwrap

        src = textwrap.dedent(inspect.getsource(redact._redact_database))
        lines = [ln for ln in src.splitlines() if ln.strip()]
        vacuum = next(i for i, ln in enumerate(lines) if 'conn.execute("VACUUM")' in ln)
        indent = len(lines[vacuum]) - len(lines[vacuum].lstrip())
        enclosing = [
            ln.strip()
            for ln in lines[:vacuum]
            if (len(ln) - len(ln.lstrip())) < indent and ln.strip().endswith(":")
        ]
        assert not any(
            "if hits" in ln or "if pass_hits" in ln for ln in enclosing
        ), f"the rebuild is gated on a replacement having happened: {enclosing[-3:]}"

    def test_the_rebuild_runs_after_the_content_is_clean(self) -> None:
        import inspect

        src = inspect.getsource(redact._redact_database)
        assert 'conn.execute("VACUUM")' in src
        assert src.index("UPDATE") < src.index(
            'conn.execute("VACUUM")'
        ), "rebuilding before the rows are cleaned would preserve the old bytes"

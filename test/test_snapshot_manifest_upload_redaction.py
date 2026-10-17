"""The manifest leaves the host too, and an UPDATE can put back what it just removed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import snapshot_redact as redact

KEY = "AKIAIOSFODNN7EXAMPLE"


def _stage(tmp_path: Path, manifest: dict | None = None) -> Path:
    stage = tmp_path / "bundle"
    stage.mkdir()
    (stage / "MANIFEST.json").write_text(
        json.dumps(manifest if manifest is not None else {"components": {}}),
        encoding="utf-8",
    )
    return stage


class TestTheManifestIsRedactedToo:
    """It is the one file guaranteed to be in the upload."""

    def test_a_credential_in_the_manifest_does_not_leave(self, tmp_path: Path) -> None:
        stage = _stage(tmp_path, {"components": {}, "home": f"/home/{KEY}/crew"})

        redact.redact_bundle_for_egress(stage)

        assert KEY not in (stage / "MANIFEST.json").read_text(encoding="utf-8")

    def test_it_stays_valid_json_so_the_bundle_stays_restorable(self, tmp_path: Path) -> None:
        stage = _stage(tmp_path, {"components": {"memory": "ok"}, "note": f"k={KEY}"})

        redact.redact_bundle_for_egress(stage)

        data = json.loads((stage / "MANIFEST.json").read_text(encoding="utf-8"))
        assert data["components"] == {"memory": "ok"}

    def test_the_scan_runs_after_the_stamp_so_it_covers_what_the_stamp_wrote(
        self, tmp_path: Path
    ) -> None:
        """The stamp writes paths and error text, so scanning before it would miss them."""
        import inspect

        src = inspect.getsource(redact.redact_bundle_for_egress)
        assert src.index("_stamp_manifest(") < src.index("_redact_manifest(")

    def test_the_replacement_is_reported(self, tmp_path: Path) -> None:
        stage = _stage(tmp_path, {"components": {}, "home": f"/home/{KEY}/crew"})

        report = redact.redact_bundle_for_egress(stage)

        assert report.replacements.get("MANIFEST.json")

    def test_a_scrub_that_would_break_the_json_refuses(self, tmp_path: Path, monkeypatch) -> None:
        """Today's tag keeps the JSON valid; the guard is what makes that a checked fact.

        No real input can break it, because the replacement carries no quote or backslash.
        So the guard is exercised where it is decided -- by substituting a replacement that
        does -- rather than left as an assertion nothing runs.
        """
        stage = _stage(tmp_path, {"components": {}, "home": f"/home/{KEY}/crew"})
        monkeypatch.setattr(
            redact, "_scrub", lambda text: (text.replace(KEY, '"'), 1) if KEY in text else (text, 0)
        )

        try:
            redact.redact_bundle_for_egress(stage)
        except Exception as e:  # the private refusal type is an implementation detail
            assert "MANIFEST.json" in str(e)
        else:
            raise AssertionError("an unparseable manifest was written")

        assert json.loads((stage / "MANIFEST.json").read_text(encoding="utf-8"))


class TestATriggerCannotPutBackWhatWasRemoved:
    def test_a_trigger_copying_the_old_value_is_still_cleaned(self, tmp_path: Path) -> None:
        """One pass cleans `items`; the trigger then refills `audit`, already scanned."""
        stage = _stage(tmp_path)
        db = stage / "memory.db"
        with redact.sqlite3.connect(str(db)) as conn:
            conn.executescript("""
                CREATE TABLE audit(note TEXT);
                CREATE TABLE items(id INTEGER PRIMARY KEY, body TEXT);
                CREATE TRIGGER keep_old AFTER UPDATE ON items BEGIN
                  INSERT INTO audit(note) VALUES (OLD.body);
                END;
                """)
            conn.execute("INSERT INTO items(body) VALUES(?)", (f"secret {KEY} here",))
            conn.commit()
        conn.close()

        redact.redact_bundle_for_egress(stage)

        assert db.is_file(), "a settling database must still ship"
        assert KEY.encode("ascii") not in db.read_bytes()

    def test_a_database_that_never_settles_is_refused(self, tmp_path: Path) -> None:
        """The trigger names no credential, so only the value scan can see the loop."""
        stage = _stage(tmp_path)
        db = stage / "memory.db"
        with redact.sqlite3.connect(str(db)) as conn:
            conn.executescript("""
                CREATE TABLE t(id INTEGER PRIMARY KEY, body TEXT);
                CREATE TRIGGER never AFTER UPDATE ON t BEGIN
                  INSERT INTO t(body) VALUES (OLD.body);
                END;
                """)
            conn.execute("INSERT INTO t(body) VALUES(?)", (f"{KEY} start",))
            conn.commit()
        conn.close()

        with pytest.raises(redact.PayloadDatabaseUnprovable) as e:
            redact.redact_bundle_for_egress(stage)

        assert "memory.db" in e.value.paths
        assert db.exists(), "the payload was deleted instead of the upload being refused"
        # The reason travels with the refusal, so the operator learns WHY it stopped.
        assert any("did not settle" in d for d in e.value.details)

    def test_the_products_own_fts_triggers_do_not_trip_it(self, tmp_path: Path) -> None:
        """The guard against a fixpoint rule that refuses every real backup.

        The product's search schema is maintained BY triggers, including on UPDATE. If
        those counted as non-settling, this rule would drop the databases the backup
        exists to carry.
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
                CREATE TRIGGER items_au AFTER UPDATE ON items BEGIN
                  INSERT INTO items_fts(items_fts, rowid, body)
                    VALUES('delete', old.rowid, old.body);
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
            found = conn.execute(
                "SELECT count(*) FROM items_fts WHERE items_fts MATCH ?", (KEY,)
            ).fetchone()[0]
            assert found == 0, "the index still answers a search for the credential"
            assert (
                conn.execute(
                    "SELECT count(*) FROM items_fts WHERE items_fts MATCH 'key'"
                ).fetchone()[0]
                == 1
            ), "ordinary search stopped working"
        conn.close()

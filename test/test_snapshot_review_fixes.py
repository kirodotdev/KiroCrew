"""Tests for the five defects the review found on the first M1 SHA.

Each one asserts the consequence, not the mechanism: what state would have been
destroyed, or what would have escaped, if the fix were not there.
"""

from __future__ import annotations

import json
import sqlite3
import tarfile
from pathlib import Path

import pytest
from test_snapshot import _setup_fake_kirocrew

from kiro_crew import snapshot_remote as remote
from kiro_crew.deploy import engine
from kiro_crew.snapshot import restore_main, snapshot_main


def _tagset(pairs: dict[str, str]) -> str:
    return json.dumps({"TagSet": [{"Key": k, "Value": v} for k, v in pairs.items()]})


@pytest.fixture
def src(tmp_path, monkeypatch):
    d = tmp_path / "src"
    _setup_fake_kirocrew(d)
    monkeypatch.setenv("KIROCREW_HOME", str(d))
    return d


class TestDownloadNeverOverwrites:
    """Two S3 keys can share a basename; the second fetch must not clobber the first."""

    def test_an_existing_bundle_is_preserved(self, tmp_path, monkeypatch):
        monkeypatch.setattr(remote.shutil, "which", lambda _n: "/usr/bin/aws")
        into = tmp_path / "snaps"
        into.mkdir()
        first = into / "kirocrew-snapshot-20260811T000000Z.tar.gz"
        first.write_bytes(b"the first bundle")

        def fake_run(args, profile, timeout=30):
            Path(args[-1]).write_bytes(b"the second bundle")
            return 0, "", ""

        monkeypatch.setattr(engine, "run_aws", fake_run)
        local = remote.download(
            "s3://my-backups/desktop/kirocrew-snapshot-20260811T000000Z.tar.gz", into, "p"
        )
        assert local != first
        assert first.read_bytes() == b"the first bundle", "the retained bundle was destroyed"
        assert local.read_bytes() == b"the second bundle"


class TestReplaceKeepsARecoverableRollback:
    """Restoring memory+workspace must not overwrite its own rollback copy."""

    def test_the_original_memory_is_recoverable_after_a_full_replace(
        self, src, tmp_path, monkeypatch
    ):
        # Bundle carries INCOMING content.
        (src / "workspace/memory/preferences.md").write_text("incoming prefs\n")
        out = tmp_path / "out"
        assert snapshot_main([str(out)]) == 0
        tars = sorted(out.glob("kirocrew-snapshot-*.tar.gz"))
        assert tars

        # Destination holds DIFFERENT, original content.
        dest = tmp_path / "dest"
        (dest / "workspace/memory").mkdir(parents=True)
        (dest / "workspace/memory/preferences.md").write_text("ORIGINAL prefs\n")
        monkeypatch.setenv("KIROCREW_HOME", str(dest))

        assert restore_main([str(tars[-1]), "--mode", "replace", "--force"]) == 0

        # Live state is the incoming copy.
        assert (dest / "workspace/memory/preferences.md").read_text() == "incoming prefs\n"

        # And the pre-restore original is still recoverable from the rollback dir.
        saved = list(dest.glob("pre-restore-*/workspace/memory/preferences.md"))
        assert saved, "no rollback copy was kept"
        assert (
            saved[0].read_text() == "ORIGINAL prefs\n"
        ), "the rollback copy was overwritten with incoming data"


class TestLiveDatabasesAreStagedConsistently:
    """A SQLite database inside a staged tree gets the backup API, not a byte copy."""

    def _stage(self, src: Path, out: Path) -> Path:
        assert snapshot_main([str(out), "--components", "memory"]) == 0
        tar = sorted(out.glob("kirocrew-snapshot-*.tar.gz"))[-1]
        extract = out / "x"
        extract.mkdir(exist_ok=True)
        with tarfile.open(str(tar)) as t:
            t.extractall(extract, filter=lambda m, _d="": m)
        return next(
            d
            for d in extract.iterdir()
            if d.name.startswith(("kirocrew-snapshot-", "kirocrew-partial-"))
        )

    def test_a_knowledge_database_survives_with_its_rows(self, src, tmp_path):
        kb = src / "workspace/knowledge/knowledge.db"
        conn = sqlite3.connect(str(kb))
        conn.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, body TEXT)")
        conn.executemany("INSERT INTO facts (body) VALUES (?)", [("a",), ("b",), ("c",)])
        conn.commit()
        conn.close()

        snap = self._stage(src, tmp_path / "out")
        staged = snap / "workspace/knowledge/knowledge.db"
        assert staged.is_file()
        c = sqlite3.connect(str(staged))
        assert c.execute("SELECT count(*) FROM facts").fetchone()[0] == 3
        c.close()

    def test_wal_and_shm_sidecars_are_not_staged(self, src, tmp_path):
        """They describe the source's transaction state, not the copy's.

        Asserted on a `.db` that SQLite CANNOT open, because that is the only case
        where the glob is the thing doing the work: for a real database, re-opening
        the staged copy makes SQLite discard the copied sidecars as a side effect, so
        a test using a real database passes either way and proves nothing.
        """
        kdir = src / "workspace/knowledge"
        (kdir / "notes.db").write_bytes(b"not a database")
        (kdir / "notes.db-wal").write_bytes(b"stale journal")
        (kdir / "notes.db-shm").write_bytes(b"stale shm")

        snap = self._stage(src, tmp_path / "out")
        staged = snap / "workspace/knowledge"
        assert (staged / "notes.db").is_file(), "the operator's file must still ride"
        assert not (staged / "notes.db-wal").exists(), "a WAL sidecar rode the bundle"
        assert not (staged / "notes.db-shm").exists(), "a SHM sidecar rode the bundle"

    def test_a_real_databases_sidecars_also_stay_out(self, src, tmp_path):
        kb = src / "workspace/knowledge/knowledge.db"
        conn = sqlite3.connect(str(kb))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO facts (body) VALUES ('uncheckpointed')")
        conn.commit()
        # Leave the connection OPEN so -wal / -shm exist on disk while we snapshot.
        assert (src / "workspace/knowledge/knowledge.db-wal").exists()
        try:
            snap = self._stage(src, tmp_path / "out")
            kdir = snap / "workspace/knowledge"
            assert not list(kdir.glob("knowledge.db-*"))
            # The committed row still arrives: the backup API checkpointed it.
            c = sqlite3.connect(str(kdir / "knowledge.db"))
            assert c.execute("SELECT count(*) FROM facts").fetchone()[0] == 1
            c.close()
        finally:
            conn.close()

    def test_a_non_database_named_db_still_rides(self, src, tmp_path, capsys):
        """A .db file that is not SQLite is the operator's file — keep the byte copy."""
        decoy = src / "workspace/knowledge/notes.db"
        decoy.write_bytes(b"not a database at all")
        snap = self._stage(src, tmp_path / "out")
        assert (snap / "workspace/knowledge/notes.db").read_bytes() == b"not a database at all"

"""A bundle's incoming databases are validated BEFORE any live state moves.

Replace mode swaps the live database for the incoming one, so a check that runs after
the swap can only report that the home is now sitting on a corrupt database. Bundles
arriving from object storage are untrusted input, which is what makes the ordering load
bearing rather than tidy.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest
from test_snapshot import unpinnable_argv

from kiro_crew import snapshot as snap


def _make_bundle(tmp_path: Path, memory_db: bytes, *, name: str = "memory.db") -> Path:
    payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
    payload.mkdir(parents=True, exist_ok=True)
    (payload / name).write_bytes(memory_db)
    manifest = payload / "MANIFEST.json"
    manifest.write_text('{"version": 3, "components": {"memory": "unresolved"}}', encoding="utf-8")
    bundle = tmp_path / "bundle.tar.gz"
    with tarfile.open(bundle, "w:gz") as tf:
        tf.add(str(payload), arcname=payload.name)
    return bundle


def _real_db(path: Path) -> bytes:
    conn = snap.sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (a INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    return path.read_bytes()


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(snap, "_mc_dir", lambda: h)
    monkeypatch.setattr(snap, "_is_gateway_running", lambda: False)
    return h


class TestAnUnsoundIncomingDatabaseIsRefusedBeforeMutation:
    def test_an_unreadable_incoming_database_leaves_live_memory_untouched(
        self, home, tmp_path, capsys
    ):
        live = home / "memory.db"
        live.write_bytes(_real_db(tmp_path / "live.db"))
        before = live.read_bytes()
        bundle = _make_bundle(tmp_path, b"this is not a database at all")

        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
        )
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "cannot be opened as a database" in out, out
        assert (
            live.read_bytes() == before
        ), "live memory was replaced before the incoming database was validated"

    def test_a_corrupt_incoming_database_is_refused(self, home, tmp_path, capsys):
        live = home / "memory.db"
        live.write_bytes(_real_db(tmp_path / "live2.db"))
        before = live.read_bytes()

        good = _real_db(tmp_path / "src.db")
        # Valid header, damaged pages: opens as a database, fails integrity_check.
        corrupt = bytearray(good)
        for off in range(100, min(len(corrupt), 3000)):
            corrupt[off] = 0xFF
        bundle = _make_bundle(tmp_path, bytes(corrupt))

        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
        )
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "integrity check failed" in out, out
        assert live.read_bytes() == before

    def test_a_sound_bundle_still_restores(self, home, tmp_path, capsys):
        (home / "memory.db").write_bytes(_real_db(tmp_path / "old.db"))
        incoming = _real_db(tmp_path / "new.db")
        bundle = _make_bundle(tmp_path, incoming)

        rc = snap.restore_main(
            [
                str(bundle),
                "--mode",
                "replace",
                "--force",
                "--components",
                "memory",
                *unpinnable_argv(),
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0, out
        assert (home / "memory.db").is_file()

    def test_a_database_that_opens_but_fails_integrity_is_refused(self, home, tmp_path, capsys):
        """The distinct branch: page-level damage behind an intact header.

        SQLite opens this file happily, so nothing short of `PRAGMA integrity_check`
        catches it. Asserting only "refused somehow" would pass on the unopenable path
        and leave this branch untested.
        """
        live = home / "memory.db"
        live.write_bytes(_real_db(tmp_path / "live2.db"))
        before = live.read_bytes()

        big = tmp_path / "big.db"
        conn = snap.sqlite3.connect(str(big))
        conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
        conn.executemany("INSERT INTO t VALUES (?, ?)", [(i, "x" * 200) for i in range(400)])
        conn.execute("CREATE INDEX idx_b ON t(b)")
        conn.commit()
        conn.close()
        raw = bytearray(big.read_bytes())
        assert len(raw) > 8192, "need several pages for page-level damage"
        # Leave page 1 (header + schema) intact and zero page 2, which is measured to
        # open cleanly and fail integrity_check with a btreeInitPage error.
        for off in range(4096, 8192):
            raw[off] = 0x00
        candidate = tmp_path / "candidate.db"
        candidate.write_bytes(bytes(raw))
        c = snap.sqlite3.connect(str(candidate))
        res = c.execute("PRAGMA integrity_check;").fetchone()[0]
        c.close()
        assert res != "ok", "fixture no longer models an opens-but-corrupt database"

        bundle = _make_bundle(tmp_path, bytes(raw))
        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
        )
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "integrity check failed" in out, out
        assert "cannot be opened" not in out, (
            "this file DOES open; it must be caught by integrity_check, not by the "
            "unopenable branch"
        )
        assert live.read_bytes() == before

    def test_a_second_declared_database_is_also_checked(self, home, tmp_path, capsys):
        """memory declares two databases; checking only the first leaves a hole."""
        live = home / "memory.db"
        live.write_bytes(_real_db(tmp_path / "live3.db"))
        before = live.read_bytes()

        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        payload.mkdir(parents=True, exist_ok=True)
        (payload / "memory.db").write_bytes(_real_db(tmp_path / "sound.db"))
        (payload / "memory_index.db").write_bytes(b"not a database either")
        (payload / "MANIFEST.json").write_text(
            '{"version": 3, "components": {"memory": "unresolved"}}', encoding="utf-8"
        )
        bundle = tmp_path / "second.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
        )
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "memory_index.db" in out, out
        assert live.read_bytes() == before

    def test_a_corrupt_database_inside_a_component_tree_is_refused(self, home, tmp_path, capsys):
        """The knowledge store lives INSIDE a declared tree, and trees copy wholesale.

        Validating only the top-level declared files leaves the same hole one directory
        down.
        """
        live = home / "memory.db"
        live.write_bytes(_real_db(tmp_path / "live4.db"))
        before = live.read_bytes()

        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        kdir = payload / "workspace" / "knowledge"
        kdir.mkdir(parents=True)
        (payload / "memory.db").write_bytes(_real_db(tmp_path / "sound2.db"))
        # Opens as a database, fails integrity_check.
        good = _real_db(tmp_path / "k.db")
        raw = bytearray(good)
        conn = snap.sqlite3.connect(str(tmp_path / "kbig.db"))
        conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
        conn.executemany("INSERT INTO t VALUES (?, ?)", [(i, "y" * 200) for i in range(400)])
        conn.execute("CREATE INDEX idx ON t(b)")
        conn.commit()
        conn.close()
        raw = bytearray((tmp_path / "kbig.db").read_bytes())
        for off in range(4096, 8192):
            raw[off] = 0x00
        (kdir / "knowledge.db").write_bytes(bytes(raw))
        (payload / "MANIFEST.json").write_text(
            '{"version": 3, "components": {"memory": "unresolved"}}', encoding="utf-8"
        )
        bundle = tmp_path / "tree.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
        )
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "knowledge.db" in out, out
        assert live.read_bytes() == before

    def test_a_non_database_file_inside_a_tree_does_not_block_a_restore(
        self, home, tmp_path, capsys
    ):
        """A `.db` inside the operator's own tree may simply not be SQLite.

        A Windows `Thumbs.db` is on this product's own ignore list, so refusing every
        unopenable `.db` under a tree would block restores over files that were never
        databases.
        """
        (home / "memory.db").write_bytes(_real_db(tmp_path / "live5.db"))

        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        kdir = payload / "workspace" / "knowledge"
        kdir.mkdir(parents=True)
        (payload / "memory.db").write_bytes(_real_db(tmp_path / "sound3.db"))
        (kdir / "Thumbs.db").write_bytes(b"\x00\x01 windows thumbnail cache, not sqlite")
        (payload / "MANIFEST.json").write_text(
            '{"version": 3, "components": {"memory": "unresolved"}}', encoding="utf-8"
        )
        bundle = tmp_path / "thumbs.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        rc = snap.restore_main(
            [
                str(bundle),
                "--mode",
                "replace",
                "--force",
                "--components",
                "memory",
                *unpinnable_argv(),
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0, out
        assert (home / "workspace" / "knowledge" / "Thumbs.db").is_file()


class TestAnUnextractableBundleIsARefusalNotATraceback:
    def test_a_readable_archive_that_cannot_be_extracted_is_refused(
        self, home, tmp_path, capsys, monkeypatch
    ):
        """Listing an archive and extracting it are different operations.

        The download probe passing does not mean extraction will, and a refusal has to
        read as a refusal rather than a traceback.
        """
        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        payload.mkdir(parents=True)
        (payload / "memory.db").write_bytes(_real_db(tmp_path / "ok.db"))
        bundle = tmp_path / "ok.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        real = tarfile.TarFile.extractall

        def boom(self, *a, **kw):
            raise OSError("Not a directory")

        monkeypatch.setattr(tarfile.TarFile, "extractall", boom)
        rc = snap.restore_main([str(bundle), "--mode", "replace", "--force"])
        out = capsys.readouterr().out
        monkeypatch.setattr(tarfile.TarFile, "extractall", real)
        assert rc == 1, out
        assert "could not be extracted" in out, out
        assert "Nothing was restored" in out, out


class TestEveryDeclaredDatabaseIsCovered:
    def test_every_declared_database_is_checked_not_just_the_first(self):
        """The finding named one database; the check must cover all of them."""
        dbs = [
            name
            for files in snap.CORE_FILES.values()
            for name in files
            if name.endswith((".db", ".sqlite3"))
        ]
        assert len(dbs) >= 2, dbs
        import inspect

        src = inspect.getsource(snap._refuse_corrupt_source_databases)
        assert (
            "for name in files" in src
        ), "the pre-flight must iterate every declared database file"
        assert "break" not in src, "stopping early would skip a later database"

    def test_the_refusal_precedes_the_mutation_call(self):
        """Structural: ordering is the property, so pin it in the source.

        The mutation entrypoints now carry an `allow_unpinned` argument, so match the
        call by its leading `(snap, mc, components` rather than a hardcoded closing paren.
        """
        import inspect

        src = inspect.getsource(snap.restore_main)
        pre = src.index("_refuse_corrupt_source_databases(")
        assert pre < src.index("_do_replace(snap, mc, components")
        assert pre < src.index("_do_merge(snap, mc, components")

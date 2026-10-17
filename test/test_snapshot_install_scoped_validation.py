"""Validation follows what a restore will INSTALL, and the archive bound covers local files.

Two scoping errors of the same shape: a rule was justified for one of a path's behaviours
and then applied as if the path had only that behaviour.

* Merge has two behaviours -- row-merge when the destination exists, plain file copy when it
  does not. "Merge cannot corrupt the destination" is true only of the first.
* The archive bound was applied to upload and download. A local bundle handed to `restore`
  is a third path, and being local does not make a file trustworthy.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest
from test_snapshot import unpinnable_argv

from kiro_crew import snapshot as snap


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


def _bundle(tmp_path, memory_bytes: bytes, *, knowledge: bytes | None = None) -> Path:
    payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
    payload.mkdir(parents=True, exist_ok=True)
    (payload / "memory.db").write_bytes(memory_bytes)
    if knowledge is not None:
        kdir = payload / "workspace" / "knowledge"
        kdir.mkdir(parents=True, exist_ok=True)
        (kdir / "knowledge.db").write_bytes(knowledge)
    (payload / "MANIFEST.json").write_text(
        '{"version": 3, "components": {"memory": "unresolved"}}', encoding="utf-8"
    )
    bundle = tmp_path / "b.tar.gz"
    with tarfile.open(bundle, "w:gz") as tf:
        tf.add(str(payload), arcname=payload.name)
    return bundle


class TestMergeValidatesWhatItWillInstall:
    def test_merge_into_a_home_without_the_database_refuses_a_corrupt_one(
        self, home, tmp_path, capsys
    ):
        """With no local memory.db, merge COPIES the incoming file -- exactly replace's act.

        The "merge copies rows out, so it cannot corrupt the destination" argument does not
        reach this branch.
        """
        assert not (home / "memory.db").exists()
        bundle = _bundle(tmp_path, b"not a database at all")

        rc = snap.restore_main(
            [str(bundle), "--mode", "merge", "--force", "--components", "memory"]
        )
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "integrity check failed" in out, out
        assert not (
            home / "memory.db"
        ).exists(), "merge installed an unvalidated database because the destination was absent"

    def test_merge_keeps_skipping_when_the_destination_exists(self, home, tmp_path, capsys):
        """The row-merge branch must stay skip-and-continue, not become a refusal."""
        local = _real_db(tmp_path / "local.db")
        (home / "memory.db").write_bytes(local)
        bundle = _bundle(tmp_path, b"corrupt incoming")

        rc = snap.restore_main(
            [
                str(bundle),
                "--mode",
                "merge",
                "--force",
                "--components",
                "memory",
                *unpinnable_argv(),
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0, out
        assert (home / "memory.db").read_bytes() == local

    def test_a_tree_database_is_validated_only_when_merge_would_install_it(
        self, home, tmp_path, capsys
    ):
        """Same rule one directory down: absent destination means it gets installed."""
        (home / "memory.db").write_bytes(_real_db(tmp_path / "l2.db"))
        assert not (home / "workspace" / "knowledge" / "knowledge.db").exists()
        bundle = _bundle(
            tmp_path,
            _real_db(tmp_path / "sound.db"),
            knowledge=b"torn knowledge database",
        )

        rc = snap.restore_main(
            [str(bundle), "--mode", "merge", "--force", "--components", "memory"]
        )
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "knowledge.db" in out, out
        assert not (home / "workspace" / "knowledge" / "knowledge.db").exists()

    def test_a_tree_database_that_already_exists_locally_is_still_skipped(
        self, home, tmp_path, capsys
    ):
        """The mirror case: merge keeps the local knowledge database and does not refuse.

        Without this, validating every tree database unconditionally would look correct.
        """
        (home / "memory.db").write_bytes(_real_db(tmp_path / "l3.db"))
        kdir = home / "workspace" / "knowledge"
        kdir.mkdir(parents=True)
        local = _real_db(tmp_path / "local-k.db")
        (kdir / "knowledge.db").write_bytes(local)

        bundle = _bundle(
            tmp_path,
            _real_db(tmp_path / "sound2.db"),
            knowledge=b"torn knowledge database",
        )
        rc = snap.restore_main(
            [
                str(bundle),
                "--mode",
                "merge",
                "--force",
                "--components",
                "memory",
                *unpinnable_argv(),
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0, out
        assert (
            kdir / "knowledge.db"
        ).read_bytes() == local, "merge must keep the local knowledge database"


class TestTheArchiveBoundCoversALocalBundle:
    def test_a_local_archive_declaring_too_many_entries_is_refused(
        self, home, tmp_path, capsys, monkeypatch
    ):
        """Local is not a trust boundary, and on 3.10 the fallback materialises members."""
        monkeypatch.setattr(snap, "_MAX_ARCHIVE_MEMBERS", 12)
        bundle = tmp_path / "many.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            root = tarfile.TarInfo("kirocrew-snapshot-20260101T000000Z")
            root.type = tarfile.DIRTYPE
            tf.addfile(root)
            for i in range(40):
                info = tarfile.TarInfo(f"kirocrew-snapshot-20260101T000000Z/f{i}.txt")
                info.size = 0
                tf.addfile(info)

        rc = snap.restore_main([str(bundle), "--mode", "replace", "--force"])
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "declares more than" in out, out
        assert "Nothing was restored" in out, out

    def test_the_bound_runs_before_extraction_on_the_local_path(self):
        """Ordering is the property: after extractall the damage is already written.

        Located by the CALL, not by its argument list. Pinning the exact spelling
        `filter=_data_filter` made this fail when the restore path started passing a
        rejection-recording wrapper instead -- a rename, with the ordering it exists to prove
        entirely intact. `extractall(work` still identifies the site, and the assertion is
        unchanged.
        """
        import inspect
        import re as _re

        src = inspect.getsource(snap.restore_main)
        bound = src.index("_refuse_oversized_archive(tar)")
        extract = _re.search(r"\.extractall\(\s*work", src)
        assert extract, "the local extraction site moved; retarget this locator"
        assert bound < extract.start()

    def test_the_message_does_not_claim_the_archive_was_downloaded(self):
        import inspect

        src = inspect.getsource(snap._refuse_oversized_archive)
        assert (
            "downloaded archive" not in src
        ), "the bound now covers local bundles, so the message must not say downloaded"

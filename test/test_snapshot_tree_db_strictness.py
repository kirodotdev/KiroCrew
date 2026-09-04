"""Our own tree databases are strict on restore.

A database this product owns (`memory.db`, `knowledge.db`) that cannot be opened is a bad
bundle, not an incidental file: restoring it would replace a healthy local copy with a
torn one. An incidental non-database in the same tree stays lenient, and merge says out
loud when it keeps the local copy rather than importing the incoming one.
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


class TestOurOwnDatabasesInsideATreeAreStrict:
    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        h = tmp_path / "home"
        h.mkdir()
        monkeypatch.setattr(snap, "_mc_dir", lambda: h)
        monkeypatch.setattr(snap, "_is_gateway_running", lambda: False)
        return h

    def _bundle(self, tmp_path, knowledge_bytes: bytes, name: str) -> Path:
        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        kdir = payload / "workspace" / "knowledge"
        kdir.mkdir(parents=True, exist_ok=True)
        (payload / "memory.db").write_bytes(_real_db(tmp_path / f"sound-{name}.db"))
        (kdir / name).write_bytes(knowledge_bytes)
        (payload / "MANIFEST.json").write_text(
            '{"version": 3, "components": {"memory": "unresolved"}}', encoding="utf-8"
        )
        bundle = tmp_path / f"{name}.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)
        return bundle

    def test_an_unopenable_knowledge_database_is_refused(self, home, tmp_path, capsys):
        """`knowledge.db` is as much ours as `memory.db`, so unopenable is a bad bundle."""
        live = home / "memory.db"
        live.write_bytes(_real_db(tmp_path / "live.db"))
        before = live.read_bytes()

        bundle = self._bundle(tmp_path, b"torn, not a database", "knowledge.db")
        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
        )
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "knowledge.db" in out and "integrity check failed" in out, out
        assert live.read_bytes() == before

    def test_an_incidental_non_database_in_the_same_tree_is_still_allowed(
        self, home, tmp_path, capsys
    ):
        """Leniency has to survive for files that were never databases."""
        (home / "memory.db").write_bytes(_real_db(tmp_path / "live2.db"))
        bundle = self._bundle(tmp_path, b"\x00 windows thumbnail cache", "Thumbs.db")
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
        assert rc == 0, capsys.readouterr().out

    def test_merge_says_when_it_keeps_an_existing_knowledge_database(self, home, tmp_path, capsys):
        """Merge legitimately keeps the local file; going silent is what misleads.

        The operator asked to merge their knowledge library. Reporting success while
        importing none of it reads as data loss, so the skip is stated outright.
        """
        kdir = home / "workspace" / "knowledge"
        kdir.mkdir(parents=True)
        local = _real_db(tmp_path / "local-knowledge.db")
        (kdir / "knowledge.db").write_bytes(local)
        (home / "memory.db").write_bytes(_real_db(tmp_path / "live3.db"))

        bundle = self._bundle(
            tmp_path, _real_db(tmp_path / "incoming-knowledge.db"), "knowledge.db"
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
        assert "workspace/knowledge/knowledge.db" in out, out
        assert "NOT merged" in out, out
        assert (
            kdir / "knowledge.db"
        ).read_bytes() == local, "merge must keep the local database, not clobber it"

    def test_the_component_help_does_not_promise_a_row_merge(self):
        help_text = snap.COMPONENTS["memory"].help
        assert "workspace/knowledge/" in help_text
        assert "not row-merged" in help_text, (
            "the help advertises the knowledge tree, so it must not imply its database "
            "rows are merged"
        )

    def test_the_limitation_is_documented_where_merge_is_explained(self):
        """A limitation that ships as documentation has to stay true to the code.

        The risk of choosing "document it" over "fix it" is that the two drift, so the
        doc claim and the runtime warning are pinned together.
        """
        doc = (Path(snap.__file__).parent / "docs" / "snapshot-and-restore.md").read_text(
            encoding="utf-8"
        )
        assert "not row-merged" in doc, "the merge limitation is not documented"
        assert "workspace/knowledge/knowledge.db" in doc
        # The doc promises the restore says so on the spot; that promise is the warning.
        import inspect

        src = inspect.getsource(snap._report_unmerged_databases)
        assert (
            "NOT " in src and "--mode replace" in src
        ), "the doc says the restore reports the skip and names --mode replace"

    def test_the_declared_set_is_not_empty_and_is_tree_relative(self):
        assert snap.PRODUCT_TREE_DATABASES, "the strict set must not be empty"
        for rel in snap.PRODUCT_TREE_DATABASES:
            assert "/" in rel, f"{rel} is not inside a tree"
            assert not rel.startswith("/"), rel
            covered = any(
                rel.startswith(f"{tree}/")
                for trees in snap.COMPONENT_TREES.values()
                for tree in trees
            )
            assert covered, f"{rel} is not under any declared component tree"

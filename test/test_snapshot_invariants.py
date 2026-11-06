"""Tests for the structural fixes made after the same defect class recurred.

The symlinked-tree-root invariant had been patched instance-by-instance across three
sites. These assert the invariant, and where possible assert it STRUCTURALLY so a new
instance cannot appear quietly.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from test_snapshot import _setup_fake_kirocrew, snapshot_family_paths, unpinnable_argv

from kiro_crew.snapshot import _merge_memory, restore_main, snapshot_main


@pytest.fixture
def src(tmp_path, monkeypatch):
    d = tmp_path / "src"
    _setup_fake_kirocrew(d)
    monkeypatch.setenv("KIROCREW_HOME", str(d))
    return d


def _make_memory_db(path: Path) -> None:
    """A minimal but REAL memory.db, so merge takes its normal path."""
    import sqlite3

    conn = sqlite3.connect(str(path))
    try:
        conn.executescript("""
            CREATE TABLE semantic_memory (key TEXT PRIMARY KEY, value_json TEXT NOT NULL,
                confidence REAL DEFAULT 0.5, source TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, is_deleted INTEGER DEFAULT 0, embedding BLOB);
            CREATE TABLE episodic_memories (id TEXT PRIMARY KEY, conversation_id TEXT,
                text TEXT NOT NULL, embedding BLOB, tags TEXT DEFAULT '[]',
                importance REAL DEFAULT 0.5, created_at TEXT NOT NULL,
                last_accessed_at TEXT, is_deleted INTEGER DEFAULT 0);
            """)
        conn.commit()
    finally:
        conn.close()


class TestMergeReleasesItsSourceHandles:
    """`with sqlite3.connect(...)` commits but leaves the connection OPEN.

    The open handle makes the extraction temp dir undeletable on Windows. Asserted on the
    connections because POSIX unlinks an open file, so a test that deletes the dir passes
    either way.
    """

    @pytest.mark.parametrize("source", ["sound", "corrupt"])
    def test_every_connection_the_merge_opens_is_closed(self, tmp_path, monkeypatch, source):
        import sqlite3

        from kiro_crew import snapshot as snapshot_mod

        opened: list[sqlite3.Connection] = []

        class _Recording:
            def __getattr__(self, item: str) -> object:
                return getattr(sqlite3, item)

            @staticmethod
            def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
                conn = sqlite3.connect(*args, **kwargs)  # type: ignore[arg-type]
                opened.append(conn)
                return conn

        src_db, dst_db = tmp_path / "src.db", tmp_path / "dst.db"
        if source == "sound":
            _make_memory_db(src_db)
        else:
            src_db.write_bytes(b"not a database")
        _make_memory_db(dst_db)
        monkeypatch.setattr(snapshot_mod, "sqlite3", _Recording())

        _merge_memory(src_db, dst_db)

        assert opened, "the merge opened no connection, so this proved nothing"
        for conn in opened:
            with pytest.raises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")


def _snapshot(out: Path, extra: list[str]) -> Path:
    # A selective bundle is `kirocrew-partial-*`, a complete one `kirocrew-snapshot-*`.
    assert snapshot_main([str(out)] + extra + unpinnable_argv()) == 0
    return sorted(out.glob("kirocrew-*.tar.gz"))[-1]


class TestEveryTreeRootSiteUsesTheChokepoint:
    """Structural: a new site iterating a component's trees must use safe_tree_root.

    The symlink class was found three times in three different sites. Counting call
    sites is crude but it is the property that actually failed: each site had been
    written without the check, independently.
    """

    def test_each_trees_loop_is_guarded(self):
        unguarded: list[str] = []
        scanned = 0
        for path in snapshot_family_paths():
            found, bad = self._unguarded_trees_loops(path)
            scanned += found
            unguarded.extend(f"{path.name}:{line}" for line in bad)
        assert scanned, "no `.trees` loop was found at all, so this scan proved nothing"
        assert unguarded == [], (
            f"trees loop(s) at {unguarded} neither call safe_tree_root nor sit in "
            f"a function that refused unsafe roots first -- a symlinked root would be "
            f"dereferenced"
        )

    @staticmethod
    def _unguarded_trees_loops(path: Path) -> tuple[int, list[int]]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # A loop is guarded either by calling the chokepoint itself, or by sitting in a
        # function that already REFUSED every unsafe root before the loop runs. The second
        # form is not a loophole: `_refuse_unsafe_destination_roots` iterates the same
        # `.trees` and raises, so reaching the loop proves the roots resolved inside the
        # home. Requiring the call inside every loop would force a redundant re-check whose
        # only effect is to satisfy this test.
        hoisted: set[str] = set()
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                "_refuse_unsafe_destination_roots" in ast.dump(fn)
            ):
                hoisted.add(fn.name)

        def _enclosing(node: ast.AST) -> str | None:
            for fn in ast.walk(tree):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                    n is node for n in ast.walk(fn)
                ):
                    return fn.name
            return None

        found = 0
        unguarded: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.For):
                continue
            # `for tree in <...>.trees:`
            it = node.iter
            if not (isinstance(it, ast.Attribute) and it.attr == "trees"):
                continue
            found += 1
            body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
            if "safe_tree_root" in body:
                continue
            if _enclosing(node) in hoisted:
                continue
            unguarded.append(node.lineno)
        return found, unguarded

    def test_the_chokepoint_admits_paths_inside_the_home_and_refuses_escapes(self, tmp_path):
        """Containment of the RESOLVED path, not 'is this node a link'.

        Checking the node was tried first and missed a link nested under the root or
        in one of its ancestors — every individual node the loop inspects looks
        ordinary while the write still lands outside.
        """
        from kiro_crew.snapshot import safe_tree_root

        home = tmp_path / "home"
        (home / "workspace").mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()

        inside = home / "workspace" / "memory"
        inside.mkdir()
        assert safe_tree_root(inside, what="x", home=home) == inside

        # 1. the root itself is a link out
        root_link = home / "workspace" / "linked"
        root_link.symlink_to(outside, target_is_directory=True)
        assert safe_tree_root(root_link, what="x", home=home) is None

        # 2. an ANCESTOR is a link out — the node itself is an ordinary directory
        anc = home / "workspace" / "viaparent"
        anc.symlink_to(outside, target_is_directory=True)
        (outside / "child").mkdir()
        assert safe_tree_root(anc / "child", what="x", home=home) is None

        # 3. a path that does not exist yet but resolves inside is fine
        assert safe_tree_root(home / "workspace" / "new", what="x", home=home) is not None

    def test_merge_does_not_write_through_a_linked_destination_root(
        self, src, tmp_path, monkeypatch
    ):
        """The third site: merge mode. Files must not land outside the data home."""
        tarball = _snapshot(tmp_path / "out", ["--components", "memory"])

        dest = tmp_path / "dest"
        outside = tmp_path / "outside"
        outside.mkdir()
        (dest / "workspace").mkdir(parents=True)
        (dest / "workspace/knowledge").symlink_to(outside, target_is_directory=True)
        # A REAL database, so merge mode takes the ATTACH path rather than the
        # "source unreadable, skipping" shortcut an empty file would trigger.
        _make_memory_db(dest / "memory.db")
        monkeypatch.setenv("KIROCREW_HOME", str(dest))

        rc = restore_main([str(tarball), "--components", "memory", "--mode", "merge", "--force"])
        assert list(outside.iterdir()) == [], "merge wrote through a symlinked root"
        # And it refuses rather than reporting a merge it did not fully perform. Merge
        # destroys nothing, but a merge that silently omits a tree still claims to have
        # imported it.
        assert rc == 1, "merge reported success while skipping a tree"


class TestManifestDrivenRestoreEdges:
    def test_an_unknown_only_manifest_restores_nothing(self, tmp_path):
        """An empty resolved set must not collapse into 'restore everything'."""
        from kiro_crew.snapshot import _manifest_components

        snap = tmp_path / "future"
        snap.mkdir()
        (snap / "MANIFEST.json").write_text(json.dumps({"components": {"quantum": "unresolved"}}))
        assert _manifest_components(snap) == []

    def test_a_pre_manifest_bundle_still_means_everything(self, tmp_path):
        from kiro_crew.snapshot import _manifest_components

        snap = tmp_path / "old"
        snap.mkdir()
        (snap / "MANIFEST.json").write_text(json.dumps({"version": 2}))
        assert _manifest_components(snap) is None

    def test_requesting_a_component_the_bundle_lacks_is_refused(
        self, src, tmp_path, monkeypatch, capsys
    ):
        """Replace would move the live files out with nothing to put back."""
        tarball = _snapshot(tmp_path / "out", ["--components", "memory"])

        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / "config.json").write_text('{"keep": true}')
        monkeypatch.setenv("KIROCREW_HOME", str(dest))

        rc = restore_main([str(tarball), "--components", "config", "--mode", "replace", "--force"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "does not contain" in out and "config" in out
        assert json.loads((dest / "config.json").read_text()) == {"keep": True}


class TestUnreadableManifestRefuses:
    """'We could not read it' must never resolve to the most destructive reading."""

    def test_a_malformed_manifest_raises_rather_than_defaulting(self, tmp_path):
        from kiro_crew.snapshot import ManifestUnreadable, _manifest_components

        snap = tmp_path / "broken"
        snap.mkdir()
        (snap / "MANIFEST.json").write_text("{not json at all")
        with pytest.raises(ManifestUnreadable):
            _manifest_components(snap)

    def test_a_components_key_of_the_wrong_type_raises(self, tmp_path):
        from kiro_crew.snapshot import ManifestUnreadable, _manifest_components

        snap = tmp_path / "weird"
        snap.mkdir()
        (snap / "MANIFEST.json").write_text(json.dumps({"components": ["memory"]}))
        with pytest.raises(ManifestUnreadable):
            _manifest_components(snap)

    def test_restore_refuses_a_bundle_with_a_malformed_manifest(
        self, src, tmp_path, monkeypatch, capsys
    ):
        """Falling back to all-components would displace live state in replace mode."""
        import tarfile

        tarball = _snapshot(tmp_path / "out", ["--components", "memory"])

        # Rewrite the bundle with a corrupt manifest.
        work = tmp_path / "rebuild"
        work.mkdir()
        with tarfile.open(str(tarball)) as t:
            t.extractall(work, filter=lambda m, _d="": m)
        inner = next(
            d
            for d in work.iterdir()
            if d.name.startswith(("kirocrew-snapshot-", "kirocrew-partial-"))
        )
        (inner / "MANIFEST.json").write_text("{corrupt")
        broken = tmp_path / "broken.tar.gz"
        with tarfile.open(str(broken), "w:gz") as t:
            t.add(str(inner), arcname=inner.name)

        dest = tmp_path / "dest"
        dest.mkdir()
        keep = dest / "config.json"
        keep.write_text('{"keep": true}')
        monkeypatch.setenv("KIROCREW_HOME", str(dest))

        rc = restore_main([str(broken), "--mode", "replace", "--force"])
        assert rc == 1
        assert "unreadable" in capsys.readouterr().out
        assert json.loads(keep.read_text()) == {"keep": True}

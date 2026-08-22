"""Tests for the structural fixes made after the same defect class recurred.

Three invariants had each been patched instance-by-instance across review rounds —
symlinked tree roots (three sites), the bucket guard (three rewrites), and per
component share-safety guesses (two wrong guesses). These assert the invariant, and
where possible assert it STRUCTURALLY so a new instance cannot appear quietly.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from test_snapshot import _setup_fake_kirocrew

from kiro_crew import snapshot_remote
from kiro_crew.snapshot import restore_main, snapshot_main

SNAPSHOT_SRC = Path(__file__).resolve().parents[1] / "src/kiro_crew/snapshot.py"


def _tagset(pairs: dict[str, str]) -> str:
    return json.dumps({"TagSet": [{"Key": k, "Value": v} for k, v in pairs.items()]})


def _policy(statements: list[dict]) -> str:
    return json.dumps({"Policy": json.dumps({"Statement": statements})})


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
    """`with sqlite3.connect(...)` commits the TRANSACTION and leaves the connection
    OPEN — a long-standing Python gotcha. The handle it kept on the extracted
    memory.db made the caller's extraction temp dir undeletable on Windows, which is
    how it surfaced.

    Asserted on the source, because the leak is invisible on POSIX: an open handle
    does not prevent unlink there, so a functional test would pass either way and
    prove nothing.
    """

    def test_the_integrity_check_connection_is_closed(self):
        src = (Path(__file__).resolve().parents[1] / "src/kiro_crew/snapshot.py").read_text(
            encoding="utf-8"
        )
        body = src.split("def _merge_memory(")[1].split("\ndef ")[0]
        assert "closing(sqlite3.connect(str(src_db)))" in body, (
            "the integrity-check connection must be wrapped in closing(); a bare "
            "`with sqlite3.connect(...)` leaves it open and holds the source file"
        )
        assert "with sqlite3.connect(str(src_db))" not in body


def _snapshot(out: Path, extra: list[str]) -> Path:
    assert snapshot_main([str(out)] + extra) == 0
    return sorted(out.glob("kirocrew-snapshot-*.tar.gz"))[-1]


class TestEveryTreeRootSiteUsesTheChokepoint:
    """Structural: a new site iterating a component's trees must use safe_tree_root.

    The symlink class was found three times in three different sites. Counting call
    sites is crude but it is the property that actually failed: each site had been
    written without the check, independently.
    """

    def test_each_trees_loop_is_guarded(self):
        tree = ast.parse(SNAPSHOT_SRC.read_text(encoding="utf-8"))
        unguarded: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.For):
                continue
            # `for tree in <...>.trees:`
            it = node.iter
            if not (isinstance(it, ast.Attribute) and it.attr == "trees"):
                continue
            body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
            if "safe_tree_root" not in body:
                unguarded.append(node.lineno)
        assert unguarded == [], (
            f"trees loop(s) at line(s) {unguarded} do not call safe_tree_root — a "
            f"symlinked root there would be dereferenced"
        )

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


class TestDownloadLockdownFailureIsControlled:
    def test_a_failed_owner_lockdown_removes_the_file_and_exits_nonzero(
        self, tmp_path, monkeypatch, capsys
    ):
        """A world-readable copy of the operator's memory must not be left behind, and
        a traceback here is indistinguishable from a crash."""
        import kiro_crew.snapshot as snap
        from kiro_crew import platform_compat

        downloaded: dict[str, Path] = {}

        def fake_download(url, into, profile):
            into.mkdir(parents=True, exist_ok=True)
            local = into / "kirocrew-snapshot-20260811T000000Z.tar.gz"
            local.write_bytes(b"bundle")
            downloaded["path"] = local
            return local

        def boom(_p):
            raise OSError("chmod refused")

        monkeypatch.setattr(snapshot_remote, "download", fake_download)
        monkeypatch.setattr(platform_compat, "restrict_to_owner", boom)
        monkeypatch.setattr(snap, "_resolve_aws_profile", lambda _e: ("p", "us-west-2"))
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))

        rc = restore_main(["s3://my-backups/x/bundle.tar.gz", "--force"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "owner-only" in out
        assert "Traceback" not in out
        assert not downloaded["path"].exists(), "the un-locked-down download was kept"

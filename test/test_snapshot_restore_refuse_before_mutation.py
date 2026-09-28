"""Tests for the cycle-9 review findings.

The surviving finding: the RESTORE side kept skipping an unsafe tree instead of
refusing before any mutation, leaving state split between two versions while the
command reported success.
"""

from __future__ import annotations

import pytest
from test_snapshot import _setup_fake_kirocrew, unpinnable_argv

from kiro_crew import snapshot as snap


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


class TestAnUnsafeDestinationRootRefusesBeforeAnyMutation:
    """The staging side was fixed last cycle; the RESTORE side kept skipping.

    `_backup_and_copy` swaps the databases before the tree loops run, so skipping an
    unsafe markdown tree left memory split between two versions — and the command
    reported success. Validation is now hoisted ahead of every mutation.
    """

    def _bundle(self, tmp_path, monkeypatch):
        home = tmp_path / "src"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        _setup_fake_kirocrew(home)
        md = home / "workspace" / "memory"
        md.mkdir(parents=True, exist_ok=True)
        (md / "preferences.md").write_text("from the bundle")
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), "--components", "memory"] + unpinnable_argv()) == 0
        return next(out.glob("kirocrew-snapshot-*.tar.gz"))

    def _linked_dest(self, tmp_path, name: str, tree: str):
        """A seeded data home whose *tree* root is a symlink pointing outside it.

        Order matters: seed FIRST, then replace the real directory with the link.
        Creating the link first makes the seeding write straight through it, which is a
        fixture bug that quietly invalidates the assertion about the outside directory.
        """
        import shutil

        dest = tmp_path / name
        dest.mkdir()
        _setup_fake_kirocrew(dest)
        outside = tmp_path / f"outside-{name}"
        outside.mkdir()
        target = dest / tree
        if target.is_dir():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(outside, target_is_directory=True)
        return dest, outside

    def test_replace_refuses_before_replacing_the_databases(self, tmp_path, monkeypatch):
        bundle = self._bundle(tmp_path, monkeypatch)
        try:
            dest, _outside = self._linked_dest(tmp_path, "dest", "workspace/memory")
        except (OSError, NotImplementedError):
            pytest.skip("cannot create a directory symlink on this platform")
        original = (dest / "memory.db").read_bytes() if (dest / "memory.db").is_file() else None
        monkeypatch.setenv("KIROCREW_HOME", str(dest))

        rc = snap.restore_main(
            [str(bundle), "--components", "memory", "--mode", "replace", "--force"]
        )
        assert rc == 1
        # The database was NOT swapped, and no rollback directory was created.
        if original is not None:
            assert (dest / "memory.db").read_bytes() == original
        assert list(dest.glob("pre-restore-*")) == []

    def test_merge_refuses_too(self, tmp_path, monkeypatch):
        bundle = self._bundle(tmp_path, monkeypatch)
        try:
            dest, outside = self._linked_dest(tmp_path, "dest2", "workspace/knowledge")
        except (OSError, NotImplementedError):
            pytest.skip("cannot create a directory symlink on this platform")
        monkeypatch.setenv("KIROCREW_HOME", str(dest))
        rc = snap.restore_main(
            [str(bundle), "--components", "memory", "--mode", "merge", "--force"]
        )
        assert rc == 1
        assert list(outside.iterdir()) == []

    def test_a_clean_destination_still_restores(self, tmp_path, monkeypatch):
        """The refusal must not fire on an ordinary home."""
        bundle = self._bundle(tmp_path, monkeypatch)
        dest = tmp_path / "clean"
        dest.mkdir()
        _setup_fake_kirocrew(dest)
        monkeypatch.setenv("KIROCREW_HOME", str(dest))
        assert (
            snap.restore_main(
                [str(bundle), "--components", "memory", "--mode", "replace", "--force"]
                + unpinnable_argv()
            )
            == 0
        )
        assert (dest / "workspace" / "memory" / "preferences.md").read_text() == ("from the bundle")

    def test_the_refusal_is_a_message_not_a_traceback(self, tmp_path, monkeypatch, capsys):
        bundle = self._bundle(tmp_path, monkeypatch)
        try:
            dest, _outside = self._linked_dest(tmp_path, "dest3", "workspace/memory")
        except (OSError, NotImplementedError):
            pytest.skip("cannot create a directory symlink on this platform")
        monkeypatch.setenv("KIROCREW_HOME", str(dest))
        snap.restore_main([str(bundle), "--components", "memory", "--mode", "replace", "--force"])
        out = capsys.readouterr().out
        assert "do not resolve inside the data home" in out
        assert "Nothing has been changed" in out

"""Tests for the cycle-3 review findings.

The surviving cases are restore-side partial-state defects: a tree the archive lacks
was left behind, and a rollback directory was merged with another restore's.
"""

from __future__ import annotations

import shutil
import tarfile
from datetime import datetime, timezone

from test_snapshot import _setup_fake_kirocrew, unpinnable_argv

from kiro_crew import snapshot as snap


def _peek(bundle, tmp_path):
    """Unpack a bundle so a test can assert on what it actually contains."""
    dest = tmp_path / "peek"
    if not dest.is_dir():
        dest.mkdir()
        with tarfile.open(bundle) as tf:
            tf.extractall(dest)
    return next(p for p in dest.iterdir() if p.is_dir())


def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    _setup_fake_kirocrew(home)
    return home


class TestReplaceDoesNotKeepATreeTheArchiveLacks:
    """A bundle without `workspace/knowledge` used to leave the destination's own
    knowledge tree in place, so a "replace" produced restored memory mixed with stale
    notes — and reported success. Replace means the destination matches the archive."""

    def test_a_tree_absent_from_the_bundle_is_removed(self, tmp_path, monkeypatch):
        home = _home(tmp_path, monkeypatch)
        md = home / "workspace" / "memory"
        md.mkdir(parents=True, exist_ok=True)
        (md / "preferences.md").write_text("original")
        # The fixture ships a knowledge tree; remove it so the bundle genuinely
        # cannot carry one. Without this the case under test does not exist.
        shutil.rmtree(home / "workspace" / "knowledge")
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), "--components", "memory", *unpinnable_argv()]) == 0
        bundle = next(out.glob("kirocrew-snapshot-*.tar.gz"))
        assert not (
            _peek(bundle, tmp_path) / "workspace" / "knowledge"
        ).exists(), "the bundle carries a knowledge tree, so this test proves nothing"

        # The destination has since grown one.
        kd = home / "workspace" / "knowledge"
        kd.mkdir(parents=True, exist_ok=True)
        (kd / "stale.md").write_text("not in the bundle")

        assert (
            snap.restore_main(
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
            == 0
        )
        assert not (
            kd / "stale.md"
        ).exists(), "a tree the bundle does not carry survived a replace restore"
        assert (md / "preferences.md").read_text() == "original"

    def test_the_removed_tree_is_still_in_the_rollback_copy(self, tmp_path, monkeypatch):
        """Clearing is only defensible because the state is recoverable."""
        home = _home(tmp_path, monkeypatch)
        md = home / "workspace" / "memory"
        md.mkdir(parents=True, exist_ok=True)
        (md / "preferences.md").write_text("original")
        shutil.rmtree(home / "workspace" / "knowledge")
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), "--components", "memory", *unpinnable_argv()]) == 0
        bundle = next(out.glob("kirocrew-snapshot-*.tar.gz"))

        kd = home / "workspace" / "knowledge"
        kd.mkdir(parents=True, exist_ok=True)
        (kd / "stale.md").write_text("not in the bundle")

        assert (
            snap.restore_main(
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
            == 0
        )
        saved = list(home.glob("pre-restore-*/workspace/knowledge/stale.md"))
        assert saved and saved[0].read_text() == "not in the bundle"

    def test_a_bundle_that_has_the_tree_still_replaces_it(self, tmp_path, monkeypatch):
        home = _home(tmp_path, monkeypatch)
        md = home / "workspace" / "memory"
        md.mkdir(parents=True, exist_ok=True)
        (md / "preferences.md").write_text("original")
        kd = home / "workspace" / "knowledge"
        kd.mkdir(parents=True, exist_ok=True)
        (kd / "note.md").write_text("from the bundle")
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), "--components", "memory", *unpinnable_argv()]) == 0
        bundle = next(out.glob("kirocrew-snapshot-*.tar.gz"))

        (kd / "note.md").write_text("changed since the backup")
        assert (
            snap.restore_main(
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
            == 0
        )
        assert (kd / "note.md").read_text() == "from the bundle"


class TestTwoRestoresInOneSecondKeepSeparateRollbackSets:
    """The rollback directory name is second-granular, so two restores inside one second
    resolve to the same name. One directory holding two pre-restore states would roll back
    to neither, so each restore gets its own — allocated, not merged, and not crashed."""

    def test_the_second_restore_gets_its_own_rollback_set(self, tmp_path, monkeypatch):
        home = _home(tmp_path, monkeypatch)
        md = home / "workspace" / "memory"
        md.mkdir(parents=True, exist_ok=True)
        (md / "preferences.md").write_text("original")
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), "--components", "memory", *unpinnable_argv()]) == 0
        bundle = next(out.glob("kirocrew-snapshot-*.tar.gz"))

        frozen = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        class _Frozen:
            @staticmethod
            def now(tz=None):
                return frozen

        monkeypatch.setattr(snap, "datetime", _Frozen)

        (md / "preferences.md").write_text("live state A")
        assert (
            snap.restore_main(
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
            == 0
        )
        assert (md / "preferences.md").read_text() == "original"

        # Second restore in the SAME frozen second. Earlier this raised an uncaught
        # FileExistsError; a collision has to be resolved, not thrown, because a crash is
        # not the clean abort it was described as.
        (md / "preferences.md").write_text("live state B")
        assert (
            snap.restore_main(
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
            == 0
        )

        saved = sorted(p.name for p in home.glob("pre-restore-*"))
        assert len(saved) == 2, f"each restore needs its own rollback set: {saved}"
        # And neither set holds the other's state: A was saved by the first restore, B by
        # the second, so the two directories differ in content.
        contents = {
            (home / name / "workspace" / "memory" / "preferences.md").read_text() for name in saved
        }
        assert contents == {"live state A", "live state B"}, contents

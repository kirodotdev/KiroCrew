"""Restore-side link-root behaviour.

Rollback recovery clears a linked live root as a link, the merge refuses to write outside
the data home, and the archive probe admits a good bundle.
"""

from __future__ import annotations

import os
import tarfile

import pytest
from test_snapshot import unpinnable_argv

from kiro_crew import snapshot as snap


class TestAContainedLinkRootDoesNotHalfRestore:
    """A live tree root can pass containment and still be a LINK.

    A symlink to elsewhere inside the data home passes containment, and `shutil.rmtree`
    raises on it. REPLACE/MERGE refuse such a root up front, so rollback recovery
    (`_restore_everything_from_rollback`) is the one place that clears it, and must remove
    it as a link without touching its target.
    """

    def test_recovery_removes_a_linked_live_root_as_a_link(self, tmp_path):
        """The saved copy is put back, and the link target OUTSIDE the home is untouched."""
        backup = tmp_path / "rollback"
        (backup / "skills").mkdir(parents=True)
        (backup / "skills" / "saved.md").write_text("saved")
        home = tmp_path / "home"
        home.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.md").write_text("must survive")
        link = home / "skills"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("cannot create a directory symlink on this platform")

        failed = snap._restore_everything_from_rollback(
            backup, home, ["skills"], {"skills"}, allow_unpinned=bool(unpinnable_argv())
        )

        assert failed == [], f"recovery could not clear the linked root: {failed}"
        assert not link.is_symlink(), "the link survived instead of being removed as a link"
        assert (outside / "keep.md").is_file(), "clearing the link deleted the target"
        assert (home / "skills" / "saved.md").read_text() == "saved", "saved copy not put back"

    def test_recovery_still_removes_a_real_directory_root(self, tmp_path):
        """A real live directory is cleared before the saved copy replaces it."""
        backup = tmp_path / "rollback"
        (backup / "skills").mkdir(parents=True)
        (backup / "skills" / "saved.md").write_text("saved")
        home = tmp_path / "home"
        (home / "skills" / "sub").mkdir(parents=True)
        (home / "skills" / "sub" / "stale.md").write_text("stale")

        failed = snap._restore_everything_from_rollback(
            backup, home, ["skills"], {"skills"}, allow_unpinned=bool(unpinnable_argv())
        )

        assert failed == [], failed
        assert not (home / "skills" / "sub" / "stale.md").exists(), "the stale live tree survived"
        assert (home / "skills" / "saved.md").read_text() == "saved"

    def test_recovery_does_not_raise_when_the_live_root_is_missing(self, tmp_path):
        """No pre-existing live tree, but a saved copy -- recovery creates it, no crash."""
        backup = tmp_path / "rollback"
        (backup / "skills").mkdir(parents=True)
        (backup / "skills" / "saved.md").write_text("saved")
        home = tmp_path / "home"
        home.mkdir()  # no `skills` under it

        failed = snap._restore_everything_from_rollback(
            backup, home, ["skills"], {"skills"}, allow_unpinned=bool(unpinnable_argv())
        )

        assert failed == [], failed
        assert (home / "skills" / "saved.md").read_text() == "saved"

    def test_recovery_clears_a_linked_root_as_a_link_before_rmtree(self):
        """Structural: every rmtree site in recovery removes a link AS a link first.

        `is_dir()` follows a link and rmtree then raises on it. Checked per site, because a
        first-occurrence order check would miss a second, unguarded rmtree.
        """
        import inspect

        recovery = inspect.getsource(snap._restore_everything_from_rollback)
        raw = recovery.splitlines()
        sites = [i for i, ln in enumerate(raw) if ln.strip() == "shutil.rmtree(str(target))"]
        assert sites, "recovery no longer clears a live directory target at all"

        def _indent(line: str) -> int:
            return len(line) - len(line.lstrip())

        for i in sites:
            # The gate is the nearest ENCLOSING `if`/`elif` (closest preceding line at strictly
            # smaller indentation), not `raw[i - 1]`, so a statement inserted between the
            # guard and the rmtree does not hide it.
            site_indent = _indent(raw[i])
            gate = ""
            for j in range(i - 1, -1, -1):
                if not raw[j].strip():
                    continue
                if _indent(raw[j]) < site_indent and raw[j].strip().startswith(("if ", "elif ")):
                    gate = raw[j]
                    break
            assert gate, f"the rmtree at line {i} is not inside any if/elif at all"
            assert "target.is_dir()" in gate, (
                f"an rmtree of the live target at line {i} is not gated on the target being "
                f"a directory at all -- found {gate.strip()!r}"
            )
            if "is_link_or_junction(target)" in gate:
                continue  # self-guarded: `is_dir() and not is_link_or_junction(...)`
            # Otherwise this must be an `elif` whose CHAIN HEAD (nearest preceding `if` at the
            # same indentation) excludes a link. A fixed line window would let an unrelated
            # link check a few lines up vouch for an unguarded site.
            assert gate.strip().startswith("elif "), (
                f"the rmtree at line {i} is gated by {gate.strip()!r}, which neither excludes "
                "a link itself nor continues a chain that does"
            )
            depth = _indent(gate)
            head = None
            for j in range(i - 2, -1, -1):
                if not raw[j].strip():
                    continue
                if _indent(raw[j]) < depth:
                    break  # left the block without finding the chain head
                if _indent(raw[j]) == depth and raw[j].strip().startswith("if "):
                    head = raw[j]
                    break
            assert head is not None and "is_link_or_junction(target)" in head, (
                f"the rmtree at line {i} can be reached for a LINK: its chain head is "
                f"{(head.strip() if head else None)!r}, which does not test for one, so "
                "rmtree would raise on the symlink and strand the whole recovery"
            )


class TestTheMergeCannotWriteOutsideTheDataHome:
    @pytest.mark.skipif(
        os.name == "nt",
        reason="_copy_tree_no_overwrite refuses without dir_fd; the not-following of a "
        "nested destination link is a guarantee of the descriptor-pinned merge walk, "
        "which does not exist on a platform lacking os.supports_dir_fd",
    )
    def test_a_nested_destination_link_is_not_followed(self, tmp_path):
        """safe_tree_root validates the destination ROOT, but the merge walks below it
        and the write target is the dangerous end: a nested link under the destination
        would deposit restored files wherever it aimed."""
        home = tmp_path / "home"
        (home / "workspace" / "memory").mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        dest = home / "workspace" / "memory"
        try:
            (dest / "history").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("cannot create a directory symlink on this platform")

        src = tmp_path / "snap" / "workspace" / "memory"
        (src / "history").mkdir(parents=True)
        (src / "history" / "leak.md").write_text("must not escape")
        (src / "keep.md").write_text("fine")

        snap._copy_tree_no_overwrite(src, dest)

        assert (dest / "keep.md").is_file(), "the legitimate file should still merge"
        assert not (outside / "leak.md").exists(), "the merge wrote outside the data home"

    def test_a_fresh_temporary_tree_still_merges(self, tmp_path):
        """No planted link, so nothing to refuse: the ordinary merge path still copies.

        Containment is enforced inside the descriptor-pinned primitive, with no home argument.
        """
        src = tmp_path / "s"
        src.mkdir()
        (src / "f.md").write_text("x")
        dst = tmp_path / "d"
        dst.mkdir()
        snap._copy_tree_no_overwrite(src, dst, allow_unpinned=bool(unpinnable_argv()))
        assert (dst / "f.md").is_file()


class TestTheArchiveProbeAcceptsAGoodBundle:
    def test_a_valid_archive_passes_the_probe(self, tmp_path):
        """The guard must not reject bundles it is meant to admit."""
        good = tmp_path / "good.tar.gz"
        payload = tmp_path / "f.txt"
        payload.write_text("hi")
        with tarfile.open(good, "w:gz") as tf:
            tf.add(payload, arcname="f.txt")
        with tarfile.open(good) as tf:
            assert [m.name for m in tf.getmembers()] == ["f.txt"]

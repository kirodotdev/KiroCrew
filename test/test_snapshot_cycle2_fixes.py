"""Tests for the cycle-2 review findings.

Each one pins a property whose absence was a real exposure, not a style point.
"""

from __future__ import annotations

import os
import shutil
import tarfile

import pytest
from test_snapshot import _setup_fake_kirocrew, unpinnable_argv

from kiro_crew import pinned_fs, platform_compat
from kiro_crew import snapshot as snap


class TestAnUnsafeTreeRootFailsTheSnapshot:
    """Superseded contract, and the change is a strengthening.

    This class previously asserted that an unsafe root was SKIPPED and left nothing in
    the bundle. Skipping was still wrong: the manifest went on declaring the component,
    so the artefact claimed to contain memory it had silently omitted, and the operator
    only found out when they tried to recover. A backup that lies about its contents is
    worse than a refusal, so an unsafe root now fails the snapshot.
    """

    def test_a_symlinked_component_root_refuses_the_snapshot(self, tmp_path, monkeypatch, capsys):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        _setup_fake_kirocrew(home)

        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "loot.md").write_text("SHOULD NOT BE STAGED")
        target = home / "workspace" / "memory"
        if target.is_dir():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("cannot create a directory symlink on this platform")

        out = tmp_path / "out"
        rc = snap.snapshot_main([str(out), "--components", "memory"])
        assert rc != 0, "an unsafe root produced a 'successful' backup"
        printed = capsys.readouterr().out
        assert "Refusing" in printed or "refus" in printed.lower()
        # And no bundle claiming `memory` was left behind.
        assert list(out.glob("kirocrew-snapshot-*.tar.gz")) == []

    def test_the_guard_still_stages_a_legitimate_tree(self, tmp_path, monkeypatch):
        """The refusal must not turn a valid tree into a failure."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        _setup_fake_kirocrew(home)
        md = home / "workspace" / "memory"
        md.mkdir(parents=True, exist_ok=True)
        (md / "preferences.md").write_text("keep me")

        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), "--components", "memory", *unpinnable_argv()]) == 0
        with tarfile.open(next(out.glob("kirocrew-snapshot-*.tar.gz"))) as tf:
            names = tf.getnames()
        assert any(n.endswith("workspace/memory/preferences.md") for n in names)


class TestNestedLinksCannotSmuggleFilesIntoABundle:
    def test_the_tree_walkers_use_a_junction_aware_screen(self):
        """`os.path.islink` returns False for a Windows directory junction, so a junction
        nested inside a component tree must be caught by a reparse-aware check or it is
        copied THROUGH -- pulling whatever it targets into the bundle and then to S3.

        M1 retarget: staging routes through `pinned_fs`. The pinned tree walk
        (`stage_tree_pinned`) screens each entry by `lstat`/`fstat` on a descriptor -- a
        reparse point is not `S_ISREG`/a real dir there -- and the declared by-name
        fallback in `_copytree_safe` uses `pinned_fs.is_reparse_point`, which reports a
        junction where `islink` does not. This pins that the junction-aware screen is what
        both staging paths rely on, rather than the removed `is_link_or_junction` helper.
        """
        import inspect

        copytree = inspect.getsource(snap._copytree_safe)
        assert "is_reparse_point" in copytree, (
            "the by-name staging fallback dropped the reparse-aware screen, so a Windows "
            "junction would be followed"
        )
        # The bare name-based `islink` must not be the ONLY screen (it misses junctions).
        # It may still appear alongside is_reparse_point in the fallback's OR-guard.
        prim = inspect.getsource(pinned_fs.stage_tree_pinned)
        assert (
            "on_skip" in prim and "SKIP_SYMLINK" in prim
        ), "the pinned tree walk no longer reports a skipped link/junction"

    @pytest.mark.skipif(
        os.name == "nt",
        reason="_copytree_safe refuses without dir_fd; the nested-symlink screen this pins "
        "is the descriptor-pinned tree walk, which does not exist on a platform lacking "
        "os.supports_dir_fd, so there is no such guarantee here to assert",
    )
    def test_a_nested_symlink_is_not_copied(self, tmp_path):
        src = tmp_path / "src"
        (src / "sub").mkdir(parents=True)
        (src / "sub" / "real.md").write_text("keep me")
        secret_dir = tmp_path / "secrets"
        secret_dir.mkdir()
        (secret_dir / "creds").write_text("SECRET")
        try:
            (src / "sub" / "link").symlink_to(secret_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("this platform does not allow creating a directory symlink here")

        dst = tmp_path / "dst"
        snap._copytree_safe(src, dst)
        assert (dst / "sub" / "real.md").is_file()
        assert not (dst / "sub" / "link").exists()
        assert "SECRET" not in "".join(p.read_text() for p in dst.rglob("*") if p.is_file())

    @pytest.mark.skipif(
        os.name == "nt",
        reason="both walkers (_copytree_safe / _copy_tree_no_overwrite) refuse without "
        "dir_fd; the agreement this pins is a property of the descriptor-pinned tree walk, "
        "which does not exist on a platform lacking os.supports_dir_fd",
    )
    def test_both_walkers_agree(self, tmp_path):
        """The two tree walkers are used on the same data at different phases, so a
        link rejected by one and followed by the other is a hole."""
        src = tmp_path / "s"
        (src / "d").mkdir(parents=True)
        (src / "d" / "f.md").write_text("x")
        target = tmp_path / "t"
        target.mkdir()
        (target / "leak").write_text("LEAK")
        try:
            (src / "d" / "l").symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("cannot create a directory symlink on this platform")

        a = tmp_path / "a"
        snap._copytree_safe(src, a)
        b = tmp_path / "b"
        b.mkdir()
        snap._copy_tree_no_overwrite(src, b)
        for root in (a, b):
            assert not (root / "d" / "l").exists(), f"{root} followed the link"

    def test_the_predicate_itself_reports_a_plain_symlink(self, tmp_path):
        f = tmp_path / "f"
        f.write_text("x")
        link = tmp_path / "l"
        try:
            link.symlink_to(f)
        except (OSError, NotImplementedError):
            pytest.skip("cannot create a symlink on this platform")
        assert platform_compat.is_link_or_junction(link)
        assert not platform_compat.is_link_or_junction(f)

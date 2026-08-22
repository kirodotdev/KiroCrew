"""Tests for the cycle-2 review findings.

Each one pins a property whose absence was a real exposure, not a style point.
"""

from __future__ import annotations

import os
import shutil
import stat
import tarfile
from pathlib import Path

import pytest
from test_snapshot import _setup_fake_kirocrew

from kiro_crew import platform_compat
from kiro_crew import snapshot as snap
from kiro_crew import snapshot_remote as remote
from kiro_crew.deploy import engine
from kiro_crew.security import is_sensitive_path


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
        assert snap.snapshot_main([str(out), "--components", "memory"]) == 0
        with tarfile.open(next(out.glob("kirocrew-snapshot-*.tar.gz"))) as tf:
            names = tf.getnames()
        assert any(n.endswith("workspace/memory/preferences.md") for n in names)


class TestTheDestinationRegistryIsATrustAnchor:
    """The backup path takes no bucket from its caller -- so the recorded file IS the
    decision, and an agent able to author it could redirect every future backup to a
    bucket it controls. `--expected-bucket-owner` would then verify the ATTACKER's
    ownership, which is why format validation alone is not enough."""

    def test_the_registry_is_behind_the_sensitive_path_floor(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        reg = tmp_path / "backup" / "destination.json"
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text("{}")
        assert is_sensitive_path(str(reg)), (
            "destination.json must be classified sensitive: it decides where the whole "
            "memory store is uploaded"
        )

    def test_the_containing_directory_is_classified_not_just_the_leaf(self, tmp_path, monkeypatch):
        """Naming only the leaf leaves the container writable, and a writable container
        is the same hole one level up: replace `backup/` with a symlink and the
        protected leaf resolves somewhere unprotected, where it can be rewritten."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        d = tmp_path / "backup"
        d.mkdir(parents=True, exist_ok=True)
        assert is_sensitive_path(str(d)), "the directory itself must be protected"
        # And so is anything an attacker might write beside the record.
        assert is_sensitive_path(str(d / "destination.json.tmp"))
        assert is_sensitive_path(str(d / "anything-else"))

    def test_restore_rollback_copies_are_not_caught_by_it(self, tmp_path, monkeypatch):
        """The classification must not shut out an unrelated legitimate path -- restore's
        rollback copies live at pre-restore-<ts>/, not under backup/."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        rollback = tmp_path / "pre-restore-20260811T000000Z"
        rollback.mkdir(parents=True, exist_ok=True)
        assert not is_sensitive_path(str(rollback / "memory.db"))


class TestDownloadedBundlesAreNeverWorldReadable:
    def test_the_bundle_is_owner_only_when_it_lands(self, tmp_path, monkeypatch):
        """`aws s3 cp` creates its output with the process umask, and the snapshot dir
        can be shared, so downloading straight to the final path would expose the whole
        memory store for the length of the transfer.

        The mode-bit assertions are POSIX-only on purpose. Windows does not project a
        DACL back into st_mode -- a locked-down file still reads 0o666 there -- so
        asserting 0o600 unconditionally would be asserting a property the platform does
        not have, and would fail on a correctly-secured file. The Windows guarantee is
        behavioural and is covered by
        `test_windows_gets_an_explicit_dacl_on_the_staging_directory`, which runs
        everywhere.
        """
        seen: dict[str, object] = {}

        def fake_cp(args, profile, timeout=30):
            # Emulate the CLI: create the file with permissive default perms.
            dest = Path(args[-1])
            dest.write_bytes(b"bundle")
            os.chmod(dest, 0o644)
            # Record what the staging directory looked like at write time.
            seen["staging_mode"] = stat.S_IMODE(dest.parent.stat().st_mode)
            seen["staging_dir"] = str(dest.parent)
            return 0, "", ""

        monkeypatch.setattr(engine, "run_aws", fake_cp)
        into = tmp_path / "snapshots"
        into.mkdir()
        url = "s3://my-backups/backups/host/snap.tar.gz"
        local = remote.download(url, into, "p")

        assert local.is_file()
        # Platform-independent: the bundle was never written into the shared directory,
        # and the staging directory does not survive.
        assert seen["staging_dir"] != str(into)
        assert [p.name for p in into.iterdir()] == [local.name]

        if platform_compat.IS_POSIX:
            assert stat.S_IMODE(local.stat().st_mode) == 0o600
            assert seen["staging_mode"] == 0o700

    def test_windows_gets_an_explicit_dacl_on_the_staging_directory(self, tmp_path, monkeypatch):
        """On Windows the mode bits are not the access control that matters: a new
        directory inherits the parent's DACL, so a shared snapshot directory would hand
        its permissions to the staged bundle whatever the mode says. This branch cannot
        execute on POSIX, so it is exercised by faking the platform -- otherwise the only
        platform where the guard matters is the one never covered.
        """
        locked: list[str] = []

        def fake_cp(args, profile, timeout=30):
            Path(args[-1]).write_bytes(b"bundle")
            return 0, "", ""

        monkeypatch.setattr(engine, "run_aws", fake_cp)
        # Patch the platform PREDICATE, not os.name -- patching os.name makes pathlib
        # try to instantiate a WindowsPath and the test fails for the wrong reason.
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(platform_compat, "restrict_to_owner", lambda p: locked.append(str(p)))
        into = tmp_path / "snapshots"
        into.mkdir()
        local = remote.download("s3://my-backups/backups/h/snap.tar.gz", into, "p")

        assert local.is_file()
        assert len(locked) == 2, f"expected staging dir + file to be locked: {locked}"
        # The directory is locked down BEFORE the bundle is written into it.
        assert locked[0] != str(local)
        assert Path(locked[0]).name.startswith("kirocrew-fetch-")

    def test_posix_relies_on_mkdtemp_rather_than_a_redundant_chmod(self):
        """mkdtemp already creates the directory 0o700. Re-applying it adds a literal for
        a static analyser to misread as 'widely permissive' while changing nothing."""
        import inspect

        src = inspect.getsource(remote.download)
        assert "mkdtemp" in src
        assert "os.chmod" not in src


class TestRetentionSurvivesAFailingDestination:
    def test_prune_runs_even_when_the_upload_fails(self, tmp_path, monkeypatch, capsys):
        """--keep is a promise about local disk. A persistently failing destination must
        not turn a daily backup into an unbounded pile -- the disk fills, and then the
        snapshot that would have worked cannot be written either."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        _setup_fake_kirocrew(home)
        out = tmp_path / "out"
        out.mkdir()
        # Pre-existing bundles beyond --keep.
        for i in range(4):
            p = out / f"kirocrew-snapshot-2026080{i}T000000Z.tar.gz"
            p.write_bytes(b"old")
            os.utime(p, (1000 + i, 1000 + i))

        monkeypatch.setattr(snap, "_upload_bundle", lambda *_a, **_k: 1)
        rc = snap.snapshot_main([str(out), "--components", "memory", "--to-s3", "--keep", "2"])

        assert rc == 1, "a failed upload must still be reported as a failure"
        remaining = sorted(p.name for p in out.glob("kirocrew-snapshot-*.tar.gz"))
        assert len(remaining) == 2, f"prune did not run on the failure path: {remaining}"
        assert "Pruned" in capsys.readouterr().out


class TestNestedLinksCannotSmuggleFilesIntoABundle:
    def test_the_tree_walker_uses_the_junction_aware_predicate(self):
        """`os.path.islink` returns False for a Windows directory junction, so a
        junction nested inside a component tree would be copied THROUGH -- pulling
        whatever it targets into the bundle and then to S3. safe_tree_root guards the
        tree's root; this guards every node below it, and the two must agree on what
        counts as a link or the weaker one decides."""
        import inspect

        src = inspect.getsource(snap._copytree_safe)
        assert "is_link_or_junction" in src
        assert "os.path.islink" not in src.replace("``os.path.islink``", "")

        walker = inspect.getsource(snap._copy_tree_no_overwrite)
        assert "is_link_or_junction" in walker
        assert ".is_symlink()" not in walker

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

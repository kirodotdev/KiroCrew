"""Tests for :mod:`kiro_crew.staging`.

Everything runs against a monkeypatched data home under ``tmp_path``; the real
``<data home>/staging`` is never touched.

The sweep cases set mtimes explicitly instead of sleeping, and age EVERY entry
in a tree, because the module's idle signal is the tree's newest mtime rather
than the top directory's (``writing-tests`` class 7: a sleep makes a tie less
likely, never impossible, and would couple the test to the wall clock).
"""

from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path

import portability_helpers
import pytest

from kiro_crew import staging

# Fixtures reused by the import-extraction regression lock below, aliased from the
# shared helper module (the suite's house pattern) so pytest resolves
# ``patched_config_dir`` and the ``fake_kirocrew_home`` it depends on.
fake_kirocrew_home = portability_helpers.fake_kirocrew_home
patched_config_dir = portability_helpers.patched_config_dir

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")


@pytest.fixture
def home(monkeypatch, tmp_path: Path) -> Path:
    """A data home under ``tmp_path``, with both module seams pointed at it.

    ``staging_dir`` reads ``config_dir`` from ``config.loader`` imported into this
    module's namespace, so patching the module attribute reaches it; the sweep
    reads the same name. A clean sandbox-marker baseline: the leaf is VISIBLE so
    ``staging_dir`` ignores the marker (``TestVisibleInSandboxToo`` pins that), but
    clearing it keeps these cases independent of the ambient environment.
    """
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setattr(staging, "config_dir", lambda: root)
    monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
    return root


def _age(path: Path, seconds: float) -> None:
    """Push *path* and everything under it *seconds* into the past.

    Links are aged as THEMSELVES (``follow_symlinks=False``), never through, so a
    link left at the current time cannot make its whole tree read as active and a
    sweep case pass for the wrong reason.
    """
    stamp = time.time() - seconds
    for current, dirnames, filenames in os.walk(path, topdown=False):
        for name in filenames + dirnames:
            target = Path(current) / name
            try:
                os.utime(target, (stamp, stamp), follow_symlinks=not target.is_symlink())
            except (NotImplementedError, OSError):
                pass
    os.utime(path, (stamp, stamp))


class TestStagingDir:
    def test_creates_the_root_under_the_data_home(self, home: Path) -> None:
        path = staging.staging_dir()

        assert path == home / staging.STAGING_DIRNAME
        assert path.is_dir()
        if sys.platform != "win32":
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o700

    def test_is_idempotent_and_keeps_existing_contents(self, home: Path) -> None:
        first = staging.staging_dir()
        (first / "kc_import_abc.zip").write_text("upload", encoding="utf-8")

        second = staging.staging_dir()

        assert second == first
        assert (second / "kc_import_abc.zip").read_text(encoding="utf-8") == "upload"

    def test_reasserts_owner_only_on_every_call(self, home: Path) -> None:
        if sys.platform == "win32":
            pytest.skip("POSIX mode bits")
        root = staging.staging_dir()
        # Deliberately loosen to a world-readable/traversable mode so the next call has
        # something to re-tighten; the assertion below proves staging_dir() restores 0700.
        # Kept on one line (fmt: skip) so the nosemgrep suppression stays on the matched line.
        os.chmod(root, 0o755)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- intentional test loosening  # noqa: E501  # fmt: skip

        again = staging.staging_dir()

        assert again == root
        assert stat.S_IMODE(os.stat(root).st_mode) == 0o700

    @_POSIX_ONLY
    def test_a_symlinked_leaf_is_refused(self, home: Path, tmp_path: Path) -> None:
        # A link planted at the leaf would put every staged file outside the data
        # home and outside the sandbox mask, which no per-file check downstream can
        # see — so it is refused rather than followed.
        outside = tmp_path / "outside"
        outside.mkdir()
        (home / staging.STAGING_DIRNAME).symlink_to(outside, target_is_directory=True)

        with pytest.raises(ValueError, match="not a real directory"):
            staging.staging_dir()

    def test_a_file_at_the_leaf_is_refused(self, home: Path) -> None:
        (home / staging.STAGING_DIRNAME).write_text("not a dir", encoding="utf-8")

        with pytest.raises((ValueError, FileExistsError, NotADirectoryError, OSError)):
            staging.staging_dir()


class TestVisibleInSandboxToo:
    """The leaf is VISIBLE (unmasked), matching its destinations (the ``snapshots``
    archive leaf and the data home a restore/import installs into). So an agent
    shelling out to ``kirocrew snapshot``/``restore`` from inside the sandbox stages
    on real disk exactly like the gateway -- there is no mask to avoid and no
    tmpfs-fallback branch. The resolver returns the real root whether or not the
    sandbox marker is set.
    """

    def test_staging_dir_is_the_root_even_in_sandbox(self, home: Path, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")

        path = staging.staging_dir()

        assert path == home / staging.STAGING_DIRNAME
        assert path.is_dir()


class TestSweep:
    def test_removes_a_tree_idle_past_the_grace(self, home: Path) -> None:
        root = staging.staging_dir()
        orphan = root / "kc-build-dead"
        orphan.mkdir()
        (orphan / "archive.tar.gz").write_bytes(b"gbs of the data home in the clear")
        _age(orphan, staging.IDLE_GRACE_SECONDS + 60)

        removed = staging.sweep_staging()

        assert removed == 1
        assert not orphan.exists()

    def test_removes_a_tree_when_the_platform_cannot_pin_a_tree_walk(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Windows path: where ``pinned_fs.supports_pinned_tree_walk()`` is False
        (no ``dir_fd`` ``openat``), the sweep still reclaims an idle orphan via the
        by-name ``rmtree_force`` fallback, safe because the held root pin blocks a
        root/ancestor swap on that platform. Forced here with a monkeypatch so the
        branch is covered on POSIX CI too -- without it the sweep returned 0 on Windows
        and the real Backend Tests (Windows) shard went red.
        """
        monkeypatch.setattr(staging.pinned_fs, "supports_pinned_tree_walk", lambda: False)
        root = staging.staging_dir()
        orphan = root / "kc-build-dead"
        orphan.mkdir()
        (orphan / "archive.tar.gz").write_bytes(b"gbs of the data home in the clear")
        _age(orphan, staging.IDLE_GRACE_SECONDS + 60)

        removed = staging.sweep_staging()

        assert removed == 1
        assert not orphan.exists()

    def test_keeps_a_fresh_tree_when_the_platform_cannot_pin_a_tree_walk(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The non-pinnable branch still honours the idle grace: a fresh tree is kept.
        monkeypatch.setattr(staging.pinned_fs, "supports_pinned_tree_walk", lambda: False)
        root = staging.staging_dir()
        fresh = root / "kc-build-live"
        fresh.mkdir()
        (fresh / "archive.tar.gz").write_bytes(b"in flight")

        assert staging.sweep_staging() == 0
        assert fresh.is_dir()

        # sweep produces or claims to reclaim.
        root = staging.staging_dir()
        stray = root / "kc_import_deadbeef.zip"
        stray.write_bytes(b"not a shape this leaf produces")
        _age(stray, staging.IDLE_GRACE_SECONDS + 60)

        assert staging.sweep_staging() == 0
        assert stray.is_file()

    def test_keeps_a_tree_a_live_transfer_is_still_writing(self, home: Path) -> None:
        root = staging.staging_dir()
        active = root / "kc-build-live"
        active.mkdir()
        # Aged the DIRECTORY into the past, but a file inside is fresh: a transfer
        # writing through an open descriptor never touches the top dir's mtime, so
        # tree-newest is the signal that keeps it.
        _age(active, staging.IDLE_GRACE_SECONDS + 60)
        (active / "chunk.part").write_bytes(b"still arriving")

        removed = staging.sweep_staging()

        assert removed == 0
        assert active.is_dir()

    def test_keeps_a_fresh_tree(self, home: Path) -> None:
        root = staging.staging_dir()
        fresh = root / "kc-import-fresh"
        fresh.mkdir()

        assert staging.sweep_staging() == 0
        assert fresh.is_dir()

    @_POSIX_ONLY
    def test_never_deletes_a_symlink_entry(self, home: Path, tmp_path: Path) -> None:
        root = staging.staging_dir()
        real = tmp_path / "precious"
        real.mkdir()
        (real / "keep.txt").write_text("not staging's", encoding="utf-8")
        link = root / "a-link"
        link.symlink_to(real, target_is_directory=True)
        _age(link, staging.IDLE_GRACE_SECONDS + 60)

        assert staging.sweep_staging() == 0
        assert link.is_symlink()
        assert (real / "keep.txt").exists()

    def test_a_missing_root_is_a_no_op(self, home: Path) -> None:
        # No staging_dir() call, so the root never existed.
        assert staging.sweep_staging() == 0

    @_POSIX_ONLY
    def test_a_linked_root_skips_the_whole_sweep(self, home: Path, tmp_path: Path) -> None:
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        victim = outside / "victim"
        victim.mkdir()
        _age(victim, staging.IDLE_GRACE_SECONDS + 60)
        (home / staging.STAGING_DIRNAME).symlink_to(outside, target_is_directory=True)

        assert staging.sweep_staging() == 0
        assert victim.is_dir()  # nothing under the linked target was touched

    def test_the_grace_is_per_sweep_not_per_entry(self, home: Path) -> None:
        root = staging.staging_dir()
        entry = root / "kc-build-x"
        entry.mkdir()
        _age(entry, 120)

        # Under a tiny grace the same entry is now past its window.
        assert staging.sweep_staging(grace=60) == 1
        assert not entry.exists()

    @_POSIX_ONLY
    def test_a_root_swapped_for_a_link_mid_sweep_never_reaches_a_governance_file(
        self, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sweep removal cannot be redirected by a root swap: the target is composed
        from the data home (unswappable by the agent) plus the LITERAL ``staging/<name>``,
        and ``remove_tree_pinned`` pins the parent chain with ``O_NOFOLLOW``, so swapping
        the agent-writable staging ROOT for a symlink to the data home mid-sweep is
        refused by that open rather than followed onto a governance tree.

        The swap is injected where the real race lands -- between the sweep's idle read
        and its removal -- by hijacking ``_tree_newest_mtime`` (the idle probe called
        just before ``remove_tree_pinned``) to rename the real root away and plant a
        symlink to the governance directory at its path. A removal that rebuilt the
        target from the swapped root's PATH (``root.resolve()/profiles``) would follow
        that link and delete the governance ``profiles``; a removal whose parent walk
        hits ``staging`` with ``O_NOFOLLOW`` refuses the swap, so the governance tree
        survives. Its tree is left stale so nothing but the ``O_NOFOLLOW`` refusal can
        be what spares it.
        """
        governance = tmp_path / "realconfig" / "security_policy.json"
        governance.parent.mkdir(parents=True)
        governance.write_text('{"ceiling": true}', encoding="utf-8")
        # A stale governance child whose NAME matches the staged child below, so a
        # removal that re-resolved the swapped root (``root.resolve()/profiles``) would
        # land on THIS directory and delete it (and could then take the dir with it).
        gov_profiles = governance.parent / "profiles"
        gov_profiles.mkdir()
        (gov_profiles / "owner.json").write_text('{"role": "owner"}', encoding="utf-8")
        _stale = time.time() - (staging.IDLE_GRACE_SECONDS + 300)
        for p in (governance, gov_profiles / "owner.json", gov_profiles, governance.parent):
            os.utime(p, (_stale, _stale))

        root = staging.staging_dir()
        child = root / "profiles"  # same name as the governance child above
        child.mkdir()
        (child / "payload").write_text("staged", encoding="utf-8")
        _age(child, staging.IDLE_GRACE_SECONDS + 60)

        real_mtime = staging._tree_newest_mtime
        swapped = {"done": False}

        def _swap_root_then_probe(path, *, fallback):  # type: ignore[no-untyped-def]
            value = real_mtime(path, fallback=fallback)
            if not swapped["done"]:
                swapped["done"] = True
                # The attacker wins the race: the real staging root is renamed away and
                # its path is repointed at the governance directory, in the window
                # between this idle read and the removal.
                moved = tmp_path / "moved_staging"
                os.rename(root, moved)
                os.symlink(governance.parent, root, target_is_directory=True)
            return value

        monkeypatch.setattr(staging, "_tree_newest_mtime", _swap_root_then_probe)

        staging.sweep_staging()

        # The governance tree is untouched: remove_tree_pinned's parent walk hit the
        # swapped ``staging`` with O_NOFOLLOW and refused it, never reaching the link's
        # target.
        assert governance.exists()
        assert governance.read_text(encoding="utf-8") == '{"ceiling": true}'
        assert gov_profiles.is_dir()
        assert (gov_profiles / "owner.json").exists()

    @_POSIX_ONLY
    def test_removes_a_nested_tree_idle_past_the_grace(self, home: Path) -> None:
        # A multi-level subtree, so the pinned idle re-check walks INTO subdirectories
        # (opening a descriptor per level) rather than only scanning the root -- the
        # branch a flat tree never exercises.
        root = staging.staging_dir()
        orphan = root / "kc-restore-nested"
        (orphan / "a" / "b").mkdir(parents=True)
        (orphan / "a" / "f1").write_bytes(b"x")
        (orphan / "a" / "b" / "f2").write_bytes(b"y")
        _age(orphan, staging.IDLE_GRACE_SECONDS + 60)

        assert staging.sweep_staging() == 1
        assert not orphan.exists()

    def test_keeps_a_tree_when_the_by_name_remove_fails(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Non-pinnable platform AND the by-name rmtree_force cannot fully remove the
        # tree: the sweep keeps it and retries rather than claiming a removal.
        monkeypatch.setattr(staging.pinned_fs, "supports_pinned_tree_walk", lambda: False)
        monkeypatch.setattr(staging.platform_compat, "rmtree_force", lambda _p: False)
        root = staging.staging_dir()
        orphan = root / "kc-build-stuck"
        orphan.mkdir()
        _age(orphan, staging.IDLE_GRACE_SECONDS + 60)

        assert staging.sweep_staging() == 0
        assert orphan.is_dir()

    @_POSIX_ONLY
    def test_keeps_a_tree_when_the_pinned_removal_is_refused(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # remove_tree_pinned raising PinnedPathRefusal (the ancestor-swap guard, or an
        # unopenable target) keeps the tree and retries -- never a by-name fallback that
        # could follow the swap.
        def _refuse(*_a, **_k):  # type: ignore[no-untyped-def]
            raise staging.pinned_fs.PinnedPathRefusal("refused for the test")

        monkeypatch.setattr(staging.pinned_fs, "remove_tree_pinned", _refuse)
        root = staging.staging_dir()
        orphan = root / "kc-build-refused"
        orphan.mkdir()
        _age(orphan, staging.IDLE_GRACE_SECONDS + 60)

        assert staging.sweep_staging() == 0
        assert orphan.is_dir()

    @_POSIX_ONLY
    def test_keeps_a_tree_that_goes_active_between_the_probe_and_the_pinned_recheck(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The first idle probe reads the tree as stale, but by the time the pinned
        # ``approve`` re-check runs a fresh write has landed: the removal commits only
        # while still idle, so the tree is kept.
        root = staging.staging_dir()
        orphan = root / "kc-build-reactivated"
        orphan.mkdir()
        (orphan / "f").write_bytes(b"x")
        _age(orphan, staging.IDLE_GRACE_SECONDS + 60)

        real_mtime = staging._tree_newest_mtime
        touched = {"done": False}

        def _probe_then_touch(path, *, fallback):  # type: ignore[no-untyped-def]
            value = real_mtime(path, fallback=fallback)
            if not touched["done"]:
                touched["done"] = True
                # A fresh write lands after the first (stale) probe but before the
                # pinned re-check, which then reads the tree as active again.
                (orphan / "late.part").write_bytes(b"just arrived")
            return value

        monkeypatch.setattr(staging, "_tree_newest_mtime", _probe_then_touch)

        assert staging.sweep_staging() == 0
        assert orphan.is_dir()


class TestTheLargeSitesStageUnderTheDataHome:
    """The regression lock: the three large-staging callers pass ``dir=`` resolving
    under ``<data home>/staging``, never the system temp dir.

    Driven through the real command entrypoint rather than asserted on source, and
    through a spy on the module's own ``tempfile`` so a revert to a bare
    ``TemporaryDirectory()`` (``dir=None``) fails here. ``writing-tests``: no sleep,
    no network, fully under ``tmp_path``.
    """

    def test_snapshot_build_stages_under_the_staging_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from test_snapshot import _setup_fake_kirocrew

        from kiro_crew import snapshot as snap
        from kiro_crew import snapshot_build

        data_home = tmp_path / "home"
        data_home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(data_home))
        _setup_fake_kirocrew(data_home)
        expected_root = (data_home / staging.STAGING_DIRNAME).resolve()

        seen_dirs: list[str | None] = []
        real_tempdir = snapshot_build.tempfile.TemporaryDirectory

        def _spy(*args: object, **kwargs: object) -> object:
            seen_dirs.append(kwargs.get("dir"))
            return real_tempdir(*args, **kwargs)

        monkeypatch.setattr(snapshot_build.tempfile, "TemporaryDirectory", _spy)

        rc = snap.snapshot_main([str(tmp_path / "out"), "--allow-unpinned-staging"])

        assert rc == 0
        assert seen_dirs, "the snapshot build never opened a staging TemporaryDirectory"
        for passed in seen_dirs:
            assert passed is not None, "snapshot build staged in the system temp dir (dir=None)"
            assert Path(passed).resolve() == expected_root

    def test_import_extraction_stages_under_the_staging_root(
        self, patched_config_dir, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The import EXTRACTION stages under ``<data home>/staging``.

        The large copy in the import path is the extracted tree, and that is what
        stages off the system temp dir. The uploaded ``.zip`` spool does
        NOT (``_read_upload_file`` keeps it in ``tempfile.gettempdir()``), so this
        lock is on ``portability.apply_import_zip``'s own ``TemporaryDirectory``, not
        on the upload. Driven through a real export->import round trip with a spy on
        the module's ``tempfile`` so a revert to a bare ``TemporaryDirectory()`` fails.
        """
        from unittest.mock import patch

        from kiro_crew import portability

        zip_bytes, _ = portability.create_export_zip()

        target = tmp_path / "instance_b"
        target.mkdir()
        zip_path = tmp_path / "export.zip"
        zip_path.write_bytes(zip_bytes)
        expected_root = (target / staging.STAGING_DIRNAME).resolve()

        seen_dirs: list[str | None] = []
        real_tempdir = portability.tempfile.TemporaryDirectory

        def _spy(*args: object, **kwargs: object) -> object:
            seen_dirs.append(kwargs.get("dir"))
            return real_tempdir(*args, **kwargs)

        monkeypatch.setattr(portability.tempfile, "TemporaryDirectory", _spy)

        with patch("kiro_crew.portability.config_dir", return_value=target):
            with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                portability.apply_import_zip(zip_path, mode="replace")

        assert seen_dirs, "the import never opened an extraction TemporaryDirectory"
        for passed in seen_dirs:
            assert passed is not None, "import extraction staged in the system temp dir (dir=None)"
            assert Path(passed).resolve() == expected_root

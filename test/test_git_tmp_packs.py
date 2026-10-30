"""The orphaned temp-pack reaper (``kiro_crew.git_tmp_packs``).

A fetch killed by a subprocess timeout leaves its in-progress
``tmp_pack_*`` behind, and at the status refresher's pace that can fill a
disk. The contract under test here is the safety
side of the janitor: it reclaims what is provably dead (mtime past the
threshold), never touches what could be in flight, never raises, and resolves
the object store across the three real checkout layouts (plain, bare, linked
worktree).
"""

from __future__ import annotations

import asyncio
import os
import time
import types
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import git_tmp_packs
from kiro_crew.git_tmp_packs import TMP_PACK_MAX_AGE_S, reap_stale_tmp_packs

_OLD = 10 * TMP_PACK_MAX_AGE_S  # comfortably past any threshold under test


def _plant(pack_dir: Path, name: str, *, age_s: float = 0.0, size: int = 8) -> Path:
    pack_dir.mkdir(parents=True, exist_ok=True)
    target = pack_dir / name
    target.write_bytes(b"\0" * size)
    os.utime(target, (time.time() - age_s, time.time() - age_s))
    return target


def _plain_clone(tmp_path: Path) -> tuple[Path, Path]:
    clone = tmp_path / "clone"
    pack_dir = clone / ".git" / "objects" / "pack"
    pack_dir.mkdir(parents=True)
    return clone, pack_dir


class _FakeEntry:
    """A stand-in for ``os.DirEntry`` so failure injection cannot hit the
    immutable built-in type: the reaper must treat a stat that raises the same
    way it treats a file that vanished mid-scan."""

    def __init__(self, path: Path) -> None:
        self.path = str(path)
        self.name = path.name

    def is_file(self, *, follow_symlinks: bool = False) -> bool:
        return True

    def stat(self, *, follow_symlinks: bool = False) -> os.stat_result:
        if self.name == "tmp_pack_bad":
            raise FileNotFoundError(self.path)
        return os.stat(self.path)


class TestReapStaleTmpPacks:
    def test_reaps_only_stale_tmp_packs(self, tmp_path: Path) -> None:
        clone, pack_dir = _plain_clone(tmp_path)
        old = _plant(pack_dir, "tmp_pack_abc123", age_s=_OLD)
        fresh = _plant(pack_dir, "tmp_pack_def456")
        keeper = _plant(pack_dir, "pack-0123abcd.pack")
        reaped = reap_stale_tmp_packs(clone)
        assert reaped == [str(old)]
        assert not old.exists()
        assert fresh.exists(), "a possibly in-flight pack must survive"
        assert keeper.exists(), "finished packs are not the janitor's business"

    def test_custom_threshold_shrinks_the_backlog_window(self, tmp_path: Path) -> None:
        clone, pack_dir = _plain_clone(tmp_path)
        _plant(pack_dir, "tmp_pack_abc123", age_s=120.0)
        assert reap_stale_tmp_packs(clone) == []
        assert reap_stale_tmp_packs(clone, max_age_s=60.0) != []

    def test_directory_named_like_a_tmp_pack_is_skipped(self, tmp_path: Path) -> None:
        clone, pack_dir = _plain_clone(tmp_path)
        decoy = pack_dir / "tmp_pack_dir"
        decoy.mkdir()
        os.utime(decoy, (time.time() - _OLD, time.time() - _OLD))
        assert reap_stale_tmp_packs(clone) == []
        assert decoy.is_dir()

    def test_missing_clone_and_empty_dir_never_raise(self, tmp_path: Path) -> None:
        assert reap_stale_tmp_packs(tmp_path / "no-such-clone") == []
        assert reap_stale_tmp_packs(tmp_path) == []

    def test_bare_layout_is_covered(self, tmp_path: Path) -> None:
        bare = tmp_path / "repo.git"
        pack_dir = bare / "objects" / "pack"
        old = _plant(pack_dir, "tmp_pack_bare", age_s=_OLD)
        (bare / "refs").mkdir()
        (bare / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (bare / "config").write_text("[core]\n\tbare = true\n", encoding="utf-8")
        assert reap_stale_tmp_packs(bare) == [str(old)]

    def test_folder_without_bare_metadata_is_not_swept(self, tmp_path: Path) -> None:
        # No ``.git`` and no HEAD/config/refs: this is just a folder of files.
        folder = tmp_path / "data"
        kept = _plant(folder / "objects" / "pack", "tmp_pack_userfile", age_s=_OLD)
        assert reap_stale_tmp_packs(folder) == []
        assert kept.exists()

    def test_checkout_project_objects_pack_is_never_swept(self, tmp_path: Path) -> None:
        # A checkout whose tree has its own top-level ``objects/pack``: only
        # ``.git/objects/pack`` is git's, the tree's files are project data.
        clone, pack_dir = _plain_clone(tmp_path)
        git_old = _plant(pack_dir, "tmp_pack_git", age_s=_OLD)
        project = _plant(clone / "objects" / "pack", "tmp_pack_project", age_s=_OLD)
        (clone / "refs").mkdir()
        (clone / "HEAD").write_text("x\n", encoding="utf-8")
        (clone / "config").write_text("x\n", encoding="utf-8")
        assert reap_stale_tmp_packs(clone) == [str(git_old)]
        assert project.exists(), "a working-tree file must never be reaped"

    def test_gitfile_worktree_reaps_from_the_common_object_store(self, tmp_path: Path) -> None:
        # A linked worktree: `.git` is a FILE naming a per-worktree gitdir,
        # whose `commondir` points back at the shared object store -- where
        # its fetches write their temp packs.
        main_pack = tmp_path / "main" / ".git" / "objects" / "pack"
        wt_gitdir = tmp_path / "main" / ".git" / "worktrees" / "wt"
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {wt_gitdir}\n", encoding="utf-8")
        wt_gitdir.mkdir(parents=True)
        (wt_gitdir / "commondir").write_text("../..\n", encoding="utf-8")
        old = _plant(main_pack, "tmp_pack_from_worktree", age_s=_OLD)
        assert reap_stale_tmp_packs(wt) == [str(old)]
        assert not old.exists()

    def test_relative_gitfile_target_resolves_against_the_clone(self, tmp_path: Path) -> None:
        clone = tmp_path / "clone"
        gitdir = clone / "real-gitdir"
        pack_dir = gitdir / "objects" / "pack"
        clone.mkdir(parents=True)
        (clone / ".git").write_text("gitdir: ./real-gitdir\n", encoding="utf-8")
        old = _plant(pack_dir, "tmp_pack_rel", age_s=_OLD)
        assert reap_stale_tmp_packs(clone) == [str(old)]

    def test_entry_whose_stat_raises_is_skipped_not_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clone, pack_dir = _plain_clone(tmp_path)
        good = _plant(pack_dir, "tmp_pack_good", age_s=_OLD)
        bad = _plant(pack_dir, "tmp_pack_bad", age_s=_OLD)
        fake_entries: list[Any] = [_FakeEntry(bad), _FakeEntry(good)]
        # Patch the module's view of os, not the global module: the suite's
        # tmp-dir cleanup rmtree's THROUGH os.scandir, so a leaked global patch
        # would break teardown itself.
        stub_os = types.SimpleNamespace(
            scandir=lambda target: iter(fake_entries),
            unlink=os.unlink,
            path=os.path,
        )
        monkeypatch.setattr(git_tmp_packs, "os", stub_os)
        assert reap_stale_tmp_packs(clone) == [str(good)]
        assert good.exists() is False
        assert bad.exists(), "the poisoned entry must not take the sweep down"

    def test_scandir_failure_is_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clone, _pack_dir = _plain_clone(tmp_path)

        def boom(target: object) -> Any:
            raise OSError("permission denied")

        stub_os = types.SimpleNamespace(scandir=boom, unlink=os.unlink, path=os.path)
        monkeypatch.setattr(git_tmp_packs, "os", stub_os)
        assert reap_stale_tmp_packs(clone) == []

    def test_injection_now_pins_the_clock(self, tmp_path: Path) -> None:
        clone, pack_dir = _plain_clone(tmp_path)
        target = _plant(pack_dir, "tmp_pack_frozen")
        # A file planted "now" is stale from the vantage of a far-future clock,
        # and a zero threshold reaps everything -- both via the same entry
        # point, so tests never sleep to make a file old.
        assert reap_stale_tmp_packs(clone, max_age_s=0.0, now=time.time() + 1.0) == [str(target)]


@pytest.mark.asyncio
class TestStatusRefresherCallSite:
    """The one fetch that runs into the operator's checkout every minute.

    Orphans pile up fastest here, so the refresher cycle itself -- not just
    the janitor -- must be what retires a stale pack, and a failed fetch must
    be logged.
    """

    @pytest.fixture
    def refresher_cycle(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from kiro_crew.apps.builtins.dev_fleet import (
            fleet_state,
            repository,
            runtime,
            worktree_ops,
        )

        clone = tmp_path / "operator-checkout"
        pack_dir = clone / ".git" / "objects" / "pack"
        old = _plant(pack_dir, "tmp_pack_stale", age_s=_OLD)

        async def fake_upstream() -> str:
            return "origin"

        async def fake_run_cmd(*args: Any, **kwargs: Any) -> tuple[int, str, str]:
            return (-1, "", "timeout (90s)")

        refreshed: list[bool] = []
        self._cycle_done = asyncio.Event()

        async def fake_refresh() -> None:
            # The refresh is the cycle's last step, after the reap.
            refreshed.append(True)
            self._cycle_done.set()

        monkeypatch.setattr(repository, "_repo", lambda: str(clone))
        monkeypatch.setattr(repository, "_upstream_remote", fake_upstream)
        monkeypatch.setattr(runtime, "_run_cmd", fake_run_cmd)
        monkeypatch.setattr(fleet_state, "_fleet_refresh", fake_refresh)
        return worktree_ops, old, pack_dir, refreshed, runtime

    async def _run_one_cycle(self, worktree_ops: Any) -> None:
        task = asyncio.ensure_future(worktree_ops._status_refresher())
        try:
            await asyncio.wait_for(self._cycle_done.wait(), timeout=30)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_cycle_reaps_stale_tmp_pack(
        self, refresher_cycle, caplog: pytest.LogCaptureFixture
    ) -> None:
        worktree_ops, old, _pack_dir, refreshed, runtime = refresher_cycle
        with caplog.at_level("WARNING", logger=runtime.logger.name):
            await self._run_one_cycle(worktree_ops)
        assert not old.exists(), "the refresher cycle must retire the stale pack"
        assert refreshed, "the rest of the cycle is unchanged"
        assert any(
            "background fetch" in r.message for r in caplog.records
        ), "a failed fetch must be visible now"

    async def test_failed_fetch_log_redacts_before_cutting(
        self, refresher_cycle, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree_ops, _old, _pack_dir, _refreshed, runtime = refresher_cycle
        tail = "SECRETPW@example.com/repo.git" + " " * 3 + "x" * 168
        assert len(tail) == 200  # the cut lands right after ``https://user:``
        stderr = "fatal: " + "y" * 50 + " https://user:" + tail

        async def fake_run_cmd(*args: Any, **kwargs: Any) -> tuple[int, str, str]:
            return (128, "", stderr)

        monkeypatch.setattr(runtime, "_run_cmd", fake_run_cmd)
        with caplog.at_level("WARNING", logger=runtime.logger.name):
            await self._run_one_cycle(worktree_ops)
        logged = [r.getMessage() for r in caplog.records if "background fetch" in r.message]
        assert logged, "the failed fetch must be logged"
        assert all("SECRETPW" not in m for m in logged), "no credential fragment in the log"

    async def test_cycle_spares_fresh_tmp_pack(self, refresher_cycle) -> None:
        worktree_ops, _old, pack_dir, _refreshed, _runtime = refresher_cycle
        fresh = _plant(pack_dir, "tmp_pack_inflight")
        await self._run_one_cycle(worktree_ops)
        assert fresh.exists(), "the mtime threshold protects an in-flight pack"


def test_threshold_is_twice_the_largest_bounded_calling_fetch() -> None:
    # The safety argument for reaping is "no fetch of ours can still own this
    # file", and it only holds for the BOUNDED callers: clone_setup's ceiling
    # is 300s, dev-fleet's fetch runs at 90s, so the
    # threshold must stay above that largest bound. (spine/driver's fetch
    # carries no timeout parameter; there the threshold is documented as
    # vouching for a 10-minute-inert transfer, a weaker claim.) Pin the ratio
    # so a future bigger bound forces a conscious revisit rather than a
    # silent race.
    assert TMP_PACK_MAX_AGE_S >= 2.0 * 300.0


class TestFetchTimeoutCallSites:
    """A fetch killed by its timeout raises ``TimeoutExpired``; the reap must still run.

    The timeout is the case that leaves an orphan, so a reap that only runs when
    the fetch returns would miss every orphan these sites produce.
    """

    @staticmethod
    def _timeout(args: Any, **kwargs: Any) -> Any:
        import subprocess

        if "fetch" in args:
            raise subprocess.TimeoutExpired(args, kwargs.get("timeout") or 0)
        return subprocess.CompletedProcess(args, 0, "", "")

    def test_clone_setup_checkout_branch_reaps_on_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup

        clone, pack_dir = _plain_clone(tmp_path)
        old = _plant(pack_dir, "tmp_pack_killed", age_s=_OLD)
        monkeypatch.setattr(clone_setup, "require_pinned", lambda *_a, **_k: None)
        monkeypatch.setattr(clone_setup, "hook_off_args", lambda *_a, **_k: [])
        monkeypatch.setattr(clone_setup, "_repository_is_safe", lambda _c: True)
        monkeypatch.setattr(clone_setup, "_push_disabled", lambda _c: True)
        monkeypatch.setattr(clone_setup.subprocess, "run", self._timeout)
        with pytest.raises(subprocess.TimeoutExpired):
            clone_setup.checkout_branch(clone, "feature/x")
        assert not old.exists(), "a timed-out fetch must not skip the reap"

    def test_commit_materialize_reaps_on_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        from kiro_crew.apps.builtins.auto_improvement.backend import commit

        clone, pack_dir = _plain_clone(tmp_path)
        old = _plant(pack_dir, "tmp_pack_killed", age_s=_OLD)
        monkeypatch.setattr(commit, "resolve_origin_url", lambda _c: "https://example.invalid/r")
        monkeypatch.setattr(commit, "_prefer_authenticated_remote", lambda u: u)
        monkeypatch.setattr(commit, "_git", lambda _clone, *args, **kw: self._timeout(args, **kw))
        with pytest.raises(subprocess.TimeoutExpired):
            commit.materialize_queued_diff(
                clone=clone, branch="main", config={}, diff_text="diff --git a/x b/x\n"
            )
        assert not old.exists(), "a timed-out fetch must not skip the reap"

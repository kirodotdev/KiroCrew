"""Regression tests for orphaned sandbox bind-mount sources (tmpfs exhaustion).

The namespace launcher stages bind-mount SOURCES (empty dirs/files over
credential paths, plus the strict-mode SSH shadow dir) on a tmpfs root
(``/run/user/$UID`` → ``/dev/shm`` → system temp). The kernel pins each source
for the mount's lifetime, so the launcher cannot unlink them and they orphan
when the sandboxed process exits — filling the runtime tmpfs until
``systemd-run --scope`` cannot allocate transient units and every agent spawn
fails.

Two halves lock the fix in:
  (a) the generated launcher tags every staging site with a pid-bearing
      ``_src_prefix`` (``kirocrew_sb_<pid>_``) assigned in the post-fork CHILD
      branch — asserted by AST so an unprefixed ``mkdtemp(dir=_tmpfs_src)`` /
      ``mkstemp(dir=_tmpfs_src)`` cannot reappear and the assignment cannot
      drift above the fork, at every sandbox level, with the script staying
      parseable;
  (b) ``_cleanup_stale_sandbox_mount_sources`` reclaims layered by cost: plain
      files and empty dirs on dead pid or over-age; dirs (whose contents are
      visible inside a live mount namespace, and whose removal S_DEADs it)
      only once absence-of-pin is POSITIVELY established — by the mountinfo
      pin scan proving host-wide coverage, or by it having read every task
      that could hold a source this uid staged (this uid's, the overflow
      uid's, root's, and their sibling threads: ``_PinScanCoverage``).
      Another user's unreadable or departing task lowers only the host-wide
      flag, and requiring that flag alone retained the directory class until
      the runtime tmpfs ran out of inodes. A readable pin always wins, a
      recycled pid is reclaimed rather than stranded. Foreign
      ``tmp*`` names, probe names, planted hostile pid segments, and
      ``kirocrew_sandbox_*`` launcher scripts are preserved, without the sweep
      ever raising.
"""

from __future__ import annotations

import ast
import builtins
import errno
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kiro_crew.sandbox import (
    _MOUNT_SOURCE_MAX_AGE_SECONDS,
    _build_launcher_script,
    _cleanup_legacy_mount_source_residue,
    _cleanup_stale_sandbox_mount_sources,
    _mount_pinned_source_names,
    _PinScanCoverage,
    cleanup_stale_sandbox_profiles,
)

# ``_build_launcher_script`` calls POSIX-only ``os.getuid``/``os.getgid`` (the
# namespace launcher never runs on Windows).
requires_posix = pytest.mark.skipif(
    not hasattr(os, "getuid"),
    reason="_build_launcher_script uses POSIX-only os.getuid (#2041)",
)
# The legacy-residue pass fences on the exact POSIX mode ``mkdtemp``/``mkstemp``
# create (0o700 / 0o600) and on ``st_uid``; neither is meaningful on Windows,
# where ``_launcher_tmpfs_roots`` yields no root for it to walk anyway.
requires_posix_modes = pytest.mark.skipif(
    not hasattr(os, "getuid"),
    reason="legacy-residue fences compare POSIX mode bits and st_uid",
)

_DEAD_PID = 4_194_305  # above Linux PID_MAX_LIMIT (2**22) — never a live process
_LIVE_PID = os.getpid()


def _make_dir(root: Path, name: str, *, populate: bool = False, old: bool = False) -> Path:
    path = root / name
    path.mkdir()
    if populate:
        (path / "known_hosts").write_text("example.com ssh-ed25519 AAAA\n")
    if old:
        stale = time.time() - _MOUNT_SOURCE_MAX_AGE_SECONDS - 100
        os.utime(path, (stale, stale))
    return path


def _make_file(root: Path, name: str, *, old: bool = False) -> Path:
    path = root / name
    path.write_text("")
    if old:
        stale = time.time() - _MOUNT_SOURCE_MAX_AGE_SECONDS - 100
        os.utime(path, (stale, stale))
    return path


def _pin(
    monkeypatch: pytest.MonkeyPatch,
    names: set[str],
    *,
    complete: bool = True,
    covered: bool | None = None,
) -> None:
    """Fix the pin scan's answer for one test (signature-matched to the real one).

    ``covered`` fills the caller's ``coverage`` the way the real scan does. By
    default it tracks ``complete``: an INCOMPLETE scan is also UNCOVERED -- a
    task of this uid departed on the final pass, the churn shape -- so a test
    that means "incomplete only because of root / other-user / hidepid gaps"
    passes ``covered=True`` explicitly.
    """
    if covered is None:
        covered = complete

    def _fake(proc_root: str = "/proc", *, coverage=None, **_kw):
        if coverage is not None and not covered:
            coverage.covered = False
        return (names, complete)

    monkeypatch.setattr("kiro_crew.sandbox._mount_pinned_source_names", _fake)


def _raise_einval_for(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    """Fail ``open()`` on ONE path the way procfs fails for a namespaceless task.

    ``/proc/<pid>/mountinfo`` is synthesized per read from the task's
    ``nsproxy``; once that is gone (the task has exited and awaits reap) the
    open returns ``EINVAL``. No real file can be made to do that, and a real
    zombie's pid cannot be planted under ``tmp_path``, so the errno is injected
    for the one target path and every other open is delegated untouched.
    """
    real_open = builtins.open
    target_str = str(target)

    def fake_open(file, *args, **kwargs):  # noqa: ANN001, ANN202
        if str(file) == target_str:
            raise OSError(errno.EINVAL, os.strerror(errno.EINVAL), target_str)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)


class TestMountSourceSweep:
    """_cleanup_stale_sandbox_mount_sources() reclaim/preserve matrix."""

    def test_reclaims_dead_pid_dir_including_non_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A dead-pid dir is removed even when non-empty (the SSH-shadow case)."""
        _pin(monkeypatch, set())
        empty = _make_dir(tmp_path, f"kirocrew_sb_{_DEAD_PID}_abc123")
        shadow = _make_dir(tmp_path, f"kirocrew_sb_{_DEAD_PID}_ssh456", populate=True)

        removed = _cleanup_stale_sandbox_mount_sources(roots=[str(tmp_path)])

        assert removed == 2
        assert not empty.exists()
        assert not shadow.exists()

    def test_reclaims_dead_pid_file(self, tmp_path: Path):
        stale = _make_file(tmp_path, f"kirocrew_sb_{_DEAD_PID}_file01")

        removed = _cleanup_stale_sandbox_mount_sources(roots=[str(tmp_path)])

        assert removed == 1
        assert not stale.exists()

    def test_age_backstop_reclaims_empty_dir_and_file_despite_live_pid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A recycled pid must not strand files or empty dirs.

        The file is removed unconditionally (its inode is held by any live
        mount like an open descriptor); the dir's removal is additionally
        gated on the pin scan, which here proves no namespace binds it.
        """
        _pin(monkeypatch, set())
        recycled_dir = _make_dir(tmp_path, f"kirocrew_sb_{_LIVE_PID}_old999", old=True)
        recycled_file = _make_file(tmp_path, f"kirocrew_sb_{_LIVE_PID}_old888", old=True)

        removed = _cleanup_stale_sandbox_mount_sources(roots=[str(tmp_path)])

        assert removed == 2
        assert not recycled_dir.exists()
        assert not recycled_file.exists()

    def test_mount_pinned_entry_is_preserved_however_old_and_whatever_the_pid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The pin scan is the deciding evidence for every dir removal.

        A >24h sandbox still running holds its own namespace, so its entries
        are pinned and must survive the sweep intact — including the EMPTY
        mask dirs: removing a live mount's source dir marks the mount's root
        inode S_DEAD, after which every create under the masked path fails.
        The over-age mtime and even a dead pid probe must not override the
        pin.
        """
        live_name = f"kirocrew_sb_{_LIVE_PID}_shadow1"
        dead_name = f"kirocrew_sb_{_DEAD_PID}_held01"
        empty_name = f"kirocrew_sb_{_DEAD_PID}_mask01"
        live_shadow = _make_dir(tmp_path, live_name, populate=True, old=True)
        dead_held = _make_dir(tmp_path, dead_name, populate=True)
        empty_mask = _make_dir(tmp_path, empty_name, old=True)
        _pin(monkeypatch, {live_name, dead_name, empty_name})

        removed = _cleanup_stale_sandbox_mount_sources(roots=[str(tmp_path)])

        assert removed == 0
        assert (live_shadow / "known_hosts").read_text() == "example.com ssh-ed25519 AAAA\n"
        assert (dead_held / "known_hosts").exists()
        assert empty_mask.exists()

    def test_recycled_pid_non_empty_dir_is_reclaimed_when_unpinned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A live pid probe alone must not strand a non-empty orphan.

        On a pid_max=32768 host the launcher pid is recycled quickly; the
        entry's owner is gone, no namespace references it, and stranding it
        would let the tmpfs-exhaustion failure recur through the non-empty
        class (which can hold a known-hosts or exposed-config copy).
        """
        _pin(monkeypatch, set())
        recycled = _make_dir(tmp_path, f"kirocrew_sb_{_LIVE_PID}_recyc1", populate=True, old=True)

        removed = _cleanup_stale_sandbox_mount_sources(roots=[str(tmp_path)])

        assert removed == 1
        assert not recycled.exists()

    def test_incomplete_and_uncovered_pin_scan_blocks_dir_removal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Absence-of-pin must be POSITIVELY established before any dir goes.

        With NEITHER line of evidence available — a partial /proc scan, and a
        task of this uid that departed on its final pass (so a holder may exist
        unread) — the sweep must fail closed for dirs, empty ones included,
        since rmdir of a live source S_DEADs the mount. Plain files are
        unaffected by the scan.
        """
        _pin(monkeypatch, set(), complete=False)
        held = _make_dir(tmp_path, f"kirocrew_sb_{_DEAD_PID}_part01", populate=True)
        empty = _make_dir(tmp_path, f"kirocrew_sb_{_DEAD_PID}_part03")
        plain = _make_file(tmp_path, f"kirocrew_sb_{_DEAD_PID}_part02")

        removed = _cleanup_stale_sandbox_mount_sources(roots=[str(tmp_path)])

        assert removed == 1
        assert (held / "known_hosts").exists()
        assert empty.exists()
        assert not plain.exists()

    def test_dirs_are_reclaimed_once_every_own_uid_task_was_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The regression this PR fixes: the pile must not wait on the
        host-wide flag.

        A scan left incomplete ONLY by what cannot hold a source this uid
        staged -- another user's unreadable or departing tasks -- has still
        read every task of this uid (and root's), so the pinned set is
        authoritative for a sandbox descendant: a stale unpinned dir goes, a
        pinned one stays.
        """
        _pin(monkeypatch, set(), complete=False, covered=True)
        stale = _make_dir(tmp_path, f"kirocrew_sb_{_DEAD_PID}_inherit1", populate=True, old=True)
        live_name = f"kirocrew_sb_{_DEAD_PID}_inherit2"
        live = _make_dir(tmp_path, live_name, populate=True, old=True)
        _pin(monkeypatch, {live_name}, complete=False, covered=True)

        assert _cleanup_stale_sandbox_mount_sources(roots=[str(tmp_path)]) == 1
        assert not stale.exists()
        assert (live / "known_hosts").exists()

    def test_preserves_fresh_live_pid_entry(self, tmp_path: Path):
        live = _make_dir(tmp_path, f"kirocrew_sb_{_LIVE_PID}_live01")

        removed = _cleanup_stale_sandbox_mount_sources(roots=[str(tmp_path)])

        assert removed == 0
        assert live.exists()

    def test_rmtree_raising_non_oserror_does_not_kill_the_sweep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A planted tree deep enough to exhaust recursion must not cost the
        periodic caller every later sweep — the per-entry failure is skipped
        and the remaining entries are still processed."""
        _pin(monkeypatch, set())

        def _boom(path, ignore_errors=False):  # noqa: ANN001
            raise RecursionError("maximum recursion depth exceeded")

        monkeypatch.setattr(shutil, "rmtree", _boom)
        _make_dir(tmp_path, f"kirocrew_sb_{_DEAD_PID}_deep01", populate=True)
        plain = _make_file(tmp_path, f"kirocrew_sb_{_DEAD_PID}_zfile1")

        removed = _cleanup_stale_sandbox_mount_sources(roots=[str(tmp_path)])

        assert removed == 1
        assert not plain.exists()

    def test_preserves_foreign_and_unparseable_names(self, tmp_path: Path):
        """Only the recognized pid-bearing shape is touched, however old.

        Foreign ``tmp*`` (the unprefixed backlog), ``kirocrew_sandbox_*``
        launcher scripts, the ``kirocrew_sbprobe_*`` tmpfs probe, and a
        non-numeric pid segment are all left alone.
        """
        keep = [
            _make_dir(tmp_path, "tmpa1b2c3d4", old=True),
            _make_file(tmp_path, "tmpz9y8x7w6", old=True),
            _make_file(tmp_path, f"kirocrew_sandbox_{_DEAD_PID}_x.py", old=True),
            _make_dir(tmp_path, "kirocrew_sbprobe_ab12cd", old=True),
            _make_dir(tmp_path, f"kirocrew_sb_pid{_DEAD_PID}_z", old=True),
        ]

        removed = _cleanup_stale_sandbox_mount_sources(roots=[str(tmp_path)])

        assert removed == 0
        assert all(p.exists() for p in keep)

    def test_planted_hostile_pid_segments_neither_crash_nor_stall_the_sweep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Some roots are world-writable; a planted name must fail toward skip.

        The launcher can only emit an ASCII positive pid, so everything else
        is rejected before any probe: ``"²"`` (isdigit-true, isdecimal-false),
        ``"٣"`` (isdecimal-true but non-ASCII, and int() would accept it),
        ``"0"`` (os.kill(0, 0) probes the caller's own process group, reading
        alive forever), and a zero-padded segment. None may raise out of the
        sweep — the periodic caller would silently lose every later sweep —
        and genuine work after them still happens. A pid too large for the
        probe reads stale (40 digits stays under Windows MAX_PATH; the exact
        refusal mechanism differs per platform).
        """
        _pin(monkeypatch, set())
        superscript = _make_dir(tmp_path, "kirocrew_sb_\u00b2_x", old=True)
        arabic_indic = _make_dir(tmp_path, "kirocrew_sb_\u0663_x", old=True)
        zero = _make_dir(tmp_path, "kirocrew_sb_0_x", populate=True, old=True)
        padded = _make_dir(tmp_path, f"kirocrew_sb_0{_DEAD_PID}_x", old=True)
        oversized = _make_dir(tmp_path, f"kirocrew_sb_{'9' * 40}_x", old=True)
        genuine = _make_dir(tmp_path, f"kirocrew_sb_{_DEAD_PID}_real1")

        removed = _cleanup_stale_sandbox_mount_sources(roots=[str(tmp_path)])

        assert removed == 2  # the oversized pid reads stale; the genuine orphan goes
        assert superscript.exists()
        assert arabic_indic.exists()
        assert zero.exists()
        assert padded.exists()
        assert not oversized.exists()
        assert not genuine.exists()

    def test_second_sweep_is_a_no_op(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _pin(monkeypatch, set())
        _make_dir(tmp_path, f"kirocrew_sb_{_DEAD_PID}_one", populate=True)
        _make_file(tmp_path, f"kirocrew_sb_{_DEAD_PID}_two")
        survivor = _make_dir(tmp_path, f"kirocrew_sb_{_LIVE_PID}_three")

        assert _cleanup_stale_sandbox_mount_sources(roots=[str(tmp_path)]) == 2
        assert _cleanup_stale_sandbox_mount_sources(roots=[str(tmp_path)]) == 0
        assert survivor.exists()

    def test_missing_root_is_skipped(self, tmp_path: Path):
        removed = _cleanup_stale_sandbox_mount_sources(roots=[str(tmp_path / "absent")])
        assert removed == 0

    def test_wired_into_periodic_profile_sweep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """cleanup_stale_sandbox_profiles() drives the mount-source sweep."""
        _pin(monkeypatch, set())
        root = tmp_path / "tmpfs"
        root.mkdir()
        _make_dir(root, f"kirocrew_sb_{_DEAD_PID}_wired", populate=True)
        monkeypatch.setattr("kiro_crew.sandbox._mount_source_candidate_roots", lambda: [str(root)])
        # Point the legacy /tmp sweep at an empty per-test dir so host residue
        # cannot inflate the count.
        legacy = tmp_path / "legacy"
        legacy.mkdir()

        removed = cleanup_stale_sandbox_profiles(legacy_dir=str(legacy))

        assert removed >= 1
        assert not (root / f"kirocrew_sb_{_DEAD_PID}_wired").exists()


class TestMountPinnedSourceNames:
    """The mountinfo pin parser — the sole guard for non-empty reclaims."""

    @staticmethod
    def _write_mountinfo(proc: Path, pid: str, lines: str) -> None:
        d = proc / pid
        d.mkdir(parents=True)
        (d / "mountinfo").write_text(lines)
        # pid 1 must be visible or the scan reports a filtered procfs.
        init = proc / "1"
        if not init.exists():
            init.mkdir()
            (init / "mountinfo").write_text("")

    def test_extracts_prefix_shaped_basenames_from_the_root_field(self, tmp_path: Path):
        proc = tmp_path / "proc"
        self._write_mountinfo(
            proc,
            "101",
            "36 35 0:22 /kirocrew_sb_123_ab /home/u/.aws rw,nosuid - tmpfs tmpfs rw\n"
            "37 35 0:22 /kirocrew_sb_123_cd /home/u/.ssh rw,nosuid - tmpfs tmpfs rw\n"
            "38 35 8:1 / / rw - ext4 /dev/sda1 rw\n",
        )

        pinned, complete = _mount_pinned_source_names(proc_root=str(proc))

        assert pinned == {"kirocrew_sb_123_ab", "kirocrew_sb_123_cd"}
        assert complete is True

    def test_prefix_in_the_mount_point_field_alone_pins_nothing(self, tmp_path: Path):
        """Field 4 (root), not field 5 (mount point), names the source."""
        proc = tmp_path / "proc"
        self._write_mountinfo(
            proc,
            "102",
            "36 35 0:22 /elsewhere /tmp/kirocrew_sb_123_ab rw,nosuid - tmpfs tmpfs rw\n",
        )

        pinned, complete = _mount_pinned_source_names(proc_root=str(proc))

        assert pinned == set()
        assert complete is True

    def test_vanished_process_is_not_a_coverage_gap(self, tmp_path: Path):
        """A listed pid without a readable mountinfo (exited mid-scan) forces
        one more listing pass; when nothing new appears, the scan is complete
        — a namespace whose last holder exited has no mounts left to
        protect."""
        proc = tmp_path / "proc"
        (proc / "103").mkdir(parents=True)  # pid dir, no mountinfo file
        self._write_mountinfo(
            proc,
            "104",
            "36 35 0:22 /kirocrew_sb_9_zz /home/u/.aws rw - tmpfs tmpfs rw\n",
        )

        pinned, complete = _mount_pinned_source_names(proc_root=str(proc))

        assert pinned == {"kirocrew_sb_9_zz"}
        assert complete is True

    def test_successor_appearing_after_a_vanish_is_scanned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A holder can fork a successor and exit between the pid listing and
        its own mountinfo read; the successor must still be seen, or its
        namespace's pins would be missed while the scan reports complete."""
        proc = tmp_path / "proc"
        (proc / "201").mkdir(parents=True)  # the vanisher: listed, no mountinfo
        self._write_mountinfo(
            proc,
            "202",
            "36 35 0:22 /kirocrew_sb_7_hh /home/u/.ssh rw - tmpfs tmpfs rw\n",
        )
        real_listdir = os.listdir
        calls = {"n": 0}

        def listing(path):  # noqa: ANN001
            if str(path) == str(proc):
                calls["n"] += 1
                if calls["n"] == 1:
                    return ["1", "201"]  # the successor is not yet visible
                return ["1", "201", "202"]
            return real_listdir(path)

        monkeypatch.setattr(os, "listdir", listing)

        pinned, complete = _mount_pinned_source_names(proc_root=str(proc))

        assert pinned == {"kirocrew_sb_7_hh"}
        assert complete is True

    def test_scan_still_churning_after_the_pass_budget_reports_incomplete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Unbounded churn must resolve to fail-closed, not an infinite loop."""
        proc = tmp_path / "proc"
        real_listdir = os.listdir
        calls = {"n": 0}

        def listing(path):  # noqa: ANN001
            if str(path) == str(proc):
                calls["n"] += 1
                # Every pass surfaces one NEW pid dir with no mountinfo, so
                # every pass observes a vanish and the scan never stabilizes.
                pids = ["1"] + [str(300 + i) for i in range(calls["n"])]
                for pid in pids[1:]:
                    (proc / pid).mkdir(parents=True, exist_ok=True)
                return pids
            return real_listdir(path)

        proc.mkdir()
        (proc / "1").mkdir()
        (proc / "1" / "mountinfo").write_text("")
        monkeypatch.setattr(os, "listdir", listing)

        pinned, complete = _mount_pinned_source_names(proc_root=str(proc))

        assert pinned == set()
        assert complete is False

    @pytest.mark.skipif(
        os.getuid() == 0 if hasattr(os, "getuid") else True,
        reason="root ignores file modes; the EACCES probe needs a non-root uid",
    )
    def test_unreadable_same_uid_mountinfo_marks_the_scan_incomplete(self, tmp_path: Path):
        proc = tmp_path / "proc"
        self._write_mountinfo(proc, "105", "irrelevant\n")
        os.chmod(proc / "105" / "mountinfo", 0)
        try:
            pinned, complete = _mount_pinned_source_names(proc_root=str(proc))
        finally:
            os.chmod(proc / "105" / "mountinfo", 0o644)

        assert pinned == set()
        assert complete is False

    def test_namespaceless_task_is_not_a_coverage_gap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """``EINVAL`` establishes absence-of-pin; it must not read as a gap.

        procfs answers ``EINVAL`` for one reason here — the task's ``nsproxy``
        is already gone, i.e. it has exited and awaits reap — so it belongs to
        NO mount namespace and can reference no source. That is the strongest
        evidence this scan can obtain, yet it was filed as "coverage
        unprovable", which holds back EVERY directory candidate. One unreaped
        child (routine on any host) therefore disabled directory reclamation
        permanently and host-wide, until the runtime tmpfs was out of inodes
        and ``systemd-run --scope`` could no longer start a spawn.
        """
        proc = tmp_path / "proc"
        self._write_mountinfo(
            proc,
            "301",
            "36 35 0:22 /kirocrew_sb_5_aa /home/u/.aws rw - tmpfs tmpfs rw\n",
        )
        zombie = proc / "302"
        zombie.mkdir()
        (zombie / "mountinfo").write_text("")  # present, but the READ raises
        _raise_einval_for(monkeypatch, zombie / "mountinfo")

        pinned, complete = _mount_pinned_source_names(proc_root=str(proc))

        assert pinned == {"kirocrew_sb_5_aa"}
        assert complete is True

    def test_namespaceless_task_still_forces_a_rescan_for_its_successor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Forgiving ``EINVAL`` must not also skip the re-listing.

        The task has EXITED, so it may have handed its namespace to a child
        forked after this pass's listing, and only another listing can see
        that successor. Forgiving without a rescan would report complete while
        the successor's pin went unrecorded — and the sweep would then rmdir a
        source a live namespace still binds, leaving that mount's root inode
        ``S_DEAD`` and every create under the masked path failing.
        """
        proc = tmp_path / "proc"
        proc.mkdir()
        (proc / "1").mkdir()
        (proc / "1" / "mountinfo").write_text("")
        gone = proc / "401"
        gone.mkdir()
        (gone / "mountinfo").write_text("")
        _raise_einval_for(monkeypatch, gone / "mountinfo")
        successor = proc / "402"
        successor.mkdir()
        (successor / "mountinfo").write_text(
            "36 35 0:22 /kirocrew_sb_11_bb /home/u/.ssh rw - tmpfs tmpfs rw\n"
        )
        real_listdir = os.listdir
        calls = {"n": 0}

        def listing(path):  # noqa: ANN001
            if str(path) == str(proc):
                calls["n"] += 1
                if calls["n"] == 1:
                    return ["1", "401"]  # the successor is not yet visible
                return ["1", "401", "402"]
            return real_listdir(path)

        monkeypatch.setattr(os, "listdir", listing)

        pinned, complete = _mount_pinned_source_names(proc_root=str(proc))

        assert calls["n"] >= 2, "a namespaceless task must trigger another listing pass"
        assert pinned == {"kirocrew_sb_11_bb"}
        assert complete is True

    @pytest.mark.skipif(
        not os.path.isdir("/proc/self") or not os.path.exists("/bin/true"),
        reason="needs a real Linux procfs and a trivial child to leave unreaped",
    )
    def test_kernel_answers_einval_for_a_real_zombie(self):
        """Pin the KERNEL premise the forgiveness rests on.

        The fix reads one errno as proof of "no mount namespace". If a future
        kernel answers something else for a task past ``exit_task_namespaces``,
        that proof silently becomes a coverage gap again and directory
        reclamation dies exactly as before, with no test failing. So assert the
        behaviour directly, on a zombie this test creates: an exited child that
        is deliberately not reaped until the assertions are done.
        """
        child = subprocess.Popen(["/bin/true"])  # noqa: S603 - fixed argv, no shell
        try:
            deadline = time.time() + 5.0
            state = ""
            while time.time() < deadline:
                try:
                    with open(f"/proc/{child.pid}/stat") as fh:
                        state = fh.read().rsplit(") ", 1)[1][0]
                except OSError:  # pragma: no cover - raced the reap
                    break
                if state == "Z":
                    break
                time.sleep(0.01)
            if state != "Z":  # pragma: no cover - scheduler-dependent
                pytest.skip("child did not reach the zombie state in time")

            with pytest.raises(OSError) as caught:
                with open(f"/proc/{child.pid}/mountinfo"):
                    pass
            assert caught.value.errno == errno.EINVAL
            assert not isinstance(caught.value, FileNotFoundError)
        finally:
            child.wait()

    def test_zombie_leader_with_a_live_thread_is_pinned_not_forgiven(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A live namespace can sit behind a zombie LEADER, and must still pin.

        A thread-group leader can exit through ``pthread_exit`` while sibling
        threads keep running. Threads share the ``nsproxy``, so the mount
        namespace -- and its binds on our staged sources -- stays alive, yet
        ``/proc/<tgid>/mountinfo`` answers EINVAL because the LEADER's nsproxy is
        gone. ``/proc`` lists only leaders, so the vanish re-listing can never see
        the surviving thread either. Forgiving on the leader's errno alone would
        report ``complete=True`` with that namespace's pin unrecorded, and the
        sweep would then rmdir a source it still binds, leaving the mount's root
        inode S_DEAD and every create under the masked credential path failing.

        Measured kernel behaviour, pinned by
        ``test_kernel_answers_einval_for_a_zombie_leader_with_a_live_thread``.
        """
        proc = tmp_path / "proc"
        self._write_mountinfo(proc, "500", "")
        leader = proc / "501"
        leader.mkdir()
        (leader / "mountinfo").write_text("")  # present, but the READ raises
        _raise_einval_for(monkeypatch, leader / "mountinfo")
        thread_dir = leader / "task" / "502"
        thread_dir.mkdir(parents=True)
        (thread_dir / "mountinfo").write_text(
            "36 35 0:22 /kirocrew_sb_21_tt /home/u/.aws rw - tmpfs tmpfs rw\n"
        )

        pinned, complete = _mount_pinned_source_names(proc_root=str(proc))

        assert pinned == {"kirocrew_sb_21_tt"}
        assert complete is True

    def test_group_with_no_namespace_left_is_still_a_vanish(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """When NO task in the group holds a namespace, absence-of-pin is real.

        The task dir carries only the leader, so nothing in the group can be
        binding a source: coverage is kept and the entry stays reclaimable. (The
        re-listing this records is asserted by the successor test above.)
        """
        proc = tmp_path / "proc"
        self._write_mountinfo(proc, "600", "")
        leader = proc / "601"
        (leader / "task" / "601").mkdir(parents=True)
        (leader / "mountinfo").write_text("")
        _raise_einval_for(monkeypatch, leader / "mountinfo")

        pinned, complete = _mount_pinned_source_names(proc_root=str(proc))

        assert pinned == set()
        assert complete is True

    @pytest.mark.skipif(
        os.getuid() == 0 if hasattr(os, "getuid") else True,
        reason="root ignores file modes; the EACCES probe needs a non-root uid",
    )
    def test_unreadable_thread_in_the_group_marks_the_scan_incomplete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A group we cannot interrogate is a coverage gap, not a free pass.

        The leader answers EINVAL and the one sibling thread cannot be read, so
        whether that namespace binds a source is unknown and directory removal
        must stay closed.
        """
        proc = tmp_path / "proc"
        self._write_mountinfo(proc, "700", "")
        leader = proc / "701"
        leader.mkdir()
        (leader / "mountinfo").write_text("")
        _raise_einval_for(monkeypatch, leader / "mountinfo")
        thread_dir = leader / "task" / "702"
        thread_dir.mkdir(parents=True)
        sibling = thread_dir / "mountinfo"
        sibling.write_text("irrelevant\n")
        os.chmod(sibling, 0)
        try:
            pinned, complete = _mount_pinned_source_names(proc_root=str(proc))
        finally:
            os.chmod(sibling, 0o644)

        assert pinned == set()
        assert complete is False

    @pytest.mark.skipif(
        not os.path.isdir("/proc/self"),
        reason="needs a real Linux procfs to observe a zombie leader",
    )
    def test_kernel_answers_einval_for_a_zombie_leader_with_a_live_thread(self):
        """Pin the kernel behaviour the thread-group step exists for.

        The leader exits through ``pthread_exit`` while a sibling thread runs on.
        The tgid read must answer EINVAL (its nsproxy is gone) while the sibling's
        per-task read must SUCCEED (the namespace is alive). If a kernel ever made
        both answer the same way, the reachability argument for consulting the
        group would be gone and this fails instead of the sweep quietly removing a
        live bind source.
        """
        program = (
            "import ctypes, threading, time\n"
            "threading.Thread(target=time.sleep, args=(30,)).start()\n"
            "time.sleep(0.2)\n"
            "ctypes.CDLL('libc.so.6', use_errno=True).pthread_exit(None)\n"
        )
        child = subprocess.Popen([sys.executable, "-c", program])  # noqa: S603
        try:
            deadline = time.time() + 10.0
            state = ""
            while time.time() < deadline:
                try:
                    with open("/proc/%d/stat" % child.pid) as fh:
                        state = fh.read().rsplit(") ", 1)[1][0]
                except OSError:  # pragma: no cover - raced the exit
                    break
                if state == "Z":
                    break
                time.sleep(0.02)
            if state != "Z":  # pragma: no cover - platform/scheduler dependent
                pytest.skip("leader did not become a zombie while a thread lived")

            with pytest.raises(OSError) as caught:
                with open("/proc/%d/mountinfo" % child.pid):
                    pass
            assert caught.value.errno == errno.EINVAL

            tids = os.listdir("/proc/%d/task" % child.pid)
            siblings = [t for t in tids if t != str(child.pid)]
            assert siblings, "the surviving thread must still be listed under task/"
            readable = []
            for tid in siblings:
                try:
                    with open("/proc/%d/task/%s/mountinfo" % (child.pid, tid)) as fh:
                        fh.read()
                    readable.append(tid)
                except OSError:
                    pass
            assert readable, (
                "a surviving thread's mountinfo must be readable -- that is the "
                "only way the scan can see the namespace behind a zombie leader"
            )
        finally:
            child.kill()
            child.wait()

    @pytest.mark.skipif(
        os.getuid() == 0 if hasattr(os, "getuid") else True,
        reason="root ignores file modes; the EACCES probe needs a non-root uid",
    )
    def test_a_readable_sibling_must_not_mask_an_unreadable_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """One unaccounted task sinks coverage, whatever a sibling reported.

        Tasks in one thread group can hold DIFFERENT mount namespaces: a thread
        may ``unshare(CLONE_NEWNS)``, and the launcher's own user namespace grants
        its descendants the CAP_SYS_ADMIN that needs. So reading one sibling
        proves nothing about a sibling that cannot be read, and letting the
        readable one satisfy coverage would report ``complete=True`` while an
        unread namespace may still bind a source -- which the sweep would then
        remove, leaving that mount's root inode S_DEAD.

        The pin that WAS read still counts: pins are additive and real either way.
        """
        proc = tmp_path / "proc"
        self._write_mountinfo(proc, "800", "")
        leader = proc / "801"
        leader.mkdir()
        (leader / "mountinfo").write_text("")
        _raise_einval_for(monkeypatch, leader / "mountinfo")
        readable = leader / "task" / "802"
        readable.mkdir(parents=True)
        (readable / "mountinfo").write_text(
            "36 35 0:22 /kirocrew_sb_31_rr /home/u/.aws rw - tmpfs tmpfs rw\n"
        )
        opaque = leader / "task" / "803"
        opaque.mkdir(parents=True)
        blocked = opaque / "mountinfo"
        blocked.write_text("irrelevant\n")
        os.chmod(blocked, 0)
        try:
            pinned, complete = _mount_pinned_source_names(proc_root=str(proc))
        finally:
            os.chmod(blocked, 0o644)

        assert pinned == {"kirocrew_sb_31_rr"}, "the pin that was read must be kept"
        assert complete is False, "an unaccounted sibling must sink coverage"

    @pytest.mark.parametrize("departure", ["gone", "namespaceless"])
    def test_a_departed_sibling_sinks_coverage_even_beside_a_readable_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, departure: str
    ):
        """A sibling that does not read makes coverage unprovable, full stop.

        A sibling exiting between the ``tids`` listing and its read may have spawned
        a successor THREAD first. A thread is invisible to the outer re-listing,
        which enumerates thread-group LEADERS only, so unlike a successor PROCESS it
        cannot be recovered by taking another pass -- fail-closed is the only honest
        answer. Excusing the departure instead would report ``complete=True`` with
        that successor's namespace never read, and the sweep would then remove a
        source it may still bind, leaving the mount's root inode S_DEAD.

        The pin that WAS read still counts: pins are additive and real either way.
        Both departure shapes are covered -- gone (``ENOENT``) and namespaceless
        (``EINVAL``).
        """
        proc = tmp_path / "proc"
        self._write_mountinfo(proc, "900", "")
        leader = proc / "901"
        leader.mkdir()
        (leader / "mountinfo").write_text("")
        _raise_einval_for(monkeypatch, leader / "mountinfo")
        readable = leader / "task" / "902"
        readable.mkdir(parents=True)
        (readable / "mountinfo").write_text(
            "36 35 0:22 /kirocrew_sb_41_aa /home/u/.aws rw - tmpfs tmpfs rw\n"
        )
        departed = leader / "task" / "903"
        departed.mkdir(parents=True)
        if departure == "namespaceless":
            (departed / "mountinfo").write_text("")
            _raise_einval_for(monkeypatch, departed / "mountinfo")
        # "gone" leaves no mountinfo file at all, so the read raises ENOENT.

        pinned, complete = _mount_pinned_source_names(proc_root=str(proc))

        assert pinned == {"kirocrew_sb_41_aa"}, "the pin that was read must be kept"
        assert complete is False, "a sibling that did not read must sink coverage"

    def test_missing_proc_root_reports_incomplete(self, tmp_path: Path):
        pinned, complete = _mount_pinned_source_names(proc_root=str(tmp_path / "noproc"))
        assert pinned == set()
        assert complete is False

    def test_filtered_procfs_without_pid_1_reports_incomplete_but_still_pins(self, tmp_path: Path):
        """hidepid/subset=pid procfs hides other users' processes entirely —
        including a root holder — and pid 1 always exists, so a listing
        without it proves filtering and must fail closed on COVERAGE.

        It must NOT stop reading: this uid's own processes stay visible under
        hidepid, and every sandbox descendant is one. An early return would
        hand the directory gate an empty pinned set with nothing to reason
        from. Caught by GPT 5.6 review of PR #8559.
        """
        proc = tmp_path / "proc"
        d = proc / "106"
        d.mkdir(parents=True)
        (d / "mountinfo").write_text(
            f"1932 1355 0:55 /kirocrew_sb_{_DEAD_PID}_live1 /home/u/.ssh rw - tmpfs tmpfs rw\n"
        )

        pinned, complete = _mount_pinned_source_names(proc_root=str(proc))

        assert complete is False
        assert pinned == {f"kirocrew_sb_{_DEAD_PID}_live1"}

    @staticmethod
    def _proc_task(
        proc: Path, pid: int, *, mountinfo: str | None, threads: dict[int, str | None] | None = None
    ) -> None:
        """Plant ``/proc/<pid>`` with its ``task/`` group; a missing
        ``mountinfo`` reads as a task that departed between the listing and
        its read. ``threads`` plants sibling tids the same way."""
        d = proc / str(pid)
        d.mkdir(parents=True)
        if mountinfo is not None:
            (d / "mountinfo").write_text(mountinfo)
            (d / "task" / str(pid)).mkdir(parents=True)
            (d / "task" / str(pid) / "mountinfo").write_text(mountinfo)
        for tid, text in (threads or {}).items():
            (d / "task" / str(tid)).mkdir(parents=True)
            if text is not None:
                (d / "task" / str(tid) / "mountinfo").write_text(text)

    def test_a_sibling_thread_in_another_namespace_pins(self, tmp_path: Path):
        """A thread can ``unshare(CLONE_FS)`` + ``setns`` into a sandbox's
        namespace while its leader stays outside; leaders-only reading would
        miss its binds. Raised by GPT review of PR #8559."""
        proc = tmp_path / "proc"
        self._proc_task(proc, 1, mountinfo="")
        line = "100 99 0:40 /kirocrew_sb_777_home /root/home rw - tmpfs tmpfs rw\n"
        self._proc_task(proc, 500, mountinfo="", threads={501: line})

        coverage = _PinScanCoverage()
        pinned, complete = _mount_pinned_source_names(proc_root=str(proc), coverage=coverage)

        assert "kirocrew_sb_777_home" in pinned
        assert complete is True and coverage.covered is True

    def test_a_sibling_thread_departing_on_the_final_pass_clears_coverage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A thread gone between the ``task/`` snapshot and its read may have
        left a successor thread holding its namespace; only re-reading the
        group can tell, and the final pass has no next pass."""
        monkeypatch.setattr("kiro_crew.sandbox._PIN_SCAN_MAX_PASSES", 1)
        proc = tmp_path / "proc"
        self._proc_task(proc, 1, mountinfo="")
        self._proc_task(proc, 500, mountinfo="", threads={501: None})

        coverage = _PinScanCoverage()
        _, complete = _mount_pinned_source_names(proc_root=str(proc), coverage=coverage)

        assert complete is False and coverage.covered is False

    def test_a_sibling_thread_departure_is_re_read_on_the_next_pass(self, tmp_path: Path):
        """With a pass to spare the group is re-read; a settled group then
        proves coverage."""
        proc = tmp_path / "proc"
        self._proc_task(proc, 1, mountinfo="")
        self._proc_task(proc, 500, mountinfo="", threads={501: None})
        real_listdir = os.listdir
        calls = {"n": 0}

        def _settling_listdir(path):
            names = real_listdir(path)
            if str(path).endswith(os.sep + "500" + os.sep + "task"):
                calls["n"] += 1
                if calls["n"] > 1:
                    names = [n for n in names if n != "501"]  # the thread is gone
            return names

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("kiro_crew.sandbox.os.listdir", _settling_listdir)
            coverage = _PinScanCoverage()
            _, complete = _mount_pinned_source_names(proc_root=str(proc), coverage=coverage)

        assert calls["n"] == 2
        assert complete is True and coverage.covered is True

    def test_final_pass_departure_clears_coverage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A task of this uid that departs on the FINAL pass may have handed its
        namespace to a child forked after that pass's listing, which no
        re-listing will catch — coverage of launcher descendants is unproven.
        """
        monkeypatch.setattr("kiro_crew.sandbox._PIN_SCAN_MAX_PASSES", 1)
        proc = tmp_path / "proc"
        self._proc_task(proc, 1, mountinfo="")
        self._proc_task(proc, 4242, mountinfo=None)  # departed before its read
        self._proc_task(proc, 4300, mountinfo="")

        coverage = _PinScanCoverage()
        pinned, complete = _mount_pinned_source_names(proc_root=str(proc), coverage=coverage)

        assert complete is False
        assert coverage.covered is False

    def test_departure_followed_by_a_relisting_keeps_coverage(self, tmp_path: Path):
        """A departure on a NON-final pass is followed by a re-listing that
        would show any successor, so coverage stands."""
        proc = tmp_path / "proc"
        self._proc_task(proc, 1, mountinfo="")
        self._proc_task(proc, 4242, mountinfo=None)

        coverage = _PinScanCoverage()
        pinned, complete = _mount_pinned_source_names(proc_root=str(proc), coverage=coverage)

        # Pass 1 saw the departure and re-listed; pass 2 saw nothing new and no
        # departure, so both the host-wide flag and coverage hold.
        assert complete is True
        assert coverage.covered is True

    def test_filtered_procfs_clears_coverage(self, tmp_path: Path):
        """``hidepid`` hides root's tasks along with pid 1, and root can hold
        any namespace (``nsenter``), so coverage falls with the host-wide flag
        rather than licensing removal on this uid's tasks alone. Raised by
        GPT review of PR #8559."""
        proc = tmp_path / "proc"
        self._proc_task(proc, 106, mountinfo="")
        self._proc_task(proc, 107, mountinfo="")

        coverage = _PinScanCoverage()
        pinned, complete = _mount_pinned_source_names(proc_root=str(proc), coverage=coverage)

        assert complete is False
        assert coverage.covered is False

    @staticmethod
    def _stat_uid_of(monkeypatch: pytest.MonkeyPatch, pid: int, uid: int) -> None:
        """Make ``/proc/<pid>`` stat as ``uid`` for the scan's uid pre-read."""
        real_stat = os.stat

        def _fake_stat(path, *a, **kw):
            st = real_stat(path, *a, **kw)
            if str(path).endswith(os.sep + str(pid)):
                return os.stat_result(tuple(st)[:4] + (uid,) + tuple(st)[5:])
            return st

        monkeypatch.setattr("kiro_crew.sandbox.os.stat", _fake_stat)

    @pytest.mark.skipif(
        not hasattr(os, "getuid"), reason="without os.getuid every task is a possible holder"
    )
    def test_foreign_uid_departure_keeps_coverage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Another user's task departing on the final pass lowers only the
        host-wide flag: it cannot hold a source this uid staged."""
        monkeypatch.setattr("kiro_crew.sandbox._PIN_SCAN_MAX_PASSES", 1)
        monkeypatch.setattr("kiro_crew.sandbox._overflow_uid", lambda: 65534)
        proc = tmp_path / "proc"
        self._proc_task(proc, 1, mountinfo="")
        self._proc_task(proc, 4242, mountinfo=None)
        self._stat_uid_of(monkeypatch, 4242, (os.getuid() if hasattr(os, "getuid") else 1000) + 1)

        coverage = _PinScanCoverage()
        _, complete = _mount_pinned_source_names(proc_root=str(proc), coverage=coverage)

        assert complete is False
        assert coverage.covered is True

    def test_root_departure_clears_coverage(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Root can ``nsenter`` a sandbox's namespace, so a root task departing
        on the final pass is a possible holder gone unread."""
        monkeypatch.setattr("kiro_crew.sandbox._PIN_SCAN_MAX_PASSES", 1)
        monkeypatch.setattr("kiro_crew.sandbox._overflow_uid", lambda: 65534)
        proc = tmp_path / "proc"
        self._proc_task(proc, 1, mountinfo="")
        self._proc_task(proc, 4242, mountinfo=None)
        self._stat_uid_of(monkeypatch, 4242, 0)

        coverage = _PinScanCoverage()
        _, complete = _mount_pinned_source_names(proc_root=str(proc), coverage=coverage)

        assert complete is False
        assert coverage.covered is False

    def test_unknown_overflow_uid_makes_a_foreign_departure_clear_coverage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A holder inside a nested user namespace stats as the overflow uid.
        When that uid cannot be learned (sysctl unreadable), a departing task of
        ANY other uid may have been such a holder, so coverage fails closed.
        Raised by GPT review of PR #8559."""
        monkeypatch.setattr("kiro_crew.sandbox._PIN_SCAN_MAX_PASSES", 1)
        monkeypatch.setattr("kiro_crew.sandbox._overflow_uid", lambda: None)
        proc = tmp_path / "proc"
        self._proc_task(proc, 1, mountinfo="")
        self._proc_task(proc, 4242, mountinfo=None)
        self._stat_uid_of(monkeypatch, 4242, (os.getuid() if hasattr(os, "getuid") else 1000) + 1)
        coverage = _PinScanCoverage()
        _, complete = _mount_pinned_source_names(proc_root=str(proc), coverage=coverage)

        assert complete is False
        assert coverage.covered is False

    def test_gate_retains_a_pinned_dir_under_an_incomplete_but_covered_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A pin from an incomplete scan still retains: a ``setsid()``
        descendant's own mountinfo, being this uid's, is readable and wins."""
        name = f"kirocrew_sb_{_DEAD_PID}_setsid1"
        _pin(monkeypatch, {name}, complete=False, covered=True)
        held = _make_dir(tmp_path, name)

        assert _cleanup_stale_sandbox_mount_sources(roots=[str(tmp_path)]) == 0
        assert held.exists()

    @pytest.mark.skipif(
        not os.path.isdir("/proc/1"),
        reason="needs an unfiltered procfs exposing pid 1 (Linux, no hidepid)",
    )
    def test_real_host_scan_can_reach_complete_when_unconstrained(self):
        """The fail-closed gate must be shown to OPEN somewhere real.

        An always-incomplete scan is silent and indistinguishable from a
        working one while the dominant (directory) leak class re-accumulates.
        This runs the REAL scan against the host /proc (read-only). Some
        runtimes legitimately cannot reach completeness — a sandboxed test
        process cannot read sibling-namespace mountinfo (EINVAL, own uid →
        gap) — so the assertion is conditional: when nothing blocks coverage,
        the scan must say so; when something does, retention is the correct
        answer and the environment is reported via skip.
        """
        pinned, complete = _mount_pinned_source_names()
        assert isinstance(pinned, set)
        if not complete:
            pytest.skip(
                "host /proc has a genuine coverage gap in this runtime "
                "(e.g. the test itself runs sandboxed) — fail-closed retention applies"
            )
        assert complete is True


@requires_posix_modes
class TestLegacyResidueSweep:
    """The pre-#6268 ``tmp*`` residue is reclaimed once, behind every fence.

    An install that upgraded past #6268 inherited a pile the keyed sweep cannot
    reason about (no pid in the name), so shipping only the reclaim fix leaves
    such a host at its inode ceiling and every spawn still failing. These names
    cannot be PROVEN to be ours, so each test below pins one fence that keeps a
    stranger's entry.
    """

    def _fence(
        self,
        monkeypatch: pytest.MonkeyPatch,
        root: Path,
        *,
        bound: set[str] | None = None,
        complete: bool = True,
        covered: bool | None = None,
    ) -> None:
        if covered is None:
            covered = complete
        monkeypatch.setattr("kiro_crew.sandbox._launcher_tmpfs_roots", lambda: [str(root)])

        def _fake(proc_root="/proc", *, coverage=None, **_kw):
            if coverage is not None and not covered:
                coverage.covered = False
            return (bound or set(), complete)

        monkeypatch.setattr("kiro_crew.sandbox._bound_source_basenames", _fake)
        # Each test below pins ONE fence; the pile threshold has its own tests.
        monkeypatch.setattr("kiro_crew.sandbox._LEGACY_PILE_THRESHOLD", 1)

    def _legacy_dir(self, root: Path, name: str = "tmpab12cd34", *, old: bool = True) -> Path:
        path = root / name
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)  # umask can shave bits off the mkdir mode  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
        if old:
            stale = time.time() - _MOUNT_SOURCE_MAX_AGE_SECONDS - 100
            os.utime(path, (stale, stale))
        return path

    def _legacy_file(self, root: Path, name: str = "tmpef56gh78", *, old: bool = True) -> Path:
        path = root / name
        path.write_text("")
        os.chmod(path, 0o600)
        if old:
            stale = time.time() - _MOUNT_SOURCE_MAX_AGE_SECONDS - 100
            os.utime(path, (stale, stale))
        return path

    def test_reclaims_the_unkeyed_residue_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        self._fence(monkeypatch, tmp_path)
        empty = self._legacy_dir(tmp_path)
        plain = self._legacy_file(tmp_path)

        removed = _cleanup_legacy_mount_source_residue()

        assert removed == 1
        assert not empty.exists()
        # The old build's mkstemp FILE sources are left alone: an unlinked file
        # another program still holds open loses what it writes next, and no
        # fence can tell such a file from ours. Raised by GPT review of #8559.
        assert plain.exists()
        # Second call is a no-op: no current build creates the shape, so a
        # completed pass is final and must not re-walk the tmpfs forever.
        assert _cleanup_legacy_mount_source_residue() == 0

    def test_a_planted_symlink_at_the_marker_is_not_followed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The stamp is create-only and O_NOFOLLOW: a dangling link at the
        marker path fails the stamp (the pass repeats) instead of making the
        gateway write at the link's target. Raised by GPT review of #8559."""
        from kiro_crew.sandbox import _LEGACY_RESIDUE_MARKER, config_dir

        self._fence(monkeypatch, tmp_path)
        marker = config_dir() / _LEGACY_RESIDUE_MARKER
        marker.parent.mkdir(parents=True, exist_ok=True)
        target = marker.parent / "security_policy.json"
        marker.symlink_to(target)
        self._legacy_dir(tmp_path)

        assert _cleanup_legacy_mount_source_residue() == 1
        assert not target.exists()
        assert marker.is_symlink()  # untouched, so the pass is not retired

    def test_incomplete_but_covered_scan_still_heals(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The same two claims as the keyed dir gate: ``/run/user/$UID`` is
        reachable by this uid and root alone, so every-possible-holder-read is
        enough when another user's task keeps the host-wide flag down. Raised
        by First Principles review of #8559."""
        self._fence(monkeypatch, tmp_path, complete=False, covered=True)
        empty = self._legacy_dir(tmp_path)

        assert _cleanup_legacy_mount_source_residue() == 1
        assert not empty.exists()

    def test_a_cohort_under_the_age_fence_withholds_the_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A host upgrading within a day of its last old-build spawn must not
        retire the pass on the cohort that is merely too young yet. Raised by
        Design review of #8559."""
        self._fence(monkeypatch, tmp_path)
        old = self._legacy_dir(tmp_path)
        young = self._legacy_dir(tmp_path, "tmpyoung001", old=False)

        assert _cleanup_legacy_mount_source_residue() == 1
        assert not old.exists() and young.exists()
        stale = time.time() - _MOUNT_SOURCE_MAX_AGE_SECONDS - 100
        os.utime(young, (stale, stale))
        # Not retired: the aged cohort is reclaimed by the next pass, which
        # then finds nothing young and stamps.
        assert _cleanup_legacy_mount_source_residue() == 1
        assert not young.exists()
        assert _cleanup_legacy_mount_source_residue() == 0

    def test_unproven_bind_coverage_removes_nothing_and_does_not_retire_the_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        """A failed scan must cost a retry, never the whole pass — and it must
        be VISIBLE at the default log level, or a host whose /proc never settles
        leaves this pass inert with nothing in the log to say so."""
        self._fence(monkeypatch, tmp_path, complete=False)
        held = self._legacy_dir(tmp_path)

        with caplog.at_level(logging.INFO, logger="kiro_crew.sandbox"):
            assert _cleanup_legacy_mount_source_residue() == 0
        assert held.exists()
        retained = [r for r in caplog.records if "legacy pass retained" in r.getMessage()]
        assert len(retained) == 1
        assert retained[0].levelno == logging.WARNING

        self._fence(monkeypatch, tmp_path, complete=True)
        assert _cleanup_legacy_mount_source_residue() == 1

    def test_bound_entry_is_preserved(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Removing a live mount's source dir S_DEADs it — the bind scan decides."""
        name = "tmpbound123"
        self._fence(monkeypatch, tmp_path, bound={name})
        held = self._legacy_dir(tmp_path, name)

        assert _cleanup_legacy_mount_source_residue() == 0
        assert held.exists()

    def test_non_empty_dir_survives(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """``os.rmdir`` refusing a populated dir IS the emptiness fence.

        It keeps both a stranger's working scratch dir and the legacy SSH shadow
        dir, which holds a known-hosts copy.
        """
        self._fence(monkeypatch, tmp_path)
        populated = self._legacy_dir(tmp_path, "tmpshadow12")
        (populated / "known_hosts").write_text("example.com ssh-ed25519 AAAA\n")

        assert _cleanup_legacy_mount_source_residue() == 0
        assert (populated / "known_hosts").exists()

    def test_bound_scan_keys_on_the_tmpfs_relative_root_field(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sandbox_sweep_original
    ):
        """mountinfo names a source relative to ITS filesystem, never by host path.

        A source staged on the /run/user/$UID tmpfs reads as ``/tmpab12cd34``,
        so any filter on the field's dirname matches nothing and silently
        disarms the fence — which is exactly what the first cut of this scan did.
        The synthetic lines below mirror a real host's (including a source
        deleted while still bound, which reads ``//deleted``).
        """
        # The autouse floor pins both module attributes fail-closed; the real
        # functions are reachable only through the conftest accessor, and the
        # bound scan delegates to the pinned scan by NAME, so that one must be
        # restored on the module for the delegation to reach the real walk.
        bound_scan = sandbox_sweep_original("_bound_source_basenames")
        monkeypatch.setattr(
            "kiro_crew.sandbox._mount_pinned_source_names",
            sandbox_sweep_original("_mount_pinned_source_names"),
        )

        proc = tmp_path / "proc"
        (proc / "1").mkdir(parents=True)
        (proc / "1" / "mountinfo").write_text("38 35 8:1 / / rw - ext4 /dev/sda1 rw\n")
        (proc / "202").mkdir()
        (proc / "202" / "mountinfo").write_text(
            "1930 1355 0:55 /tmpab12cd34 /home/u/.gnupg rw,nosuid - tmpfs tmpfs rw\n"
            "1931 1355 0:55 /tmpef56gh78//deleted /home/u/.aws rw,nosuid - tmpfs tmpfs rw\n"
            "1932 1355 0:55 /kirocrew_sb_7_x /home/u/.ssh rw,nosuid - tmpfs tmpfs rw\n"
            "1933 1355 0:55 /notlegacy /home/u/x rw - tmpfs tmpfs rw\n"
        )

        bound, complete = bound_scan(proc_root=str(proc))

        assert complete is True
        assert bound == {"tmpab12cd34", "tmpef56gh78"}

    def test_fresh_foreign_and_wrong_shaped_entries_survive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Age, mode and name shape each keep an entry this sweep must not own."""
        self._fence(monkeypatch, tmp_path)
        fresh = self._legacy_dir(tmp_path, "tmpfresh1234"[:11], old=False)
        loose = self._legacy_dir(tmp_path, "tmploose5678")
        os.chmod(loose, 0o755)  # not a mkdtemp mode — hand-made or umask-shaped  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
        sized = self._legacy_file(tmp_path, "tmpsized9012")
        sized.write_text("payload")
        os.utime(sized, (time.time() - _MOUNT_SOURCE_MAX_AGE_SECONDS - 100,) * 2)
        named = tmp_path / "tmp-not-mkdtemp"
        named.mkdir(mode=0o700)
        keyed = _make_dir(tmp_path, f"kirocrew_sb_{_DEAD_PID}_keyed01")

        assert _cleanup_legacy_mount_source_residue() == 0
        for path in (fresh, loose, sized, named, keyed):
            assert path.exists(), path

    def test_below_the_pile_threshold_everything_is_retained_and_the_pass_retires(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """An unkeyed name has no provenance; the PILE is the provenance.

        A handful of empty, day-old ``tmp*`` dirs is what another same-uid
        program's ``tempfile`` scratch looks like, and one it may still write
        into — so below the threshold nothing is touched. There is also no
        pile to heal, so the one-shot pass retires rather than re-walking the
        tmpfs on every sweep. Caught by GPT 5.6 review of PR #8559.
        """
        self._fence(monkeypatch, tmp_path)
        monkeypatch.setattr("kiro_crew.sandbox._LEGACY_PILE_THRESHOLD", 4)
        strays = [self._legacy_dir(tmp_path, f"tmpstray00{i}") for i in range(3)]

        assert _cleanup_legacy_mount_source_residue() == 0
        for stray in strays:
            assert stray.exists(), stray
        # Retired: a later pass is a no-op even once more candidates appear.
        self._legacy_dir(tmp_path, "tmpstray003")
        assert _cleanup_legacy_mount_source_residue() == 0

    def test_at_the_pile_threshold_the_buffered_candidates_are_reclaimed_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Candidates seen BEFORE the threshold is reached are not stranded."""
        self._fence(monkeypatch, tmp_path)
        monkeypatch.setattr("kiro_crew.sandbox._LEGACY_PILE_THRESHOLD", 4)
        pile = [self._legacy_dir(tmp_path, f"tmppile000{i}") for i in range(6)]

        assert _cleanup_legacy_mount_source_residue() == 6
        for entry in pile:
            assert not entry.exists(), entry

    @pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX-only root chain")
    def test_legacy_roots_exclude_dev_shm(self, sandbox_sweep_original):
        """``/dev/shm`` is host-wide and where other programs' ``tempfile``
        scratch legitimately lives; the unkeyed pass must never walk it, even
        though the launcher falls back to it for staging."""
        real = sandbox_sweep_original("_launcher_tmpfs_roots")
        assert real() == [f"/run/user/{os.getuid()}"]

    def test_wired_into_the_periodic_entry_point(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """It must run from the same call the gateway already makes, or a
        just-updated host stays broken until someone finds it by hand."""
        self._fence(monkeypatch, tmp_path)
        monkeypatch.setattr(
            "kiro_crew.sandbox._mount_source_candidate_roots", lambda: [str(tmp_path)]
        )
        legacy = self._legacy_dir(tmp_path)

        removed = cleanup_stale_sandbox_profiles(legacy_dir=str(tmp_path / "absent"))

        assert removed >= 1
        assert not legacy.exists()

    def test_reclaim_starts_at_boot_without_holding_the_loop(self):
        """The cleanup loop must START the reclaim, and must not WAIT on it.

        Two failures this pins apart. Deferring the reclaim to the first interval
        leaves a just-updated gateway unable to spawn anything for 5-10 minutes
        with the fix already installed. Awaiting it instead queues every other
        sweep in that loop behind a pass that a pathological pile or a stalled
        filesystem can make arbitrarily slow -- so the reclaim is dispatched as
        its own task, before the tick loop is entered.

        Asserted structurally: the ORDER and the non-await are the contract, and
        both are one edit away from silently regressing.
        """
        import kiro_crew.session_cleanup as cleanup_mod

        tree = ast.parse(Path(cleanup_mod.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef) or node.name != "_cleanup_loop":
                continue
            body = [ast.dump(stmt) for stmt in node.body]
            ticks = [i for i, dumped in enumerate(body) if "_run_cleanup_ticks" in dumped]
            if not ticks:
                continue  # the Protocol stub (`async def _cleanup_loop(self) -> None: ...`)
            dispatch = [i for i, dumped in enumerate(body) if "_sweep_sandbox_artifacts" in dumped]
            assert dispatch, "_cleanup_loop does not start the sandbox reclaim"
            assert min(dispatch) < min(ticks), "the reclaim must be dispatched before the ticks"
            assert (
                "create_task" in body[min(dispatch)]
            ), "the reclaim must be dispatched as a task, not awaited inline"
            return
        raise AssertionError("no _cleanup_loop implementation found in session_cleanup")


class TestSweepTimeBudget:
    """One pass is bounded; the remainder is the next pass's work.

    The sweep shares the maintenance executor with other housekeeping, so a
    multi-million-entry backlog must not hold a worker for a minute. Reclaim is
    decided per entry, so a truncated pass is progress rather than an
    inconsistent state.
    """

    def _spent_budget(self) -> pytest.MonkeyPatch:
        """A SCOPED patcher for the budget knobs, deliberately not the test's own.

        ``monkeypatch.undo()`` would also revert the autouse host-isolation floor
        — including the ``KIROCREW_HOME`` pin — and this sweep then stamps its
        one-shot marker into the operator's REAL data home. Caught in review of
        this very test. Every knob here is therefore undone through its own
        context, never the shared fixture.

        Budget already spent, checked every SECOND entry: the check runs before
        the entry it counts, so checking on the first would stop a pass having
        done nothing, and a pass that can never progress is a different bug.
        """
        budget = pytest.MonkeyPatch()
        budget.setattr("kiro_crew.sandbox._SWEEP_TIME_BUDGET_SECONDS", -1.0)
        budget.setattr("kiro_crew.sandbox._SWEEP_BUDGET_CHECK_EVERY", 2)
        return budget

    def test_keyed_pass_stops_at_the_budget_and_resumes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        _pin(monkeypatch, set())
        for index in range(4):
            _make_file(tmp_path, f"kirocrew_sb_{_DEAD_PID}_file{index:02d}")

        budget = self._spent_budget()
        try:
            with caplog.at_level(logging.INFO, logger="kiro_crew.sandbox"):
                first = _cleanup_stale_sandbox_mount_sources(roots=[str(tmp_path)])
        finally:
            budget.undo()

        assert first == 1
        assert any("paused at the" in r.getMessage() for r in caplog.records)
        assert len(list(tmp_path.iterdir())) == 3

        # Budget restored: the remainder is reclaimed, nothing is stranded.
        assert _cleanup_stale_sandbox_mount_sources(roots=[str(tmp_path)]) == 3
        assert not list(tmp_path.iterdir())

    @requires_posix_modes
    def test_truncated_legacy_pass_does_not_stamp_the_one_shot_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Stamping a partial pass would retire the rest of the residue unswept."""
        monkeypatch.setattr("kiro_crew.sandbox._launcher_tmpfs_roots", lambda: [str(tmp_path)])
        monkeypatch.setattr(
            "kiro_crew.sandbox._bound_source_basenames",
            lambda proc_root="/proc", **_kw: (set(), True),
        )
        monkeypatch.setattr("kiro_crew.sandbox._LEGACY_PILE_THRESHOLD", 1)
        stale = time.time() - _MOUNT_SOURCE_MAX_AGE_SECONDS - 100
        for name in ("tmpaaaaaaaa", "tmpbbbbbbbb", "tmpcccccccc"):
            path = tmp_path / name
            path.mkdir(mode=0o700)
            os.chmod(path, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
            os.utime(path, (stale, stale))

        budget = self._spent_budget()
        try:
            assert _cleanup_legacy_mount_source_residue() == 1
        finally:
            budget.undo()
        assert len(list(tmp_path.iterdir())) == 2

        # The marker was NOT stamped, so the next pass finishes the residue.
        assert _cleanup_legacy_mount_source_residue() == 2
        assert not list(tmp_path.iterdir())


class TestHeldBackDiagnostic:
    """Retention must be distinguishable from reclamation in the log.

    A permanently-closed pin scan produces the same observable as a healthy
    one — zero removals — while the dominant (directory) leak class
    re-accumulates. The report exists to break that tie, so its LEVEL is part
    of the contract: at INFO it does not reach a default deployment's log at
    all, which is how the exhaustion this sweep guards against ran unobserved
    while this very line fired on every sweep.
    """

    def test_unprovable_coverage_is_reported_as_a_fault(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        _pin(monkeypatch, set(), complete=False)
        _make_dir(tmp_path, f"kirocrew_sb_{_DEAD_PID}_held01")

        with caplog.at_level(logging.INFO, logger="kiro_crew.sandbox"):
            removed = _cleanup_stale_sandbox_mount_sources(roots=[str(tmp_path)])

        assert removed == 0
        held = [r for r in caplog.records if "held back" in r.getMessage()]
        assert len(held) == 1
        assert held[0].levelno == logging.WARNING
        assert "pin scan incomplete" in held[0].getMessage()

    def test_a_live_pin_stays_ordinary_information(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        """Holding an entry a live namespace binds is correct operation, not a
        fault — it must not be escalated, or the fault signal drowns."""
        name = f"kirocrew_sb_{_DEAD_PID}_pinned1"
        _pin(monkeypatch, {name}, complete=True)
        _make_dir(tmp_path, name)

        with caplog.at_level(logging.INFO, logger="kiro_crew.sandbox"):
            removed = _cleanup_stale_sandbox_mount_sources(roots=[str(tmp_path)])

        assert removed == 0
        held = [r for r in caplog.records if "held back" in r.getMessage()]
        assert len(held) == 1
        assert held[0].levelno == logging.INFO
        assert "pinned by a live mount namespace" in held[0].getMessage()


class TestMountSourceCandidateRoots:
    """The root chain aims the sweep at the actual leak site."""

    @pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX-only root chain")
    def test_chain_order_runtime_dir_then_shm_then_tempdir(self, sandbox_sweep_original):
        import tempfile as _tempfile

        # The autouse host-isolation fixture patches the module attribute; the
        # rootdir conftest stashes the real function before doing so.
        real = sandbox_sweep_original("_mount_source_candidate_roots")
        roots = real()

        assert roots == [
            f"/run/user/{os.getuid()}",
            "/dev/shm",
            _tempfile.gettempdir(),
        ]


@requires_posix
class TestLauncherStagingSitesArePrefixed:
    """Every generated staging call against the tmpfs source carries the pid prefix."""

    @staticmethod
    def _staging_calls(tree: ast.AST) -> list[ast.Call]:
        """tempfile.mkdtemp/mkstemp calls whose ``dir=`` is ``_tmpfs_src``."""
        calls = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "tempfile"
                and func.attr in ("mkdtemp", "mkstemp")
            ):
                continue
            dir_kw = next((k for k in node.keywords if k.arg == "dir"), None)
            if dir_kw is not None and (
                isinstance(dir_kw.value, ast.Name) and dir_kw.value.id == "_tmpfs_src"
            ):
                calls.append(node)
        return calls

    @staticmethod
    def _src_prefix_assignments(tree: ast.AST) -> list[ast.Assign]:
        return [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "_src_prefix" for t in node.targets)
        ]

    @pytest.mark.parametrize("level", ["strict", "cc", "standard"])
    def test_launcher_parses_and_all_staging_sites_prefixed(self, level: str):
        script = _build_launcher_script(level)
        tree = ast.parse(script)  # string-template edits must keep it parseable

        staging = self._staging_calls(tree)
        # The template always emits all three staging sites (per-dir empties,
        # per-file empties, SSH shadow); the level varies the DATA, not the code.
        assert len(staging) == 3
        for call in staging:
            prefix_kw = next((k for k in call.keywords if k.arg == "prefix"), None)
            assert prefix_kw is not None, ast.dump(call)
            assert isinstance(prefix_kw.value, ast.Name)
            assert prefix_kw.value.id == "_src_prefix"

    @pytest.mark.parametrize("level", ["strict", "cc", "standard"])
    def test_every_tempfile_call_in_the_launcher_carries_a_known_prefix(self, level: str):
        """Closed over ALL tempfile.mkdtemp/mkstemp calls, however spelled.

        The staging-site assertion above keys on ``dir=_tmpfs_src``, which a
        future positional ``mkdtemp(_tmpfs_src)`` or ``dir=_tmpfs_src or
        None`` would evade — silently re-opening the unprefixed-orphan class.
        Every temp creation in the launcher must carry either the pid-bearing
        ``_src_prefix`` or the probe's own literal prefix.
        """
        tree = ast.parse(_build_launcher_script(level))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "tempfile"
            and node.func.attr in ("mkdtemp", "mkstemp")
        ]
        assert len(calls) == 4  # three staging sites + the tmpfs probe
        for call in calls:
            prefix_kw = next((k for k in call.keywords if k.arg == "prefix"), None)
            assert prefix_kw is not None, ast.dump(call)
            ok_name = isinstance(prefix_kw.value, ast.Name) and prefix_kw.value.id == "_src_prefix"
            ok_probe = (
                isinstance(prefix_kw.value, ast.Constant)
                and prefix_kw.value.value == "kirocrew_sbprobe_"
            )
            assert ok_name or ok_probe, ast.dump(call)

    @pytest.mark.parametrize("level", ["strict", "cc", "standard"])
    def test_prefix_is_assigned_in_the_post_fork_child_branch(self, level: str):
        """The embedded pid must be the CHILD's — the process that execs the
        agent — so the assignment has to sit in the fork's else branch, after
        ``os.fork()`` returned 0. An assignment hoisted above the fork would
        bake in the short-lived parent launcher's pid and every entry would
        read dead the moment the parent exits."""
        tree = ast.parse(_build_launcher_script(level))

        assignments = self._src_prefix_assignments(tree)
        assert len(assignments) == 1
        assignment = assignments[0]

        def _in_child_branch(node: ast.AST) -> bool:
            for candidate in ast.walk(node):
                if (
                    isinstance(candidate, ast.If)
                    and isinstance(candidate.test, ast.Compare)
                    and isinstance(candidate.test.left, ast.Name)
                    and candidate.test.left.id == "pid"
                ):
                    return any(assignment is n for b in candidate.orelse for n in ast.walk(b))
            return False

        assert _in_child_branch(tree), "_src_prefix must be assigned in the fork child branch"

    @pytest.mark.parametrize("level", ["strict", "cc", "standard"])
    def test_prefix_embeds_launcher_runtime_pid(self, level: str):
        """The prefix value is computed from ``os.getpid()`` at launcher
        runtime (not baked in by the gateway at template-format time)."""
        tree = ast.parse(_build_launcher_script(level))
        (assignment,) = self._src_prefix_assignments(tree)

        getpid_calls = [
            node
            for node in ast.walk(assignment.value)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "getpid"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
        ]
        assert getpid_calls, "_src_prefix must derive from os.getpid() at runtime"

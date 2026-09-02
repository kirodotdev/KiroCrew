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
      files and empty dirs on dead pid or over-age; non-empty dirs (contents
      are visible inside a live mount namespace) only when the mountinfo pin
      scan positively establishes no namespace references the entry — the pin
      scan, not the pid probe, is the deciding evidence, so a recycled pid is
      reclaimed while a genuine long-lived sandbox is kept. Foreign ``tmp*``
      names, probe names, planted hostile pid segments, and
      ``kirocrew_sandbox_*`` launcher scripts are preserved, without the sweep
      ever raising.
"""

from __future__ import annotations

import ast
import os
import shutil
import time
from pathlib import Path

import pytest

from kiro_crew.sandbox import (
    _MOUNT_SOURCE_MAX_AGE_SECONDS,
    _build_launcher_script,
    _cleanup_stale_sandbox_mount_sources,
    _mount_pinned_source_names,
    cleanup_stale_sandbox_profiles,
)

# ``_build_launcher_script`` calls POSIX-only ``os.getuid``/``os.getgid`` (the
# namespace launcher never runs on Windows).
requires_posix = pytest.mark.skipif(
    not hasattr(os, "getuid"),
    reason="_build_launcher_script uses POSIX-only os.getuid (#2041)",
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


def _pin(monkeypatch: pytest.MonkeyPatch, names: set[str], *, complete: bool = True) -> None:
    """Fix the pin scan's answer for one test (signature-matched to the real one)."""
    monkeypatch.setattr(
        "kiro_crew.sandbox._mount_pinned_source_names",
        lambda proc_root="/proc": (names, complete),
    )


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

    def test_incomplete_pin_scan_blocks_dir_removal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Absence-of-pin must be POSITIVELY established before any dir goes.

        A partial /proc scan (fd exhaustion, an unmappable-uid holder) cannot
        prove an entry is unreferenced, so the sweep must fail closed for
        dirs — empty ones included, since rmdir of a live source S_DEADs the
        mount — while plain files are unaffected by the scan.
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

    def test_missing_proc_root_reports_incomplete(self, tmp_path: Path):
        pinned, complete = _mount_pinned_source_names(proc_root=str(tmp_path / "noproc"))
        assert pinned == set()
        assert complete is False

    def test_filtered_procfs_without_pid_1_reports_incomplete(self, tmp_path: Path):
        """hidepid/subset=pid procfs hides other users' processes entirely —
        including a root holder — and pid 1 always exists, so a listing
        without it proves filtering and must fail closed."""
        proc = tmp_path / "proc"
        d = proc / "106"
        d.mkdir(parents=True)
        (d / "mountinfo").write_text("")

        pinned, complete = _mount_pinned_source_names(proc_root=str(proc))

        assert pinned == set()
        assert complete is False

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

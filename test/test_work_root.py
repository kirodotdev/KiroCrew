"""Tests for :mod:`kiro_crew.work_root`.

Everything runs against a monkeypatched data home under ``tmp_path``; the real
``<data home>/work`` is never touched.

The sweep cases set mtimes explicitly instead of sleeping, and they age EVERY
entry in a tree, because the module's idle signal is the tree's newest mtime
rather than the top directory's.
"""

from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path

import pytest

if sys.platform != "win32":
    import fcntl

from kiro_crew import work_root as wr

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")


@pytest.fixture
def root(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(wr, "config_dir", lambda: home)
    return home / wr.WORK_DIRNAME


def age(path: Path, seconds: float) -> None:
    """Push *path* and everything under it *seconds* into the past.

    Links are aged as THEMSELVES (``follow_symlinks=False``), never through:
    the module's idle signal lstats every entry, so a link left at the current
    time would make its whole tree read as active and a sweep case would pass
    for the wrong reason.
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


def _lock_exists(root: Path, key: str) -> bool:
    """Whether *key* carries a sidecar lock file.

    Deliberately NOT the allocation evidence: the lock outlives every entry it
    ever served, so its presence says nothing about the directory currently at
    that name. :func:`_marker_exists` is the evidence.
    """
    return os.path.lexists(root / f".{key}{wr._LOCK_SUFFIX}")


def _marker_exists(entry: Path) -> bool:
    """Whether *entry* carries the allocation evidence the sweep requires."""
    return os.path.lexists(entry / wr._MARKER_NAME)


class TestAllocate:
    def test_creates_private_dir_under_managed_root(self, root: Path) -> None:
        path = wr.allocate_work("issue-6788")

        assert path == root / "issue-6788"
        assert path.is_dir()
        if sys.platform != "win32":
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o700

    def test_same_key_rejoins_the_same_directory(self, root: Path) -> None:
        # The whole reason this root exists: a LATER, different process finds
        # the directory again by computing the same key.
        first = wr.allocate_work("pr-13019")
        (first / "clone.txt").write_text("state", encoding="utf-8")

        second = wr.allocate_work("pr-13019")

        assert second == first
        assert (second / "clone.txt").read_text(encoding="utf-8") == "state"

    @pytest.mark.parametrize(
        "key",
        ["", ".", "..", ".hidden", "a/b", "a\\b", "a b", "x" * 129, "-leading"],
    )
    def test_unnameable_keys_are_refused_not_rewritten(self, root: Path, key: str) -> None:
        # A sanitizer would map two distinct keys onto one directory and mix
        # their work, so the contract is a refusal.
        with pytest.raises(ValueError):
            wr.allocate_work(key)
        assert not root.exists()

    def test_non_string_key_is_refused(self, root: Path) -> None:
        with pytest.raises(ValueError):
            wr.allocate_work(7)  # type: ignore[arg-type]

    @_POSIX_ONLY
    def test_linked_managed_root_is_refused(self, root: Path, tmp_path: Path) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        root.parent.mkdir(parents=True, exist_ok=True)
        root.symlink_to(elsewhere, target_is_directory=True)

        with pytest.raises(wr.WorkRootBoundaryError):
            wr.allocate_work("k")
        assert list(elsewhere.iterdir()) == []

    @_POSIX_ONLY
    def test_linked_work_dir_is_refused(self, root: Path, tmp_path: Path) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        root.mkdir(parents=True)
        (root / "k").symlink_to(elsewhere, target_is_directory=True)

        with pytest.raises(wr.WorkRootBoundaryError):
            wr.allocate_work("k")
        assert list(elsewhere.iterdir()) == []


class TestOnlyThisModulesOwnTreesAreAdopted:
    def test_an_existing_unmarked_directory_is_refused_not_adopted(self, root: Path) -> None:
        # Adopting a stranger means MARKING it, and the marker is exactly the
        # evidence the sweep deletes on -- so an operator's directory sitting at a
        # key would be destroyed the first time any job allocated that key.
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        stranger = root / "k"
        stranger.mkdir()
        (stranger / "operator.db").write_text("not ours", encoding="utf-8")

        with pytest.raises(wr.WorkRootBoundaryError):
            wr.allocate_work("k")

        assert not _marker_exists(stranger)
        assert (stranger / "operator.db").is_file()
        age(stranger, wr.IDLE_GRACE_SECONDS * 4)

        assert wr.sweep_work_root() == 0
        assert (stranger / "operator.db").is_file()

    def test_a_directory_restored_at_a_reclaimed_key_is_refused(self, root: Path) -> None:
        # Keys are deterministic, so a reclaimed key comes round again. The
        # reclaimed entry's sidecar lock is still sitting there, which is why the
        # lock cannot be the evidence; the restored directory carries no marker, so
        # it is not adopted and stays out of the sweep's reach.
        allocated = wr.allocate_work("k")
        age(allocated, wr.IDLE_GRACE_SECONDS + 60)
        assert wr.sweep_work_root() == 1
        assert _lock_exists(root, "k")

        restored = root / "k"
        restored.mkdir()
        (restored / "operator.db").write_text("restored by hand", encoding="utf-8")

        with pytest.raises(wr.WorkRootBoundaryError):
            wr.allocate_work("k")

        assert not _marker_exists(restored)
        assert (restored / "operator.db").is_file()

    def test_a_directory_created_here_is_removed_when_its_mark_fails(self, root: Path) -> None:
        # Leaving it behind would wedge the key for good, because every later call
        # must refuse an unmarked directory and the key is deterministic, so the
        # caller cannot route around it. Removing an EMPTY directory takes no data
        # with it, and nothing has been handed out yet.
        real_open = wr.os.open
        seen = {"marker_attempts": 0}

        def refuse_the_marker(path, flags, mode=0o777, **kwargs):
            if str(path).endswith(wr._MARKER_NAME):
                seen["marker_attempts"] += 1
                raise OSError("marker refused")
            return real_open(path, flags, mode, **kwargs)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(wr.os, "open", refuse_the_marker)
            with pytest.raises(OSError):
                wr.allocate_work("k")

        assert seen["marker_attempts"] == 1
        assert not (root / "k").exists()

        rebuilt = wr.allocate_work("k")

        assert rebuilt.is_dir()
        assert _marker_exists(rebuilt)


class TestNoCallerCanShortenTheWindow:
    # Two runs legitimately share a key, and this root carries no owner, so a
    # caller-shortened window lets a run that lost the key speak for the run that
    # holds it: the stale signal arrives later in wall-clock order either way, so
    # nothing can tell the two apart. The window is therefore not shortenable at
    # all, and these pin that as surface rather than as prose.

    def test_the_module_exposes_no_release(self) -> None:
        assert not [name for name in dir(wr) if "release" in name.lower()]

    def test_the_sweep_takes_one_window_for_every_entry(self) -> None:
        import inspect

        params = inspect.signature(wr.sweep_work_root).parameters
        assert [p for p in params if p != "now"] == ["grace"]

    def test_a_planted_marker_does_not_hasten_reclamation(self, root: Path) -> None:
        # Aging comes AFTER the plant, so the marker's own write cannot refresh
        # the tree and mask the question being asked: the entry is genuinely old
        # AND carries the name, and it still keeps the one window every entry
        # gets. Written the other way round this passes against a sweep that
        # honours the name, which is the regression it exists to forbid.
        path = wr.allocate_work("k")
        (path / ".released").write_text("", encoding="utf-8")
        age(path, 2 * 24 * 3600)

        assert wr.sweep_work_root() == 0
        assert path.is_dir()

    def test_writing_into_the_tree_delays_reclamation(self, root: Path) -> None:
        # The other direction: a late write is content, and content refreshes the
        # newest mtime, so it can only push reclamation further out.
        path = wr.allocate_work("k")
        age(path, wr.IDLE_GRACE_SECONDS + 60)
        (path / ".released").write_text("", encoding="utf-8")

        assert wr.sweep_work_root() == 0
        assert path.is_dir()


class TestOnlyAllocatedEntriesAreSwept:
    # The managed root is a directory inside the data home, so something other
    # than this module can be sitting at a name in it -- an operator's own
    # directory placed before this leaf was claimed, or one restored at a name an
    # earlier sweep reclaimed. The marker INSIDE an entry is the only thing that
    # says "allocate_work made the directory that is here now", because it shares
    # that directory's lifetime. The sidecar lock cannot say it: the lock outlives
    # every entry it serves, so its presence attests a key, not a directory.

    def test_a_directory_with_no_marker_is_never_swept(self, root: Path) -> None:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        stranger = root / "project"
        stranger.mkdir()
        (stranger / "operator.db").write_text("not ours", encoding="utf-8")
        age(stranger, wr.IDLE_GRACE_SECONDS * 4)

        assert wr.sweep_work_root() == 0
        assert (stranger / "operator.db").is_file()

    def test_a_directory_restored_at_a_reclaimed_name_is_never_swept(self, root: Path) -> None:
        # Keys are deterministic, so a name this sweep reclaims can be occupied
        # again by something this module did not allocate. The marker went with
        # the tree, while the sidecar lock is still sitting there -- so an
        # evidence test that reads the lock would delete the newcomer on the
        # strength of the reclaimed entry's allocation.
        allocated = wr.allocate_work("k")
        age(allocated, wr.IDLE_GRACE_SECONDS + 60)
        assert wr.sweep_work_root() == 1
        assert not allocated.exists()
        assert _lock_exists(root, "k")

        restored = root / "k"
        restored.mkdir()
        (restored / "operator.db").write_text("restored by hand", encoding="utf-8")
        age(restored, wr.IDLE_GRACE_SECONDS * 4)

        assert wr.sweep_work_root() == 0
        assert (restored / "operator.db").is_file()

    @_POSIX_ONLY
    def test_a_linked_marker_is_not_evidence(self, root: Path) -> None:
        # The entry's own writer chooses what a link inside it names, so honouring
        # one would let a planted link authorize deleting a directory this module
        # never allocated.
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        elsewhere = root.parent / "elsewhere"
        elsewhere.write_text("", encoding="utf-8")
        stranger = root / "project"
        stranger.mkdir()
        (stranger / wr._MARKER_NAME).symlink_to(elsewhere)
        age(stranger, wr.IDLE_GRACE_SECONDS * 4)

        assert wr.sweep_work_root() == 0
        assert stranger.is_dir()

    def test_the_sweep_does_not_create_the_evidence_it_tests_for(self, root: Path) -> None:
        # If the sweep wrote the marker, the check would pass on the NEXT wake and
        # the entry would be deleted a week late rather than never. The sweep must
        # not leave a lock file behind either: it tests for the marker before
        # opening a lock, so an entry it will never sweep accumulates nothing.
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        stranger = root / "project"
        stranger.mkdir()
        age(stranger, wr.IDLE_GRACE_SECONDS * 4)

        wr.sweep_work_root()

        assert not _marker_exists(stranger)
        assert not _lock_exists(root, "project")
        assert wr.sweep_work_root() == 0
        assert stranger.is_dir()

    def test_an_allocated_entry_is_still_swept(self, root: Path) -> None:
        # The control: the evidence check must not turn the sweep off. An entry
        # allocate_work made carries a marker, so it is reclaimable as before.
        path = wr.allocate_work("k")
        assert _marker_exists(path)
        age(path, wr.IDLE_GRACE_SECONDS + 60)

        assert wr.sweep_work_root() == 1
        assert not path.exists()


class TestAFailedStampIsNotSwallowed:
    def test_allocate_raises_when_the_refresh_fails(self, root: Path, monkeypatch) -> None:
        # A stamp that silently did not happen leaves the tree exactly as having
        # no stamp leaves it, so returning the path would hand a caller a
        # rejoined tree the next sweep deletes under it.
        path = wr.allocate_work("k")
        age(path, wr.IDLE_GRACE_SECONDS + 60)

        def refuse(*args, **kwargs):
            raise OSError("utime refused")

        monkeypatch.setattr(wr.os, "utime", refuse)

        with pytest.raises(OSError):
            wr.allocate_work("k")

    def test_a_failed_first_stamp_leaves_a_reclaimable_directory(self, root: Path) -> None:
        # The marker is written BEFORE the stamp so both failure directions are
        # safe: a marked tree with no stamp is reclaimable, while a stamped tree
        # with no marker would leak for good.
        with pytest.MonkeyPatch.context() as patch:

            def refuse(*args, **kwargs):
                raise OSError("utime refused")

            patch.setattr(wr.os, "utime", refuse)
            with pytest.raises(OSError):
                wr.allocate_work("k")

        abandoned = root / "k"
        assert _marker_exists(abandoned)
        age(abandoned, wr.IDLE_GRACE_SECONDS + 60)

        assert wr.sweep_work_root() == 1
        assert not abandoned.exists()

    def test_the_stamp_actually_moves_the_mtime(self, root: Path) -> None:
        # The control for the case above: without this, a refresh that is a no-op
        # would satisfy the raise-on-failure test while protecting nothing.
        path = wr.allocate_work("k")
        age(path, wr.IDLE_GRACE_SECONDS + 60)
        stale = path.stat().st_mtime

        rejoined = wr.allocate_work("k")

        assert rejoined == path
        assert rejoined.stat().st_mtime > stale
        assert wr.sweep_work_root() == 0


class TestSweep:
    def test_entry_past_the_window_is_removed(self, root: Path) -> None:
        path = wr.allocate_work("k")
        age(path, wr.IDLE_GRACE_SECONDS + 60)

        assert wr.sweep_work_root() == 1
        assert not path.exists()

    def test_entry_inside_the_window_is_kept(self, root: Path) -> None:
        path = wr.allocate_work("k")
        age(path, wr.IDLE_GRACE_SECONDS - 60)

        assert wr.sweep_work_root() == 0
        assert path.is_dir()

    def test_an_entry_whose_job_died_keeps_its_evidence_for_the_week(self, root: Path) -> None:
        # Nothing marks an entry done, so a crashed job's evidence is held for the
        # full window rather than reclaimed early on someone else's word.
        path = wr.allocate_work("k")
        (path / "evidence.log").write_text("why it died", encoding="utf-8")
        age(path, wr.IDLE_GRACE_SECONDS - 3600)

        assert wr.sweep_work_root() == 0
        assert (path / "evidence.log").is_file()

    def test_a_recent_nested_write_keeps_an_old_top_directory(self, root: Path) -> None:
        # A writer holding an open descriptor never touches the top directory's
        # mtime, so the idle signal has to be the tree's newest.
        path = wr.allocate_work("k")
        nested = path / "clone" / "deep"
        nested.mkdir(parents=True)
        age(path, wr.IDLE_GRACE_SECONDS + 3600)
        (nested / "active.txt").write_text("fresh", encoding="utf-8")

        assert wr.sweep_work_root() == 0
        assert path.is_dir()

    def test_a_stray_file_in_the_root_is_never_swept(self, root: Path) -> None:
        root.mkdir(parents=True)
        stray = root / "notes.txt"
        stray.write_text("x", encoding="utf-8")
        age(root, wr.IDLE_GRACE_SECONDS + 60)

        assert wr.sweep_work_root() == 0
        assert stray.is_file()

    @_POSIX_ONLY
    def test_a_linked_child_is_never_swept_or_followed(self, root: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "precious.txt").write_text("keep", encoding="utf-8")
        root.mkdir(parents=True)
        link = root / "k"
        link.symlink_to(outside, target_is_directory=True)
        os.utime(root, (0, 0))

        assert wr.sweep_work_root() == 0
        assert link.is_symlink()
        assert (outside / "precious.txt").is_file()

    @_POSIX_ONLY
    def test_a_linked_managed_root_sweeps_nothing(self, root: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        victim = outside / "victim"
        victim.mkdir()
        os.utime(victim, (0, 0))
        root.parent.mkdir(parents=True, exist_ok=True)
        root.symlink_to(outside, target_is_directory=True)

        assert wr.sweep_work_root() == 0
        assert victim.is_dir()

    def test_a_missing_root_is_not_an_error(self, root: Path) -> None:
        assert not root.exists()
        assert wr.sweep_work_root() == 0

    def test_the_window_is_caller_overridable(self, root: Path) -> None:
        path = wr.allocate_work("k")
        age(path, 120)

        assert wr.sweep_work_root(grace=60.0) == 1
        assert not path.exists()

    def test_now_is_injectable(self, root: Path) -> None:
        path = wr.allocate_work("k")

        removed = wr.sweep_work_root(now=time.time() + wr.IDLE_GRACE_SECONDS + 60)

        assert removed == 1
        assert not path.exists()

    def test_rejoin_refreshes_the_tree_so_the_next_sweep_keeps_it(self, root: Path) -> None:
        # The cross-run case: a weekly job rejoins a tree idle for a week. Without
        # the refresh in allocate_work the very next wake deletes what it handed back.
        path = wr.allocate_work("k")
        (path / "clone.txt").write_text("a week of work", encoding="utf-8")
        age(path, wr.IDLE_GRACE_SECONDS + 3600)

        rejoined = wr.allocate_work("k")

        assert wr.sweep_work_root() == 0
        assert rejoined.is_dir()
        assert (rejoined / "clone.txt").read_text(encoding="utf-8") == "a week of work"

    @_POSIX_ONLY
    def test_a_held_lock_defers_the_entry(self, root: Path) -> None:
        # flock conflicts between open file descriptions, so a second handle in
        # this process is a faithful stand-in for a rejoin in another one.
        path = wr.allocate_work("k")
        age(path, wr.IDLE_GRACE_SECONDS + 60)
        held = os.open(root / f".k{wr._LOCK_SUFFIX}", os.O_RDWR)
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)

            assert wr.sweep_work_root() == 0
            assert path.is_dir()
        finally:
            os.close(held)

    def test_the_lock_file_survives_its_entry(self, root: Path) -> None:
        # Surviving is what makes it a usable lock -- deleting a file another
        # process may hold is the race it exists to prevent -- and it is the same
        # property that disqualifies it as allocation evidence, since it attests a
        # key that was once allocated rather than the directory now at that name.
        path = wr.allocate_work("k")
        lock_file = root / f".k{wr._LOCK_SUFFIX}"
        age(path, wr.IDLE_GRACE_SECONDS + 60)

        assert wr.sweep_work_root() == 1
        assert not path.exists()
        assert lock_file.is_file()

    @_POSIX_ONLY
    def test_a_linked_lock_file_defers_the_entry(self, root: Path) -> None:
        elsewhere = root.parent / "elsewhere.lock"
        elsewhere.write_text("", encoding="utf-8")
        path = wr.allocate_work("k")
        real_lock = root / f".k{wr._LOCK_SUFFIX}"
        real_lock.unlink()
        real_lock.symlink_to(elsewhere)
        age(path, wr.IDLE_GRACE_SECONDS + 60)

        assert wr.sweep_work_root() == 0
        assert path.is_dir()


class TestSandboxMask:
    def test_the_root_is_masked_from_sandboxed_agents(self) -> None:
        # Pins the name the module owns to the name the sandbox hides, so a
        # rename on one side cannot quietly unmask the root on the other.
        from kiro_crew import sandbox

        assert wr.WORK_DIRNAME in sandbox._CREW_HIDDEN_LEAVES

    def test_the_root_is_precreated_so_the_mask_is_not_vacuous(self) -> None:
        # The root is created lazily by the first allocate_work call. The mask
        # loop is isdir-guarded, so an absent leaf gets no bind at all and every
        # sandbox spawned before the first allocation would see the real path.
        from kiro_crew import sandbox

        assert wr.WORK_DIRNAME in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES

    def test_the_root_is_fenced_from_file_tools(self) -> None:
        # The mask covers a spawned subprocess; this covers the agent's own file
        # tools. Keys are guessable by design, and allocate_work REJOINS whatever
        # sits at one, so a plantable root is adopted as a job's prior state.
        from kiro_crew.security import paths

        assert wr.WORK_DIRNAME in paths._CREW_SECRET_LEAVES

    def test_the_mask_is_given_no_window(self) -> None:
        # The difference from the scratch root beside it. On Linux the mask is a
        # writable empty tmpfs bind, so an in-sandbox allocation would SUCCEED and
        # hand back a directory whose bytes vanish with the namespace -- the exact
        # pathology this module exists to remove, reached through this module. A
        # sandboxed consumer is served one already-allocated key directory by
        # whatever spawns it, so no entry here may lift the mask over the root.
        from kiro_crew import sandbox

        assert wr.WORK_DIRNAME not in sandbox._CREW_SANDBOX_VISIBLE_LEAVES
        assert wr.WORK_DIRNAME not in sandbox._CREW_READONLY_LEAVES
        for owned in sandbox._APP_BACKEND_OWNED_LEAVES.values():
            assert wr.WORK_DIRNAME not in owned

    def test_no_environment_variable_names_the_root(self) -> None:
        # The distinction this module draws is per-process residue versus
        # cross-process state; a variable beside KIROCREW_SCRATCH collapses it.
        from kiro_crew import agent_scratch

        env = agent_scratch.scratch_env(Path("/somewhere"))
        assert not any("WORK" in name for name in env)

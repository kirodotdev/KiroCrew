"""The owner marker must be installed inside the scratch directory it names,
never through a link planted where that marker belongs.

A spawned agent OWNS its scratch directory: ``agent_scratch.scratch_env`` hands
that path to the child as ``TMPDIR``/``KIROCREW_SCRATCH``. The gateway that
records the child's pid in ``.owner`` there runs UNSANDBOXED, and it recorded it
with ``Path.write_text``, which opens ``O_WRONLY|O_CREAT|O_TRUNC`` by name with
no ``lstat`` and no ``O_NOFOLLOW``. So the child could replace its own marker
with a link and have the gateway truncate whatever the link named -- including a
file the child's own sandbox seals READONLY. ``mcp_gateway.backend_tmp`` carried
the same code for MCP-server temp dirs, whose ``TMPDIR`` is likewise the
directory holding the marker.

The marker is now link-checked with
``platform_compat.is_link_or_junction`` (which, unlike ``os.path.islink``, also
sees a Windows directory junction) and installed with ``atomic_write``, whose
``O_EXCL`` temp plus ``os.replace`` does not follow the final component on any
platform. The refusal is reported so the spawners can reap a child that tried.

Every case builds its own fake data home under ``tmp_path``; the real
``<data home>/scratch`` and ``<data home>/run`` are never touched, no gateway is
contacted, and no protection on the host is weakened.
"""

from __future__ import annotations

import ast
import errno
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import get_args

import pytest

from kiro_crew import agent_scratch as sc
from kiro_crew import platform_compat
from kiro_crew.mcp_gateway import backend_tmp as bt

#: Only for a case that links a FILE. A Windows directory JUNCTION needs no
#: privilege and every DIRECTORY-link case uses one via
#: ``platform_compat.symlink_or_junction``, so those run everywhere; a junction
#: cannot point at a file, and ``os.symlink`` to one needs
#: SeCreateSymbolicLinkPrivilege, which an ordinary Windows user does not hold.
needs_file_symlinks = pytest.mark.skipif(
    sys.platform == "win32", reason="a file symlink needs privilege; a junction cannot replace it"
)

VICTIM_BYTES = "KEEP-ME"
CHILD_PID = 4242


@pytest.fixture
def scratch_root(monkeypatch, tmp_path: Path) -> Path:
    """Point :mod:`kiro_crew.agent_scratch` at a fabricated data home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(sc, "config_dir", lambda: home)
    return home / "scratch"


@pytest.fixture
def backend_tmp_root(monkeypatch, tmp_path: Path) -> Path:
    """Point :mod:`kiro_crew.mcp_gateway.backend_tmp` at a fabricated data home."""
    home = tmp_path / "backend-home"
    home.mkdir()
    monkeypatch.setattr(bt, "config_dir", lambda: home)
    return home / "run" / "mcp-tmp"


def _child_swaps_its_marker(scratch: Path, victim: Path) -> None:
    """Reproduce what a spawned child can do inside its own scratch dir."""
    marker = scratch / sc.OWNER_FILENAME
    marker.unlink()
    marker.symlink_to(victim)


class TestAgentScratchOwnerMarker:
    @needs_file_symlinks
    def test_a_linked_marker_is_refused_and_its_target_survives(self, scratch_root: Path) -> None:
        scratch = sc.allocate_scratch("chat-31")
        victim = scratch_root.parent / "security_policy.json"
        victim.write_text(VICTIM_BYTES)
        _child_swaps_its_marker(scratch, victim)

        assert sc.record_owner(scratch, CHILD_PID) == "refused"
        assert victim.read_text() == VICTIM_BYTES

    def test_a_linked_scratch_dir_is_refused_before_the_marker_is_resolved(
        self, scratch_root: Path, tmp_path: Path
    ) -> None:
        """The DIRECTORY is the other half: every component has to be judged.

        A marker path is only as anchored as the directory it hangs off, so a
        link one level up redirects the write just as well as one at the leaf.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        scratch_root.mkdir(parents=True)
        linked = scratch_root / "chat-31-deadbeef"
        platform_compat.symlink_or_junction(outside, linked)

        assert sc.record_owner(linked, CHILD_PID) == "refused"
        assert list(outside.iterdir()) == []

    def test_the_normal_path_still_records_the_owner(self, scratch_root: Path) -> None:
        """The positive control: a real directory still gets a real marker."""
        scratch = sc.allocate_scratch("chat-31")

        assert sc.record_owner(scratch, CHILD_PID) == "recorded"
        marker = scratch / sc.OWNER_FILENAME
        assert not marker.is_symlink()
        assert marker.read_text(encoding="utf-8").strip() == str(CHILD_PID)

    def test_the_sweep_reads_back_what_was_recorded(self, scratch_root: Path) -> None:
        """The marker exists for the sweep, so the sweep must still parse it.

        Installing by rename replaces the inode the marker names, which is
        exactly the operation a reader holding the old path would miss. The
        sweep reads by name on every pass, so it sees the new one -- asserted
        rather than assumed, because a marker the sweep cannot read makes the
        directory unreclaimable.
        """
        scratch = sc.allocate_scratch("chat-31")
        sc.record_owner(scratch, CHILD_PID)

        recorded = int((scratch / sc.OWNER_FILENAME).read_text(encoding="utf-8").strip())
        assert recorded == CHILD_PID

    def test_an_unwritable_marker_still_fails_open(self, monkeypatch, scratch_root: Path) -> None:
        """A write that could not HAPPEN must not read as one that was refused.

        Scratch is hygiene: an unowned dir is covered by the grace-window rule,
        so a full disk has always left the agent running. Only ``"refused"``
        fails a spawn, so ENOSPC must keep answering something else.
        """
        scratch = sc.allocate_scratch("chat-31")

        def _boom(*args: object, **kwargs: object) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(sc, "atomic_write", _boom)

        assert sc.record_owner(scratch, CHILD_PID) == "unwritable"

    def test_a_vanished_scratch_dir_is_not_recreated(self, scratch_root: Path) -> None:
        """The install must not build back a directory nothing allocated.

        ``atomic_write`` does ``mkdir(parents=True, exist_ok=True)`` on the
        parent, which ``Path.write_text`` never did. Left unguarded that
        resurrects a gone allocation -- and under the managed root's TARGET if
        that root had become a link since allocation checked it. It is not an
        attack signal, so it answers ``"unwritable"`` rather than failing a
        spawn.
        """
        scratch = sc.allocate_scratch("chat-31")
        shutil.rmtree(scratch)

        assert sc.record_owner(scratch, CHILD_PID) == "unwritable"
        assert not scratch.exists()

    def test_the_refusal_is_decided_by_the_junction_aware_probe(
        self, monkeypatch, scratch_root: Path
    ) -> None:
        """Runs on every platform, including the one that needs it most.

        ``os.path.islink`` answers False for a Windows directory junction, so an
        ``islink``-only guard would leave the one platform without
        ``O_NOFOLLOW`` following exactly the link the other two refuse. Forcing
        ``is_link_or_junction`` to answer True proves that probe is the one the
        refusal is read off, without needing the privilege to create a link.
        """
        scratch = sc.allocate_scratch("chat-31")
        monkeypatch.setattr(platform_compat, "is_link_or_junction", lambda _path: True)

        assert sc.record_owner(scratch, CHILD_PID) == "refused"


class TestManagedRootIsARealDirectory:
    def test_allocation_refuses_a_linked_managed_root(
        self, scratch_root: Path, tmp_path: Path
    ) -> None:
        """``mkdir(parents=True, exist_ok=True)`` succeeds on a link to a dir.

        Without this check the root link relocates every agent's ``TMPDIR`` and,
        worse, the target of the sweep's ``shutil.rmtree``. Allocation is
        hygiene rather than a spawn prerequisite, so the spawners degrade to
        inherited temp on the refusal instead of failing.
        """
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        scratch_root.parent.mkdir(parents=True, exist_ok=True)
        platform_compat.symlink_or_junction(elsewhere, scratch_root)

        with pytest.raises(sc.ScratchBoundaryError):
            sc.allocate_scratch("chat-31")
        assert list(elsewhere.iterdir()) == []

    def test_the_sweep_skips_a_linked_managed_root(
        self, scratch_root: Path, tmp_path: Path
    ) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "keep").mkdir()
        scratch_root.parent.mkdir(parents=True, exist_ok=True)
        platform_compat.symlink_or_junction(elsewhere, scratch_root)

        assert sc.sweep_dead_scratch() == 0
        assert (elsewhere / "keep").exists()

    def test_a_real_root_still_allocates(self, scratch_root: Path) -> None:
        """The positive control for the root check."""
        path = sc.allocate_scratch("chat-31")

        assert path.parent == scratch_root
        assert path.is_dir()
        assert (path / sc.OWNER_FILENAME).is_file()


class TestBackendTmpOwnerMarker:
    """The twin, whose ``TMPDIR`` is likewise the dir holding the marker.

    It answers a refusal by declining the write and returning: ``spawn_backend``
    records the owner after the process is live with no guard that reaps it, so
    raising would leak the very process the marker exists to track.
    """

    @needs_file_symlinks
    def test_a_linked_marker_is_refused_and_its_target_survives(
        self, backend_tmp_root: Path
    ) -> None:
        path = bt.allocate_backend_tmp("b" * 64)
        victim = backend_tmp_root.parent / "denied_commands.json"
        victim.write_text(VICTIM_BYTES)
        marker = path / bt.OWNER_FILENAME
        marker.unlink()
        marker.symlink_to(victim)

        bt.record_owner(path, CHILD_PID)

        assert victim.read_text() == VICTIM_BYTES

    def test_a_linked_dir_is_refused(self, backend_tmp_root: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside-backend"
        outside.mkdir()
        backend_tmp_root.mkdir(parents=True)
        linked = backend_tmp_root / "cccccccccccc-deadbeef"
        platform_compat.symlink_or_junction(outside, linked)

        bt.record_owner(linked, CHILD_PID)

        assert list(outside.iterdir()) == []

    def test_a_vanished_dir_is_not_recreated(self, backend_tmp_root: Path) -> None:
        """The twin declines an absent directory for the same reason."""
        path = bt.allocate_backend_tmp("b" * 64)
        shutil.rmtree(path)

        bt.record_owner(path, CHILD_PID)

        assert not path.exists()

    def test_the_normal_path_still_records_the_owner(self, backend_tmp_root: Path) -> None:
        """The positive control."""
        path = bt.allocate_backend_tmp("b" * 64)

        bt.record_owner(path, CHILD_PID)

        marker = path / bt.OWNER_FILENAME
        assert not marker.is_symlink()
        assert marker.read_text(encoding="utf-8").strip() == str(CHILD_PID)


class TestAFailedOwnerUpdateLeavesNoStaleOwner:
    """A marker that could not be UPDATED must not keep naming the gateway.

    ``atomic_write`` stages into a temp and renames it on, so a write that fails
    leaves the PREVIOUS bytes intact -- and on the update path those bytes are
    the provisional pid of the SPAWNING GATEWAY. The gateway exits before the
    child it spawned (``start_new_session=True``), so a kept stale marker reads
    as a dead owner while the real owner is still alive, and the next boot sweep
    deletes a live agent's temp dir. The truncating write this fix replaced left
    an EMPTY marker there instead, which the sweep read as garbled and kept, so
    discarding the marker restores that protection without restoring the
    truncation. Both twins do it; both are asserted, each against a control that
    shows the sweep really would have deleted the directory.
    """

    @staticmethod
    def _enospc(*args: object, **kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    @staticmethod
    def _eacces(*args: object, **kwargs: object) -> None:
        """A refused unlink -- what Windows answers for a file held open."""
        raise OSError(errno.EACCES, "Permission denied")

    @staticmethod
    def _age(path: Path, seconds: float) -> None:
        """Backdate *path* and every entry under it by *seconds*.

        The idle test reads the tree's NEWEST mtime, so every entry has to move;
        the sweep that takes no ``now`` cannot be reached any other way.
        """
        stamp = time.time() - seconds
        for target in [path, *path.rglob("*")]:
            os.utime(target, (stamp, stamp))

    def test_the_scratch_marker_is_discarded(self, monkeypatch, scratch_root: Path) -> None:
        """The dir survives; only its stale owner goes."""
        scratch = sc.allocate_scratch("chat-31")
        assert (scratch / sc.OWNER_FILENAME).exists()  # the provisional gateway pid
        monkeypatch.setattr(sc, "atomic_write", self._enospc)

        assert sc.record_owner(scratch, CHILD_PID) == "unwritable"

        assert not (scratch / sc.OWNER_FILENAME).exists()
        assert scratch.is_dir()

    def test_the_scratch_sweep_keeps_the_now_unowned_dir(
        self, monkeypatch, scratch_root: Path
    ) -> None:
        """Unowned is the state the sweep refuses to delete on."""
        scratch = sc.allocate_scratch("chat-31")
        monkeypatch.setattr(sc, "atomic_write", self._enospc)
        sc.record_owner(scratch, CHILD_PID)
        monkeypatch.setattr(sc, "_pgroup_alive", lambda _pid: False)
        past_the_grace_window = time.time() + sc._UNOWNED_GRACE_SECONDS * 2

        assert sc.sweep_dead_scratch(now=past_the_grace_window) == 0
        assert scratch.is_dir()

    def test_the_scratch_sweep_deletes_a_dir_the_stale_marker_would_have_kept(
        self, monkeypatch, scratch_root: Path
    ) -> None:
        """The control: the same aged dir IS deleted while a dead owner is named.

        Without it the test above passes on a sweep that deletes nothing at all,
        which is the shape a broken idle or liveness probe takes.
        """
        scratch = sc.allocate_scratch("chat-31")
        monkeypatch.setattr(sc, "_pgroup_alive", lambda _pid: False)
        past_the_grace_window = time.time() + sc._UNOWNED_GRACE_SECONDS * 2

        assert sc.sweep_dead_scratch(now=past_the_grace_window) == 1
        assert not scratch.exists()

    def test_the_backend_marker_is_discarded(self, monkeypatch, backend_tmp_root: Path) -> None:
        """The twin, whose caller cannot absorb an exception, does it too."""
        path = bt.allocate_backend_tmp("b" * 64)
        assert (path / bt.OWNER_FILENAME).exists()
        monkeypatch.setattr(bt, "atomic_write", self._enospc)

        bt.record_owner(path, CHILD_PID)

        assert not (path / bt.OWNER_FILENAME).exists()
        assert path.is_dir()

    def test_the_backend_sweep_keeps_the_now_unowned_dir(
        self, monkeypatch, backend_tmp_root: Path
    ) -> None:
        """A surviving backend's temp dir outlives the gateway that spawned it."""
        path = bt.allocate_backend_tmp("b" * 64)
        monkeypatch.setattr(bt, "atomic_write", self._enospc)
        bt.record_owner(path, CHILD_PID)
        monkeypatch.setattr(bt, "_pgroup_alive", lambda _pid: False)
        self._age(path, bt._UNOWNED_GRACE_SECONDS * 2)

        assert bt.sweep_all_backend_tmp() == 0
        assert path.is_dir()

    def test_the_backend_sweep_deletes_a_dir_the_stale_marker_would_have_kept(
        self, monkeypatch, backend_tmp_root: Path
    ) -> None:
        """The twin's control."""
        path = bt.allocate_backend_tmp("b" * 64)
        monkeypatch.setattr(bt, "_pgroup_alive", lambda _pid: False)
        self._age(path, bt._UNOWNED_GRACE_SECONDS * 2)

        assert bt.sweep_all_backend_tmp() == 1
        assert not path.exists()

    def test_a_surviving_stale_marker_fails_the_scratch_spawn(
        self, monkeypatch, scratch_root: Path
    ) -> None:
        """A discard that could not clear the marker is not hygiene lost.

        Windows refuses to unlink a file another process holds open, so both the
        update and the discard can fail on the same marker -- and what is left
        names the GATEWAY over a live child. ``"unwritable"`` would let the spawn
        continue into exactly the state the discard exists to prevent, so this
        answers ``"stale"`` and the spawners reap on it.
        """
        scratch = sc.allocate_scratch("chat-31")
        monkeypatch.setattr(sc, "atomic_write", self._enospc)
        monkeypatch.setattr(sc.os, "unlink", self._eacces)

        assert sc.record_owner(scratch, CHILD_PID) == "stale"
        assert (scratch / sc.OWNER_FILENAME).exists()

    def test_a_cleared_marker_is_still_only_unwritable(
        self, monkeypatch, scratch_root: Path
    ) -> None:
        """The control for the outcome split: no spawn fails on a full disk.

        Without it the case above passes on a ``record_owner`` that answers
        ``"stale"`` for every failed write, which would reap an agent whose
        marker was cleaned up correctly.
        """
        scratch = sc.allocate_scratch("chat-31")
        monkeypatch.setattr(sc, "atomic_write", self._enospc)

        assert sc.record_owner(scratch, CHILD_PID) == "unwritable"

    def test_a_surviving_stale_marker_is_reported_by_the_backend_twin(
        self, monkeypatch, caplog, backend_tmp_root: Path
    ) -> None:
        """The twin reports it and keeps running, which is all it can do.

        ``spawn_backend`` records the owner after the process is live with no
        guard that reaps it, so raising here would leak the very process the
        marker tracks. The warning is therefore the observable, and it is what a
        host operator has to go on.
        """
        path = bt.allocate_backend_tmp("b" * 64)
        monkeypatch.setattr(bt, "atomic_write", self._enospc)
        monkeypatch.setattr(bt.os, "unlink", self._eacces)

        with caplog.at_level(logging.WARNING, logger=bt.logger.name):
            bt.record_owner(path, CHILD_PID)

        assert (path / bt.OWNER_FILENAME).exists()
        assert any("still names this gateway" in record.message for record in caplog.records)


class TestTheSweepNeverJudgesAnOwnerThroughALink:
    """The marker is READ by the one loop that deletes, so the read is a boundary.

    ``record_owner`` declines to WRITE through a linked marker, but declining a
    write does not remove the link -- and on the MCP surface nothing reaps the
    process that planted it. Both sweeps then read the marker by name, and
    ``read_text`` FOLLOWS a link, so the subject of the judgement can choose the
    bytes the judgement is made on: aim ``.owner`` at any file holding a dead pid
    and the sweep deletes the live process's own tree. A linked marker therefore
    joins the unowned-and-garbled rule -- never delete on evidence the subject
    controls -- which leaves the directory unreclaimable while the link stands,
    the safe direction to fail.
    """

    @staticmethod
    def _aged(path: Path, seconds: float) -> None:
        stamp = time.time() - seconds
        for target in [path, *path.rglob("*")]:
            os.utime(target, (stamp, stamp), follow_symlinks=False)

    @needs_file_symlinks
    def test_the_scratch_sweep_keeps_a_dir_whose_marker_is_a_link(
        self, monkeypatch, scratch_root: Path, tmp_path: Path
    ) -> None:
        dead_pid_bytes = tmp_path / "dead-pid"
        dead_pid_bytes.write_text("999999", encoding="utf-8")
        scratch = sc.allocate_scratch("chat-31")
        _child_swaps_its_marker(scratch, dead_pid_bytes)
        monkeypatch.setattr(sc, "_pgroup_alive", lambda _pid: False)
        past_the_grace_window = time.time() + sc._UNOWNED_GRACE_SECONDS * 2

        assert sc.sweep_dead_scratch(now=past_the_grace_window) == 0
        assert scratch.is_dir()

    @needs_file_symlinks
    def test_the_backend_sweep_keeps_a_dir_whose_marker_is_a_link(
        self, monkeypatch, backend_tmp_root: Path, tmp_path: Path
    ) -> None:
        """The surface where it matters most: nothing reaped the planter."""
        dead_pid_bytes = tmp_path / "dead-pid"
        dead_pid_bytes.write_text("999999", encoding="utf-8")
        path = bt.allocate_backend_tmp("b" * 64)
        marker = path / bt.OWNER_FILENAME
        marker.unlink()
        marker.symlink_to(dead_pid_bytes)
        monkeypatch.setattr(bt, "_pgroup_alive", lambda _pid: False)
        self._aged(path, bt._UNOWNED_GRACE_SECONDS * 2)

        assert bt.sweep_all_backend_tmp() == 0
        assert path.is_dir()

    @needs_file_symlinks
    def test_the_same_bytes_in_a_real_marker_are_still_swept(
        self, monkeypatch, backend_tmp_root: Path, tmp_path: Path
    ) -> None:
        """The control: the refusal is the LINK, not the pid it happens to name.

        Without it both cases above pass on a sweep that cannot delete anything
        at this age, and the guard would be indistinguishable from a broken idle
        or liveness probe.
        """
        path = bt.allocate_backend_tmp("b" * 64)
        (path / bt.OWNER_FILENAME).write_text("999999", encoding="utf-8")
        monkeypatch.setattr(bt, "_pgroup_alive", lambda _pid: False)
        self._aged(path, bt._UNOWNED_GRACE_SECONDS * 2)

        assert bt.sweep_all_backend_tmp() == 1
        assert not path.exists()

    def test_the_refusal_is_decided_by_the_junction_aware_probe(
        self, monkeypatch, scratch_root: Path
    ) -> None:
        """Runs on Windows, where the marker could be a junction rather than a link.

        The predicate answers True for the MARKER only. A blanket True would be
        satisfied by the sweep's managed-root check, which runs before any child
        is looked at, and the assertion would then hold with the guard under test
        deleted -- it did, until a mutation run caught it.
        """
        scratch = sc.allocate_scratch("chat-31")
        marker = scratch / sc.OWNER_FILENAME
        monkeypatch.setattr(sc, "_pgroup_alive", lambda _pid: False)
        monkeypatch.setattr(
            platform_compat, "is_link_or_junction", lambda path: Path(path) == marker
        )
        past_the_grace_window = time.time() + sc._UNOWNED_GRACE_SECONDS * 2

        assert sc.sweep_dead_scratch(now=past_the_grace_window) == 0
        assert scratch.is_dir()


#: The two spawn chokepoints that must reap a child which planted a link.
SPAWNERS = (
    Path("src/kiro_crew/acp/client.py"),
    Path("src/kiro_crew/acp/runtime.py"),
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


#: The outcomes a spawn must NOT continue past. Both are reported rather than
#: raised, so the fail-closed half of each lives at the call site.
FATAL_OUTCOMES = ("refused", "stale")


def _outcome_branches(source: str, outcome: str) -> list[ast.If]:
    """Every ``if <expr> == outcome:`` statement in *source*.

    Structural rather than textual: the invariant is "the outcome is branched on
    and the branch raises", and a text scan would pass on the two halves sitting
    in unrelated functions.
    """
    found: list[ast.If] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        comparators = node.test.comparators
        if len(comparators) != 1:
            continue
        right = comparators[0]
        if isinstance(right, ast.Constant) and right.value == outcome:
            found.append(node)
    return found


def test_the_fatal_outcomes_are_the_ones_record_owner_can_report() -> None:
    """Pin the list against the source of truth, so a new outcome is not missed.

    Without this the ratchet below silently stops covering a fifth outcome that
    a later change adds to ``OwnerOutcome`` and branches on in the spawners.
    """
    declared = set(get_args(sc.OwnerOutcome))

    assert set(FATAL_OUTCOMES) <= declared
    assert declared - set(FATAL_OUTCOMES) == {"recorded", "unwritable"}


@pytest.mark.parametrize("outcome", FATAL_OUTCOMES)
@pytest.mark.parametrize("spawner", SPAWNERS, ids=lambda path: path.name)
def test_both_spawners_fail_closed_on_a_fatal_owner_outcome(spawner: Path, outcome: str) -> None:
    """Each fatal outcome has to stop the spawn, not merely skip one write.

    ``record_owner`` reports rather than raises, so the fail-closed half lives
    at the call site. Both chokepoints record the owner inside a guard that
    reaps a live child on any exception, so raising there is what reaps it --
    and a branch that stopped raising would leave the child running with its
    attempt behind it and nothing red.
    """
    source = (_repo_root() / spawner).read_text(encoding="utf-8")
    branches = _outcome_branches(source, outcome)

    assert branches, f"{spawner} does not branch on a {outcome!r} owner marker"
    assert any(
        any(isinstance(inner, ast.Raise) for inner in ast.walk(branch)) for branch in branches
    ), f"{spawner} branches on a {outcome!r} owner marker without failing the spawn"

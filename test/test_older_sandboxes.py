"""Which live sandboxes mask one crew-home path but not another (``older_sandboxes``).

The Linux answer is read from each process's ``mountinfo``, so these tests build a
fixture ``/proc`` and hand it in through ``proc_root``; the real-namespace case lives in
``test_md_notebook_real_sandbox.py``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from kiro_crew import older_sandboxes
from kiro_crew.platform_compat import DarwinKinfoProc

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process table")

MASKED = "md-notebook-staging"
UNCOVERED = "apps/md-notebook/data/state"


def _mountinfo(*points: str) -> str:
    return "".join(
        f"{n} 1 0:1 / {p.replace(' ', chr(92) + '040')} rw - tmpfs none rw\n"
        for n, p in enumerate(points, start=100)
    )


def _process(
    proc: Path, pid: int, *points: str, ns: str | None = None, uid: int | None = None
) -> Path:
    base = proc / str(pid)
    (base / "ns").mkdir(parents=True)
    (base / "ns" / "mnt").write_text(ns or f"mnt-{pid}")
    (base / "mountinfo").write_text(_mountinfo("/", *points))
    real = os.getuid() if uid is None else uid
    (base / "status").write_text(f"Name:\tpython3\nUid:\t{real}\t{real}\t{real}\t{real}\n")
    (base / "stat").write_text(
        f"{pid} (python3) S 1 {pid} {pid} 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 {1000 + pid} 0 0\n"
    )
    return base


@pytest.fixture
def proc(tmp_path: Path) -> Path:
    root = tmp_path / "proc"
    root.mkdir()
    # This process: the gateway's own mount namespace.
    _process(root, os.getpid())
    return root


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = (tmp_path / "data home").resolve()
    home.mkdir()
    return home


def _scan(home: Path, proc: Path) -> list[int] | None:
    return older_sandboxes.sandboxes_masking_only(home, MASKED, UNCOVERED, proc_root=proc)


def test_a_sandbox_that_masks_only_the_old_dir_is_named(home, proc):
    _process(proc, 4001, f"{home}/{MASKED}")
    assert _scan(home, proc) == [4001]


def test_a_sandbox_that_masks_both_is_not(home, proc):
    _process(proc, 4002, f"{home}/{MASKED}", f"{home}/{UNCOVERED}")
    assert _scan(home, proc) == []


def test_each_home_spelling_is_judged_on_its_own(home, proc, tmp_path):
    """A mask at the new path under ONE spelling does not cover the other spelling."""
    alias = tmp_path / "alias"
    alias.symlink_to(home)
    _process(proc, 4003, f"{home}/{MASKED}", f"{home}/{UNCOVERED}", f"{alias}/{MASKED}")
    assert _scan(home, proc) == [4003]


def test_another_users_process_is_not_read(home, proc):
    _process(proc, 4009, f"{home}/{MASKED}", uid=os.getuid() + 1)
    assert _scan(home, proc) == []


def test_another_data_home_and_an_unconfined_process_are_not(home, proc, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    _process(proc, 4004, f"{other}/{MASKED}")
    _process(proc, 4005)
    assert _scan(home, proc) == []


def test_a_process_in_this_mount_namespace_is_not_read(home, proc):
    """Same namespace as the reader means the same (unmasked) view."""
    own = (proc / str(os.getpid()) / "ns" / "mnt").read_text()
    _process(proc, 4006, f"{home}/{MASKED}", ns=own)
    # A fixture file stands in for the ns link, so equal content is not equal inodes;
    # point the second process at the very same file instead.
    link = proc / "4006" / "ns" / "mnt"
    link.unlink()
    link.symlink_to(proc / str(os.getpid()) / "ns" / "mnt")
    assert _scan(home, proc) == []


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads anything")
def test_a_process_whose_mounts_cannot_be_read_is_named(home, proc):
    base = _process(proc, 4007)
    (base / "mountinfo").chmod(0)
    try:
        assert _scan(home, proc) == [4007]
    finally:
        (base / "mountinfo").chmod(0o600)


def test_a_process_that_exited_mid_scan_is_not(home, proc):
    base = _process(proc, 4008)
    (base / "mountinfo").unlink()
    assert _scan(home, proc) == []


def _exit_leader(base: Path, *thread_points: str) -> None:
    """The leader exits while a second thread runs on: only that thread has a view."""
    (base / "mountinfo").unlink()
    (base / "ns" / "mnt").unlink()
    thread = base / "task" / "9999"
    (thread / "ns").mkdir(parents=True)
    (thread / "ns" / "mnt").write_text("mnt-thread")
    (thread / "mountinfo").write_text(_mountinfo("/", *thread_points))


def test_a_live_thread_of_an_exited_leader_is_judged(home, proc):
    _exit_leader(_process(proc, 4011), f"{home}/{MASKED}")
    _exit_leader(_process(proc, 4012), f"{home}/{MASKED}", f"{home}/{UNCOVERED}")
    assert _scan(home, proc) == [4011]


def test_an_exited_leader_with_no_live_thread_is_not_named(home, proc):
    base = _process(proc, 4013)
    (base / "mountinfo").unlink()
    (base / "task").mkdir()
    assert _scan(home, proc) == []


def test_an_unreadable_process_table_answers_none(home, tmp_path):
    assert _scan(home, tmp_path / "no-proc") is None


def test_an_entry_whose_stat_cannot_be_read_is_another_users(home, proc):
    """Under ``hidepid`` another user's ``/proc/<pid>`` is listed but cannot be read."""
    (proc / "4010").mkdir()
    assert _scan(home, proc) == []


def _table(*passes: dict[int, object]):
    """A snapshot callable that answers *passes* in order, then repeats the last."""
    taken: list[int] = []

    def snapshot():
        taken.append(len(taken))
        return passes[min(len(taken) - 1, len(passes) - 1)]

    return snapshot, taken


def test_a_process_that_exits_before_it_is_judged_leaves_its_child_to_the_next_pass():
    """The older sandbox forks and exits between the listing and its judgment."""
    snapshot, taken = _table({10: ("t10", False)}, {11: ("t11", False)})
    assert older_sandboxes._settled(snapshot, {10: None, 11: True}.get) == [11]
    assert len(taken) == 2


def test_a_child_the_first_snapshot_never_listed_is_judged_in_the_next():
    """Its parent forked it and went while the first snapshot was being read."""
    snapshot, taken = _table({}, {12: ("t12", False)})
    assert older_sandboxes._settled(snapshot, {12: True}.get) == [12]
    assert len(taken) == 2


def test_a_table_that_never_holds_still_answers_none():
    def snapshot():
        snapshot.pid += 1
        return {snapshot.pid: ("t", False)}

    # Fixture pids only, never signalled; above every pid_max all the same.
    first = 2**33
    snapshot.pid = first
    assert older_sandboxes._settled(snapshot, lambda pid: None) is None
    assert snapshot.pid == first + older_sandboxes._PASSES


def test_a_process_that_cannot_be_judged_but_stays_put_settles():
    """Another user's process, or a zombie that was a zombie already."""
    snapshot, taken = _table({20: ("t20", False), 21: ("t21", True), 22: ("t22", False)})
    judged: list[int] = []

    def judge(pid):
        judged.append(pid)
        return {20: None, 21: None, 22: False}[pid]

    assert older_sandboxes._settled(snapshot, judge) == []
    assert len(taken) == 2
    assert sorted(judged) == [20, 21, 22], "each identity is judged once"


@pytest.mark.parametrize(
    "second",
    [{}, {30: ("t30", True)}, {30: ("t30-recycled", False)}],
    ids=["gone", "now-a-zombie", "pid-reused"],
)
def test_a_process_that_changed_since_the_listing_takes_another_pass(second):
    snapshot, taken = _table({30: ("t30", False)}, second)
    assert older_sandboxes._settled(snapshot, lambda pid: None) == []
    assert len(taken) == 3


def test_a_new_process_that_cannot_be_judged_keeps_the_scan_open():
    """It may have gone after forking a child no snapshot has listed yet."""
    first = {40: ("t40", False)}
    second = {40: ("t40", False), 41: ("t41", False)}
    snapshot, taken = _table(first, second)
    assert older_sandboxes._settled(snapshot, {40: False, 41: None}.get) == []
    assert len(taken) == 3


def test_an_unreadable_second_snapshot_answers_none():
    answers = iter([{40: ("t40", False)}, None])
    assert older_sandboxes._settled(lambda: next(answers), lambda pid: None) is None


class _FakeDarwin:
    """A macOS process table: per pid, ``(can read MASKED, can read UNCOVERED)``.

    ``sandbox_check`` answers stand in for the kernel; a pid missing from *same_uid*
    is another user's process (or one that is gone), for which ``proc_pidinfo``
    answers nothing.
    """

    def __init__(self, monkeypatch, home: Path, table: dict[int, tuple[bool | None, bool | None]]):
        self.home = home
        self.table = table
        self.same_uid = set(table)
        self.asked: list[tuple[int, Path]] = []
        self.zombies: set[int] = set()
        monkeypatch.setattr(older_sandboxes.platform_compat, "_darwin_kinfo_members", self._members)
        monkeypatch.setattr(older_sandboxes.platform_compat, "darwin_process_facts", self._facts)
        monkeypatch.setattr(
            older_sandboxes.platform_compat, "process_can_read_under_sandbox", self._can_read
        )

    def _members(self, selector: int, arg: int) -> list[DarwinKinfoProc]:
        assert (selector, arg) == (older_sandboxes.platform_compat._DARWIN_KERN_PROC_ALL, 0)
        return [
            DarwinKinfoProc(pid=pid, zombie=pid in self.zombies, start_id="1.000000")
            for pid in [*self.table, os.getpid(), 1]
        ]

    def _facts(self, pid: int) -> object | None:
        return object() if pid in self.same_uid and pid not in self.zombies else None

    def _can_read(self, pid: int, path: Path) -> bool | None:
        self.asked.append((pid, path))
        old, new = self.table[pid]
        if path == self.home / MASKED:
            return old
        assert path == self.home / UNCOVERED, path
        return new

    def scan(self) -> list[int] | None:
        return older_sandboxes._darwin(str(self.home), MASKED, UNCOVERED)


def test_macos_names_a_sandbox_denied_the_old_dir_and_not_the_new(home, monkeypatch):
    table = _FakeDarwin(
        monkeypatch,
        home,
        {
            5001: (False, True),  # an older build's sandbox
            5002: (False, False),  # a sandbox of this build: denied both
            5003: (True, True),  # unconfined
            5004: (True, False),  # may read the old dir already
        },
    )
    assert table.scan() == [5001]
    assert {pid for pid, _ in table.asked} == {5001, 5002, 5003, 5004}, "never itself or pid 1"


def test_macos_an_unknown_answer_holds_the_move_while_the_process_lives(home, monkeypatch):
    table = _FakeDarwin(monkeypatch, home, {5011: (None, None), 5012: (False, None)})
    assert table.scan() == [5011, 5012]
    # Gone by the time its answer came back: nothing left that could read.
    table.same_uid = set()
    assert table.scan() == []


def test_macos_another_users_process_is_not_asked(home, monkeypatch):
    table = _FakeDarwin(monkeypatch, home, {5021: (False, True)})
    table.same_uid = set()
    assert table.scan() == []
    assert table.asked == []


def test_macos_a_zombie_is_not_asked(home, monkeypatch):
    table = _FakeDarwin(monkeypatch, home, {5041: (False, True)})
    table.zombies = {5041}
    assert table.scan() == []
    assert table.asked == []


@pytest.mark.parametrize("answer", [None, []], ids=["unreadable", "empty"])
def test_macos_an_unreadable_process_table_answers_none(home, monkeypatch, answer):
    monkeypatch.setattr(older_sandboxes.platform_compat, "_darwin_kinfo_members", lambda *_: answer)
    assert older_sandboxes._darwin(str(home), MASKED, UNCOVERED) is None


def test_macos_asks_at_the_canonical_spelling_of_the_home(home, monkeypatch, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(home)
    table = _FakeDarwin(monkeypatch, home, {5031: (False, True)})
    monkeypatch.setattr(sys, "platform", "darwin")
    assert older_sandboxes.sandboxes_masking_only(alias, MASKED, UNCOVERED) == [5031]
    assert {path for _, path in table.asked} == {home / MASKED, home / UNCOVERED}

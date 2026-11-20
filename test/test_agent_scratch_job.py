"""A script cron's background process keeps its temp files on Windows.

``agent_scratch.sweep_dead_scratch`` keeps a tree for a live process outside the
owner's groups on Linux (``agent_scratch_users``, read from ``/proc``). On
Windows a script cron's run would be judged by the recorded script pid alone,
so a background process the script started with the run's temp dir as its
working directory would lose every file it does not hold open an hour after
the script returned. The run's script child joins a Job object the gateway
keeps (``agent_scratch_job.join``), and the sweep reads that job's member list.

The sweep tests here run on every host: they stand fakes in for the Win32 read
(``agent_scratch_job.job_pids``), the handle close and the platform check, and
delete real files. The last test drives a real Job object on Windows only.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import agent_scratch as sc
from kiro_crew import agent_scratch_job, cron_script_child_env, platform_compat
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing.ids import unallocatable_pids
from kiro_crew.testing.wait import default_timeout, wait_until

#: Past the grace window, so every tree reads as idle without ageing its files.
_LATER = 2 * sc._UNOWNED_GRACE_SECONDS

#: An owner pid that almost surely names no process.
_DEAD_OWNER = 2**22 - 1

#: A member pid and two child pids; none can name a real process.
_MEMBER, _CHILD, _FROZEN = unallocatable_pids(3)


@pytest.fixture
def scratch_root(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(sc, "config_dir", lambda: home)
    return home / "scratch"


@dataclass
class _FakeJobs:
    """Fake job handles, the members a Windows host would report for each, and closes."""

    members: dict[int, list[int] | None] = field(default_factory=dict)
    closed: list[int] = field(default_factory=list)

    def hold(self, tree: Path, handle: int, pids: list[int] | None) -> None:
        agent_scratch_job._JOBS[tree.name] = handle
        self.members[handle] = pids


@pytest.fixture
def jobs(monkeypatch) -> _FakeJobs:
    """The sweep reads the fakes as it reads real jobs on Windows."""
    fake = _FakeJobs()
    monkeypatch.setattr(agent_scratch_job, "_JOBS", {})
    monkeypatch.setattr(
        sc.agent_scratch_users,
        "platform_compat",
        SimpleNamespace(IS_WINDOWS=True, IS_LINUX=False),
    )
    monkeypatch.setattr(agent_scratch_job, "job_pids", lambda job: fake.members[job])
    monkeypatch.setattr(agent_scratch_job, "_close", fake.closed.append)
    return fake


def _dead_tree_with_a_file(label: str) -> Path:
    tree = sc.allocate_scratch(label)
    assert sc.record_owner(tree, _DEAD_OWNER) == "recorded"
    (tree / "saved.txt").write_text("the run's own file\n")
    return tree


def test_a_tree_a_job_member_uses_survives_and_goes_once_the_member_exits(
    scratch_root: Path, jobs: _FakeJobs, caplog
) -> None:
    tree = _dead_tree_with_a_file("cron-job")
    jobs.hold(tree, 101, [_MEMBER])

    with caplog.at_level(logging.WARNING, logger="kiro_crew.agent_scratch"):
        assert sc.sweep_dead_scratch(now=time.time() + _LATER) == 0
    assert (tree / "saved.txt").is_file(), "a live member keeps every file of the tree"
    assert f"keeping {tree.name!r}: its owners are dead, but live pid {_MEMBER} still uses it" in (
        caplog.text
    )
    assert jobs.closed == []

    jobs.members[101] = []
    assert sc.sweep_dead_scratch(now=time.time() + _LATER) == 1
    assert not tree.exists(), "once the member is gone the tree is reclaimed"
    assert jobs.closed == [101], "an empty job's handle is closed"
    assert agent_scratch_job._JOBS == {}


def test_a_dead_idle_tree_with_no_job_is_still_deleted(scratch_root: Path, jobs: _FakeJobs) -> None:
    kept = _dead_tree_with_a_file("cron-kept")
    gone = _dead_tree_with_a_file("cron-gone")
    jobs.hold(kept, 101, [_MEMBER])

    assert sc.sweep_dead_scratch(now=time.time() + _LATER) == 1
    assert kept.is_dir()
    assert not gone.exists()


def test_a_job_that_cannot_be_read_is_no_user_and_is_kept_for_the_next_sweep(
    tmp_path: Path, jobs: _FakeJobs
) -> None:
    (tmp_path / "unread").mkdir()
    jobs.hold(tmp_path / "unread", 101, None)

    assert agent_scratch_job.scratch_users(tmp_path) == {}
    assert agent_scratch_job._JOBS == {"unread": 101}
    assert jobs.closed == []


def test_the_users_are_the_trees_under_the_root_whose_job_has_a_member(
    tmp_path: Path, jobs: _FakeJobs
) -> None:
    (tmp_path / "used").mkdir()
    jobs.hold(tmp_path / "used", 101, [_MEMBER, _CHILD])
    jobs.hold(tmp_path / "elsewhere", 102, [_CHILD])  # not a tree under this root

    assert agent_scratch_job.scratch_users(tmp_path) == {"used": _MEMBER}
    assert set(agent_scratch_job._JOBS) == {"used", "elsewhere"}


@pytest.mark.skipif(sys.platform == "win32", reason="the job calls run for real on Windows")
def test_off_windows_nothing_joins_and_nothing_is_held(monkeypatch) -> None:
    monkeypatch.setattr(agent_scratch_job, "_JOBS", {})
    assert agent_scratch_job.join("tree", SimpleNamespace(pid=_MEMBER)) is False  # type: ignore[arg-type]
    assert agent_scratch_job._JOBS == {}


def test_the_script_child_joins_its_trees_job_once_the_owner_is_recorded(
    scratch_root: Path, monkeypatch
) -> None:
    joined: list[tuple[str, object]] = []
    monkeypatch.setattr(agent_scratch_job, "join", lambda tree, proc: joined.append((tree, proc)))
    tree = sc.allocate_scratch("cron-run")
    child = SimpleNamespace(pid=os.getpid())

    assert cron_script_child_env._record_cron_scratch_owner(tree, child) is True  # type: ignore[arg-type]
    assert joined == [(tree.name, child)]


def test_a_run_whose_owner_could_not_be_recorded_joins_no_job(
    scratch_root: Path, monkeypatch
) -> None:
    joined: list[tuple[str, object]] = []
    monkeypatch.setattr(agent_scratch_job, "join", lambda tree, proc: joined.append((tree, proc)))
    monkeypatch.setattr(sc, "record_owner", lambda directory, pid: "refused")
    tree = sc.allocate_scratch("cron-run")
    child = SimpleNamespace(pid=os.getpid())

    assert cron_script_child_env._record_cron_scratch_owner(tree, child) is False  # type: ignore[arg-type]
    assert joined == []


def test_a_suspended_child_is_resumed_on_windows_and_never_elsewhere(monkeypatch) -> None:
    resumed: list[int] = []

    def resume(pid: int) -> bool:
        resumed.append(pid)
        return pid != _FROZEN

    child = SimpleNamespace(pid=_CHILD)
    monkeypatch.setattr(cron_script_child_env.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(cron_script_child_env.platform_compat, "resume_process_main_thread", resume)
    assert cron_script_child_env._resume_cron_child(child) is True  # type: ignore[arg-type]
    assert resumed == []

    monkeypatch.setattr(cron_script_child_env.platform_compat, "IS_WINDOWS", True)
    assert cron_script_child_env._resume_cron_child(child) is True  # type: ignore[arg-type]
    frozen = SimpleNamespace(pid=_FROZEN)
    assert cron_script_child_env._resume_cron_child(frozen) is False  # type: ignore[arg-type]
    assert resumed == [_CHILD, _FROZEN]


#: The script stand-in. It starts a detached process with the tree as its working
#: directory, prints that process's pid, and returns while it still runs. The
#: background process holds ``held.txt`` open and exits once ``stop`` exists.
_SCRIPT = """
import os, subprocess, sys
tree, stop = sys.argv[1], sys.argv[2]
background = subprocess.Popen(
    [sys.executable, "-I", "-c",
     "import os, sys, time\\n"
     "held = open('held.txt', 'w')\\n"
     "deadline = time.monotonic() + 120\\n"
     "while not os.path.exists(sys.argv[1]) and time.monotonic() < deadline:\\n"
     "    time.sleep(0.05)\\n", stop],
    cwd=tree, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
print(background.pid, flush=True)
"""


@pytest.mark.skipif(sys.platform != "win32", reason="Job objects are Windows only")
def test_a_background_process_of_a_script_run_keeps_the_tree_on_windows(
    scratch_root: Path, tmp_path: Path, monkeypatch, caplog
) -> None:
    """The issue's acceptance criteria 1 and 2, against a real Job object."""
    monkeypatch.setattr(agent_scratch_job, "_JOBS", {})
    tree = sc.allocate_scratch("cron-job")
    (tree / "saved.txt").write_text("the run's own file\n")
    stop = tmp_path / "stop"
    # As the cron runner spawns it: suspended, into the job, then resumed, so no
    # process the script starts can be outside the job.
    script = subprocess.Popen(
        [sys.executable, "-I", "-c", _SCRIPT, str(tree), str(stop)],
        stdout=subprocess.PIPE,
        creationflags=platform_compat.CREATE_SUSPENDED,
        **UTF8_TEXT,
    )
    try:
        assert sc.record_owner(tree, script.pid) == "recorded"
        assert agent_scratch_job.join(tree.name, script)
        assert platform_compat.resume_process_main_thread(script.pid)
        out, _ = script.communicate(timeout=default_timeout())
        background = int(out.strip())
        later = time.time() + _LATER
        members = agent_scratch_job.job_pids(agent_scratch_job._JOBS[tree.name]) or []
        # A venv's python.exe starts the real interpreter, so the job can hold two
        # processes for the one the script started.
        assert background in members

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent_scratch"):
            assert sc.sweep_dead_scratch(now=later) == 0
        assert (tree / "saved.txt").is_file(), "the tree and all its files remain"
        assert any(f"live pid {pid} still uses it" in caplog.text for pid in members)

        stop.write_text("")
        wait_until(lambda: not agent_scratch_job.job_pids(agent_scratch_job._JOBS[tree.name]))
        assert sc.sweep_dead_scratch(now=later) == 1
        assert not tree.exists()
        assert agent_scratch_job._JOBS == {}
    finally:
        stop.write_text("")
        if script.poll() is None:
            script.kill()
            script.wait(timeout=default_timeout())
        for handle in agent_scratch_job._JOBS.values():
            agent_scratch_job._close(handle)

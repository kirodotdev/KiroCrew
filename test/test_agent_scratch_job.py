"""A script cron's background process keeps its temp files on Windows.

``agent_scratch.sweep_dead_scratch`` kept a tree for a live process outside the
owner's groups on Linux only (``agent_scratch_users``, read from ``/proc``). On
Windows a script cron's run was judged by the recorded script pid alone, so a
background process the script started with the run's temp dir as its working
directory lost every file it did not hold open an hour after the script
returned. The run's script child now joins a named Job object
(``agent_scratch_job.join``), and the sweep reads that job's member list.

The sweep tests here run on every host: they stand a fake in for the Win32
read (``agent_scratch_job.job_pids``) and for the platform check, and delete
real files. The last test drives the real Job object and runs on Windows only.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import agent_scratch as sc
from kiro_crew import agent_scratch_job, cron_script_child_env, platform_compat
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing.wait import default_timeout, wait_until

#: Past the grace window, so every tree reads as idle without ageing its files.
_LATER = 2 * sc._UNOWNED_GRACE_SECONDS

#: An owner pid that almost surely names no process.
_DEAD_OWNER = 2**22 - 1


@pytest.fixture
def scratch_root(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(sc, "config_dir", lambda: home)
    return home / "scratch"


@pytest.fixture
def job_members(monkeypatch) -> dict[str, list[int]]:
    """The jobs a Windows host would report, by tree name; the sweep reads them as it does there."""
    members: dict[str, list[int]] = {}
    monkeypatch.setattr(
        sc.agent_scratch_users,
        "platform_compat",
        SimpleNamespace(IS_WINDOWS=True, IS_LINUX=False),
    )
    monkeypatch.setattr(agent_scratch_job, "job_pids", lambda tree: list(members.get(tree, [])))
    return members


def _dead_tree_with_a_file(label: str) -> Path:
    tree = sc.allocate_scratch(label)
    assert sc.record_owner(tree, _DEAD_OWNER) == "recorded"
    (tree / "saved.txt").write_text("the run's own file\n")
    return tree


def test_a_tree_a_job_member_uses_survives_and_goes_once_the_member_exits(
    scratch_root: Path, job_members: dict[str, list[int]], caplog
) -> None:
    tree = _dead_tree_with_a_file("cron-job")
    job_members[tree.name] = [4242]

    with caplog.at_level(logging.WARNING, logger="kiro_crew.agent_scratch"):
        assert sc.sweep_dead_scratch(now=time.time() + _LATER) == 0
    assert (tree / "saved.txt").is_file(), "a live member keeps every file of the tree"
    assert f"keeping {tree.name!r}: its owners are dead, but live pid 4242 still uses it" in (
        caplog.text
    )

    job_members.clear()
    assert sc.sweep_dead_scratch(now=time.time() + _LATER) == 1
    assert not tree.exists(), "once the member is gone the tree is reclaimed"


def test_a_dead_idle_tree_with_no_job_is_still_deleted(
    scratch_root: Path, job_members: dict[str, list[int]]
) -> None:
    kept = _dead_tree_with_a_file("cron-kept")
    gone = _dead_tree_with_a_file("cron-gone")
    job_members[kept.name] = [4242]

    assert sc.sweep_dead_scratch(now=time.time() + _LATER) == 1
    assert kept.is_dir()
    assert not gone.exists()


def test_the_users_are_the_trees_whose_job_has_a_member(
    tmp_path: Path, job_members: dict[str, list[int]]
) -> None:
    (tmp_path / "used").mkdir()
    (tmp_path / "idle").mkdir()
    job_members["used"] = [7, 8]
    job_members["gone-tree"] = [9]  # no such directory: never read

    assert agent_scratch_job.scratch_users(tmp_path) == {"used": 7}
    assert agent_scratch_job.scratch_users(tmp_path / "absent") == {}


@pytest.mark.skipif(sys.platform == "win32", reason="the job calls run for real on Windows")
def test_off_windows_nothing_joins_and_nothing_is_read() -> None:
    assert agent_scratch_job.join("tree", os.getpid()) is False
    assert agent_scratch_job.job_pids("tree") == []


@pytest.mark.parametrize("name", ["", "a\\b", "a/b"])
def test_a_name_with_a_separator_has_no_job(name: str) -> None:
    assert agent_scratch_job.job_name(name) is None
    assert agent_scratch_job.join(name, os.getpid()) is False
    assert agent_scratch_job.job_pids(name) == []


def test_the_script_child_joins_its_trees_job_once_the_owner_is_recorded(
    scratch_root: Path, monkeypatch
) -> None:
    joined: list[tuple[str, int]] = []
    monkeypatch.setattr(agent_scratch_job, "join", lambda tree, pid: joined.append((tree, pid)))
    tree = sc.allocate_scratch("cron-run")
    child = SimpleNamespace(pid=os.getpid())

    assert cron_script_child_env._record_cron_scratch_owner(tree, child) is True  # type: ignore[arg-type]
    assert joined == [(tree.name, os.getpid())]


def test_a_run_whose_owner_could_not_be_recorded_joins_no_job(
    scratch_root: Path, monkeypatch
) -> None:
    joined: list[tuple[str, int]] = []
    monkeypatch.setattr(agent_scratch_job, "join", lambda tree, pid: joined.append((tree, pid)))
    monkeypatch.setattr(sc, "record_owner", lambda directory, pid: "refused")
    tree = sc.allocate_scratch("cron-run")
    child = SimpleNamespace(pid=os.getpid())

    assert cron_script_child_env._record_cron_scratch_owner(tree, child) is False  # type: ignore[arg-type]
    assert joined == []


def test_a_suspended_child_is_resumed_on_windows_and_never_elsewhere(monkeypatch) -> None:
    resumed: list[int] = []

    def resume(pid: int) -> bool:
        resumed.append(pid)
        return pid != 13

    child = SimpleNamespace(pid=11)
    monkeypatch.setattr(cron_script_child_env.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(cron_script_child_env.platform_compat, "resume_process_main_thread", resume)
    assert cron_script_child_env._resume_cron_child(child) is True  # type: ignore[arg-type]
    assert resumed == []

    monkeypatch.setattr(cron_script_child_env.platform_compat, "IS_WINDOWS", True)
    assert cron_script_child_env._resume_cron_child(child) is True  # type: ignore[arg-type]
    assert cron_script_child_env._resume_cron_child(SimpleNamespace(pid=13)) is False  # type: ignore[arg-type]
    assert resumed == [11, 13]


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
    scratch_root: Path, tmp_path: Path, caplog
) -> None:
    """The issue's acceptance criteria 1 and 2, against the real Job object."""
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
        assert agent_scratch_job.join(tree.name, script.pid)
        assert platform_compat.resume_process_main_thread(script.pid)
        out, _ = script.communicate(timeout=default_timeout())
        background = int(out.strip())
        later = time.time() + _LATER
        members = agent_scratch_job.job_pids(tree.name)
        # A venv's python.exe starts the real interpreter, so the job can hold two
        # processes for the one the script started.
        assert background in members

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent_scratch"):
            assert sc.sweep_dead_scratch(now=later) == 0
        assert (tree / "saved.txt").is_file(), "the tree and all its files remain"
        assert any(f"live pid {pid} still uses it" in caplog.text for pid in members)

        stop.write_text("")
        wait_until(lambda: not agent_scratch_job.job_pids(tree.name))
        assert sc.sweep_dead_scratch(now=later) == 1
        assert not tree.exists()
    finally:
        stop.write_text("")
        if script.poll() is None:
            script.kill()
            script.wait(timeout=default_timeout())

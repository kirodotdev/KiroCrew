"""The live-agent confinement check judges a namespace launcher by its child.

On Linux the pid a session records for a confined spawn is Crew's sandbox
launcher. The launcher stays in the host namespaces on purpose: it forks, writes
the child's uid/gid maps from outside, and waits. Only the child unshares and
execs the agent. ``unconfined_live_agent_pid`` must therefore accept a launcher
THIS gateway recorded spawning whose one child is confined, and must keep
refusing every shape it cannot prove.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import platform_compat, sandbox, sandbox_spawn
from kiro_crew.platform_compat_proctable import ProcessStartIdentity

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="the namespace launcher is Linux-only"
)

_LAUNCHER = 41001
_CHILD = 41002


def _launcher_spawn() -> tuple[list[str], str]:
    """The argv and script a real namespace wrap returns, for the recorder."""
    script = "/run/kirocrew_sandbox_1_ab.py"
    return [sys.executable, *sandbox._LAUNCHER_INTERPRETER_FLAGS, script, "kiro-cli", "acp"], script


@pytest.fixture
def launcher_host(monkeypatch):
    """A fake process table: a recorded launcher in the host namespaces, its child confined.

    Each test bends exactly one fact away from that confined shape.
    """
    monkeypatch.setattr(sandbox_spawn, "_NAMESPACE_LAUNCHERS", {_LAUNCHER: f"start-{_LAUNCHER}"})
    facts = {
        "children": [_CHILD],
        "child_ppid": _LAUNCHER,
        "child_shares_host": False,
        "launcher_shares_host": True,
    }

    def namespaces_match(pid, reference):
        assert reference == os.getpid()
        if pid == _LAUNCHER:
            return facts["launcher_shares_host"]
        if pid == _CHILD:
            return facts["child_shares_host"]
        return None

    monkeypatch.setattr(platform_compat, "process_namespaces_match", namespaces_match)
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: f"start-{pid}")
    monkeypatch.setattr(
        platform_compat,
        "get_process_start_identity",
        lambda pid: ProcessStartIdentity(f"start-{pid}", facts["child_ppid"]),
    )
    monkeypatch.setattr(
        platform_compat,
        "_linux_direct_child_pids",
        lambda pid, root: facts["children"] if pid == _LAUNCHER else [],
    )
    return facts


def test_a_recorded_launcher_whose_one_child_is_confined_counts_as_confined(launcher_host):
    assert sandbox.unconfined_live_agent_pid([_LAUNCHER]) is None


@pytest.mark.parametrize(
    "fact, value",
    [
        ("children", []),
        ("children", [_CHILD, _CHILD + 1]),
        ("children", None),
        ("child_ppid", 1),
        ("child_shares_host", True),
        ("child_shares_host", None),
    ],
    ids=[
        "no-child-yet",
        "a-second-child",
        "children-unreadable",
        "child-reparented",
        "child-has-not-unshared",
        "child-namespaces-unreadable",
    ],
)
def test_any_unproven_child_fact_stays_unconfined(launcher_host, fact, value):
    launcher_host[fact] = value
    assert sandbox.unconfined_live_agent_pid([_LAUNCHER]) == _LAUNCHER


def test_an_unrecorded_process_with_a_confined_child_stays_unconfined(launcher_host, monkeypatch):
    """A process this gateway did not spawn through the launcher is never judged by its child.

    This is the counterfeit: an unconfined harness that forks one namespaced child
    (or execs a launcher-shaped script) still holds the host namespaces itself.
    """
    monkeypatch.setattr(sandbox_spawn, "_NAMESPACE_LAUNCHERS", {})
    assert sandbox.unconfined_live_agent_pid([_LAUNCHER]) == _LAUNCHER


def test_a_record_from_an_earlier_incarnation_of_the_pid_stays_unconfined(
    launcher_host, monkeypatch
):
    monkeypatch.setattr(sandbox_spawn, "_NAMESPACE_LAUNCHERS", {_LAUNCHER: "start-earlier"})
    assert sandbox.unconfined_live_agent_pid([_LAUNCHER]) == _LAUNCHER


def test_unreadable_launcher_namespaces_stay_unconfined_without_a_walk(launcher_host):
    launcher_host["launcher_shares_host"] = None
    assert sandbox.unconfined_live_agent_pid([_LAUNCHER]) == _LAUNCHER


def test_a_confined_launcher_does_not_hide_a_later_unconfined_pid(launcher_host):
    assert sandbox.unconfined_live_agent_pid([_LAUNCHER, 41009]) == 41009


def test_recording_drops_entries_whose_process_is_gone(monkeypatch):
    monkeypatch.setattr(sandbox_spawn, "_NAMESPACE_LAUNCHERS", {41007: "start-gone"})
    monkeypatch.setattr(
        platform_compat,
        "get_process_start_id",
        lambda pid: None if pid == 41007 else f"start-{pid}",
    )
    sandbox.remember_namespace_launcher(_LAUNCHER, *_launcher_spawn())
    assert sandbox_spawn._NAMESPACE_LAUNCHERS == {_LAUNCHER: f"start-{_LAUNCHER}"}


def test_recording_an_unreadable_pid_records_nothing(monkeypatch):
    monkeypatch.setattr(sandbox_spawn, "_NAMESPACE_LAUNCHERS", {})
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: None)
    sandbox.remember_namespace_launcher(_LAUNCHER, *_launcher_spawn())
    assert sandbox_spawn._NAMESPACE_LAUNCHERS == {}


def test_a_genuine_launcher_spawn_is_recorded(monkeypatch):
    monkeypatch.setattr(sandbox_spawn, "_NAMESPACE_LAUNCHERS", {})
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: f"start-{pid}")
    sandbox.remember_namespace_launcher(_LAUNCHER, *_launcher_spawn())
    assert sandbox_spawn._NAMESPACE_LAUNCHERS == {_LAUNCHER: f"start-{_LAUNCHER}"}


_SCRIPT = "/run/kirocrew_sandbox_1_ab.py"


@pytest.mark.parametrize(
    "argv, script",
    [
        # sandbox off: the wrap generated no script, whatever the harness is named.
        (["/opt/kirocrew_sandbox_custom", "acp"], None),
        (["/opt/kirocrew_sandbox_custom", "acp"], "/opt/kirocrew_sandbox_custom"),
        # a script that is not one the wrap generates
        ([sys.executable, "-I", "-S", "/run/other.py", "kiro-cli"], "/run/other.py"),
        # the argv does not run the generated script
        (["/opt/kiro-cli", "acp"], _SCRIPT),
        (["/usr/bin/python3-other", "-I", "-S", _SCRIPT, "kiro-cli"], _SCRIPT),
        ([sys.executable, "-S", _SCRIPT, "kiro-cli"], _SCRIPT),
        # nothing for the launcher to run
        ([sys.executable, "-I", "-S", _SCRIPT], _SCRIPT),
    ],
    ids=[
        "launcher-named-harness-no-script",
        "launcher-named-harness-as-its-own-script",
        "foreign-script",
        "argv-does-not-run-script",
        "other-interpreter",
        "flags-differ",
        "no-wrapped-command",
    ],
)
def test_a_spawn_that_is_not_the_wraps_own_launcher_is_not_recorded(monkeypatch, argv, script):
    monkeypatch.setattr(sandbox_spawn, "_NAMESPACE_LAUNCHERS", {})
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: f"start-{pid}")
    sandbox.remember_namespace_launcher(_LAUNCHER, argv, script)
    assert sandbox_spawn._NAMESPACE_LAUNCHERS == {}


def test_a_real_process_tree_is_read_end_to_end(monkeypatch):
    """The real /proc readers walk a real parent and child; only the child's namespaces decide.

    Nothing here can unshare, so the real child shares the host namespaces and the
    parent stays unconfined even once recorded. Faking ONLY the child's namespace
    answer flips a RECORDED parent to confined, and leaves an unrecorded one
    unconfined, which shows every other fact was read from the kernel.
    """
    monkeypatch.setattr(sandbox_spawn, "_NAMESPACE_LAUNCHERS", {})
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os, sys, time\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            "    time.sleep(60)\n"
            "    os._exit(0)\n"
            "sys.stdout.write(str(pid) + '\\n')\n"
            "sys.stdout.flush()\n"
            "os.waitpid(pid, 0)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    child = 0
    try:
        assert proc.stdout is not None
        # The parent prints the pid only after fork() returned, so the kernel
        # already lists the child: no wait is needed before reading it.
        child = int(proc.stdout.readline())
        assert platform_compat._linux_direct_child_pids(proc.pid, Path("/proc")) == [child]

        real_match = platform_compat.process_namespaces_match

        def child_unshared(pid, reference):
            return False if pid == child else real_match(pid, reference)

        monkeypatch.setattr(platform_compat, "process_namespaces_match", child_unshared)
        assert sandbox.unconfined_live_agent_pid([proc.pid]) == proc.pid

        sandbox.remember_namespace_launcher(proc.pid, *_launcher_spawn())
        assert sandbox.unconfined_live_agent_pid([proc.pid]) is None

        monkeypatch.setattr(platform_compat, "process_namespaces_match", real_match)
        assert sandbox.unconfined_live_agent_pid([proc.pid]) == proc.pid
    finally:
        if child:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.kill()
        proc.wait(timeout=10)

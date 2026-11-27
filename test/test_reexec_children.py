"""An exec'd gateway image reaps the children its predecessor named, and only those."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from kiro_crew import reexec_children
from kiro_crew.platform_compat_runtime import (
    REEXEC_CHILDREN_ENV,
    _publish_children_for_successor,
)
from kiro_crew.reexec_children import adopt_inherited_children, reap_once, take_inherited_children

posix_only = pytest.mark.skipif(os.name != "posix", reason="execv keeps the pid only on POSIX")

# Two cases start real interpreters; one group keeps them on one worker instead of
# adding concurrent interpreter cold starts across the shard.
pytestmark = pytest.mark.xdist_group("reexec_children_live_process")


@posix_only
def test_publish_names_the_children_and_clears_a_stale_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(REEXEC_CHILDREN_ENV, raising=False)
    _publish_children_for_successor(lambda: [41, 42])
    assert os.environ[REEXEC_CHILDREN_ENV] == "41,42"
    _publish_children_for_successor(lambda: [])
    assert REEXEC_CHILDREN_ENV not in os.environ
    monkeypatch.setenv(REEXEC_CHILDREN_ENV, "7")
    _publish_children_for_successor(lambda: None)  # unreadable: never pass a stale list on
    assert REEXEC_CHILDREN_ENV not in os.environ


def test_take_pops_the_variable_and_ignores_junk() -> None:
    env = {REEXEC_CHILDREN_ENV: "12, x,0,-3,,34"}
    assert take_inherited_children(env) == [12, 34]
    assert REEXEC_CHILDREN_ENV not in env
    assert take_inherited_children(env) == []


@posix_only
def test_reap_once_keeps_only_children_still_running() -> None:
    answers = {1: (1, 0), 2: (0, 0)}

    def waitpid(pid: int, flags: int) -> tuple[int, int]:
        assert flags == os.WNOHANG
        if pid == 3:
            raise ChildProcessError
        return answers[pid]

    assert reap_once([1, 2, 3], waitpid) == [2]


@posix_only
def test_a_child_that_exits_later_is_reaped_by_the_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A real child that waits for us to close its stdin, so it is still running
    # at the first pass and exits only when told to.
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )
    pid = child.pid
    # Forget the Popen the way an exec does: nothing in this image owns it now.
    child.returncode = 0
    passes: list[list[int]] = []
    first_pass = threading.Event()
    real_reap_once = reexec_children.reap_once

    def counting_reap_once(pids, waitpid=None):  # type: ignore[no-untyped-def]
        left = real_reap_once(pids, waitpid)
        passes.append(left)
        first_pass.set()
        return left

    monkeypatch.setattr(reexec_children, "reap_once", counting_reap_once)
    stop = threading.Event()
    thread = None
    try:
        env = {REEXEC_CHILDREN_ENV: str(pid)}
        # Pacing only: the test waits on events and thread.join, never on this interval.
        thread = adopt_inherited_children(env, interval=0.01, stop=stop)  # flake-ok: pacing only
        assert thread is not None
        # Hang ceilings only, together well inside the 120 s pytest timeout.
        assert first_pass.wait(timeout=20)
        assert passes[0] == [pid]  # still running at the first pass
        assert child.stdin is not None
        child.stdin.close()
        thread.join(timeout=20)
        assert not thread.is_alive()
        assert passes[-1] == []
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
    finally:
        # Never leave the child behind, whichever assertion failed.
        if child.stdin is not None and not child.stdin.closed:
            child.stdin.close()
        stop.set()
        if thread is not None:
            thread.join(timeout=20)
        try:
            # (0, 0) means our child is still running and unreaped, so the kill
            # can only reach it; ChildProcessError means it is already collected.
            if os.waitpid(pid, os.WNOHANG)[0] == 0:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
        except (ChildProcessError, ProcessLookupError):
            pass


_PREDECESSOR = textwrap.dedent("""
    import os, subprocess, sys
    from kiro_crew.platform_compat import reexec_python_module
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    # Wait for the exit WITHOUT collecting it, so the successor inherits a zombie.
    os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
    print(child.pid, flush=True)
    reexec_python_module("successor", [str(child.pid)], executable=sys.executable)
    """)

_SUCCESSOR = textwrap.dedent("""
    import os, sys
    from kiro_crew.reexec_children import REEXEC_CHILDREN_ENV, adopt_inherited_children
    named = os.environ.get(REEXEC_CHILDREN_ENV, "") or "-"
    pid = int(sys.argv[1])
    zombie_before = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    thread = adopt_inherited_children()
    if thread is not None:
        thread.join(timeout=20)
    try:
        os.waitpid(pid, os.WNOHANG)
        reaped = False
    except ChildProcessError:
        reaped = True
    print(named, zombie_before, reaped, REEXEC_CHILDREN_ENV in os.environ, flush=True)
    """)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="lists children via /proc")
def test_the_successor_reaps_the_zombie_its_predecessor_left(tmp_path: Path) -> None:
    (tmp_path / "successor.py").write_text(_SUCCESSOR, encoding="utf-8")
    env = dict(os.environ)
    src = str(Path(reexec_children.__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), src])
    env.pop(REEXEC_CHILDREN_ENV, None)
    proc = subprocess.run(  # flake-ok: the timeout is a hang ceiling, not a barrier
        [sys.executable, "-c", _PREDECESSOR],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    child_pid, line = proc.stdout.strip().splitlines()
    named, zombie_before, reaped, still_set = line.split()
    assert zombie_before == "True"  # the predecessor's child, exited and never collected
    assert reaped == "True", "the successor left its predecessor's child a zombie"
    assert named == child_pid
    assert still_set == "False"  # popped, so nothing the successor spawns inherits it

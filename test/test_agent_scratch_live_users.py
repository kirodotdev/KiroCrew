"""A dead owner over a live user the marker does not name must not lose its tree.

``agent_scratch.sweep_dead_scratch`` reads a tree's liveness from the process
groups its ``.owner`` marker names. A runtime swap leaves a process outside
them: the successor adopts only the session tree's first directory, so the
predecessor's OWN directory, its ``TMPDIR``, names the predecessor alone. A
tool that daemonized out of the predecessor (fork, ``setsid``, fork) leaves its
group and its tree, survives the kill path, and keeps that directory as its
``TMPDIR``. Read from the marker alone, the tree is dead once the predecessor
is, so after an idle hour the sweep would delete it and the tool's next temp
write would raise ``ENOENT``. So the sweep also asks the process table
(``agent_scratch_users.scratch_users``) and keeps a tree that a live process
still uses through its scratch exports, its working directory or an open
descriptor. The exports the sweeping gateway was itself started with are not
such a use, whether they sit in its own exec-time environment or in that of
the launcher that runs it as a child.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from kiro_crew import agent_scratch as sc
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing.wait import default_timeout

_LINUX_ONLY = pytest.mark.skipif(
    sys.platform != "linux", reason="the live-user signal reads /proc, which only Linux has"
)

_SRC = Path(__file__).resolve().parents[1] / "src"

#: The user stand-in: says it is running, then blocks until the test closes its stdin.
_UNTIL_STDIN_CLOSES = "import sys; print('ready', flush=True); sys.stdin.read()"

#: The gateway stand-in: drops its inherited exports as the gateway does at
#: start, then runs one sweep judged from the time in its first argument.
_SWEEP_ONCE = (
    f"import sys; sys.path.insert(0, {str(_SRC)!r})\n"
    "from kiro_crew import agent_scratch as sc\n"
    "sc.drop_own_inherited_scratch_env()\n"
    "print(sc.sweep_dead_scratch(now=float(sys.argv[1])))\n"
)

#: Bounds every wait on a child this file starts.
_WAIT_SECONDS = 60


@pytest.fixture
def scratch_root(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(sc, "config_dir", lambda: home)
    return home / "scratch"


def _exited_launcher_pid() -> int:
    """The pid of a launcher that led its own group and has exited: a dead owner."""
    launcher = subprocess.Popen([sys.executable, "-I", "-c", ""], start_new_session=True)
    assert launcher.wait(timeout=_WAIT_SECONDS) == 0
    return launcher.pid


@contextmanager
def _live_user(tree: Path, cwd: Path, use: str) -> Iterator[subprocess.Popen]:
    """A live process in a group of its own that uses *tree* the way *use* names."""
    env = {key: value for key, value in os.environ.items() if key not in sc.SCRATCH_ENV_KEYS}
    held: list[int] = []
    if use == "tmpdir":
        env.update(sc.scratch_env(tree))
    elif use == "cwd":
        cwd = tree
    else:
        held.append(os.open(tree / "bash-1.output", os.O_WRONLY | os.O_CREAT, 0o600))
    try:
        child = subprocess.Popen(
            [sys.executable, "-I", "-c", _UNTIL_STDIN_CLOSES],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=env,
            cwd=cwd,
            pass_fds=held,
            start_new_session=True,
        )
    finally:
        for fd in held:
            os.close(fd)  # the child holds its own copy; this process holds none
    try:
        # Popen returns once the exec is under way, before the kernel has
        # published the new image's environment; a line the child writes
        # proves it is past that, so the sweep's one read of /proc sees it.
        assert child.stdout is not None and child.stdout.readline() == b"ready\n"
        yield child
    finally:
        for pipe in (child.stdin, child.stdout):
            if pipe is not None:
                pipe.close()
        try:
            child.wait(timeout=_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            # Still running, so still its group's leader: the group id is its pid.
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=_WAIT_SECONDS)


@_LINUX_ONLY
@pytest.mark.parametrize("use", ["tmpdir", "cwd", "descriptor"])
def test_a_live_user_outside_the_owner_groups_keeps_the_tree(
    scratch_root: Path, tmp_path: Path, caplog, use: str
) -> None:
    tree = sc.allocate_scratch("dashboard-chat-89")
    assert sc.record_owner(tree, _exited_launcher_pid()) == "recorded"
    # Judged from past the grace window rather than by ageing every entry.
    later = time.time() + 2 * sc._UNOWNED_GRACE_SECONDS

    with _live_user(tree, tmp_path, use) as user:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent_scratch"):
            assert sc.sweep_dead_scratch(now=later) == 0
        assert tree.is_dir(), "a live user the marker does not name keeps the tree"
        assert f"live pid {user.pid} still uses it" in caplog.text

    assert sc.sweep_dead_scratch(now=later) == 1, "once that user is gone it is reclaimed"
    assert not tree.exists()


@_LINUX_ONLY
def test_the_launcher_above_the_sweeper_does_not_keep_the_tree_it_was_started_in(
    scratch_root: Path, tmp_path: Path
) -> None:
    """A gateway can run as the child of a launcher that stays alive for its whole
    life (``toolbox-exec``), and a restart from an agent shell starts that launcher
    with the shell's exports. Read as a user, the launcher would keep the shell's
    tree for as long as the gateway runs."""
    tree = sc.allocate_scratch("dashboard-chat-89")
    assert sc.record_owner(tree, _exited_launcher_pid()) == "recorded"
    later = time.time() + 2 * sc._UNOWNED_GRACE_SECONDS
    env = {**os.environ, **sc.scratch_env(tree), "KIROCREW_HOME": str(scratch_root.parent)}
    sweeper = [sys.executable, "-I", "-B", "-c", _SWEEP_ONCE, str(later)]

    # The trailing ":" keeps the shell from exec-ing the sweeper, so the shell
    # stays alive above it as its launcher, holding the tree's exports.
    swept = subprocess.run(
        ["/bin/sh", "-c", '"$@"; :', "sh", *sweeper],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        timeout=default_timeout(),
        **UTF8_TEXT,
    )

    assert swept.returncode == 0, swept.stderr
    assert swept.stdout.strip() == "1", swept.stderr
    assert not tree.exists()


def test_only_scratch_exports_count_and_never_the_sweepers_own(tmp_path: Path) -> None:
    """The kernel keeps a process's exec-time environment for its whole life, so
    a gateway started from an agent shell still shows that shell's ``TMPDIR``
    there after dropping it; reading its own would keep that tree for good."""
    from kiro_crew import agent_scratch_users

    root = tmp_path / "scratch"
    proc = tmp_path / "proc"
    other = os.getpid() + 1
    environs = {
        os.getpid(): {"TMPDIR": "gateway-shell-1"},
        other: {"HOME": "fake-home-3", "KIROCREW_SCRATCH": "runtime-2"},
    }
    for pid, exports in environs.items():
        (proc / str(pid)).mkdir(parents=True)
        body = b"PATH=/usr/bin\0" + b"".join(
            os.fsencode(f"{key}={root / tree}") + b"\0" for key, tree in exports.items()
        )
        (proc / str(pid) / "environ").write_bytes(body)

    users = agent_scratch_users.scratch_users(root, sc.SCRATCH_ENV_KEYS, proc_root=proc)

    assert users == {"runtime-2": other}

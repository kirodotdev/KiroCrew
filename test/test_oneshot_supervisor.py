"""One-shot kiro-cli calls must not leave helpers running after they return.

A kiro-cli launcher wrapper can start a credential helper for a call as
short as ``kiro-cli chat --list-models`` and leave it running when
the call returns. ``/api/models`` re-polls every 8s while degraded, so one
gateway leaked a ~140-thread helper per poll until its agent cgroup hit
``pids.max``. These tests pin the supervisor's ``--reap-survivors`` mode that
ends such leftovers, and that the one-shot spawn sites use it.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import _process_group_supervisor as supervisor
from kiro_crew import kiro_prerequisite, platform_compat
from kiro_crew.dashboard.handlers import agents
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

# Above every supported platform's pid_max, so no cleanup path can reach a live
# process by it (same spelling as test/test_update_provider.py).
_UNALLOCATABLE_PID = 99_999_999_999

posix_only = pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="POSIX process groups")

# A command that starts a detached-looking helper in its own process group (the
# credential-helper shape: same group, parent about to exit) and then returns at once.
# The helper records its pid so the test can check whether it survived.
# The pid file is published atomically (write, then rename), so a reader that sees
# it can always parse it.
_LEAKY_COMMAND = """
import os, subprocess, sys
helper = subprocess.Popen(
    [sys.executable, "-c", {helper!r}],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
with open({pid_file!r} + ".tmp", "w") as out:
    out.write(str(helper.pid))
os.replace({pid_file!r} + ".tmp", {pid_file!r})
sys.exit(3)
"""

_HELPER_SLEEPS = "import time; time.sleep(120)"
_HELPER_IGNORES_TERM = (
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)"
)


def _alive(pid: int) -> bool:
    """Running and not a zombie (a zombie holds no resources)."""
    if sys.platform == "linux":
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return False
        return stat.rpartition(")")[2].split()[0] != "Z"
    return platform_compat.pid_exists(pid)


def _wait_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _run_supervised(tmp_path: Path, helper: str, *flags: str) -> tuple[int, int, int, float]:
    """Run the supervisor over a command that leaves a helper behind.

    Returns ``(exit code, helper pid, group id, seconds)``. The group id is the
    supervisor's pid, since it is spawned as a session leader.
    """
    pid_file = tmp_path / "helper.pid"
    code = Path(supervisor.__file__).read_text(encoding="utf-8")
    command = _LEAKY_COMMAND.format(helper=helper, pid_file=str(pid_file))
    started = time.monotonic()
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, test-local
        [
            sys.executable,
            "-I",
            "-c",
            code,
            *flags,
            os.path.realpath(sys.executable),
            "-c",
            command,
        ],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        returncode = proc.wait(timeout=60)
    finally:
        if proc.returncode is None:
            # A regressed supervisor still leads its group here, so the group id
            # names only this test's processes: end all of them, helper included.
            os.killpg(proc.pid, 9)
            proc.wait()
    elapsed = time.monotonic() - started
    helper_pid = int(pid_file.read_text())
    return returncode, helper_pid, proc.pid, elapsed


reaping = pytest.mark.skipif(not supervisor.can_reap(), reason="reaping needs Linux pidfd support")


def _kill_if_member(pid: int, pgid: int) -> None:
    """Test cleanup through the supervisor's own pinned signal, never a bare pid."""
    if supervisor.can_reap():
        supervisor._signal_member(pid, pgid, 9)


@reaping
@pytest.mark.parametrize(
    "helper",
    [_HELPER_SLEEPS, _HELPER_IGNORES_TERM],
    ids=["exits-on-term", "ignores-term"],
)
def test_reap_survivors_ends_what_the_command_left_behind(tmp_path: Path, helper: str) -> None:
    returncode, helper_pid, pgid, elapsed = _run_supervised(tmp_path, helper, "--reap-survivors")
    try:
        # The command's own exit status comes back, not the supervisor's.
        assert returncode == 3
        # The helper is gone: SIGTERM, or SIGKILL after the grace for one that
        # ignores SIGTERM. The call returns in about that grace, not 120s.
        assert _wait_gone(helper_pid), "helper survived the supervised call"
        assert elapsed < 20
    finally:
        _kill_if_member(helper_pid, pgid)


@posix_only
def test_without_the_flag_the_supervisor_still_waits_for_the_group(tmp_path: Path) -> None:
    # The default mode is what _run_process relies on: the leader keeps the
    # group anchored until the last member exits on its own.
    helper = "import time; time.sleep(1.5)"
    returncode, helper_pid, _pgid, elapsed = _run_supervised(tmp_path, helper)
    assert returncode == 3
    assert elapsed >= 1.4
    assert _wait_gone(helper_pid)


@posix_only
def test_unknown_leading_flag_is_refused(tmp_path: Path) -> None:
    code = Path(supervisor.__file__).read_text(encoding="utf-8")
    done = subprocess.run(  # noqa: S603 - fixed argv, test-local
        [sys.executable, "-I", "-c", code, "--no-such-flag", "/bin/true"],
        cwd=tmp_path,
        capture_output=True,
        timeout=30,
        start_new_session=True,
    )
    assert done.returncode == 127


# Leads its own group, starts the supervisor WITHOUT a new session (so the
# supervisor is a member of this group, not its leader) and exits at once. The
# supervisor then must not reap: the group is not its own.
_NON_LEADER_PARENT = """
import subprocess, sys
subprocess.Popen(
    [sys.executable, "-I", "-c", {code!r}, "--reap-survivors", sys.executable, "-c", {command!r}],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
"""


@reaping
def test_reap_is_skipped_when_the_supervisor_does_not_lead_its_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "helper.pid"
    code = Path(supervisor.__file__).read_text(encoding="utf-8")
    command = _LEAKY_COMMAND.format(helper="import time; time.sleep(4)", pid_file=str(pid_file))
    parent = subprocess.Popen(  # noqa: S603 - fixed argv, test-local
        [sys.executable, "-c", _NON_LEADER_PARENT.format(code=code, command=command)],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pgid = parent.pid
    helper_pid: int | None = None
    try:
        parent.wait(timeout=30)
        deadline = time.monotonic() + 30
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pid_file.exists(), "the supervised command never started its helper"
        helper_pid = int(pid_file.read_text())
        # A reaping supervisor would end the helper within a poll or two; one that
        # correctly declines leaves it running for its full sleep.
        time.sleep(1.5)
        assert _alive(helper_pid), "supervisor reaped a group it does not lead"
    finally:
        if parent.returncode is None:
            # Still leading its group, so the group id names only this test's tree.
            os.killpg(parent.pid, 9)
            parent.wait()
        if helper_pid is not None:
            # The helper outlives the supervisor's 4 s wait at most; end it now.
            _kill_if_member(helper_pid, pgid)


@reaping
def test_signal_member_refuses_a_pid_outside_the_group() -> None:
    # The identity check, not the pid, decides: this test process is alive but
    # not in that group, so it must not be signalled.
    assert supervisor._signal_member(os.getpid(), _UNALLOCATABLE_PID, 0) is False


def _spawn_capture() -> AsyncMock:
    return AsyncMock(return_value=SimpleNamespace(pid=_UNALLOCATABLE_PID))


@pytest.fixture
def reap_capable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the gateway-side capability probe on, so argv tests run on any POSIX host."""
    monkeypatch.setattr(kiro_prerequisite, "_host_can_reap", lambda: True)


def test_spawn_supervised_oneshot_skips_the_supervisor_where_it_cannot_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without pidfd the supervisor would only WAIT for leftovers, holding the call
    # open; the spawn must then be exactly today's plain one, in its own session.
    monkeypatch.setattr(kiro_prerequisite, "_host_can_reap", lambda: False)
    spawn = _spawn_capture()
    with patch.object(kiro_prerequisite, "create_subprocess_limited", spawn):
        asyncio.run(kiro_prerequisite.spawn_supervised_oneshot(["/usr/bin/env", "x"]))
    assert list(spawn.await_args.args) == ["/usr/bin/env", "x"]
    assert spawn.await_args.kwargs == {"start_new_session": True}


@posix_only
@pytest.mark.usefixtures("reap_capable")
def test_spawn_supervised_oneshot_wraps_the_command_in_its_own_session() -> None:
    spawn = _spawn_capture()
    with patch.object(kiro_prerequisite, "create_subprocess_limited", spawn):
        asyncio.run(kiro_prerequisite.spawn_supervised_oneshot(["/usr/bin/env", "x"], env={}))
    args = list(spawn.await_args.args)
    assert args[:3] == [sys.executable, "-I", "-c"]
    assert args[3] == kiro_prerequisite._PROCESS_GROUP_SUPERVISOR_CODE
    assert args[4:] == ["--reap-survivors", "/usr/bin/env", "x"]
    assert spawn.await_args.kwargs == {"start_new_session": True, "env": {}}


@posix_only
@pytest.mark.usefixtures("reap_capable")
def test_spawn_supervised_oneshot_resolves_a_relative_wrapper() -> None:
    spawn = _spawn_capture()
    with (
        patch.object(platform_compat, "trusted_system_bin", return_value="/usr/bin/env"),
        patch.object(kiro_prerequisite, "create_subprocess_limited", spawn),
    ):
        asyncio.run(kiro_prerequisite.spawn_supervised_oneshot(["env", "x"]))
    assert list(spawn.await_args.args)[-2:] == ["/usr/bin/env", "x"]


@posix_only
@pytest.mark.usefixtures("reap_capable")
def test_spawn_supervised_oneshot_runs_an_unresolvable_wrapper_unsupervised() -> None:
    spawn = _spawn_capture()
    with (
        patch.object(platform_compat, "trusted_system_bin", return_value=None),
        patch.object(kiro_prerequisite, "create_subprocess_limited", spawn),
    ):
        asyncio.run(kiro_prerequisite.spawn_supervised_oneshot(["nope", "x"]))
    assert list(spawn.await_args.args) == ["nope", "x"]
    assert spawn.await_args.kwargs["start_new_session"] is True


class _FakeProc:
    def __init__(self, stdout: bytes) -> None:
        self._stdout = stdout
        self.returncode = 0
        self.pid = _UNALLOCATABLE_PID

    def kill(self) -> None:
        pass

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""


async def _no_audit(**kwargs: Any) -> None:
    del kwargs


@posix_only
@pytest.mark.usefixtures("reap_capable")
def test_api_models_spawns_the_list_under_the_reaping_supervisor(tmp_path: Path) -> None:
    payload = json.dumps({"models": [{"model_name": "claude-opus-4.8"}]}).encode()
    spawn = AsyncMock(return_value=_FakeProc(payload))
    service = KiroPrerequisiteService(
        platform_name="linux",
        environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        home=tmp_path,
        audit_writer=_no_audit,
        assume_ready=True,
    )
    request = MagicMock()
    request.app = {"kiro_prerequisite_service": service}
    cfg = SimpleNamespace(agent=SimpleNamespace(provider="kiro"))
    with (
        patch.object(agents.KiroCrewConfig, "load", return_value=cfg),
        patch("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", return_value="/usr/bin/kiro-cli"),
        patch("kiro_crew.acp.client._resolve_ssh_auth_sock", lambda env: None),
        patch("kiro_crew.env.augmented_path", lambda p: p),
        patch(
            "kiro_crew.dashboard.handlers.agents.wrap_argv",
            lambda argv, **kwargs: (argv, None),
        ),
        patch("kiro_crew.dashboard.handlers.agents.cgroup_scope_argv", lambda argv: argv),
        patch("kiro_crew.sandbox.resource_limit_preexec", lambda: None),
        patch.object(agents.asyncio, "create_subprocess_exec", spawn),
    ):
        resp = asyncio.run(agents.api_models(request))

    assert resp.status == 200
    argv = [str(a) for a in spawn.await_args.args]
    code = kiro_prerequisite._PROCESS_GROUP_SUPERVISOR_CODE
    at = argv.index(code)
    assert argv[at + 1] == "--reap-survivors"
    # The supervisor wraps the whole command, so the model list runs inside it.
    assert argv.index("/usr/bin/kiro-cli") > at
    assert spawn.await_args.kwargs["start_new_session"] is True

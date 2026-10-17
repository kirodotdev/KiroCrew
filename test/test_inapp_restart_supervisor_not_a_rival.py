"""An in-app restart through a supervising launcher leaves the old pid alive as
the new gateway's ancestor, and neither ownership check may read it as a rival.

The dashboard and update restarts ``os.execv`` the launcher. On a Toolbox host
that launcher is ``toolbox-exec``, which runs the gateway as a supervised CHILD:
the old pid survives as the supervisor with its start time unchanged, and the new
gateway is its descendant. Two checks then met a live, start-token-matching pid
that is not ``os.getpid()``:

* ``run_marker._record_names_another_live_gateway`` -- the pid record still names
  the supervisor, so ``write_marker`` declined and the record kept naming a
  process that does not serve the port.
* ``GatewayManager._owned_by_a_live_other`` -- the broker daemon's
  ``--owner-pid`` is the supervisor, so ``start()`` refused the broker for the
  gateway's whole life. The pre-exec broker stop prevents that for restarts
  made by code that has it, but not for the restart that installs it nor for a
  host already in that state.

Being an ancestor is not enough on its own: a gateway started from a shell that a
LIVE gateway spawned also has that gateway as an ancestor, and must keep reading
it as a rival. What the exec changes is the image -- the supervisor now runs the
launcher, and every gateway runs a Python interpreter. The tests below build both
layouts from real processes: a Python "old gateway" that really ``os.execv``s into
a non-Python supervisor which runs the "new gateway" as its child, and a Python
gateway that stays alive and runs the child itself.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from kiro_crew import code_fingerprint as cf
from kiro_crew import platform_compat
from kiro_crew.instances import run_marker
from kiro_crew.mcp_gateway import manager as mgr
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_PORT = 5476

# Only POSIX keeps a pid across exec, so only there can a supervisor be left
# behind; the real-process layouts need a non-Python supervisor at /bin/sh.
posix_exec = pytest.mark.skipif(
    sys.platform == "win32" or not os.path.exists("/bin/sh"),
    reason="exec keeps the pid only on POSIX",
)

# The "new gateway". It reports its own pid, its parent, and the verdict of the
# check under test.
_NEW_GATEWAY = textwrap.dedent("""
    import os, sys
    from pathlib import Path
    check, port, old_pid, sock = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
    if check == "marker":
        from kiro_crew.instances import run_marker
        record = run_marker.read_pid_record_path(run_marker.pid_path(port))
        token_matches = (
            record is not None
            and record[0] == old_pid
            and bool(record[1])
            and run_marker.pid_start_token(old_pid) == record[1]
        )
        run_marker.write_marker(port)
        print(os.getpid(), os.getppid(), token_matches, run_marker.read_pid(port))
    else:
        from kiro_crew.mcp_gateway import manager as m
        g = m.GatewayManager(m.GatewaySpec(socket_path=Path(sock)))
        print(os.getpid(), os.getppid(), True, g._owned_by_a_live_other({"owner_pid": old_pid}))
    """)

# The "old gateway". It publishes its own pid record with its real start token,
# as a running gateway does, then either becomes a non-Python supervisor through
# a real os.execv (pid and start time kept) or stays a live Python gateway.
_OLD_GATEWAY = textwrap.dedent("""
    import os, subprocess, sys
    layout, check, port, sock, new_gateway = sys.argv[1:6]
    if check == "marker":
        from kiro_crew.dashboard import server as dashboard_server
        from kiro_crew.instances import run_marker
        p = run_marker.pid_path(int(port))
        dashboard_server._write_secret_file(p, f"{os.getpid()}\\n")
        token = run_marker.pid_start_token(os.getpid())
        dashboard_server._write_secret_file(run_marker._start_path_for(p), f"{token}\\n")
    child = [sys.executable, "-c", new_gateway, check, port, str(os.getpid()), sock]
    if layout == "nested":
        # A live Python gateway between the supervisor and the new gateway: the
        # supervisor's own gateway, which started the new one.
        relay = "import subprocess, sys; sys.exit(subprocess.run(sys.argv[1:]).returncode)"
        child = [sys.executable, "-c", relay, *child]
    if layout in ("exec", "nested"):
        # The trailing command keeps the shell from exec-ing the child itself,
        # so the shell stays alive as the supervisor, as toolbox-exec does.
        os.execv("/bin/sh", ["/bin/sh", "-c", '"$@"; exit $?', "sh", *child])
    sys.exit(subprocess.run(child).returncode)
    """)


def _restart(layout: str, check: str, home: Path, tmp_path: Path) -> tuple[int, int, int, str]:
    """Run the old/new gateway pair; return (old pid, new pid, new's parent, verdict)."""
    old = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _OLD_GATEWAY,
            layout,
            check,
            str(_PORT),
            str(tmp_path / "gw.sock"),
            _NEW_GATEWAY,
        ],
        env={**os.environ, "KIROCREW_HOME": str(home)},
        # Both processes may create files (the record, marker temporaries); the
        # grandchild inherits this directory through the supervisor.
        cwd=tmp_path,
        # Its own process group, so a hung tree is reaped whole below.
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **UTF8_TEXT,
    )
    try:
        out, err = old.communicate(timeout=120)
    finally:
        if old.poll() is None:
            os.killpg(old.pid, signal.SIGKILL)
            old.communicate(timeout=30)
    assert old.returncode == 0, err
    new_pid, new_parent, token_matches, verdict = out.split()
    # Without a live, token-matching record naming the old pid, the run-marker
    # check reads the record as unproven and writes anyway, and the test would
    # pass for the wrong reason.
    assert token_matches == "True", "the old gateway's record did not survive as live"
    return old.pid, int(new_pid), int(new_parent), verdict


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    from kiro_crew.config import paths

    monkeypatch.setattr(paths, "_config_dir_memo", None, raising=False)
    monkeypatch.setattr(run_marker, "_PUBLISHED_LISTENERS", {}, raising=True)
    return tmp_path


@posix_exec
class TestTheRunMarkerSurvivesASupervisingRestart:
    def test_the_new_gateway_takes_over_the_record_of_its_exec_supervisor(
        self, home: Path, tmp_path: Path
    ) -> None:
        old, new, parent, recorded = _restart("exec", "marker", home, tmp_path)
        assert parent == old, "the supervisor must be the exec-ed old gateway itself"
        assert int(recorded) == new

    def test_a_live_python_gateway_ancestor_keeps_its_record(
        self, home: Path, tmp_path: Path
    ) -> None:
        # A gateway started from a shell that a live gateway spawned: that
        # gateway is an ancestor, still a gateway, and its record must stand.
        old, new, parent, recorded = _restart("live", "marker", home, tmp_path)
        assert parent == old
        assert int(recorded) == old

    def test_a_live_gateway_between_the_supervisor_and_the_writer_keeps_the_record(
        self, home: Path, tmp_path: Path
    ) -> None:
        # The supervisor's own gateway started this one: the record names the
        # supervisor, but the port's gateway is the Python process in between.
        old, _new, parent, recorded = _restart("nested", "marker", home, tmp_path)
        assert parent != old, "a live Python gateway must sit between the two"
        assert int(recorded) == old


@posix_exec
class TestTheBrokerSurvivesASupervisingRestart:
    def test_the_exec_supervisor_is_not_a_rival(self, home: Path, tmp_path: Path) -> None:
        old, _new, parent, verdict = _restart("exec", "broker", home, tmp_path)
        assert parent == old
        assert verdict == "False"

    def test_a_live_python_gateway_ancestor_is_still_a_rival(
        self, home: Path, tmp_path: Path
    ) -> None:
        old, _new, parent, verdict = _restart("live", "broker", home, tmp_path)
        assert parent == old
        assert verdict == "True"

    def test_a_live_gateway_between_the_supervisor_and_the_caller_keeps_it_a_rival(
        self, home: Path, tmp_path: Path
    ) -> None:
        # The daemon belongs to the supervisor's own gateway, which is alive and
        # sits between them; adopting or standing it down takes that gateway's
        # broker.
        old, _new, parent, verdict = _restart("nested", "broker", home, tmp_path)
        assert parent != old
        assert verdict == "True"


class TestTheSupervisorPredicate:
    def test_init_is_never_a_supervisor(self) -> None:
        # pid 1 is every process's ancestor on POSIX and is not a launcher this
        # gateway was exec-ed from. Its image is pinned to a readable non-Python
        # one so that only the walk's own refusal of pid 1 can answer False.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(platform_compat, "process_executable_path", lambda pid: "/sbin/init")
            assert platform_compat.is_exec_supervisor_of_this_process(1) is False

    def test_a_python_parent_is_not_a_supervisor(self) -> None:
        # This test process's parent (the pytest runner or a shell) is either a
        # Python interpreter or not an exec-ed gateway; with the image pinned to
        # Python the predicate must refuse even though the pid IS an ancestor.
        parent = os.getppid()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                platform_compat, "process_executable_path", lambda pid: "/usr/bin/python3.12"
            )
            assert platform_compat.is_exec_supervisor_of_this_process(parent) is False

    def test_an_unreadable_image_keeps_the_refusal(self) -> None:
        # Windows, or a process the host will not describe: no proof, no exemption.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(platform_compat, "process_executable_path", lambda pid: None)
            assert platform_compat.is_exec_supervisor_of_this_process(os.getppid()) is False

    @pytest.mark.parametrize(
        "exe, is_python",
        [
            ("/usr/bin/python3.12", True),
            ("/opt/py/bin/python3.13t", True),
            ("/usr/bin/python", True),
            (
                "/Library/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python",
                True,
            ),
            ("C:\\Python312\\python.exe", True),
            ("/home/u/.toolbox/bin/toolbox-exec", False),
            ("/usr/bin/bash", False),
            ("/home/u/.toolbox/bin/kirocrew", False),
        ],
    )
    def test_what_counts_as_a_python_image(self, exe: str, is_python: bool) -> None:
        assert platform_compat._is_python_interpreter_path(exe) is is_python


def _manager(tmp_path: Path) -> mgr.GatewayManager:
    return mgr.GatewayManager(mgr.GatewaySpec(socket_path=tmp_path / "gw.sock"))


def _pong(owner: int, fingerprint: str) -> dict[str, Any]:
    return {"type": "pong", "targets": [], "fingerprint": fingerprint, "owner_pid": owner}


class TestTheStartPathWithASupervisorOwner:
    """The manager's wiring, with the predicate pinned; the predicate itself is
    proved on real processes above."""

    _SUPERVISOR = 4242

    @pytest.fixture(autouse=True)
    def _supervisor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mgr.platform_compat, "pid_exists", lambda pid: pid == self._SUPERVISOR)
        monkeypatch.setattr(
            mgr.platform_compat,
            "is_exec_supervisor_of_this_process",
            lambda pid: pid == self._SUPERVISOR,
        )

    @pytest.mark.asyncio
    async def test_a_same_code_daemon_owned_by_the_supervisor_is_adopted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _manager(tmp_path)
        monkeypatch.setattr(
            manager,
            "_ping_payload",
            AsyncMock(return_value=_pong(self._SUPERVISOR, cf.code_fingerprint())),
        )
        asked = AsyncMock(side_effect=RuntimeError("a fit daemon is adopted, not stood down"))
        monkeypatch.setattr(manager, "_request_stand_down", asked)
        spawned = AsyncMock(side_effect=RuntimeError("must not spawn into a held socket"))
        monkeypatch.setattr(manager, "_spawn_and_confirm", spawned)
        try:
            assert await manager._start_locked() is True
            assert manager._adopted is True
            asked.assert_not_awaited()
            spawned.assert_not_awaited()
        finally:
            if manager._watchdog is not None:
                manager._watchdog.cancel()

    @pytest.mark.asyncio
    async def test_a_previous_releases_daemon_owned_by_the_supervisor_is_replaced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The restart that INSTALLS this code runs the previous release's restart,
        # which did not stop the broker. Its daemon is stale code owned by the
        # supervisor: it is stood down on the stale-code ground and replaced.
        manager = _manager(tmp_path)
        monkeypatch.setattr(
            manager,
            "_ping_payload",
            AsyncMock(return_value=_pong(self._SUPERVISOR, "previous-release")),
        )
        asked = AsyncMock(return_value=mgr._RELEASED)
        monkeypatch.setattr(manager, "_request_stand_down", asked)

        class _Proc:
            pid = 12345
            returncode = None

        async def _spawn() -> dict[str, Any]:
            manager._process = _Proc()  # type: ignore[assignment]
            return _pong(os.getpid(), cf.code_fingerprint())

        monkeypatch.setattr(manager, "_spawn_and_confirm", _spawn)
        try:
            assert await manager._start_locked() is True
            assert manager._adopted is False
            asked.assert_awaited_once_with([], stale_code=True, orphaned=False)
        finally:
            if manager._watchdog is not None:
                manager._watchdog.cancel()

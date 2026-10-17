"""The session rows and retraction leftovers this gateway's previous image left
behind are this gateway's, even while the supervisor an in-app restart left alive
still holds that image's pid.

An in-app restart ``os.execv``s the launcher, and a launcher that runs the gateway
as a supervised CHILD (``toolbox-exec``) leaves the old pid alive as the new
gateway's ancestor, start identity unchanged. Two readers in ``session_pid``
compare a recorded owning pid against liveness alone, and each met that
supervisor as a LIVE owner:

* ``cleanup_orphaned_sessions`` skipped every ``<gw>:<pid>`` row whose owning
  gateway pid is alive, so the rows the previous image filed were never swept
  while the supervisor lives.
* ``_retraction_leftover_is_orphaned`` left a ``.retract-<pid>-<hex>`` leftover of
  a retraction the previous image had in flight at the exec unsettled.

A live Python ancestor is still a gateway and keeps both, and so does any reader
with a live Python process between it and the supervisor. The layouts are built
from real processes, in the shape ``test_inapp_restart_supervisor_owned_records.py``
uses.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.subprocess_utf8 import UTF8_TEXT

posix_exec = pytest.mark.skipif(
    sys.platform == "win32" or not os.path.exists("/bin/sh"),
    reason="exec keeps the pid only on POSIX",
)

# A child pid the sweep reads as dead: its row is handled only once the sweep
# stops skipping it on its OWNER's reading.
_DEAD_CHILD = 5002

_NEW_GATEWAY = textwrap.dedent("""
    import os, sys
    old_pid, start_id = int(sys.argv[1]), sys.argv[2]
    from kiro_crew import platform_compat, session_pid
    still_live = (
        platform_compat.pid_exists(old_pid)
        and platform_compat.get_process_start_id(old_pid) == start_id
    )

    # 1. The startup sweep over the old image's row for a dead child.
    real_liveness = platform_compat.pid_liveness
    platform_compat.pid_liveness = (
        lambda pid: platform_compat.PID_DEAD if pid == %(dead)d else real_liveness(pid)
    )
    session_pid.cleanup_orphaned_sessions(narrow_with_leaders=False)
    path = session_pid._session_pid_file_path()
    rows = path.read_text(encoding="utf-8").split()
    sweep = f"rows={len(rows)}"

    # 2. The periodic root sweep over the same row, filed again.
    path.write_text(f"{old_pid}:%(dead)d:TOK-B\\n", encoding="utf-8")
    session_pid.cleanup_orphaned_session_roots()
    platform_compat.pid_liveness = real_liveness
    roots = f"roots={len(path.read_text(encoding='utf-8').split())}"

    # 3. A retraction leftover the old image left in flight at the exec.
    name = f".session_pid_123.txt{session_pid._RETRACT_SUFFIX}{old_pid}-abcd"
    leftover = f"orphaned={session_pid._retraction_leftover_is_orphaned(name)}"

    print(os.getpid(), os.getppid(), still_live, sweep, roots, leftover)
    """) % {"dead": _DEAD_CHILD}

_OLD_GATEWAY = textwrap.dedent("""
    import os, subprocess, sys
    layout, new_gateway = sys.argv[1:3]
    from kiro_crew import platform_compat, session_pid
    path = session_pid._session_pid_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}:%(dead)d:TOK-B\\n", encoding="utf-8")
    start_id = platform_compat.get_process_start_id(os.getpid()) or ""
    child = [sys.executable, "-c", new_gateway, str(os.getpid()), start_id]
    if layout == "nested":
        relay = "import subprocess, sys; sys.exit(subprocess.run(sys.argv[1:]).returncode)"
        child = [sys.executable, "-c", relay, *child]
    if layout in ("exec", "nested"):
        # The trailing command keeps the shell alive as the supervisor.
        os.execv("/bin/sh", ["/bin/sh", "-c", '"$@"; exit $?', "sh", *child])
    sys.exit(subprocess.run(child).returncode)
    """) % {"dead": _DEAD_CHILD}


def _restart(layout: str, tmp_path: Path) -> tuple[int, int, dict[str, str]]:
    """Run the old/new gateway pair; return (old pid, new's parent, verdicts)."""
    home = tmp_path / "home"
    home.mkdir()
    old = subprocess.Popen(
        [sys.executable, "-c", _OLD_GATEWAY, layout, _NEW_GATEWAY],
        env={**os.environ, "KIROCREW_HOME": str(home)},
        cwd=tmp_path,
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
    _new_pid, new_parent, still_live, sweep, roots, leftover = out.split()
    # Without the old pid still live under the recorded identity, every check
    # reads the owner as gone and the test would pass for the wrong reason.
    assert still_live == "True", "the old gateway's pid did not survive with its identity"
    return old.pid, int(new_parent), {"sweep": sweep, "roots": roots, "leftover": leftover}


@posix_exec
class TestTheExecSupervisorsRowsAreThisGateways:
    def test_the_new_gateway_sweeps_what_its_exec_supervisor_left(self, tmp_path: Path) -> None:
        old, parent, verdicts = _restart("exec", tmp_path)
        assert parent == old, "the supervisor must be the exec-ed old gateway itself"
        assert verdicts == {"sweep": "rows=0", "roots": "roots=0", "leftover": "orphaned=True"}


@posix_exec
class TestALiveGatewayAncestorKeepsItsRows:
    def test_a_live_python_gateway_ancestor_keeps_them(self, tmp_path: Path) -> None:
        old, parent, verdicts = _restart("live", tmp_path)
        assert parent == old
        assert verdicts == {"sweep": "rows=1", "roots": "roots=1", "leftover": "orphaned=False"}

    def test_a_live_gateway_between_the_supervisor_and_the_reader_keeps_them(
        self, tmp_path: Path
    ) -> None:
        old, parent, verdicts = _restart("nested", tmp_path)
        assert parent != old, "a live Python gateway must sit between the two"
        assert verdicts == {"sweep": "rows=1", "roots": "roots=1", "leftover": "orphaned=False"}


class TestEachReaderWithTheSupervisorPinned:
    """Each reader's wiring, with the predicate pinned to one live pid."""

    _SUPERVISOR = 4242
    _OTHER_GATEWAY = 4343

    @pytest.fixture(autouse=True)
    def _supervisor(self, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
        live = {self._SUPERVISOR, self._OTHER_GATEWAY}
        _floor_monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: pid in live)
        _floor_monkeypatch.setattr(
            platform_compat,
            "pid_liveness",
            lambda pid: platform_compat.PID_ALIVE if pid in live else platform_compat.PID_DEAD,
        )
        _floor_monkeypatch.setattr(
            platform_compat,
            "is_exec_supervisor_of_this_process",
            lambda pid: pid == self._SUPERVISOR,
        )

    def test_the_sweep_handles_the_supervisors_row_and_keeps_a_live_gateways(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import session_pid

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(session_pid, "config_dir", lambda: home)
        monkeypatch.setattr(session_pid, "_cleanup_orphaned_mcp_servers", lambda: 0)
        rows = home / "kiro_session_pids.txt"
        mine = f"{self._SUPERVISOR}:{_DEAD_CHILD}:TOK-B"
        theirs = f"{self._OTHER_GATEWAY}:{_DEAD_CHILD + 1}:TOK-C"
        rows.write_text(f"{mine}\n{theirs}\n", encoding="utf-8")

        session_pid.cleanup_orphaned_sessions(narrow_with_leaders=False)

        assert rows.read_text(encoding="utf-8").split() == [theirs]

    def _root_rows(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *rows: str) -> Path:
        from kiro_crew import session_pid

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(session_pid, "config_dir", lambda: home)
        monkeypatch.setattr(platform_compat, "retry_pending_windows_process_trees", lambda: [])
        monkeypatch.setattr(
            platform_compat, "windows_tree_cleanup_pending", lambda _pid, _token: False
        )
        path = home / "kiro_session_pids.txt"
        path.write_text("".join(f"{row}\n" for row in rows), encoding="utf-8")
        return path

    def test_the_root_sweep_handles_the_supervisors_row_and_keeps_a_live_gateways(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import session_pid

        mine = f"{self._SUPERVISOR}:{_DEAD_CHILD}:TOK-B"
        theirs = f"{self._OTHER_GATEWAY}:{_DEAD_CHILD + 1}:TOK-C"
        twice = f"{self._SUPERVISOR}:{_DEAD_CHILD + 2}:TOK-D"
        rows = self._root_rows(tmp_path, monkeypatch, mine, twice, theirs)
        asked: list[int] = []
        real = platform_compat.is_exec_supervisor_of_this_process
        monkeypatch.setattr(
            platform_compat,
            "is_exec_supervisor_of_this_process",
            lambda pid: asked.append(pid) or real(pid),
        )

        session_pid.cleanup_orphaned_session_roots()

        assert rows.read_text(encoding="utf-8").split() == [theirs]
        # One ancestry walk per owner, not per row.
        assert sorted(asked) == [self._SUPERVISOR, self._OTHER_GATEWAY]

    def test_the_root_sweep_still_asks_the_argv_gate_for_the_supervisors_live_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import session_pid

        child = 6001
        live = {self._SUPERVISOR, self._OTHER_GATEWAY, child}
        monkeypatch.setattr(
            platform_compat,
            "pid_liveness",
            lambda pid: platform_compat.PID_ALIVE if pid in live else platform_compat.PID_DEAD,
        )
        monkeypatch.setattr(session_pid, "_pid_start_token", lambda _pid: "TOK-OTHER")
        monkeypatch.setattr(session_pid, "_is_managed_agent_process", lambda _pid: False)
        kills: list[int] = []
        monkeypatch.setattr(
            session_pid, "_kill_pid_tree", lambda pid: kills.append(pid) or (1, True)
        )
        recycled = f"{self._SUPERVISOR}:{child}:TOK-B"
        foreign = f"{self._SUPERVISOR}:{child}"
        rows = self._root_rows(tmp_path, monkeypatch, recycled, foreign)

        session_pid.cleanup_orphaned_session_roots()

        # A recycled token prunes and the argv gate refuses: nothing is signalled.
        assert kills == []
        assert rows.read_text(encoding="utf-8").split() == []

    def test_the_supervisors_leftover_is_orphaned_and_a_live_gateways_is_not(self) -> None:
        from kiro_crew import session_pid

        suffix = session_pid._RETRACT_SUFFIX
        assert session_pid._retraction_leftover_is_orphaned(
            f".session_pid_1.txt{suffix}{self._SUPERVISOR}-abcd"
        )
        assert not session_pid._retraction_leftover_is_orphaned(
            f".session_pid_1.txt{suffix}{self._OTHER_GATEWAY}-abcd"
        )

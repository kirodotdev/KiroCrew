"""The records this gateway's previous image left behind are this gateway's, even
while the supervisor an in-app restart left alive still holds that image's pid.

An in-app restart ``os.execv``s the launcher, and a launcher that runs the gateway
as a supervised CHILD (``toolbox-exec``) leaves the old pid alive as the new
gateway's ancestor, start identity unchanged. Four readers compare a recorded pid
against ``os.getpid()`` plus liveness or start identity, and each met that
supervisor as a LIVE owner whose identity still matched:

* ``runtime_reconcile``'s retraction left the previous image's dead session rows
  "not-mine" -- the next start's to clear once their owner reads dead, which the
  supervisor never does while this gateway runs.
* ``metrics.sessions._owner_still_running`` kept the previous image's start
  crumbs as live sessions, so their casualties were never reported.
* ``crash_dump_store._owner_alive`` and ``pid_identity_alive`` kept its header-only
  dump and its cron in-flight marker as a live owner's.
* ``transcribe._cleanup_stale_ffmpeg_snapshots`` kept its decoder snapshot.

Being an ancestor is not enough on its own: a gateway started from a shell that a
LIVE gateway spawned also has that gateway as an ancestor, and that gateway's
records must stand. What the exec changes is the image, and every gateway runs a
Python interpreter. The tests build both layouts from real processes, in the
shape ``test_inapp_restart_supervisor_not_a_rival.py`` uses: a Python "old
gateway" that writes each record with its real pid and start identity, then
really ``os.execv``s into a non-Python supervisor which runs the "new gateway" as
its child -- or stays a live Python gateway and runs it itself.
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

# Only POSIX keeps a pid across exec, so only there can a supervisor be left
# behind; the real-process layouts need a non-Python supervisor at /bin/sh.
posix_exec = pytest.mark.skipif(
    sys.platform == "win32" or not os.path.exists("/bin/sh"),
    reason="exec keeps the pid only on POSIX",
)

# A pid the reconcile check reads as dead. Its liveness is pinned in the new
# gateway, so the row's verdict comes only from its OWNER's reading.
_DEAD_CHILD = 5002

# The "new gateway". It reports its own pid, its parent, whether the old pid still
# reads live with the recorded identity, and the four verdicts.
_NEW_GATEWAY = textwrap.dedent("""
    import os, stat, sys
    from pathlib import Path
    old_pid, start_id, work = int(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    from kiro_crew import platform_compat
    still_live = (
        platform_compat.pid_exists(old_pid)
        and platform_compat.get_process_start_id(old_pid) == start_id
    )

    # 1. runtime_reconcile: the old image's row for a dead child.
    from kiro_crew import runtime_reconcile as rr, session_pid
    rr._mcp_backend_pids = lambda: set()
    rr.instance_slice_pids = lambda: set()
    real_liveness = platform_compat.pid_liveness
    platform_compat.pid_liveness = (
        lambda pid: platform_compat.PID_DEAD if pid == %(dead)d else real_liveness(pid)
    )
    reading = rr.build_reconciler(active_pids=lambda: set(), notify_dead=lambda pid: None).run_once()
    platform_compat.pid_liveness = real_liveness
    rows = session_pid._session_pid_file_path().read_text(encoding="utf-8").split()
    reconcile = f"forgotten={reading.forgotten},rows={len(rows)}"

    # 2. metrics.sessions: the old image's start crumb.
    from kiro_crew.metrics import sessions
    metrics = str(sessions._owner_still_running(old_pid, start_id))

    # 3. crash_dump_store: the old image's header-only dump and in-flight identity.
    from kiro_crew.dashboard import crash_dump_store as cds
    dump = work / "loopstall-old.txt"
    dump.write_text(
        f"# kirocrew loopstall dump\\n# PID: {old_pid} @ {cds._pid_domain()} start={start_id}\\n"
        "# header\\n\\n",
        encoding="utf-8",
    )
    owner_alive = cds._owner_alive(dump, cds.pid_exists)
    identity_alive = cds.pid_identity_alive(old_pid, cds._pid_domain(), start_id)
    swept = cds.sweep_stale_dumps(work)
    dumps = f"owner={owner_alive},identity={identity_alive},swept={swept}"

    # 4. transcribe: the old image's decoder snapshot.
    from kiro_crew import transcribe
    root = work / "snapshots"
    root.mkdir(mode=0o700)
    snap = root / f".kirocrew-ffmpeg-{old_pid}-abc"
    snap.mkdir(mode=0o700)
    (snap / "ffmpeg").write_bytes(b"")
    transcribe._cleanup_stale_ffmpeg_snapshots(str(root))
    snapshot = f"removed={not snap.exists()}"

    print(os.getpid(), os.getppid(), still_live, reconcile, metrics, dumps, snapshot)
    """) % {"dead": _DEAD_CHILD}

# The "old gateway". It files one session row for a dead child under its own pid,
# as a running gateway does, then either becomes a non-Python supervisor through
# a real os.execv (pid and start identity kept) or stays a live Python gateway.
_OLD_GATEWAY = textwrap.dedent("""
    import os, subprocess, sys
    layout, work, new_gateway = sys.argv[1:4]
    from kiro_crew import platform_compat, session_pid
    path = session_pid._session_pid_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}:%(dead)d:TOK-B\\n", encoding="utf-8")
    start_id = platform_compat.get_process_start_id(os.getpid()) or ""
    child = [sys.executable, "-c", new_gateway, str(os.getpid()), start_id, work]
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
    """) % {"dead": _DEAD_CHILD}


def _restart(layout: str, tmp_path: Path) -> tuple[int, int, int, dict[str, str]]:
    """Run the old/new gateway pair; return (old pid, new pid, new's parent, verdicts)."""
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    old = subprocess.Popen(
        [sys.executable, "-c", _OLD_GATEWAY, layout, str(work), _NEW_GATEWAY],
        env={**os.environ, "KIROCREW_HOME": str(home)},
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
    new_pid, new_parent, still_live, reconcile, metrics, dumps, snapshot = out.split()
    # Without the old pid still live under the recorded identity, every check
    # reads the owner as gone and the test would pass for the wrong reason.
    assert still_live == "True", "the old gateway's pid did not survive with its identity"
    verdicts = {"reconcile": reconcile, "metrics": metrics, "dumps": dumps, "snapshot": snapshot}
    return old.pid, int(new_pid), int(new_parent), verdicts


@posix_exec
class TestTheExecSupervisorsRecordsAreThisGateways:
    def test_the_new_gateway_owns_what_its_exec_supervisor_left(self, tmp_path: Path) -> None:
        old, _new, parent, verdicts = _restart("exec", tmp_path)
        assert parent == old, "the supervisor must be the exec-ed old gateway itself"
        assert verdicts["reconcile"] == "forgotten=1,rows=0", verdicts
        assert verdicts["metrics"] == "False", verdicts
        assert verdicts["dumps"] == "owner=False,identity=False,swept=1", verdicts
        assert verdicts["snapshot"] == "removed=True", verdicts


@posix_exec
class TestALiveGatewayAncestorKeepsItsRecords:
    def test_a_live_python_gateway_ancestor_keeps_them(self, tmp_path: Path) -> None:
        # A gateway started from a shell that a live gateway spawned: that
        # gateway is an ancestor, still a gateway, and its records must stand.
        old, _new, parent, verdicts = _restart("live", tmp_path)
        assert parent == old
        assert verdicts["reconcile"] == "forgotten=0,rows=1", verdicts
        assert verdicts["metrics"] == "True", verdicts
        assert verdicts["dumps"] == "owner=True,identity=True,swept=0", verdicts
        assert verdicts["snapshot"] == "removed=False", verdicts

    def test_a_live_gateway_between_the_supervisor_and_the_reader_keeps_them(
        self, tmp_path: Path
    ) -> None:
        # The supervisor's own gateway started this one: the records name the
        # supervisor, but the Python process in between is a live gateway.
        old, _new, parent, verdicts = _restart("nested", tmp_path)
        assert parent != old, "a live Python gateway must sit between the two"
        assert verdicts["reconcile"] == "forgotten=0,rows=1", verdicts
        assert verdicts["metrics"] == "True", verdicts
        assert verdicts["dumps"] == "owner=True,identity=True,swept=0", verdicts
        assert verdicts["snapshot"] == "removed=False", verdicts


class TestEachReaderWithTheSupervisorPinned:
    """Each reader's wiring, with the predicate pinned to one live pid; the
    predicate itself is proved on real processes above."""

    _SUPERVISOR = 4242

    @pytest.fixture(autouse=True)
    def _supervisor(self, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
        _floor_monkeypatch.setattr(
            platform_compat, "pid_exists", lambda pid: pid == self._SUPERVISOR
        )
        _floor_monkeypatch.setattr(
            platform_compat,
            "pid_liveness",
            lambda pid: (
                platform_compat.PID_ALIVE if pid == self._SUPERVISOR else platform_compat.PID_DEAD
            ),
        )
        _floor_monkeypatch.setattr(
            platform_compat,
            "is_exec_supervisor_of_this_process",
            lambda pid: pid == self._SUPERVISOR,
        )

    def test_the_reconciler_retracts_the_supervisors_dead_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import runtime_reconcile as rr
        from kiro_crew import session_pid

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(session_pid, "config_dir", lambda: home)
        rows = home / "kiro_session_pids.txt"
        rows.write_text(f"{self._SUPERVISOR}:{_DEAD_CHILD}:TOK-B\n", encoding="utf-8")
        monkeypatch.setattr(rr, "_mcp_backend_pids", lambda: set())
        monkeypatch.setattr(rr, "instance_slice_pids", lambda: set())

        reading = rr.build_reconciler(
            active_pids=lambda: set(), notify_dead=lambda pid: None
        ).run_once()

        assert reading.forgotten == 1, "the previous image's row is this gateway's to retract"
        assert rows.read_text(encoding="utf-8").strip() == ""

    def test_the_supervisor_is_not_running_its_crumbs_sessions(self) -> None:
        from kiro_crew.metrics import sessions

        assert sessions._owner_still_running(self._SUPERVISOR, "any-start-id") is False

    def test_the_supervisors_dump_and_marker_have_no_live_owner(self, tmp_path: Path) -> None:
        from kiro_crew.dashboard import crash_dump_store as cds

        dump = tmp_path / "loopstall-old.txt"
        dump.write_text(
            f"# dump\n# PID: {self._SUPERVISOR} @ {cds._pid_domain()}\n# header\n\n",
            encoding="utf-8",
        )
        assert cds._owner_alive(dump, platform_compat.pid_exists) is False
        assert cds.pid_identity_alive(self._SUPERVISOR, cds._pid_domain(), None) is False

    def test_the_supervisors_decoder_snapshot_is_pruned(self, tmp_path: Path) -> None:
        from kiro_crew import transcribe

        snap = tmp_path / f".kirocrew-ffmpeg-{self._SUPERVISOR}-abc"
        snap.mkdir(mode=0o700)
        transcribe._cleanup_stale_ffmpeg_snapshots(str(tmp_path))
        assert not snap.exists()

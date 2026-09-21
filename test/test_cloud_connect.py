"""Unit tests for connect over SSM (cloud/connect.py)."""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from kiro_crew import platform_compat as pc
from kiro_crew.cloud import aws, connect, ssm


class TestMintToken:
    def test_parses_token_from_url(self, monkeypatch):
        out = "http://localhost:5476/?token=abc.def.ghi"
        monkeypatch.setattr(
            ssm, "run_command", lambda *a, **k: ssm.CommandResult("Success", out, "", 0)
        )
        assert connect.mint_token("i-0abc", "dev") == "abc.def.ghi"

    def test_empty_when_no_token(self, monkeypatch):
        monkeypatch.setattr(
            ssm, "run_command", lambda *a, **k: ssm.CommandResult("Success", "no url", "", 0)
        )
        assert connect.mint_token("i-0abc", "dev") == ""

    def test_remote_grep_matches_token_marker_not_hostname(self, monkeypatch):
        # The on-box grep must key on `token=`, not `localhost` — the printed
        # hostname is presentation and could become 127.0.0.1.
        captured = {}

        def fake_run_command(_iid, command, *_a, **_k):
            captured["command"] = command
            return ssm.CommandResult("Success", "http://127.0.0.1:5476/?token=tok123", "", 0)

        monkeypatch.setattr(ssm, "run_command", fake_run_command)
        assert connect.mint_token("i-0abc", "dev") == "tok123"
        assert "grep -m1 'token='" in captured["command"]
        assert "localhost" not in captured["command"]


class TestBuildUrl:
    def test_with_token(self):
        assert connect.build_url(5599, "tok") == "http://127.0.0.1:5599/?token=tok"

    def test_without_token(self):
        assert connect.build_url(5599, "") == "http://127.0.0.1:5599/"


class TestSafeTtl:
    def test_valid(self):
        assert connect._safe_ttl("6h") == "6h"
        assert connect._safe_ttl("30m") == "30m"

    def test_invalid_falls_back(self):
        assert connect._safe_ttl("evil; rm -rf") == "6h"
        assert connect._safe_ttl("") == "6h"


class TestConnect:
    def test_connect_opens_tunnel_and_browser(self, monkeypatch):
        monkeypatch.setattr(ssm, "require_session_manager_plugin", lambda: None)
        monkeypatch.setattr(ssm, "port_is_free", lambda *a, **k: True)
        monkeypatch.setattr(connect, "mint_token", lambda *a, **k: "tok")

        class FakeProc:
            def poll(self):
                return None

        monkeypatch.setattr(ssm, "open_port_forward", lambda *a, **k: FakeProc())
        monkeypatch.setattr(ssm, "wait_for_local_port", lambda *a, **k: True)
        monkeypatch.setattr(connect, "_browser_open_supported", lambda: True)
        opened = {}
        monkeypatch.setattr(connect.webbrowser, "open", lambda u, **_k: opened.update(url=u))
        conn = connect.connect("i-0abc", "dev", "us-east-1")
        assert conn.token == "tok"
        assert conn.url == "http://127.0.0.1:5599/?token=tok"
        assert conn.ready is True
        assert opened["url"] == conn.url

    def test_connect_no_browser(self, monkeypatch):
        monkeypatch.setattr(ssm, "require_session_manager_plugin", lambda: None)
        monkeypatch.setattr(ssm, "port_is_free", lambda *a, **k: True)
        monkeypatch.setattr(connect, "mint_token", lambda *a, **k: "tok")
        monkeypatch.setattr(ssm, "open_port_forward", lambda *a, **k: None)
        monkeypatch.setattr(ssm, "wait_for_local_port", lambda *a, **k: True)
        called = {"opened": False}
        monkeypatch.setattr(connect, "_browser_open_supported", lambda: True)
        monkeypatch.setattr(connect.webbrowser, "open", lambda u, **_k: called.update(opened=True))
        connect.connect("i-0abc", "dev", open_browser=False)
        assert called["opened"] is False

    def test_connect_refuses_when_local_port_occupied(self, monkeypatch):
        # If the local port is already taken, we must NOT mint a token, spawn,
        # or open a browser — a foreign listener would otherwise receive the JWT.
        monkeypatch.setattr(ssm, "require_session_manager_plugin", lambda: None)
        monkeypatch.setattr(ssm, "port_is_free", lambda *a, **k: False)
        minted = {"n": 0}
        monkeypatch.setattr(
            connect, "mint_token", lambda *a, **k: minted.update(n=minted["n"] + 1) or "tok"
        )

        def _boom(*a, **k):  # pragma: no cover - must not be called
            raise AssertionError("must not spawn a tunnel on an occupied port")

        monkeypatch.setattr(ssm, "open_port_forward", _boom)
        conn = connect.connect("i-0abc", "dev", "us-east-1")
        assert conn.ready is False
        assert conn.token == ""
        assert conn.url == ""
        assert "already in use" in conn.error
        assert minted["n"] == 0  # never minted a token

    def test_connect_returns_not_ready_without_url(self, monkeypatch):
        monkeypatch.setattr(ssm, "require_session_manager_plugin", lambda: None)
        monkeypatch.setattr(ssm, "port_is_free", lambda *a, **k: True)
        minted = {"n": 0}
        monkeypatch.setattr(
            connect, "mint_token", lambda *a, **k: minted.update(n=minted["n"] + 1) or "tok"
        )

        class FakeProc:
            terminated = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

        proc = FakeProc()
        monkeypatch.setattr(ssm, "open_port_forward", lambda *a, **k: proc)
        monkeypatch.setattr(ssm, "wait_for_local_port", lambda *a, **k: False)
        monkeypatch.setattr(connect, "_open_browser", lambda *_a: True)
        conn = connect.connect("i-0abc", "dev", "us-east-1")
        assert conn.ready is False
        assert conn.url == ""
        assert "did not become ready" in conn.error
        assert proc.terminated is True
        # Deferred mint: a tunnel that never became ready must NOT mint a token
        # (which would otherwise linger in SSM command history for its TTL).
        assert minted["n"] == 0
        assert conn.token == ""

    def test_connect_refuses_when_child_died_but_port_answers(self, monkeypatch):
        # Residual free-check->bind race: the port answers, but our SSM child
        # exited (a foreign process won the bind). We must NOT open the token URL
        # against that stranger, even though wait_for_local_port returned True.
        monkeypatch.setattr(ssm, "require_session_manager_plugin", lambda: None)
        monkeypatch.setattr(ssm, "port_is_free", lambda *a, **k: True)
        monkeypatch.setattr(connect, "mint_token", lambda *a, **k: "tok")

        class DeadProc:
            returncode = 1

            def poll(self):
                return 1  # already exited

        monkeypatch.setattr(ssm, "open_port_forward", lambda *a, **k: DeadProc())
        monkeypatch.setattr(ssm, "wait_for_local_port", lambda *a, **k: True)
        opened = {"n": 0}
        monkeypatch.setattr(connect, "_open_browser", lambda *_a: opened.update(n=opened["n"] + 1))
        conn = connect.connect("i-0abc", "dev", "us-east-1")
        assert conn.ready is False
        assert conn.url == ""
        assert opened["n"] == 0  # never opened the token URL

    def test_connect_tears_down_tunnel_when_mint_fails(self, monkeypatch):
        # Tunnel comes up ready but mint_token returns "" (e.g. `kirocrew token`
        # failed on the box). A ready tunnel with no URL is useless and would
        # leak the SSM child; connect() must tear it down and report ready=False.
        monkeypatch.setattr(ssm, "require_session_manager_plugin", lambda: None)
        monkeypatch.setattr(ssm, "port_is_free", lambda *a, **k: True)
        monkeypatch.setattr(connect, "mint_token", lambda *a, **k: "")  # mint fails

        class FakeProc:
            terminated = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                return 0

        proc = FakeProc()
        monkeypatch.setattr(ssm, "open_port_forward", lambda *a, **k: proc)
        monkeypatch.setattr(ssm, "wait_for_local_port", lambda *a, **k: True)
        opened = {"n": 0}
        monkeypatch.setattr(connect, "_open_browser", lambda *_a: opened.update(n=opened["n"] + 1))
        conn = connect.connect("i-0abc", "dev", "us-east-1")
        assert conn.ready is False
        assert conn.url == ""
        assert conn.token == ""
        assert "could not mint" in conn.error
        assert proc.terminated is True  # no orphaned tunnel
        assert opened["n"] == 0

    def test_connect_tears_down_tunnel_when_mint_raises(self, monkeypatch):
        # mint_token goes through the aws chokepoint, which RAISES (AWSError) on
        # e.g. ssm:SendCommand AccessDenied. That exception must NOT escape
        # connect() with the tunnel child still alive (orphaned plugin + bound
        # port). connect() must tear it down and return ready=False + error —
        # the same contract as the empty-token path, just via an exception.
        monkeypatch.setattr(ssm, "require_session_manager_plugin", lambda: None)
        monkeypatch.setattr(ssm, "port_is_free", lambda *a, **k: True)

        def _raise(*a, **k):
            raise aws.AWSError("ssm:SendCommand denied", action="ssm:SendCommand")

        monkeypatch.setattr(connect, "mint_token", _raise)

        class FakeProc:
            terminated = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                return 0

        proc = FakeProc()
        monkeypatch.setattr(ssm, "open_port_forward", lambda *a, **k: proc)
        monkeypatch.setattr(ssm, "wait_for_local_port", lambda *a, **k: True)
        opened = {"n": 0}
        monkeypatch.setattr(connect, "_open_browser", lambda *_a: opened.update(n=opened["n"] + 1))
        # Must NOT propagate the AWSError — it's folded into the Connection.
        conn = connect.connect("i-0abc", "dev", "us-east-1")
        assert conn.ready is False
        assert conn.url == ""
        assert conn.token == ""
        assert "minting a dashboard token failed" in conn.error
        assert "ssm:SendCommand denied" in conn.error
        assert proc.terminated is True  # no orphaned tunnel
        assert opened["n"] == 0

    def test_connect_fails_fast_without_session_manager_plugin(self, monkeypatch):
        def raise_missing():
            raise aws.AWSError("session-manager-plugin missing", action="ssm:StartSession")

        monkeypatch.setattr(ssm, "require_session_manager_plugin", raise_missing)
        monkeypatch.setattr(
            connect,
            "mint_token",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not mint token")),
        )

        with pytest.raises(aws.AWSError, match="session-manager-plugin"):
            connect.connect("i-0abc", "dev", "us-east-1")


#: Stands in for what `trusted_system_bin` returns on Windows; never spawned.
TASKKILL_BIN = r"C:\Windows\System32	askkill.exe"


class TestKillProcessTree:
    def test_kills_whole_group_not_just_parent(self, tmp_path):
        # The SSM tunnel is spawned with start_new_session=True, so the parent
        # `aws` process leads its own group and the plugin child is in that group.
        # _kill_process_tree must reap the WHOLE tree — a plain proc.terminate()
        # would leave the child (holding the local port) alive. The reap mechanism
        # differs per platform (POSIX killpg vs. Windows `taskkill /T`, since
        # start_new_session is silently ignored there) but the invariant asserted
        # here — no descendant survives teardown — is identical on both.
        import subprocess
        import sys
        import time

        # Parent spawns a grandchild `sleep`, writes its pid, then waits — so the
        # tree has two members. Start it in its own session (mirrors
        # open_port_forward's start_new_session=True).
        pidfile = tmp_path / "child.pid"
        script = (
            "import os,subprocess,sys,time;"
            f"c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
            f"open({str(pidfile)!r},'w').write(str(c.pid));"
            "time.sleep(30)"
        )
        # cwd=tmp_path: the wrapper and its grandchild write nothing by path,
        # but a child inherits pytest's CWD (the checkout) unless told otherwise.
        proc = subprocess.Popen(
            [sys.executable, "-c", script], start_new_session=True, cwd=tmp_path
        )
        # Bound BEFORE the try: the pidfile read below raises FileNotFoundError /
        # ValueError whenever the 5s poll budget expires on a loaded host, and the
        # finally must still be able to skip the grandchild reap in that case.
        child_pid: int | None = None
        child_start_id: str | None = None
        # Set once the body has PROVEN the grandchild is gone. The finally must not
        # signal a pid whose death it already confirmed: that pid is free for the
        # kernel to reassign the instant it exits, so a SIGKILL sent "just in case"
        # on the passing path is aimed at whatever process now holds the number --
        # every green run, not a rare race.
        grandchild_reaped = False
        try:
            # Wait for the grandchild pid to be recorded.
            for _ in range(50):
                if pidfile.exists() and pidfile.read_text(encoding="utf-8").strip():
                    break
                time.sleep(0.1)
            child_pid = int(pidfile.read_text(encoding="utf-8").strip())
            # Identity, captured WITH the pid. A pid alone is not a handle: the
            # kernel may reassign it the moment the process exits, so the reap in
            # the finally must be able to prove the number still names the process
            # this test spawned. `None` means unknown, and unknown means do not
            # signal -- the rule `get_process_start_id`'s own docstring states.
            child_start_id = pc.get_process_start_id(child_pid)
            assert _pid_alive(child_pid), "grandchild should be alive before teardown"

            connect._kill_process_tree(proc)

            # Both the parent and the grandchild must be gone.
            assert proc.poll() is not None, "parent should be reaped"
            # Poll: the tree kill is asynchronous w.r.t. the grandchild exiting.
            for _ in range(50):
                if not _pid_alive(child_pid):
                    break
                time.sleep(0.1)
            assert not _pid_alive(child_pid), "grandchild (same tree) must also be killed"
            grandchild_reaped = True
        finally:
            # `connect._kill_process_tree` is the ONLY reaper in the body above and
            # it is the code under test, so every failing exit — the pidfile read
            # raising when the 5s poll budget expires, either assertion, a real
            # regression in the tree kill — abandons a live `time.sleep(30)`
            # wrapper AND its grandchild for 30s past the test. Both sit in their
            # own session thanks to start_new_session=True, i.e. in a group no
            # run-level sweep of the xdist worker's group can reach. Reap through
            # `platform_compat` (`killpg` on POSIX, `taskkill /T /F` on Windows) —
            # an implementation independent of `ssm.kill_port_forward`, so the
            # broken subject cannot also break its own cleanup. Group first, then
            # the grandchild by pid, and ONLY when the body did not already prove it
            # dead: once the wrapper has been reaped, getpgid(proc.pid) fails and the
            # reparented grandchild is reachable only by its own pid — which is
            # exactly the regression this test is written to catch.
            with contextlib.suppress(ProcessLookupError, OSError):
                pc.kill_process_tree(proc.pid, pc.SIGKILL)
            # Revalidate identity immediately before the pid-scoped signal: between
            # the last poll and here the grandchild may have exited and its number
            # been handed to something else on the host, and this is a SIGKILL.
            if (
                child_pid is not None
                and not grandchild_reaped
                and child_start_id is not None
                and pc.get_process_start_id(child_pid) == child_start_id
            ):
                with contextlib.suppress(ProcessLookupError, OSError):
                    pc.kill_pid(child_pid, pc.SIGKILL)
            if proc.returncode is None:
                # Bounded: SIGKILL cannot be blocked, so this returns at once —
                # the ceiling only exists so a wedged wait in a `finally` cannot
                # replace the real assertion failure with a hang.
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=10)

    def test_windows_uses_a_tree_kill_not_a_parent_only_terminate(self, monkeypatch):
        """On Windows the group signal can never work, so the tree kill must run.

        ``os.killpg``/``os.getpgid`` do not exist on Windows, so the POSIX path
        falls through to ``proc.terminate()`` -- which kills only the wrapper and
        leaves the plugin holding the forwarded port, the exact failure
        ``kill_port_forward`` exists to prevent. Exercised on any platform by
        forcing ``os.name``.
        """
        from kiro_crew.cloud import ssm as ssm_mod

        calls: list[list[str]] = []

        class FakeProc:
            pid = 4321
            terminated = False

            def poll(self):
                return None

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                FakeProc.terminated = True

            def kill(self):  # pragma: no cover - must not be reached
                FakeProc.terminated = True

        def fake_run(argv, **kwargs):
            calls.append(argv)

            class R:
                returncode = 0

            return R()

        monkeypatch.setattr(ssm_mod.os, "name", "nt")
        monkeypatch.setattr(ssm_mod.subprocess, "run", fake_run)
        # `taskkill` is resolved through `platform_compat.trusted_system_bin`, so
        # the real lookup answers None off Windows. Stubbed, or the tree kill is
        # skipped and this test falls through to the POSIX branch -- which would
        # `killpg` the REAL pid 4321 on a CI host, taking the worker with it.
        monkeypatch.setattr(
            ssm_mod.platform_compat, "trusted_system_bin", lambda name: TASKKILL_BIN
        )
        ssm_mod.kill_port_forward(FakeProc())

        assert calls, "Windows must attempt a tree kill"
        argv = calls[0]
        assert argv[0] == TASKKILL_BIN
        assert argv[0] != "taskkill", "the binary must not come from PATH"
        assert "/T" in argv, "/T is what reaps the plugin child"
        assert "/F" in argv
        assert str(FakeProc.pid) in argv
        assert not FakeProc.terminated, "parent-only terminate must not be the Windows path"

    def test_windows_tree_kill_tolerates_a_process_object_without_a_pid(self, monkeypatch) -> None:
        """A Popen-LIKE stand-in must not raise on the Windows branch.

        `kill_port_forward` accepts any object with poll/terminate/wait -- the
        POSIX branch already tolerates one with no `pid` (its group signal catches
        AttributeError and falls back). The Windows branch read `proc.pid`
        directly, so the same caller worked on Linux and raised AttributeError on
        Windows.
        """
        from kiro_crew.cloud import ssm as ssm_mod

        class NoPid:
            terminated = False

            def poll(self):
                return None

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                NoPid.terminated = True

        monkeypatch.setattr(ssm_mod.os, "name", "nt")
        ssm_mod.kill_port_forward(NoPid())  # must not raise
        assert NoPid.terminated, "should fall back to terminate() when there is no pid"

    def test_none_and_dead_proc_are_noops(self):
        connect._kill_process_tree(None)  # must not raise

        class Dead:
            def poll(self):
                return 0

        connect._kill_process_tree(Dead())  # already exited → no-op


def _pid_alive(pid: int) -> bool:
    """Liveness probe that is correct on both platforms.

    NOT ``os.kill(pid, 0)``: signal 0 is ``signal.CTRL_C_EVENT`` on Windows, so
    CPython routes it to ``GenerateConsoleCtrlEvent`` — a console-group signal
    whose return value is unrelated to whether ``pid`` exists. It reports "alive"
    for every already-dead pid, so a teardown assertion built on it can never
    observe the reap. ``platform_compat.pid_exists`` uses ``OpenProcess`` +
    ``GetExitCodeProcess`` there and ``os.kill(pid, 0)`` on POSIX.
    """
    return pc.pid_exists(pid)


class TestRegistryIntegration:
    def test_register_instance(self, monkeypatch, tmp_path):
        from kiro_crew.instances.registry import InstancesRegistry

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        monkeypatch.setattr(connect, "InstancesRegistry", InstancesRegistry, raising=False)
        # Patch the lazy import target: connect imports InstancesRegistry inside
        # the function, so patch the class used there via the module.
        import kiro_crew.instances.registry as regmod

        monkeypatch.setattr(regmod, "InstancesRegistry", lambda *a, **k: reg)

        rid = connect.register_instance(
            "i-0abc1234", name="Kiro Crew Cloud", profile="dev", region="us-west-2"
        )
        assert rid is not None
        # Registers over the native SSM transport, not the legacy ssh_host path.
        inst = next(i for i in reg.list() if i.id == rid)
        assert inst.connection_method == "ssm"
        assert inst.ssm_target == "i-0abc1234"
        assert inst.aws_profile == "dev"
        assert inst.aws_region == "us-west-2"
        assert inst.ssh_host == ""
        assert inst.provisioner_id == "aws_ec2"

    def test_register_instance_is_idempotent_on_relaunch(self, monkeypatch, tmp_path):
        from kiro_crew.instances.registry import InstancesRegistry

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        import kiro_crew.instances.registry as regmod

        monkeypatch.setattr(regmod, "InstancesRegistry", lambda *a, **k: reg)

        first = connect.register_instance("i-0abc1234", name="Kiro Crew Cloud")
        assert first is not None
        # Simulate persisted per-instance state a re-launch must NOT wipe:
        # customized TTL, an allocated local port, and sticky connect intent.
        reg.update(first, ttl="30m", local_port=5599, was_connected=True)

        second = connect.register_instance("i-0abc1234", name="Kiro Crew Cloud")
        # Re-launch updates in place: same id, no duplicate, state preserved.
        assert second == first
        matches = [i for i in reg.list() if i.ssm_target == "i-0abc1234"]
        assert len(matches) == 1
        rec = matches[0]
        assert rec.ttl == "30m"
        assert rec.local_port == 5599
        assert rec.was_connected is True
        assert rec.provisioner_id == "aws_ec2"

    def test_unregister_instance_empty_arg_is_noop(self, monkeypatch, tmp_path):
        from kiro_crew.instances.registry import InstancesRegistry

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        import kiro_crew.instances.registry as regmod

        monkeypatch.setattr(regmod, "InstancesRegistry", lambda *a, **k: reg)
        # An SSM record (ssh_host="") and an SSH record (ssm_target="") coexist.
        connect.register_instance("i-0abc1234", name="Kiro Crew Cloud")
        reg.add(name="dev-box", ssh_host="dev-box")

        # An empty needle must NOT match an empty transport field of either record.
        assert connect.unregister_instance("") is False
        assert len(reg.list()) == 2

    def test_unregister_instance_ssm(self, monkeypatch, tmp_path):
        from kiro_crew.instances.registry import InstancesRegistry

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        import kiro_crew.instances.registry as regmod

        monkeypatch.setattr(regmod, "InstancesRegistry", lambda *a, **k: reg)

        connect.register_instance("i-0abc1234", name="Kiro Crew Cloud")
        # Removal matches on ssm_target (the native registration).
        assert connect.unregister_instance("i-0abc1234") is True
        assert not any(i.ssm_target == "i-0abc1234" for i in reg.list())

    def test_unregister_instance_legacy_ssh_host(self, monkeypatch, tmp_path):
        from kiro_crew.instances.registry import InstancesRegistry

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        # A box registered the old way (ssh_host = instance id) still unregisters.
        reg.add(name="Kiro Crew Cloud", ssh_host="i-0abc")
        import kiro_crew.instances.registry as regmod

        monkeypatch.setattr(regmod, "InstancesRegistry", lambda *a, **k: reg)

        assert connect.unregister_instance("i-0abc") is True
        assert not any(i.ssh_host == "i-0abc" for i in reg.list())


class TestIsLaunchedInstance:
    """Unit coverage for is_launched_instance() — the correlation check
    handlers_instances.py uses to lock PATCH's addressing fields."""

    def _store(self, monkeypatch, tmp_path):
        import kiro_crew.cloud.launch_job as ljmod

        store = ljmod.LaunchJobStore(root=tmp_path / "launch-jobs")
        monkeypatch.setattr(ljmod, "LaunchJobStore", lambda *a, **k: store)
        return store

    def test_empty_target_is_not_launched(self):
        assert connect.is_launched_instance("") is False

    def test_matching_job_is_launched(self, monkeypatch, tmp_path):
        store = self._store(monkeypatch, tmp_path)
        job = store.create(profile="dev", region="us-west-2", size_key="light")
        job.instance_id = "i-0abc1234"
        store.save(job)

        assert connect.is_launched_instance("i-0abc1234") is True

    def test_unrelated_target_is_not_launched(self, monkeypatch, tmp_path):
        # A hand-added SSM record whose ssm_target happens to look like an EC2 id
        # but was never provisioned by a Kiro Crew launch job.
        store = self._store(monkeypatch, tmp_path)
        job = store.create(profile="dev", region="us-west-2", size_key="light")
        job.instance_id = "i-0abc1234"
        store.save(job)

        assert connect.is_launched_instance("i-neverlaunched") is False

    def test_no_jobs_at_all_is_not_launched(self, monkeypatch, tmp_path):
        self._store(monkeypatch, tmp_path)
        assert connect.is_launched_instance("i-0abc1234") is False

    def test_malformed_job_file_fails_closed(self, monkeypatch, tmp_path):
        # Fails CLOSED on a job file that cannot be PARSED. This is the failure
        # mode LaunchJobStore.list() hides: list() defers to get(), which logs
        # and returns None for an unparseable file, so the job drops out of the
        # result entirely — a corrupted record for the very instance being
        # edited would read as "no such launch job" and unlock the addressing
        # fields this check exists to freeze. Reading the files directly is
        # what keeps the refusal reachable.
        store = self._store(monkeypatch, tmp_path)
        job = store.create(profile="dev", region="us-west-2", size_key="light")
        job.instance_id = "i-0abc1234"
        store.save(job)
        (store.root / f"{job.id}.json").write_text("{not json", encoding="utf-8")

        with pytest.raises(ValueError):
            connect.is_launched_instance("i-0abc1234")

    def test_unreadable_job_file_fails_closed(self, monkeypatch, tmp_path):
        # Same guarantee for an I/O failure rather than a parse failure: the
        # OSError propagates so api_instances_update answers 503 and persists
        # nothing, instead of treating an unverifiable correlation as absent.
        store = self._store(monkeypatch, tmp_path)
        store.root.mkdir(parents=True, exist_ok=True)
        # A directory where a job file is expected: read_text() raises OSError
        # on every platform (IsADirectoryError on POSIX, PermissionError on
        # Windows), without depending on chmod semantics or the test user.
        (store.root / "00000000000000000000000000000000.json").mkdir()

        with pytest.raises(OSError):
            connect.is_launched_instance("i-0abc1234")

    def test_vanished_job_file_is_skipped(self, monkeypatch, tmp_path):
        # The one benign case: a concurrent `cloud destroy` removing a job
        # between the glob and the read. An instance whose launch record is
        # gone is not correlated to anything, so the scan continues to
        # the remaining jobs rather than refusing the edit.
        store = self._store(monkeypatch, tmp_path)
        gone = store.create(profile="dev", region="us-west-2", size_key="light")
        gone.instance_id = "i-vanishing"
        store.save(gone)
        keep = store.create(profile="dev", region="us-west-2", size_key="light")
        keep.instance_id = "i-stillhere"
        store.save(keep)

        victim = store.root / f"{gone.id}.json"
        real_read_text = Path.read_text

        def _read_text(self, *a, **k):
            if self == victim:
                raise FileNotFoundError(str(victim))
            return real_read_text(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", _read_text)

        assert connect.is_launched_instance("i-stillhere") is True
        assert connect.is_launched_instance("i-vanishing") is False


# ── The Fargate lane ──────────────────────────────────────────────────────────

_TASK = "0123456789abcdef0123456789abcdef"
_ECS_TARGET = f"ecs:crews_{_TASK}_{_TASK}-1234567890"


class _LiveProc:
    def poll(self):
        return None


class _ExitedProc:
    returncode = 1

    def poll(self):
        return 1

    def terminate(self):
        pass


def _ready(monkeypatch):
    """Make the preflight pass, so a test can exercise what comes after it."""
    monkeypatch.setattr(ssm, "task_exec_readiness", lambda *a, **k: ssm.TaskExecReadiness(True))


class TestEcsTargetSplit:
    def test_returns_the_three_parts(self):
        from kiro_crew.instances.validation import split_ecs_target

        assert split_ecs_target(_ECS_TARGET) == ("crews", _TASK, f"{_TASK}-1234567890")

    def test_a_cluster_containing_underscores_is_read_whole(self):
        """The reason this helper exists instead of a split on '_'.

        A cluster name may contain underscores, so splitting on the first two
        separators takes only part of the name and splitting on the last two works
        by accident. The pattern's own groups cannot get this wrong.
        """
        from kiro_crew.instances.validation import split_ecs_target

        target = f"ecs:my_prod_crews_{_TASK}_{_TASK}-42"
        assert split_ecs_target(target) == ("my_prod_crews", _TASK, f"{_TASK}-42")

    @pytest.mark.parametrize(
        "bad", ["i-0123456789abcdef0", f"ecs:crews_{_TASK}_{_TASK}-1234567890 --profile admin", ""]
    )
    def test_no_parts_come_out_of_a_value_the_validator_would_reject(self, bad):
        from kiro_crew.instances.validation import split_ecs_target

        assert split_ecs_target(bad) is None


class TestTaskExecReadiness:
    def test_ready_when_the_channel_is_on_and_the_agent_is_running(self, monkeypatch):
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (0, "True\tRUNNING", ""))
        assert ssm.task_exec_readiness("crews", _TASK).ready is True

    def test_a_task_without_the_channel_says_relaunch_rather_than_retry(self, monkeypatch):
        """ECS cannot enable it on a running task, so the message must not invite a retry.

        Asserted on the MESSAGE, because that is the only thing anything acts on: no
        caller branches on a structured flag, so a field carrying this distinction
        would assert a guarantee nothing delivers. Both halves are pinned here, the
        remedy named and the word that would send someone round the loop again
        absent.
        """
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (0, "False\tNone", ""))
        result = ssm.task_exec_readiness("crews", _TASK)
        assert result.ready is False
        assert "launch it again" in result.reason
        assert "retry" not in result.reason.lower()

    def test_the_readiness_result_carries_no_unread_recoverable_flag(self, monkeypatch):
        """G1. A field nobody branches on claims a guarantee no code delivers.

        Its own test rather than one more line in the message test above, so bringing
        the field back and changing the message kill DIFFERENT tests instead of two
        assertions inside one.
        """
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (0, "False\tNone", ""))
        result = ssm.task_exec_readiness("crews", _TASK)
        assert not hasattr(result, "recoverable")
        assert not any(
            "recover" in field.lower() for field in result.__dataclass_fields__
        ), result.__dataclass_fields__

    def test_an_agent_that_is_not_running_names_the_ssmmessages_possibility(self, monkeypatch):
        """The PrivateLink-only failure is invisible in every other observable.

        Such a VPC reaches the registry, so the image pulls and the task runs; only
        this agent never comes up. If the message does not name it, nothing does.
        """
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (0, "True\tPENDING", ""))
        result = ssm.task_exec_readiness("crews", _TASK)
        assert result.ready is False
        assert "ssmmessages" in result.reason
        # The counterpart to the terminal case above: this one DOES invite a retry,
        # which is the whole distinction the deleted flag was carrying.
        assert "retry" in result.reason.lower()

    def test_a_missing_task_is_reported_rather_than_read_as_ready(self, monkeypatch):
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (0, "", ""))
        assert ssm.task_exec_readiness("crews", _TASK).ready is False

    def test_a_failed_describe_is_reported_rather_than_read_as_ready(self, monkeypatch):
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (255, "", "AccessDenied"))
        result = ssm.task_exec_readiness("crews", _TASK)
        assert result.ready is False
        assert "AccessDenied" in result.reason

    def test_the_quoted_aws_error_carries_no_live_control_bytes(self, monkeypatch):
        """G2. AWS stderr reaches an operator's terminal, so the tail is !r-quoted.

        The repr is what turns an ESC or a newline into a literal instead of
        something a terminal acts on. Separate from the cap test below so dropping
        the quote and dropping the cap fail different tests.
        """
        monkeypatch.setattr(
            aws, "run_aws", lambda *a, **k: (255, "", "AccessDenied \x1b[31m\nsecond line")
        )
        reason = ssm.task_exec_readiness("crews", _TASK).reason
        assert "AccessDenied" in reason
        assert "\x1b" not in reason and "\n" not in reason, "a live control byte survived"
        assert "\\x1b" in reason and "\\n" in reason, "the tail was not !r-quoted"

    def test_the_quoted_aws_error_is_bounded(self, monkeypatch):
        """G2. An unbounded tail pastes a page of CLI output into one error line.

        The run of A's is what distinguishes a real cap from a repr that merely
        escaped everything, which is why it is asserted rather than length alone.
        """
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (255, "", "AccessDenied " + "A" * 4000))
        reason = ssm.task_exec_readiness("crews", _TASK).reason
        assert "AccessDenied" in reason
        assert "A" * ssm._MAX_AWS_ERROR_CHARS not in reason, "the tail was not capped"
        assert len(reason) < 400, f"unbounded stderr tail: {len(reason)} chars"


class TestConnectFargate:
    def test_the_preflight_runs_before_any_tunnel_is_opened(self, monkeypatch):
        """A failed prerequisite must not leave a child process behind."""
        monkeypatch.setattr(
            ssm,
            "task_exec_readiness",
            lambda *a, **k: ssm.TaskExecReadiness(False, "no channel"),
        )
        opened = []
        monkeypatch.setattr(ssm, "open_port_forward", lambda *a, **k: opened.append(1))
        conn = connect.connect_fargate(_ECS_TARGET, local_port=5599, remote_port=8080)
        assert conn.ready is False
        assert conn.error == "no channel"
        assert opened == [], "the tunnel was opened despite a failed preflight"

    def test_the_forward_goes_through_the_shared_opener(self, monkeypatch):
        """R8/R9: the shared opener carries assert_human_action and every guard.

        Asserted by observing that THIS function is what the lane calls, because a
        Fargate-specific child would silently drop the human-action gate, the
        free-port check, the process-group teardown, the resolved ``aws`` head and
        the withheld PATH -- none of which a passing happy-path test would notice.
        """
        _ready(monkeypatch)
        seen = {}

        def fake_open(target, remote, local, profile, region):
            seen.update(target=target, remote=remote, local=local)
            return _LiveProc()

        monkeypatch.setattr(ssm, "open_port_forward", fake_open)
        monkeypatch.setattr(ssm, "port_is_free", lambda *a, **k: True)
        monkeypatch.setattr(ssm, "wait_for_local_port", lambda *a, **k: True)
        conn = connect.connect_fargate(_ECS_TARGET, local_port=5599, remote_port=8080)
        assert conn.ready is True
        assert seen == {"target": _ECS_TARGET, "remote": 8080, "local": 5599}

    def test_a_ready_connection_names_the_local_turn_endpoint(self, monkeypatch):
        _ready(monkeypatch)
        monkeypatch.setattr(ssm, "open_port_forward", lambda *a, **k: _LiveProc())
        monkeypatch.setattr(ssm, "port_is_free", lambda *a, **k: True)
        monkeypatch.setattr(ssm, "wait_for_local_port", lambda *a, **k: True)
        conn = connect.connect_fargate(_ECS_TARGET, local_port=5599, remote_port=8080)
        assert conn.url == "http://127.0.0.1:5599"
        assert conn.turn_url == "http://127.0.0.1:5599/v1/chat/completions"

    def test_nothing_is_minted_and_no_browser_is_opened(self, monkeypatch):
        """This lane has no dashboard, so a token or a browser would be a bug.

        Asserted as ABSENT: the connection carries no token field at all, and
        webbrowser.open is never reached. A later change that routes this lane back
        through the gateway flow fails here rather than opening a window onto a
        JSON API.
        """
        _ready(monkeypatch)
        monkeypatch.setattr(ssm, "open_port_forward", lambda *a, **k: _LiveProc())
        monkeypatch.setattr(ssm, "port_is_free", lambda *a, **k: True)
        monkeypatch.setattr(ssm, "wait_for_local_port", lambda *a, **k: True)
        minted = []
        monkeypatch.setattr(connect, "mint_token", lambda *a, **k: minted.append(1) or "tok")
        opened = []
        monkeypatch.setattr(connect.webbrowser, "open", lambda *a, **k: opened.append(1))

        conn = connect.connect_fargate(_ECS_TARGET, local_port=5599, remote_port=8080)
        assert minted == [] and opened == []
        assert not hasattr(conn, "token")
        assert not hasattr(conn, "browser_opened")

    def test_a_foreign_listener_winning_the_bind_is_refused(self, monkeypatch):
        """A listener answering while our child is dead is not the crew.

        This lane sends no dashboard token, so the stake is lower than the gateway
        lane's -- but reporting a stranger's listener as ready would point the
        user's turn requests, which carry their prompts, at that process.
        """
        _ready(monkeypatch)
        monkeypatch.setattr(ssm, "open_port_forward", lambda *a, **k: _ExitedProc())
        monkeypatch.setattr(ssm, "port_is_free", lambda *a, **k: True)
        monkeypatch.setattr(ssm, "wait_for_local_port", lambda *a, **k: True)
        conn = connect.connect_fargate(_ECS_TARGET, local_port=5599, remote_port=8080)
        assert conn.ready is False
        assert conn.process is None

    def test_an_occupied_local_port_is_refused_before_the_tunnel(self, monkeypatch):
        _ready(monkeypatch)
        opened = []
        monkeypatch.setattr(ssm, "port_is_free", lambda *a, **k: False)
        monkeypatch.setattr(ssm, "open_port_forward", lambda *a, **k: opened.append(1))
        conn = connect.connect_fargate(_ECS_TARGET, local_port=5599, remote_port=8080)
        assert conn.ready is False and opened == []

    def test_a_target_that_is_not_an_ecs_task_is_refused(self, monkeypatch):
        opened = []
        monkeypatch.setattr(ssm, "open_port_forward", lambda *a, **k: opened.append(1))
        conn = connect.connect_fargate("i-0123456789abcdef0", local_port=5599, remote_port=8080)
        assert conn.ready is False and opened == []


def test_the_printed_paths_match_the_containers_own_constants():
    """The drift guard the module comment promises.

    ``connect.py`` spells the turn and health paths rather than importing them --
    the container is built into an image and is not a library of the gateway's --
    so a rename there would otherwise leave this lane printing a dead URL. Read
    out of the container source with ``ast`` so nothing is imported.
    """
    import ast

    source = (
        Path(__file__).resolve().parents[1]
        / "src/kiro_crew/apps/builtins/aws_control/crew/runtime/container/front/app.py"
    )
    tree = ast.parse(source.read_text(encoding="utf-8"))
    found = {
        node.targets[0].id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
        and node.targets[0].id in {"CUSTOMER_TURN_PATH", "HEALTH_PATH"}
    }
    assert found == {
        "CUSTOMER_TURN_PATH": connect.FARGATE_TURN_PATH,
        "HEALTH_PATH": connect.FARGATE_HEALTH_PATH,
    }

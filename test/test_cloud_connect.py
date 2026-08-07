"""Unit tests for connect over SSM (cloud/connect.py)."""

from __future__ import annotations

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
        proc = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
        # Wait for the grandchild pid to be recorded.
        for _ in range(50):
            if pidfile.exists() and pidfile.read_text(encoding="utf-8").strip():
                break
            time.sleep(0.1)
        child_pid = int(pidfile.read_text(encoding="utf-8").strip())
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
        ssm_mod.kill_port_forward(FakeProc())

        assert calls, "Windows must attempt a tree kill"
        argv = calls[0]
        assert argv[0] == "taskkill"
        assert "/T" in argv, "/T is what reaps the plugin child"
        assert "/F" in argv
        assert str(FakeProc.pid) in argv
        assert not FakeProc.terminated, "parent-only terminate must not be the Windows path"

    def test_windows_tree_kill_tolerates_a_process_object_without_a_pid(
        self, monkeypatch
    ) -> None:
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

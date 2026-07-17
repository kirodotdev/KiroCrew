"""Unit tests for connect over SSM (cloud/connect.py)."""

from __future__ import annotations

import pytest

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
        # `aws` process is a group leader and the plugin child is in the same
        # group. _kill_process_tree must reap the WHOLE group — a plain
        # proc.terminate() would leave the child (holding the local port) alive.
        import subprocess
        import sys
        import time

        # Parent spawns a grandchild `sleep`, writes its pid, then waits — so the
        # group has two members. Start it in its own session (mirrors
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
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.1)
        child_pid = int(pidfile.read_text().strip())
        assert _pid_alive(child_pid), "grandchild should be alive before teardown"

        connect._kill_process_tree(proc)

        # Both the parent and the grandchild must be gone.
        assert proc.poll() is not None, "parent should be reaped"
        # Give the group signal a moment to propagate to the grandchild.
        for _ in range(50):
            if not _pid_alive(child_pid):
                break
            time.sleep(0.1)
        assert not _pid_alive(child_pid), "grandchild (same group) must also be killed"

    def test_none_and_dead_proc_are_noops(self):
        connect._kill_process_tree(None)  # must not raise

        class Dead:
            def poll(self):
                return 0

        connect._kill_process_tree(Dead())  # already exited → no-op


def _pid_alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class TestRegistryIntegration:
    def test_register_instance(self, monkeypatch, tmp_path):
        from kiro_crew.instances.registry import InstancesRegistry

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        monkeypatch.setattr(connect, "InstancesRegistry", InstancesRegistry, raising=False)
        # Patch the lazy import target: connect imports InstancesRegistry inside
        # the function, so patch the class used there via the module.
        import kiro_crew.instances.registry as regmod

        monkeypatch.setattr(regmod, "InstancesRegistry", lambda *a, **k: reg)

        rid = connect.register_instance("i-0abc", name="KiroCrew Cloud")
        assert rid is not None
        assert any(i.ssh_host == "i-0abc" for i in reg.list())

    def test_unregister_instance(self, monkeypatch, tmp_path):
        from kiro_crew.instances.registry import InstancesRegistry

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="KiroCrew Cloud", ssh_host="i-0abc")
        import kiro_crew.instances.registry as regmod

        monkeypatch.setattr(regmod, "InstancesRegistry", lambda *a, **k: reg)

        assert connect.unregister_instance("i-0abc") is True
        assert not any(i.ssh_host == "i-0abc" for i in reg.list())

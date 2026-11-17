"""Instances tunnel diagnosis: the first-broken-link ladder and ``TunnelStatus``."""

from __future__ import annotations

import asyncio
import socket
import subprocess

import pytest

from kiro_crew import platform_compat
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, await_until


class TestDiagnostics:
    def _set_probes(self, monkeypatch, ssh, remote, local):
        from kiro_crew.instances import diagnostics as diag

        async def _ssh(h, connect_timeout_secs=10.0):
            return ssh

        async def _rem(h, p, connect_timeout_secs=10.0):
            return remote

        async def _loc(p):
            return local

        monkeypatch.setattr(diag, "_probe_ssh", _ssh)
        monkeypatch.setattr(diag, "_probe_remote_dashboard", _rem)
        monkeypatch.setattr(diag, "_probe_local_forward", _loc)

    def test_ladder_first_broken_link(self, monkeypatch):
        from kiro_crew.instances.diagnostics import (
            NOT_CONNECTED,
            OK,
            REMOTE_DOWN,
            SSH_UNREACHABLE,
            TUNNEL_DOWN,
            diagnose_instance,
        )

        self._set_probes(monkeypatch, False, True, True)
        assert asyncio.run(diagnose_instance("cd-1-alias", 7777, 7778)).code == SSH_UNREACHABLE
        self._set_probes(monkeypatch, True, False, True)
        assert asyncio.run(diagnose_instance("cd-1-alias", 7777, 7778)).code == REMOTE_DOWN
        self._set_probes(monkeypatch, True, True, False)
        assert asyncio.run(diagnose_instance("cd-1-alias", 7777, 7778)).code == TUNNEL_DOWN
        self._set_probes(monkeypatch, True, True, True)
        r = asyncio.run(diagnose_instance("cd-1-alias", 7777, 7778))
        assert r.code == OK and r.ok and r.to_dict()["ok"] is True
        # local_port == 0 (never connected): ssh + remote up, but no forward to
        # probe → NOT_CONNECTED, not the misleading TUNNEL_DOWN "reconnect".
        self._set_probes(monkeypatch, True, True, True)
        assert asyncio.run(diagnose_instance("cd-1-alias", 7777, 0)).code == NOT_CONNECTED

    def test_invalid_host_short_circuits(self):
        from kiro_crew.instances.diagnostics import UNKNOWN, diagnose_instance

        r = asyncio.run(diagnose_instance("-obadhost", 7777, 7778))
        assert r.code == UNKNOWN and r.probes == []

    def test_probe_helpers_via_mocked_subprocess(self, monkeypatch):
        from kiro_crew.instances import diagnostics as diag

        class FakeProc:
            def __init__(self, rc, out=b""):
                self.returncode = rc
                self._out = out

            async def wait(self):
                return self.returncode

            async def communicate(self):
                return (self._out, b"")

            def kill(self):
                pass

        def mk(rc, out=b""):
            async def _exec(*a, **k):
                return FakeProc(rc, out)

            return _exec

        # _run_ok: exit 0 -> True, nonzero -> False
        monkeypatch.setattr(asyncio, "create_subprocess_exec", mk(0))
        assert asyncio.run(diag._run_ok(["true"], 1.0)) is True
        monkeypatch.setattr(asyncio, "create_subprocess_exec", mk(1))
        assert asyncio.run(diag._run_ok(["false"], 1.0)) is False

        # _run_stdout: exit 0 -> decoded stdout, nonzero -> None
        monkeypatch.setattr(asyncio, "create_subprocess_exec", mk(0, b"200"))
        assert asyncio.run(diag._run_stdout(["x"], 1.0)) == "200"
        monkeypatch.setattr(asyncio, "create_subprocess_exec", mk(1, b"x"))
        assert asyncio.run(diag._run_stdout(["x"], 1.0)) is None

        # _probe_ssh delegates to _run_ok
        monkeypatch.setattr(asyncio, "create_subprocess_exec", mk(0))
        assert asyncio.run(diag._probe_ssh("cd-1")) is True

        # _probe_remote_dashboard: a real HTTP code -> True, '000'/empty -> False
        monkeypatch.setattr(asyncio, "create_subprocess_exec", mk(0, b"200"))
        assert asyncio.run(diag._probe_remote_dashboard("cd-1", 7777)) is True
        monkeypatch.setattr(asyncio, "create_subprocess_exec", mk(0, b"000"))
        assert asyncio.run(diag._probe_remote_dashboard("cd-1", 7777)) is False

    def test_probes_honor_connect_timeout_secs(self, monkeypatch):
        """The hardcoded ConnectTimeout=10 must not silently override a
        caller-supplied budget -- a diagnosis on a slow-proxy host the user
        already tuned instances.connect_timeout_secs for must not be
        misreported as unreachable just because the probe never saw that
        tuning."""
        from kiro_crew.instances import diagnostics as diag

        captured = {}

        class FakeProc:
            returncode = 0

            async def wait(self):
                return 0

            async def communicate(self):
                return (b"200", b"")

        async def fake_exec(*argv, **k):
            captured["argv"] = argv
            captured["kw"] = k
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        assert asyncio.run(diag._probe_ssh("cd-1", connect_timeout_secs=42.0)) is True
        assert "ConnectTimeout=42" in captured["argv"]
        # Probe children never inherit the gateway's stdin: an inherited one
        # keeps the ssh stdin channel open past the remote command's exit and
        # the probe's only bound is its wall-clock cap, whose expiry the ladder
        # renders as SSH_UNREACHABLE on a healthy host.
        assert captured["kw"].get("stdin") is asyncio.subprocess.DEVNULL
        # Both probes share token_mint._build_ssh_argv with the mint, so the two
        # options a probe cannot work without are pinned HERE too: without
        # BatchMode a probe hangs on an interactive prompt instead of reporting
        # unreachable, and without AddressFamily=inet it can resolve ::1 and miss
        # the IPv4 loopback forward. A mint-motivated edit to the shared builder
        # would otherwise change the ladder with no signal on this side.
        assert "BatchMode=yes" in captured["argv"]
        assert "AddressFamily=inet" in captured["argv"]

        assert (
            asyncio.run(diag._probe_remote_dashboard("cd-1", 7777, connect_timeout_secs=42.0))
            is True
        )
        assert "ConnectTimeout=42" in captured["argv"]
        assert "BatchMode=yes" in captured["argv"]
        assert "AddressFamily=inet" in captured["argv"]
        # Same for the stdout-capturing probe path (_run_stdout).
        assert captured["kw"].get("stdin") is asyncio.subprocess.DEVNULL

    def test_probe_local_forward(self):
        from kiro_crew.instances import diagnostics as diag

        # no port -> False without connecting
        assert asyncio.run(diag._probe_local_forward(0)) is False
        # a real listening socket -> reachable
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        try:
            assert asyncio.run(diag._probe_local_forward(port)) is True
        finally:
            s.close()


class TestTunnelStatus:
    def test_to_dict_includes_diagnosis_only_when_set(self):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        # no diagnosis -> key absent
        d = TunnelStatus("cd-1", TunnelState.CONNECTED, local_port=7778, remote_port=7777).to_dict()
        assert d["state"] == "connected" and "diagnosis" not in d
        # diagnosis attached -> surfaced verbatim
        diag = {"code": "tunnel_down", "ok": False, "reason": "x", "probes": []}
        d2 = TunnelStatus("cd-1", TunnelState.ERROR, diagnosis=diag).to_dict()
        assert d2["diagnosis"] == diag

    @pytest.mark.asyncio
    async def test_stop_leaves_connected_before_its_first_await(self):
        """Every decision to issue a credential for a forward re-reads this live status
        object, and ``stop()`` awaits for seconds (``_terminate`` waits up to 5s per
        signal). So the state has to leave CONNECTED at the START of the teardown: while
        it still reads CONNECTED, a concurrent caller is answered with this crew's token
        paired with a port that is already being released, and nothing revokes a token
        once the response is sent.
        """
        import asyncio as aio

        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, _SshTunnel

        entered = aio.Event()
        release = aio.Event()

        class _Parked(_SshTunnel):
            async def _terminate(self):
                entered.set()
                await await_until(release, what="the test releasing the teardown")

        t = _Parked("cd-1", "h", 7778, 7777)
        t.status.state = TunnelState.CONNECTED
        t.status.local_port = 7778

        task = aio.create_task(t.stop())
        await await_until(entered, what="the teardown entering")

        # Mid-teardown, with the port not yet released and the caller still holding a
        # reference to this object.
        assert t.status.state is not TunnelState.CONNECTED, (
            "stop() still reports CONNECTED while tearing down, so a concurrent "
            "caller can be handed a token paired with a port being released"
        )
        assert t.status.state is TunnelState.STOPPED

        release.set()
        await await_until(task, what="stop() finishing")
        assert t.status.state is TunnelState.STOPPED

    @pytest.mark.asyncio
    async def test_a_failed_teardown_restores_the_previous_state(self):
        """A teardown that raises leaves the forward up, so the state it reports has to
        go back to what it was: refusing to mint for a live forward would strand it.
        A CANCELLED teardown is the other way round -- the child may already be
        signalled, so that one stays non-connected.
        """
        import asyncio as aio

        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, _SshTunnel

        class _Boom(_SshTunnel):
            async def _terminate(self):
                raise RuntimeError("terminate failed")

        t = _Boom("cd-1", "h", 7778, 7777)
        t.status.state = TunnelState.CONNECTED
        with pytest.raises(RuntimeError):
            await t.stop()
        assert t.status.state is TunnelState.CONNECTED, "a failed teardown stranded the forward"

        class _Cancelled(_SshTunnel):
            async def _terminate(self):
                raise aio.CancelledError()

        t2 = _Cancelled("cd-1", "h", 7778, 7777)
        t2.status.state = TunnelState.CONNECTED
        with pytest.raises(aio.CancelledError):
            await t2.stop()
        assert t2.status.state is TunnelState.STOPPED, "a cancelled teardown re-opened minting"


_SSM_TARGET = "i-0123456789abcdef0"


def _ssm_node_online(monkeypatch):
    from kiro_crew.instances import diagnostics as diag

    async def _online(*a, **k):
        return diag._NODE_ONLINE

    monkeypatch.setattr(diag, "_probe_ssm_managed", _online)


class TestSsmRemoteCauses:
    """Rung 2 of the SSM ladder names why the remote dashboard is silent, and the field to fix."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("stderr", "code"),
        [
            (
                "An error occurred (AccessDeniedException) when calling the SendCommand "
                "operation: User: arn:aws:sts::123456789012:assumed-role/dev/me is not "
                "authorized to perform: ssm:SendCommand on resource: "
                "arn:aws:ec2:us-east-1:123456789012:instance/i-0123456789abcdef0",
                "ssm_access_denied",
            ),
            ("An error occurred (ThrottlingException) when calling the SendCommand", "remote_down"),
        ],
        ids=["denied", "throttled"],
    )
    async def test_a_refused_send_command_is_named_without_arns(self, monkeypatch, stderr, code):
        from kiro_crew.cloud import aws
        from kiro_crew.instances import diagnostics as diag

        _ssm_node_online(monkeypatch)

        async def _refused(*a, **k):
            raise aws.AWSError(
                f"ssm:SendCommand failed: {stderr}", action="ssm:SendCommand", stderr=stderr
            )

        monkeypatch.setattr(diag, "_send_over_ssm", _refused)
        res = await diag.diagnose_instance_ssm(_SSM_TARGET, 5476, 0)
        assert res.code == code
        assert res.probes[-1] == {"name": "remote_dashboard", "ok": False}
        if code == "ssm_access_denied":
            assert "ssm:SendCommand" in res.reason and "ssm:StartSession" in res.reason
        assert "arn:" not in res.reason and "123456789012" not in res.reason

    @pytest.mark.asyncio
    async def test_an_unknown_remote_user_names_the_remote_user_field(self, monkeypatch):
        from kiro_crew.cloud import ssm
        from kiro_crew.instances import diagnostics as diag

        _ssm_node_online(monkeypatch)

        async def _sudo_refuses(*a, **k):
            return ssm.CommandResult(
                status="Failed",
                stdout="",
                stderr="sudo: unknown user ec2-user\nsudo: error initializing audit plugin "
                "sudoers_audit\nfailed to run commands: exit status 1",
                exit_code=1,
            )

        monkeypatch.setattr(diag, "_send_over_ssm", _sudo_refuses)
        res = await diag.diagnose_instance_ssm(_SSM_TARGET, 5476, 0, ssm_run_as="")
        assert res.code == "ssm_user_unknown"
        assert "User ec2-user doesn't exist" in res.reason and "Remote user" in res.reason


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="runs the remote probe in a POSIX sh")
class TestSsmRemoteProbeSnippet:
    """The real rung-2 snippet, run in a local sh against a planted home, picks the cause."""

    def _home(self, tmp_path, *, markers=(), launcher=None):
        from kiro_crew.config.paths import CONFIG_DIR_NAME

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        curl = bin_dir / "curl"
        # The only gateway that answers listens on 7777 (an auth gate's 401).
        curl.write_text(
            '#!/bin/sh\nfor a; do case "$a" in http://127.0.0.1:7777/*) printf 401; exit 0;; '
            "esac; done\nprintf 000; exit 7\n"
        )
        curl.chmod(0o755)
        run = tmp_path / CONFIG_DIR_NAME / "run"
        run.mkdir(parents=True)
        (run / "gateway-7777.pid").write_text("1\n")  # a sidecar, never read as a port
        for name in markers:
            (run / name).write_text("/nowhere\n")
        if launcher:
            exe = tmp_path / launcher
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)

    def _run_snippet_locally(self, monkeypatch, home, **env):
        from kiro_crew.cloud import ssm
        from kiro_crew.instances import diagnostics as diag

        async def _send(target, command, profile, region, run_as, timeout_secs):
            proc = await asyncio.to_thread(
                subprocess.run,
                ["/bin/sh", "-c", command],
                env={"HOME": str(home), "PATH": f"{home}/bin:/usr/bin:/bin", **env},
                capture_output=True,
                encoding="utf-8",
                timeout=FALLBACK_TIMEOUT_SECS,
                cwd=home,
            )
            return ssm.CommandResult(
                status="Success" if proc.returncode == 0 else "Failed",
                stdout=proc.stdout,
                stderr=proc.stderr,
                exit_code=proc.returncode,
            )

        monkeypatch.setattr(diag, "_send_over_ssm", _send)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("markers", "launcher", "remote_bin", "remote_port", "code", "named"),
        [
            (
                ("gateway-6100.bin", "gateway-7777.bin"),
                ".local/bin/kirocrew",
                "",
                5476,
                "remote_wrong_port",
                ("port 7777, not 5476", "Remote port to 7777"),
            ),
            (
                ("gateway-7777.home",),
                ".local/bin/kirocrew",
                "",
                5476,
                "remote_wrong_port",
                ("port 7777, not 5476",),
            ),
            ((), None, "", 5476, "remote_not_installed", ("user ec2-user", "Remote kirocrew")),
            (
                ("gateway-6100.bin",),
                ".kirocrew-app/.venv/bin/kirocrew",
                "",
                5476,
                "remote_down",
                ("is the remote gateway running?",),
            ),
            (("gateway-5476.bin",), None, "", 5476, "remote_down", ()),
            (
                ("gateway-5476.bin", "gateway-7777.bin"),
                ".local/bin/kirocrew",
                "",
                5476,
                "remote_down",
                (),
            ),
            ((), "opt/kc/kirocrew", "~/opt/kc/kirocrew", 5476, "remote_down", ()),
            (
                ("gateway-5476.bin",),
                ".local/bin/kirocrew",
                "~/opt/kc/kirocrew",
                5476,
                "remote_not_installed",
                (),
            ),
            (("gateway-7777.bin",), None, "", 7777, "not_connected", ()),
            ((), "bin/kirocrew", "kirocrew", 5476, "remote_down", ()),
            ((), "kirocrew-elsewhere", "kirocrew-elsewhere", 5476, "remote_not_installed", ()),
        ],
        ids=[
            "live-marker-port",
            "home-pointer-port",
            "no-launcher",
            "stale-marker",
            "own-marker-outside-the-candidates",
            "own-marker-beside-a-live-gateway",
            "custom-path-present",
            "custom-path-absent",
            "answers-on-the-port",
            "custom-name-on-path",
            "custom-name-off-path",
        ],
    )
    async def test_the_snippet_tells_the_causes_apart(
        self, tmp_path, monkeypatch, markers, launcher, remote_bin, remote_port, code, named
    ):
        from kiro_crew.instances import diagnostics as diag

        _ssm_node_online(monkeypatch)
        self._home(tmp_path, markers=markers, launcher=launcher)
        self._run_snippet_locally(monkeypatch, tmp_path)
        res = await diag.diagnose_instance_ssm(_SSM_TARGET, remote_port, 0, remote_bin=remote_bin)
        assert res.code == code, res.reason
        for text in named:
            assert text in res.reason

    @pytest.mark.asyncio
    async def test_a_marker_under_an_exported_kirocrew_home_means_installed(
        self, tmp_path, monkeypatch
    ):
        """The mint reads ``$KIROCREW_HOME``'s marker first, so the probe does too."""
        from kiro_crew.instances import diagnostics as diag

        _ssm_node_online(monkeypatch)
        self._home(tmp_path)
        (tmp_path / "elsewhere" / "run").mkdir(parents=True)
        (tmp_path / "elsewhere" / "run" / "gateway-5476.bin").write_text("/opt/kc/bin/kirocrew\n")
        self._run_snippet_locally(monkeypatch, tmp_path, KIROCREW_HOME=str(tmp_path / "elsewhere"))
        res = await diag.diagnose_instance_ssm(_SSM_TARGET, 5476, 0)
        assert res.code == "remote_down", res.reason

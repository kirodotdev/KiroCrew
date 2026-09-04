"""Script crons must carry a gateway-vouched identity into their MCP spawns.

Every state-mutating MCP tool resolves its caller through
``mcp_core._resolve_session_key_strict``, which accepts exactly three sources:
the gateway-injected caller block, ``KIROCREW_SESSION_KEY``, or
``KIROCREW_HOST_PID`` plus its signed sidecar. A script cron had none of them:
``run_script_sandboxed`` never set the env var, nothing routes the child's direct
MCP spawns through gatewayd, and nobody publishes a sidecar for the launcher pid.
So ``ctx.call_tool("kirocrew-cron", "cron_trigger", ...)`` reached the handler
and came back with ``_unidentified_caller_refusal`` -- a plain string most
scripts swallow, so the job reported ``ok`` while writing nothing. Reads were
unaffected, which is why the compose fix (#6431) looked complete.

The fix is the same channel ``acp/client.py`` gives every agent subprocess,
including agent crons: the launcher injects ``KIROCREW_SESSION_KEY=cron:<job>``
into the child env, and the MCP bridge hard-pins that key on the server spawn so
script code cannot swap it for another session's.

Must be runnable with ``--noconftest`` (no hypothesis dependency).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.cron_script import McpToolClient, ScriptContext, run_script_sandboxed

JOB_ID = "job-8b1f"
EXPECTED_KEY = f"cron:{JOB_ID}"


def _handshake_proc() -> MagicMock:
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    proc.stdout.readline.return_value = '{"jsonrpc":"2.0","id":1,"result":{}}\n'
    return proc


def _capture_launcher_env(job_id: str) -> dict[str, str]:
    """Return the env ``run_script_sandboxed`` hands its child.

    Stops at the spawn so no interpreter is launched: ``popen_limited`` is the
    last seam and receives the fully assembled env.
    """
    captured: dict[str, dict[str, str]] = {}

    def fake_popen(argv, **kwargs):
        captured["env"] = dict(kwargs["env"])
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate.return_value = ('{"status": "ok"}', "")
        return proc

    with (
        patch("kiro_crew.cron_script.resolve_script_path", return_value=("/f.py", "run")),
        patch("kiro_crew.cron_script.wrap_argv", return_value=(["true"], None)),
        patch("kiro_crew.cron_script._resolve_internal_secret", return_value="s"),
        patch("kiro_crew.cron_script.popen_limited", side_effect=fake_popen),
    ):
        result = run_script_sandboxed("/f.py:run", job_id, "", timeout=30)

    assert result == {"status": "ok"}
    assert "env" in captured, "popen_limited was never reached"
    return captured["env"]


@pytest.fixture(autouse=True)
def _no_ambient_identity(monkeypatch):
    """The test process must not already look like an identified session.

    Otherwise a launcher that merely INHERITED the parent's key would pass the
    presence assertions below without ever setting one of its own.
    """
    for key in ("KIROCREW_SESSION_KEY", "KIROCREW_HOST_PID", "KIROCREW_CLI"):
        monkeypatch.delenv(key, raising=False)


class TestLauncherInjectsIdentity:
    def test_child_env_carries_the_jobs_session_key(self):
        env = _capture_launcher_env(JOB_ID)
        assert env.get("KIROCREW_SESSION_KEY") == EXPECTED_KEY

    def test_key_is_the_one_scriptcontext_presents_over_http(self):
        """One principal per job: the MCP identity must equal the HTTP identity.

        ``ScriptContext._post`` sends ``X-Session-Key: cron:<job>``; ownership and
        audit rows would split across two principals if the MCP side used any
        other spelling.
        """
        env = _capture_launcher_env(JOB_ID)
        job = MagicMock(id=JOB_ID, message="")
        with patch.dict(os.environ, {"_KIROCREW_DIAL_PORT": "5476"}):
            ctx = ScriptContext(job=job)
        captured: dict[str, str] = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"{}"

        def fake_urlopen(req, timeout):
            captured["key"] = req.get_header("X-session-key")
            return _Resp()

        with patch("kiro_crew.cron_script.loopback_urlopen", side_effect=fake_urlopen):
            ctx._post("/api/send-message", {"text": "x"})
        assert captured["key"] == env["KIROCREW_SESSION_KEY"]

    def test_a_forged_inherited_key_is_overwritten_not_kept(self, monkeypatch):
        """Hard-assign, not setdefault: the gateway's env must not leak a key in."""
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:someone-else")
        env = _capture_launcher_env(JOB_ID)
        assert env["KIROCREW_SESSION_KEY"] == EXPECTED_KEY


class TestBridgePinsIdentityOnTheServerSpawn:
    def _spawn_env(self, session_key: str, spec_env: dict[str, str] | None = None):
        from kiro_crew.cron_script import _resolve_mcp_server

        _resolve_mcp_server.cache_clear()
        with (
            patch(
                "kiro_crew.cron_script._resolve_mcp_server",
                return_value=(("some-mcp",), spec_env or {}),
            ),
            patch("kiro_crew.cron_script.wrap_argv", return_value=(["some-mcp"], None)),
            patch("kiro_crew.cron_script.cgroup_scope_argv", side_effect=lambda argv: list(argv)),
            patch(
                "kiro_crew.cron_script.popen_limited", return_value=_handshake_proc()
            ) as mock_popen,
        ):
            client = McpToolClient("kirocrew-cron", session_key=session_key)
            client.close()
        return mock_popen.call_args.kwargs["env"]

    def test_script_rewriting_its_own_environ_cannot_change_the_spawned_identity(self, monkeypatch):
        """The threat ``ScriptContext.notify`` already hard-assigns against.

        The bridge builds its env from the script child's ``os.environ``, which
        user code owns. A script that rewrites ``KIROCREW_SESSION_KEY`` before
        ``ctx.call_tool`` must still spawn the server as ITS job.
        """
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:victim")
        env = self._spawn_env(EXPECTED_KEY)
        assert env["KIROCREW_SESSION_KEY"] == EXPECTED_KEY

    def test_spec_env_block_still_cannot_supply_the_key(self):
        """The pin lands AFTER the spec overlay; the reserved-namespace deny holds."""
        env = self._spawn_env(EXPECTED_KEY, {"KIROCREW_SESSION_KEY": "dashboard:victim"})
        assert env["KIROCREW_SESSION_KEY"] == EXPECTED_KEY

    def test_no_session_key_means_no_key_is_invented(self):
        """The CLI preview path constructs the bridge bare; it must stay bare."""
        env = self._spawn_env("")
        assert "KIROCREW_SESSION_KEY" not in env

    def test_call_tool_passes_the_jobs_key_to_the_bridge(self):
        job = MagicMock(id=JOB_ID, message="")
        with patch.dict(os.environ, {"_KIROCREW_DIAL_PORT": "5476"}):
            ctx = ScriptContext(job=job)
        fake_client = MagicMock()
        fake_client.call_tool.return_value = "ok"
        with patch("kiro_crew.cron_script.McpToolClient", return_value=fake_client) as ctor:
            ctx.call_tool("kirocrew-cron", "cron_list", {})
        ctor.assert_called_once_with("kirocrew-cron", session_key=EXPECTED_KEY)


class TestTheRealConsumerAcceptsIt:
    """Evaluate the actual strict resolver in the env the server would start with.

    The presence tests above prove the key reaches the child. This proves the
    gate that refused script crons now answers with the job's identity -- and
    that it did so via the env channel alone, with no caller block and no
    signed sidecar (the two channels a script cron never has).
    """

    def test_strict_resolver_identifies_the_job(self):
        from kiro_crew.mcp_core import _resolve_session_key_strict

        env = _capture_launcher_env(JOB_ID)
        with patch.dict(os.environ, env, clear=True):
            assert _resolve_session_key_strict() == EXPECTED_KEY

    def test_cron_authz_gate_sees_the_job_not_an_unidentified_caller(self):
        from kiro_crew.mcp_cron import _authz_session_key

        env = _capture_launcher_env(JOB_ID)
        with patch.dict(os.environ, env, clear=True):
            assert _authz_session_key() == EXPECTED_KEY

    def test_without_the_injection_the_gate_refuses(self):
        """Baseline: the same env minus the key is exactly the reported failure."""
        from kiro_crew.mcp_core import _resolve_session_key_strict

        env = _capture_launcher_env(JOB_ID)
        env.pop("KIROCREW_SESSION_KEY")
        with patch.dict(os.environ, env, clear=True):
            assert _resolve_session_key_strict() == ""

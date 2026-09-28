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
unaffected, which is why the compose fix looked complete.

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


def _capture_launcher_env(
    job_id: str, *, during_spawn=None, expected_result: dict[str, str] | None = None
) -> dict[str, str]:
    """Return the env ``run_script_sandboxed`` hands its child.

    Stops at the spawn so no interpreter is launched: ``popen_limited`` is the
    last seam and receives the fully assembled env.
    """
    captured: dict[str, dict[str, str]] = {}

    def fake_popen(argv, **kwargs):
        captured["env"] = dict(kwargs["env"])
        if during_spawn is not None:
            during_spawn(captured["env"])
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

    # Whole-dict equality, not one key: an unexpected field in a launcher result is
    # exactly the malformation this helper is the only reader of.
    wanted = {"status": "ok"} if expected_result is None else expected_result
    assert result == wanted
    if wanted["status"] != "ok":
        return captured.get("env", {})
    assert "env" in captured, "popen_limited was never reached"
    return captured["env"]


@pytest.fixture(autouse=True)
def _no_ambient_identity(monkeypatch, tmp_path):
    """The test process must not already look like an identified session.

    Otherwise a launcher that merely INHERITED the parent's key would pass the
    presence assertions below without ever setting one of its own.

    The signed mapping directory is redirected into the test's own tmp dir for a
    separate reason: the launcher PUBLISHES one, and a unit test must not write
    into the real crew home. Tests that need the mapping to verify layer their
    own trust root over this (see ``signing_root``).
    """
    from kiro_crew import session_token_sig

    for key in (
        "KIROCREW_SESSION_KEY",
        "KIROCREW_HOST_PID",
        "KIROCREW_CLI",
        "KIROCREW_STUB_SESSION_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(session_token_sig, "config_dir", lambda: tmp_path)


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


class TestLauncherPublishesAVerifiableToken:
    """The key is the caller's own word; the signed token is what a reader verifies.

    ``member_request_scope`` and ``memory_request_identity`` accept a declared
    ``X-Session-Key`` only behind a transport attestation. A script cron cannot be
    attested by the unix-socket peer walk -- no signed pid mapping names the
    sandbox launcher's pid -- so the token is its channel, and it has to map back
    to the job's own key rather than to any other session.
    """

    @pytest.fixture
    def signing_root(self, tmp_path):
        """An isolated mapping directory over a valid SEL trust-root key.

        Four patches for the same reason ``test_session_token_sig`` needs four:
        the protocol SHARES its key loader with ``session_pid_sig``, so patching
        one module's view of the trust root leaves the loader reading the real one.
        """
        from kiro_crew import session_pid_sig, session_token_sig

        key_path = tmp_path / "sel_hmac.key"
        key_path.write_bytes(b"\x02" * 32)
        with (
            patch.object(session_token_sig, "config_dir", return_value=tmp_path),
            patch.object(session_pid_sig, "sel_hmac_key_path", return_value=key_path),
            patch.object(session_token_sig, "sel_hmac_key_path", return_value=key_path),
            patch.object(session_pid_sig, "_sel_hmac_key_bytes", return_value=None),
        ):
            yield tmp_path

    def _capture_verified_env(self, signing_root, job_id):
        from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
        from kiro_crew.session_token_sig import verify_session_token

        def verify_during_run(env):
            token = env[STUB_SESSION_TOKEN_ENV]
            assert token
            assert verify_session_token(token) == f"cron:{job_id}"
            assert len(list(signing_root.glob("session_token_*.sig"))) == 1

        env = _capture_launcher_env(job_id, during_spawn=verify_during_run)
        assert not list(signing_root.glob("session_token_*.sig"))
        assert verify_session_token(env[STUB_SESSION_TOKEN_ENV]) == ""
        return env

    def test_child_env_carries_a_token_that_maps_to_the_jobs_key(self, signing_root):
        self._capture_verified_env(signing_root, JOB_ID)

    def test_the_token_names_this_job_and_not_a_neighbour(self, signing_root):
        from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV

        mine = self._capture_verified_env(signing_root, JOB_ID)[STUB_SESSION_TOKEN_ENV]
        theirs = self._capture_verified_env(signing_root, "job-other")[STUB_SESSION_TOKEN_ENV]

        assert mine != theirs

    def test_two_runs_have_different_tokens_and_leave_no_mappings(self, signing_root):
        from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV

        first = self._capture_verified_env(signing_root, JOB_ID)[STUB_SESSION_TOKEN_ENV]
        second = self._capture_verified_env(signing_root, JOB_ID)[STUB_SESSION_TOKEN_ENV]

        assert second != first

    def test_spawn_exception_retracts_the_mapping(self, signing_root):
        from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
        from kiro_crew.session_token_sig import verify_session_token

        def fail_spawn(env):
            assert verify_session_token(env[STUB_SESSION_TOKEN_ENV]) == EXPECTED_KEY
            raise RuntimeError("spawn failed")

        with pytest.raises(RuntimeError, match="spawn failed"):
            _capture_launcher_env(JOB_ID, during_spawn=fail_spawn)
        assert not list(signing_root.glob("session_token_*.sig"))

    def test_early_overlap_return_retracts_the_mapping(self, signing_root):
        def refuse_spawn(job_id):
            assert job_id == JOB_ID
            assert len(list(signing_root.glob("session_token_*.sig"))) == 1
            return False

        with patch("kiro_crew.cron_script._begin_spawn", side_effect=refuse_spawn):
            _capture_launcher_env(
                JOB_ID,
                expected_result={
                    "status": "skipped",
                    "error": "Another run of this job is already starting or running",
                },
            )
        assert not list(signing_root.glob("session_token_*.sig"))

    def test_an_inherited_token_is_overwritten_not_kept(self, signing_root, monkeypatch):
        from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV

        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, "f" * 64)

        token = self._capture_verified_env(signing_root, JOB_ID)[STUB_SESSION_TOKEN_ENV]

        assert token != "f" * 64


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
        """Baseline: the same env minus the injection is exactly the reported failure.

        The launcher injects TWO names for one identity, and the strict resolver
        reads either, so the baseline has to strip both. Dropping only the env key
        would leave the signed token answering and measure nothing.
        """
        from kiro_crew.mcp_core import _resolve_session_key_strict
        from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV

        env = _capture_launcher_env(JOB_ID)
        env.pop("KIROCREW_SESSION_KEY")
        env.pop(STUB_SESSION_TOKEN_ENV, None)
        with patch.dict(os.environ, env, clear=True):
            assert _resolve_session_key_strict() == ""

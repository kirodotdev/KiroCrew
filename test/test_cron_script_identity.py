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

The key is the caller's own word, so the launcher also publishes a signed token
that maps back to it, and the child presents that token on BOTH of its hops: the
MCP bridge inherits it through the environment, and ``ScriptContext._post``
sends it as ``X-Session-Token`` beside ``X-Session-Key``. An owner-surface route
such as ``POST /api/crons/{id}/run`` accepts a declared key only behind that
attestation; a request carrying the key alone is answered with 409
``member_identity_unavailable``, which ``_post`` returns as a structured refusal
(``error``, ``status_code``, ``code``) rather than the bare status line.

Must be runnable with ``--noconftest`` (no hypothesis dependency).
"""

from __future__ import annotations

import io
import os
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.cron_script import McpToolClient, ScriptContext, run_script_sandboxed
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV

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


@pytest.fixture
def signing_root(tmp_path):
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


def _sent_request(ctx: ScriptContext, path: str, body: dict) -> urllib.request.Request:
    """Return the ``Request`` ``ctx._post`` hands the loopback transport.

    Stops at the transport so nothing is dialled; the gateway's answer is an
    empty JSON object.
    """
    captured: dict[str, urllib.request.Request] = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout):
        captured["req"] = req
        return _Resp()

    with patch("kiro_crew.cron_script.loopback_urlopen", side_effect=fake_urlopen):
        ctx._post(path, body)
    return captured["req"]


class _WireHeaders:
    """Case-insensitive view of a ``Request``'s headers, as aiohttp's ``CIMultiDict`` is.

    ``urllib`` stores a header under its capitalised spelling while the consumer
    asks for ``X-Session-Token``; on the wire neither spelling matters.
    """

    def __init__(self, req: urllib.request.Request) -> None:
        self._items = {name.lower(): value for name, value in req.header_items()}

    def get(self, name: str, default: str | None = None) -> str | None:
        return self._items.get(name.lower(), default)


class _GatewayRequest(dict):
    """The two things ``session_key_is_attested`` reads off an aiohttp request.

    ``request.get("peer_verified")`` is the kernel attestation, which a loopback
    TCP caller never has, so it stays absent; ``request.headers`` is the request
    the child actually built.
    """

    def __init__(self, req: urllib.request.Request) -> None:
        super().__init__()
        self.headers = _WireHeaders(req)


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


class TestChildPresentsTheTokenOverHttp:
    """The launcher's token must ride every ``ScriptContext`` request, not only the MCP hop.

    ``ctx._post("/api/crons/<id>/run", ...)`` reaches an owner-surface route,
    which accepts the declared ``X-Session-Key`` only behind an attestation. The
    child sends the key on every request, so a child that keeps the token to
    itself is refused on exactly the routes it needs the identity for, while
    ``notify()`` -- an unguarded route -- keeps working and hides the gap.
    """

    def _context_in_child_env(self, env: dict[str, str]) -> ScriptContext:
        """Construct the context the way the launcher's child does: from its env."""
        child_env = {"_KIROCREW_DIAL_PORT": "5476"}
        if STUB_SESSION_TOKEN_ENV in env:
            child_env[STUB_SESSION_TOKEN_ENV] = env[STUB_SESSION_TOKEN_ENV]
        with patch.dict(os.environ, child_env):
            return ScriptContext(job=MagicMock(id=JOB_ID, message=""))

    def test_the_launchers_token_is_sent_as_x_session_token(self):
        env = _capture_launcher_env(JOB_ID)
        ctx = self._context_in_child_env(env)

        req = _sent_request(ctx, f"/api/crons/{JOB_ID}/run", {})

        assert req.get_header("X-session-token") == env[STUB_SESSION_TOKEN_ENV]
        assert req.get_header("X-session-key") == EXPECTED_KEY

    def test_the_gateways_own_consumer_attests_the_sent_request(self, signing_root):
        """End to end through the real primitives, while the run's mapping exists.

        The launcher publishes the mapping for the life of the run and retracts
        it in its ``finally``, so the request has to be built and judged DURING
        the spawn -- the same window in which a real child makes its calls. The
        judge is ``member_memory_auth.session_key_is_attested``, the function
        every owner-surface route asks, given the request the child built and
        the key that request declares.
        """
        from kiro_crew.member_memory_auth import session_key_is_attested

        verdicts: dict[str, bool] = {}

        def judge_during_run(env):
            req = _sent_request(self._context_in_child_env(env), f"/api/crons/{JOB_ID}/run", {})
            declared = req.get_header("X-session-key")
            verdicts["declared"] = declared == EXPECTED_KEY
            verdicts["attested"] = session_key_is_attested(_GatewayRequest(req), declared)
            verdicts["not_for_a_neighbour"] = session_key_is_attested(
                _GatewayRequest(req), "cron:job-other"
            )

        _capture_launcher_env(JOB_ID, during_spawn=judge_during_run)

        assert verdicts == {"declared": True, "attested": True, "not_for_a_neighbour": False}

    def test_without_the_token_the_same_request_is_unattested(self, signing_root):
        """Baseline: the key alone is exactly the request the gateway refuses."""
        from kiro_crew.member_memory_auth import session_key_is_attested

        verdicts: dict[str, bool] = {}

        def judge_during_run(env):
            bare = {k: v for k, v in env.items() if k != STUB_SESSION_TOKEN_ENV}
            req = _sent_request(self._context_in_child_env(bare), f"/api/crons/{JOB_ID}/run", {})
            verdicts["has_token"] = req.has_header("X-session-token")
            verdicts["attested"] = session_key_is_attested(_GatewayRequest(req), EXPECTED_KEY)

        _capture_launcher_env(JOB_ID, during_spawn=judge_during_run)

        assert verdicts == {"has_token": False, "attested": False}

    def test_no_token_means_no_header(self):
        """A directly constructed context sends exactly the request it always sent."""
        ctx = self._context_in_child_env({})

        req = _sent_request(ctx, "/api/send-message", {"text": "x"})

        assert not req.has_header("X-session-token")
        assert req.get_header("X-session-key") == EXPECTED_KEY

    def test_the_token_stays_in_the_environ_for_the_mcp_bridge(self):
        """Read, not popped: ``ctx.call_tool``'s server children resolve their identity from it.

        The secret is popped so ``fn(ctx)`` cannot reach it; the token is not a
        secret of that kind -- it names the job, and the MCP bridge builds every
        server child's env from this process's ``os.environ``.
        """
        child_env = {STUB_SESSION_TOKEN_ENV: "a" * 64, "_KIROCREW_DIAL_PORT": "5476"}
        with patch.dict(os.environ, child_env):
            ctx = ScriptContext(job=MagicMock(id=JOB_ID, message=""))
            assert os.environ[STUB_SESSION_TOKEN_ENV] == "a" * 64
        assert ctx._session_token == "a" * 64

    def test_the_token_is_not_in_the_contexts_repr(self):
        """The token is a bearer name; a script that logs its context must not log it."""
        child_env = {STUB_SESSION_TOKEN_ENV: "a" * 64, "_KIROCREW_DIAL_PORT": "5476"}
        with patch.dict(os.environ, child_env):
            ctx = ScriptContext(job=MagicMock(id=JOB_ID, message=""))

        assert "a" * 64 not in repr(ctx)


class TestPostSurfacesRefusals:
    """A refused request must come back as a refusal a script can read, not a status line.

    ``str(HTTPError)`` is ``"HTTP Error 409: Conflict"``: the body that says WHY
    (``member_identity_unavailable`` versus a job that is already running) never
    reached the script, so a refused trigger looked like any transport hiccup.
    """

    def _context(self) -> ScriptContext:
        with patch.dict(os.environ, {"_KIROCREW_DIAL_PORT": "5476"}):
            return ScriptContext(job=MagicMock(id=JOB_ID, message=""))

    def _post_refused_with(self, status: int, reason: str, body: bytes) -> dict:
        exc = urllib.error.HTTPError(
            f"http://localhost:5476/api/crons/{JOB_ID}/run", status, reason, {}, io.BytesIO(body)
        )
        with patch("kiro_crew.cron_script.loopback_urlopen", side_effect=exc):
            return self._context()._post(f"/api/crons/{JOB_ID}/run", {})

    def test_a_gateway_refusal_carries_its_status_reason_and_code(self):
        result = self._post_refused_with(
            409,
            "Conflict",
            b'{"error": "The execution identity is unavailable; Global memory was not used.",'
            b' "code": "member_identity_unavailable"}',
        )

        assert result == {
            "error": "HTTP 409: The execution identity is unavailable; Global memory was not used.",
            "status_code": 409,
            "code": "member_identity_unavailable",
        }

    def test_a_refusal_without_a_json_body_still_names_the_status(self):
        result = self._post_refused_with(403, "Forbidden", b"Forbidden")

        assert result == {"error": "HTTP 403: Forbidden", "status_code": 403}

    def test_an_empty_refusal_body_falls_back_to_the_status_reason(self):
        result = self._post_refused_with(404, "Not Found", b"")

        assert result == {"error": "HTTP 404: Not Found", "status_code": 404}

    def test_a_code_that_is_not_an_identifier_is_dropped_not_echoed(self):
        result = self._post_refused_with(
            409, "Conflict", b'{"error": "job is already running", "code": "<script>x</script>"}'
        )

        assert result == {"error": "HTTP 409: job is already running", "status_code": 409}

    def test_a_body_nested_past_the_parser_limit_is_still_a_refusal(self):
        """``json.loads`` raises RecursionError there, which is not a ValueError."""
        depth = 20_000
        result = self._post_refused_with(409, "Conflict", b"[" * depth + b"]" * depth)

        assert result["status_code"] == 409
        assert result["error"].startswith("HTTP 409: ")
        assert "code" not in result

    def test_a_transport_failure_keeps_the_plain_error_shape(self):
        with patch(
            "kiro_crew.cron_script.loopback_urlopen", side_effect=OSError("connection refused")
        ):
            result = self._context()._post("/api/send-message", {"text": "x"})

        assert result == {"error": "connection refused"}

    def test_notify_raises_with_the_refusal_not_the_status_line(self):
        exc = urllib.error.HTTPError(
            "http://localhost:5476/api/send-message",
            403,
            "Forbidden",
            {},
            io.BytesIO(b'{"error": "Forbidden", "code": "internal_auth_mismatch"}'),
        )
        with (
            patch("kiro_crew.cron_script.loopback_urlopen", side_effect=exc),
            pytest.raises(RuntimeError, match=r"notify\(\) failed: HTTP 403: Forbidden"),
        ):
            self._context().notify("hello")


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

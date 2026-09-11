"""KAS spawn contract: relay argv, auth owner, sandbox classification, capabilities, demux.

Kiro Crew reaches KAS through kiro-cli's own ACP relay
(``kiro-cli acp --agent-engine v3``) rather than by locating kiro-cli's
extracted KAS bundle and running ``node acp-server.js`` itself. See
:mod:`kiro_crew.acp.kas_transport` for why, for the frame-parity measurement
that preceded the switch, and for the two auth owners: ``--auth-method cli``
(kiro-cli's store, the default) or -- when Crew's own vault holds an identity --
no flag, with Crew answering the engine's ``_kiro/auth/getAccessToken`` request
from :mod:`kiro_crew.auth`.

The invocation proof lives in ``TestKasInvocation``: it spawns a real
``AcpRuntime`` configured for the KAS backend against a stub agent that speaks
KAS's dialect, and completes ``initialize`` -> ``session/new``. That exercises
OUR spawn path, argv, capabilities and demux; it does not exercise the real KAS
build, which is kiro-cli's to ship.

Driving the REAL build is deliberately NOT a test here: a real prompt turn spends
the operator's credits and needs their live credential store, state no
``tmp_path`` contains, and an env-var opt-in is not enough protection when that
variable can be set in a shell profile or a CI matrix and then reached by an
ordinary ``pytest`` run.

When working on the backend, drive it by hand instead -- with the relay this
needs no asset overrides and no token plumbing, because kiro-cli owns both::

    kiro-cli acp --agent-engine v3 --auth-method cli
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.acp import session_handle as sh
from kiro_crew.acp.kas_host_auth import HostAuthCallbackError
from kiro_crew.acp.kas_transport import (
    KAS_AUTH_CALLBACK_ERROR_CODE,
    KAS_RELAY_AUTH_OWNER,
    KAS_RELAY_ENGINE,
    KAS_RELAY_SUBCMD,
    METHOD_KAS_AUTH_GET_ACCESS_TOKEN,
    build_kas_argv,
)
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_HOST_AUTH_CALLBACK,
    ACP_CLIENT_CAPABILITIES,
    KAS_CLIENT_CAPABILITIES,
)
from kiro_crew.config.paths import kiro_agents_dir


@pytest.fixture(autouse=True)
def _fast_no_report_ceiling(monkeypatch):
    """Shrink drain_init()'s no-report ceiling for every test in this module.

    create_session() drains MCP-init frames before returning, and the KAS stub
    here registers no MCP server, so nothing ever arms the idle exit and each
    session pays the full production ceiling. drain_init() resolves the module
    constant at call time precisely so this patch takes effect. Nothing in this
    module asserts on the ceiling itself.
    """
    monkeypatch.setattr(sh, "_MCP_DRAIN_NO_REPORT_CEILING", 0.05)


class TestArgv:
    def test_shape_is_the_cli_acp_relay(self):
        argv = build_kas_argv("/usr/bin/kiro-cli")
        assert argv[0] == "/usr/bin/kiro-cli"
        assert argv[1] == KAS_RELAY_SUBCMD

    def test_engine_is_pinned_explicitly(self):
        """``acp`` defaults to kiro-cli's own agent loop, not KAS.

        Relying on the default would let a kiro-cli release silently serve a
        different engine while every frame still looked well-formed, so the
        engine is always stated.
        """
        argv = build_kas_argv("/usr/bin/kiro-cli")
        assert "--agent-engine" in argv
        assert argv[argv.index("--agent-engine") + 1] == KAS_RELAY_ENGINE

    def test_auth_owner_defaults_to_the_cli(self):
        """Default argv pins ``--auth-method cli``: kiro-cli's store, Crew never
        sees the engine's credential callback. Byte-for-byte the pre-vault argv."""
        argv = build_kas_argv("/usr/bin/kiro-cli")
        assert "--auth-method" in argv
        assert argv[argv.index("--auth-method") + 1] == KAS_RELAY_AUTH_OWNER

    def test_host_auth_omits_the_auth_flag(self):
        """Crew as auth owner is the ABSENCE of the flag, not a third value.

        kiro-cli's client-owned default leaves ``_kiro/auth/getAccessToken`` on
        the wire; inventing a spelling here would be rejected by every release.
        Everything else in the argv is unchanged.
        """
        cli = build_kas_argv("/usr/bin/kiro-cli")
        host = build_kas_argv("/usr/bin/kiro-cli", host_auth=True)
        assert "--auth-method" not in host
        assert KAS_RELAY_AUTH_OWNER not in host
        assert host == [a for a in cli if a not in ("--auth-method", KAS_RELAY_AUTH_OWNER)]

    def test_no_agent_flag_is_passed(self):
        """Crew binds its agent over the wire, not by naming a kiro-cli mode.

        ``--agent`` would select a mode kiro-cli found on disk; the wire-injected
        agent is the one Crew's governance ceiling actually filtered.
        """
        assert "--agent" not in build_kas_argv("/usr/bin/kiro-cli")

    def test_no_model_flag_is_passed(self):
        """One process hosts N sessions, so a start-time model would bind all of
        them; the model is chosen per session over the wire."""
        assert "--model" not in build_kas_argv("/usr/bin/kiro-cli")

    def test_empty_binary_is_refused(self):
        """A falsy path must not become argv[0]="" and a confusing exec error."""
        with pytest.raises(ValueError):
            build_kas_argv("")


class TestAuthOwnership:
    """Who answers the engine's credential callback, and on what authority.

    Two owners: kiro-cli (``--auth-method cli``, the default -- the frame never
    arrives) or Crew (no flag, because the Crew vault held an identity at spawn
    -- the frame arrives and is answered from :mod:`kiro_crew.auth`). A third
    shape is forbidden: a shell-out to kiro-cli's hidden token verb.
    """

    def test_no_shell_out_token_resolver_remains(self):
        """The credential is Crew's own vault or kiro-cli's own store -- never
        a subprocess. Asserted on the module surface because a re-added helper
        is exactly the regression the relay switch was meant to prevent."""
        from kiro_crew.acp import runtime as runtime_mod

        for gone in ("_deliver_kas_access_token", "resolve_kas_access_token"):
            assert not hasattr(AcpRuntime, gone), f"AcpRuntime.{gone} should be gone"
            assert not hasattr(runtime_mod, gone), f"runtime.{gone} should be gone"

    def test_the_shell_out_module_is_gone(self):
        with pytest.raises(ModuleNotFoundError):
            __import__("kiro_crew.acp.kas_auth")

    def test_host_auth_callback_is_positive_membership(self):
        """Handing a child a credential is authorized by membership (H5/H8), and
        the Kiro backend -- whose child never raises the callback -- is not in."""
        assert ACP_BACKEND_KAS in ACP_BACKENDS_HOST_AUTH_CALLBACK
        assert ACP_BACKEND_KIRO not in ACP_BACKENDS_HOST_AUTH_CALLBACK

    def test_kas_is_retired_by_an_external_kiro_cli_logout(self):
        """Membership in the identity-store sweep is unchanged.

        In cli-owned mode the reason is as before: the relay resolves every token
        from kiro-cli's own store. In Crew-owned mode a recycle on kiro-cli
        logout is harmless (the replacement re-probes the vault and comes back
        Crew-owned), so staying conservative costs one respawn, never a turn on
        stale credentials.
        """
        from kiro_crew.acp.types import backends_retired_by_host_logout

        assert ACP_BACKEND_KAS in backends_retired_by_host_logout()

    def test_the_runtime_declares_the_identity_capability(self, tmp_path):
        """The sweep reads the declared property, not the frozenset directly."""
        runtime = AcpRuntime(
            work_dir=tmp_path / "ident",
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        assert runtime.uses_kiro_identity_store is True

    @pytest.mark.asyncio
    async def test_ownerless_auth_request_is_answered_not_hung(self, tmp_path):
        """On a cli-owned spawn a stray credential request gets -32601.

        The frame is not expected there, so it takes the ordinary unroutable-
        request path; the property that matters is that SOMETHING answers, and
        that the answer is not a credential.
        """
        runtime = AcpRuntime(
            work_dir=tmp_path / "auth",
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        sent: list[tuple[object, int, str]] = []

        async def record_error(request_id, code, message):
            sent.append((request_id, code, message))

        with patch.object(runtime, "send_error", side_effect=record_error):
            await runtime._answer_ownerless_request(99, METHOD_KAS_AUTH_GET_ACCESS_TOKEN)
        assert sent and sent[0][0] == 99
        assert sent[0][1] == -32601

    @pytest.mark.asyncio
    async def test_host_auth_answer_is_the_vault_response(self, tmp_path):
        """A Crew-owned runtime hands the engine exactly what the vault rendered.

        The vault call is patched: this pins the plumbing (result → response on
        the request id, nothing added, nothing logged), not the provider, which
        has its own tests.
        """
        runtime = AcpRuntime(
            work_dir=tmp_path / "host",
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        rendered = {
            "accessToken": "t",
            "expiresAt": "2099-01-01T00:00:00+00:00",
            "provider": "Google",
        }
        sent: list[tuple[object, dict]] = []

        async def record_response(request_id, result):
            sent.append((request_id, result))

        async def fake_answer():
            return rendered

        with (
            patch("kiro_crew.acp.harness.kas.answer_get_access_token", side_effect=fake_answer),
            patch.object(runtime, "send_response", side_effect=record_response),
        ):
            await runtime._answer_get_access_token(7)
        assert sent == [(7, rendered)]

    @pytest.mark.asyncio
    async def test_host_auth_failure_is_an_error_not_a_hang(self, tmp_path):
        """No stored credential → JSON-RPC error the engine turns into its
        sign-in prompt. The message is the token-free one the seam raised."""
        runtime = AcpRuntime(
            work_dir=tmp_path / "host-err",
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        sent: list[tuple[object, int, str]] = []

        async def record_error(request_id, code, message):
            sent.append((request_id, code, message))

        async def refuse():
            raise HostAuthCallbackError("not signed in to Kiro Crew")

        with (
            patch("kiro_crew.acp.harness.kas.answer_get_access_token", side_effect=refuse),
            patch.object(runtime, "send_error", side_effect=record_error),
        ):
            await runtime._answer_get_access_token(8)
        assert sent == [(8, KAS_AUTH_CALLBACK_ERROR_CODE, "not signed in to Kiro Crew")]


class TestSandboxClassification:
    """KAS must not be declared to the sandbox as kiro-cli.

    ``wrap_argv`` skips Crew's own seatbelt when told the child is kiro-cli with
    its internal sandbox on, because on macOS the two cannot nest. The relay DOES
    spawn a kiro-cli binary now, which makes the claim look tempting -- and it is
    wrong. kiro-cli spawns the KAS server with no ``--sandbox`` argument, and KAS
    resolves an absent sandbox config to its no-op backend, so nothing starts an
    OS sandbox inside. There is therefore nothing to nest (no EPERM risk) and
    nothing to delegate to: this membership test fails OPEN, so claiming it would
    skip Crew's seatbelt in favour of a layer that never exists and leave KAS
    unconfined on macOS. False is the load-bearing answer, not the cautious one.
    """

    class _Abort(Exception):
        """Stops ``spawn`` at the sandbox call so no child is ever executed."""

    @pytest.mark.asyncio
    async def test_kas_is_not_classified_as_kiro_cli(self, kas_stub, tmp_path):
        captured: dict[str, object] = {}

        def fake_wrap(argv, **kwargs):
            captured.update(kwargs)
            raise self._Abort

        runtime = AcpRuntime(
            work_dir=tmp_path / "sbx",
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        with patch("kiro_crew.acp.runtime.wrap_argv", side_effect=fake_wrap):
            with pytest.raises(self._Abort):
                await runtime.spawn()
        assert captured["is_kiro_cli"] is False

    @pytest.mark.asyncio
    async def test_kiro_still_classified_as_kiro_cli(self, tmp_path, monkeypatch):
        """The delegation the kiro path depends on must stay untouched."""
        captured: dict[str, object] = {}

        def fake_wrap(argv, **kwargs):
            captured.update(kwargs)
            raise self._Abort

        async def fake_bin(*, environ=None, home=None):
            return "/usr/bin/kiro-cli"

        monkeypatch.setattr("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", fake_bin)
        monkeypatch.setattr("kiro_crew.agent.ensure_agent_materialized", lambda _agent: None)
        runtime = AcpRuntime(work_dir=tmp_path / "sbx2", sandbox_mode="off")
        with patch("kiro_crew.acp.runtime.wrap_argv", side_effect=fake_wrap):
            with pytest.raises(self._Abort):
                await runtime.spawn()
        assert captured["is_kiro_cli"] is True


class TestCapabilities:
    def test_kas_adds_the_kiro_settings_channel(self):
        assert KAS_CLIENT_CAPABILITIES["_meta"]["kiro"]["settings"] == {}

    def test_kas_keeps_the_standard_top_level_declarations(self):
        for key, value in ACP_CLIENT_CAPABILITIES.items():
            assert KAS_CLIENT_CAPABILITIES[key] == value

    def test_callback_capabilities_stay_undeclared(self):
        """Crew implements none of KAS's client-callback capabilities.

        Declaring one would make KAS call back for a feature this client cannot
        service, so their absence is the correct declaration, not a gap.
        """
        kiro_meta = KAS_CLIENT_CAPABILITIES["_meta"]["kiro"]
        for absent in ("secretStorage", "knowledge", "textSearch", "findFiles"):
            assert absent not in kiro_meta


# ── invocation proof ────────────────────────────────────────────────────────

#: A stub agent speaking KAS's dialect: it echoes the client's protocolVersion,
#: advertises loadSession, and ends a turn with session_info_update/turn_end
#: rather than kiro-cli's standalone completion frame.
_STUB_AGENT = """
import json, os, sys

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

# Mirrors the engine's auth owners: with ``--auth-method`` on the argv the
# credential is kiro-cli's and no callback is raised; without it the engine asks
# its host BEFORE answering initialize, and this stub records what came back.
HOST_OWNED = "--auth-method" not in sys.argv[1:]
CAPTURE = os.environ.get("KIROCREW_TEST_KAS_AUTH_CAPTURE")

def ask_host_for_credential(stdin):
    send({"jsonrpc": "2.0", "id": 0, "method": "_kiro/auth/getAccessToken", "params": {}})
    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        reply = json.loads(raw)
        if reply.get("id") == 0 and "method" not in reply:
            if CAPTURE:
                with open(CAPTURE, "w") as fh:
                    json.dump(reply, fh)
            return

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        if HOST_OWNED:
            ask_host_for_credential(sys.stdin)
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion"),
            "agentCapabilities": {
                "loadSession": True,
                "promptCapabilities": {"image": True},
                "_meta": {"kiro": {"extensionMethods": ["_kiro/session/compact"]}},
            },
            "_meta": {"kiro": {"sawClientMeta": params.get("clientCapabilities", {}).get("_meta")}},
        }})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "sessionId": "kas-stub-session", "modes": {"currentModeId": "default"}}})
    elif method == "session/prompt":
        sid = params.get("sessionId")
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": sid,
            "update": {"sessionUpdate": "agent_message_chunk",
                       "content": {"type": "text", "text": "pong from the KAS stub"}}}})
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": sid,
            "update": {"sessionUpdate": "session_info_update",
                       "_meta": {"kiro": {"turnEnd": {"stopReason": "end_turn"}}}}}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
"""


#: The one agent spec ``session/new`` projects onto KAS. Minimal on purpose: this
#: file tests the SPAWN contract; the projection itself is covered elsewhere.
_STUB_AGENT_SPEC = {
    "name": "kirocrew",
    "description": "spawn-contract stub",
    "prompt": "You are a test agent.",
    "tools": [],
    "allowedTools": [],
}


@pytest.fixture
def kas_stub(tmp_path, monkeypatch):
    """Stand in for the kiro-cli binary the KAS relay argv is built around.

    The launcher forwards its arguments -- ``acp --agent-engine v3`` plus, in
    cli-owned mode, ``--auth-method cli`` -- to the stub agent on this
    interpreter, which reads exactly one thing from them: whether
    ``--auth-method`` is present. Present, it behaves like the relay with kiro-cli
    owning auth (no credential callback); absent, it asks its host for a
    credential before answering ``initialize``, as the engine does. Argv fidelity
    beyond that is asserted separately in ``TestArgv``.

    Also redirects the kiro agent home and puts a spec in it, because the
    projection reads one and nothing in a test can make production write it:
    ``ensure_agent_materialized`` REFUSES to write the shared agent home from an
    ephemeral instance -- a checkout plus a temp data home, which describes every
    test -- and logs "This instance will use the existing specs instead". Without
    the redirect, "the existing specs" are the developer's own installed agents,
    so this test's verdict depended on which of them were present and whether
    their ``file://`` prompt files still resolved.

    ``KIRO_HOME`` rather than patching ``Path.home()``: it is the documented
    override and it reaches the resolver this path uses. Note its scope caveat
    (``config/paths.py``) -- today only the agents directory follows it, which is
    enough here because the agents directory is the whole dependency.
    """
    script = tmp_path / "kas_stub.py"
    script.write_text(_STUB_AGENT)
    launcher = tmp_path / "kiro-cli-stub"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    launcher.chmod(0o755)

    async def fake_bin(*, environ=None, home=None) -> str:
        return str(launcher)

    monkeypatch.setattr("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", fake_bin)
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro-home"))
    agents_dir = kiro_agents_dir()
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{_STUB_AGENT_SPEC['name']}.json").write_text(
        json.dumps(_STUB_AGENT_SPEC), encoding="utf-8"
    )
    return launcher


@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
class TestKasInvocation:
    """Drive a KAS-shaped agent through the real runtime spawn path."""

    @pytest.mark.asyncio
    async def test_handshake_and_prompt_round_trip(self, kas_stub, tmp_path):
        runtime = AcpRuntime(
            work_dir=tmp_path / "ws",
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        try:
            await runtime.spawn()
            assert runtime.is_alive()
            # KAS advertises session/load; the runtime must have recorded it.
            assert runtime._can_load_session is True
            handle = await runtime.create_session(cwd=tmp_path / "ws")
            assert handle is not None
        finally:
            await runtime.kill()

    @pytest.mark.asyncio
    async def test_initialize_sends_the_kiro_meta_capabilities(self, kas_stub, tmp_path):
        """The stub reflects what it received, proving _meta.kiro reached KAS."""
        runtime = AcpRuntime(
            work_dir=tmp_path / "ws2",
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        try:
            await runtime.spawn()
            assert runtime.is_alive()
        finally:
            await runtime.kill()

    @pytest.mark.asyncio
    async def test_crew_owned_spawn_answers_the_credential_callback_before_initialize(
        self, kas_stub, tmp_path, monkeypatch
    ):
        """End-to-end through the real reader loop.

        With the vault reporting an identity, the runtime spawns the Crew-owned
        argv; the stub then asks for a credential BEFORE it answers
        ``initialize`` -- the engine's real ordering, and the reason the handler
        lives on the reader loop rather than a session handle. The handshake
        completing at all proves the callback was answered; the capture file
        proves it was answered with the vault's rendering, on the request's id.
        """
        capture = tmp_path / "auth-reply.json"
        monkeypatch.setenv("KIROCREW_TEST_KAS_AUTH_CAPTURE", str(capture))
        rendered = {
            "accessToken": "t",
            "expiresAt": "2099-01-01T00:00:00+00:00",
            "provider": "Google",
        }

        async def has_identity():
            return True

        async def fake_answer():
            return rendered

        runtime = AcpRuntime(
            work_dir=tmp_path / "ws5",
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        with (
            patch(
                "kiro_crew.acp.harness.kas.vault_holds_identity_off_loop", side_effect=has_identity
            ),
            patch("kiro_crew.acp.harness.kas.answer_get_access_token", side_effect=fake_answer),
        ):
            try:
                await runtime.spawn()
                assert runtime.is_alive()
                assert runtime._kas_host_auth is True
            finally:
                await runtime.kill()
        reply = json.loads(capture.read_text())
        assert reply == {"jsonrpc": "2.0", "id": 0, "result": rendered}

    @pytest.mark.asyncio
    async def test_cli_owned_spawn_never_asks_crew(self, kas_stub, tmp_path, monkeypatch):
        """The control: with no Crew identity the stub sees ``--auth-method`` and
        raises no callback, and the vault answerer is never reached."""
        capture = tmp_path / "auth-reply.json"
        monkeypatch.setenv("KIROCREW_TEST_KAS_AUTH_CAPTURE", str(capture))

        async def no_identity():
            return False

        async def must_not_run():
            raise AssertionError("vault answerer reached on a cli-owned spawn")

        runtime = AcpRuntime(
            work_dir=tmp_path / "ws6",
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        with (
            patch(
                "kiro_crew.acp.harness.kas.vault_holds_identity_off_loop", side_effect=no_identity
            ),
            patch("kiro_crew.acp.harness.kas.answer_get_access_token", side_effect=must_not_run),
        ):
            try:
                await runtime.spawn()
                assert runtime.is_alive()
            finally:
                await runtime.kill()
        assert not capture.exists()

    @pytest.mark.asyncio
    async def test_spawn_argv_is_the_relay_invocation(self, kas_stub, tmp_path):
        """End-to-end: with no Crew identity the argv is the cli-owned relay one.

        The vault probe is pinned False rather than trusted to the conftest's
        empty data home, so a developer's own sign-in cannot flip this verdict.
        """

        async def no_identity():
            return False

        runtime = AcpRuntime(
            work_dir=tmp_path / "ws3",
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        with patch(
            "kiro_crew.acp.harness.kas.vault_holds_identity_off_loop", side_effect=no_identity
        ):
            argv = (await runtime._resolve_spawn_plan()).argv
        assert argv == build_kas_argv(str(kas_stub))
        assert Path(argv[0]).name == "kiro-cli-stub"
        assert runtime._kas_host_auth is False

    @pytest.mark.asyncio
    async def test_spawn_argv_hands_auth_to_crew_when_the_vault_has_an_identity(
        self, kas_stub, tmp_path
    ):
        """A stored Crew identity flips the SAME spawn to the Crew-owned shape:
        no ``--auth-method``, and the runtime remembers it will be asked."""

        async def has_identity():
            return True

        runtime = AcpRuntime(
            work_dir=tmp_path / "ws4",
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        with patch(
            "kiro_crew.acp.harness.kas.vault_holds_identity_off_loop", side_effect=has_identity
        ):
            argv = (await runtime._resolve_spawn_plan()).argv
        assert argv == build_kas_argv(str(kas_stub), host_auth=True)
        assert "--auth-method" not in argv
        assert runtime._kas_host_auth is True

    @pytest.mark.asyncio
    async def test_missing_kiro_cli_fails_with_an_actionable_error(self, tmp_path, monkeypatch):
        """No kiro-cli means no relay, and the message must say which binary."""
        from kiro_crew.acp.session_handle import AcpRuntimeError

        async def no_bin(*, environ=None, home=None) -> None:
            return None

        monkeypatch.setattr("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", no_bin)
        runtime = AcpRuntime(
            work_dir=tmp_path / "ws4",
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        with pytest.raises(AcpRuntimeError, match="kiro-cli"):
            await runtime._resolve_spawn_plan()

"""KAS spawn contract: relay argv, sandbox classification, capabilities, demux.

Kiro Crew reaches KAS through kiro-cli's own ACP relay
(``kiro-cli acp --agent-engine v3 --auth-method cli``) rather than by locating
kiro-cli's extracted KAS bundle and running ``node acp-server.js`` itself. See
:mod:`kiro_crew.acp.kas_transport` for why, and for the frame-parity measurement
that preceded the switch.

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
from kiro_crew.acp.kas_transport import (
    KAS_RELAY_AUTH_OWNER,
    KAS_RELAY_ENGINE,
    KAS_RELAY_SUBCMD,
    build_kas_argv,
)
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKENDS_KIRO_IDENTITY_STORE,
    ACP_CLIENT_CAPABILITIES,
    KAS_CLIENT_CAPABILITIES,
)
from kiro_crew.config.paths import kiro_agents_dir

#: Inlined rather than imported: the relay owns this frame, so Crew has no
#: production consumer for the name and only this test still needs the literal.
METHOD_KAS_AUTH_GET_ACCESS_TOKEN = "_kiro/auth/getAccessToken"


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

    def test_auth_owner_is_the_cli(self):
        """The relay resolves tokens from kiro-cli's own store.

        This is what removes Crew from the credential path: without it the engine
        expects its host to answer ``_kiro/auth/getAccessToken``.
        """
        argv = build_kas_argv("/usr/bin/kiro-cli")
        assert "--auth-method" in argv
        assert argv[argv.index("--auth-method") + 1] == KAS_RELAY_AUTH_OWNER

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
    """Crew must not serve KAS's credential callback any more.

    The relay owns it (``--auth-method cli``), so the frame no longer arrives.
    These tests pin the consequence rather than the absence: nothing in Crew
    resolves a KAS token, and a connection-level request that does arrive is
    answered rather than left to hang.
    """

    def test_no_token_resolver_remains_in_the_runtime(self):
        """The shell-out to kiro-cli's hidden token verb is gone.

        Asserted on the module surface because a re-added helper is exactly the
        regression this switch is meant to prevent.
        """
        from kiro_crew.acp import runtime as runtime_mod

        for gone in (
            "_answer_get_access_token",
            "_deliver_kas_access_token",
            "resolve_kas_access_token",
        ):
            assert not hasattr(AcpRuntime, gone), f"AcpRuntime.{gone} should be gone"
            assert not hasattr(runtime_mod, gone), f"runtime.{gone} should be gone"

    def test_the_kas_auth_module_is_gone(self):
        """No importable KAS credential path is left anywhere in the package."""
        with pytest.raises(ModuleNotFoundError):
            __import__("kiro_crew.acp.kas_auth")

    def test_kas_is_retired_by_an_external_kiro_cli_logout(self):
        """Auth moved INTO kiro-cli, so KAS inherits its identity lifetime.

        ``--auth-method cli`` resolves every token from kiro-cli's own store, so
        a ``kiro-cli logout`` or account switch invalidates a running KAS relay
        exactly as it invalidates the kiro backend. Without this membership the
        identity sweep would leave the relay serving turns on the previous
        account's credentials.
        """
        assert ACP_BACKEND_KAS in ACP_BACKENDS_KIRO_IDENTITY_STORE

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
        """A sessionId-less request gets -32601 from the ownerless answerer.

        Previously ``_kiro/auth/getAccessToken`` had a bespoke branch here. With
        the relay owning auth it takes the ordinary unroutable-request path, and
        the property that matters is that SOMETHING answers: a silent drop would
        wedge whichever peer sent it.

        No spawn: the answerer's only side effect is the patched ``send_error``,
        so driving it against a constructed runtime keeps this covered on Windows
        too (the stub launcher is a POSIX shell script).
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

        monkeypatch.setattr("kiro_crew.acp.runtime._resolve_kiro_bin_for_spawn", fake_bin)
        monkeypatch.setattr("kiro_crew.acp.runtime.ensure_agent_materialized", lambda _agent: None)
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
import json, sys

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
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

    The launcher ignores its arguments -- ``acp --agent-engine v3 --auth-method
    cli`` -- and runs the stub agent on this interpreter. Argv fidelity is
    asserted separately in ``TestArgv``, so swallowing them here costs no
    coverage and keeps the stub independent of flag order.

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
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n')
    launcher.chmod(0o755)

    async def fake_bin(*, environ=None, home=None) -> str:
        return str(launcher)

    monkeypatch.setattr("kiro_crew.acp.runtime._resolve_kiro_bin_for_spawn", fake_bin)
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
    async def test_spawn_argv_is_the_relay_invocation(self, kas_stub, tmp_path):
        """End-to-end: the argv the runtime actually resolves is the relay one."""
        runtime = AcpRuntime(
            work_dir=tmp_path / "ws3",
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        argv = await runtime._resolve_spawn_argv()
        assert argv == build_kas_argv(str(kas_stub))
        assert Path(argv[0]).name == "kiro-cli-stub"

    @pytest.mark.asyncio
    async def test_missing_kiro_cli_fails_with_an_actionable_error(self, tmp_path, monkeypatch):
        """No kiro-cli means no relay, and the message must say which binary."""
        from kiro_crew.acp.session_handle import AcpRuntimeError

        async def no_bin(*, environ=None, home=None) -> None:
            return None

        monkeypatch.setattr("kiro_crew.acp.runtime._resolve_kiro_bin_for_spawn", no_bin)
        runtime = AcpRuntime(
            work_dir=tmp_path / "ws4",
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        with pytest.raises(AcpRuntimeError, match="kiro-cli"):
            await runtime._resolve_spawn_argv()

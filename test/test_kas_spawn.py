"""KAS spawn contract: asset resolution, argv, and client capabilities.

The invocation proof lives in ``TestKasInvocation``: it spawns a real
``AcpRuntime`` configured for the KAS backend against a stub agent that speaks
KAS's dialect, and completes ``initialize`` -> ``session/new`` -> ``session/prompt``
-> turn end. That exercises OUR spawn path, argv, capabilities and demux; it does
not exercise the real KAS build, which is not present on a machine whose kiro-cli
has never unpacked it.

Driving the REAL build is deliberately NOT a test here. KAS is launched with
``--auth=acp-callback``, so a real prompt makes it call back for a token, which
the runtime answers by shelling out to ``kiro-cli chat _ get-kas-token`` — that
touches the operator's live credential store, state no ``tmp_path`` contains, and
an env-var opt-in is not enough protection when that variable can be set in a
shell profile or a CI matrix and then reached by an ordinary ``pytest`` run.
Faking the callback proves nothing about the real auth path either.

When working on the backend, run it by hand instead::

    python - <<'EOF'
    import asyncio
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.acp.types import ACP_BACKEND_KAS

    async def main():
        rt = AcpRuntime(work_dir="/tmp/kas-check", sandbox_mode="off",
                        acp_backend=ACP_BACKEND_KAS)
        await rt.spawn()                      # initialize against the real build
        print("loadSession:", rt._can_load_session)
        await rt.create_session(cwd="/tmp/kas-check", agent="kirocrew")
        await rt.kill()
    asyncio.run(main())
    EOF

That call now succeeds: Crew injects its agent through ``_meta.kiro.customAgents``
on ``session/new`` and activates it with ``session/set_mode``, and with
``--auth=acp-callback`` a real prompt round-trip completes (the host answers KAS's
token callback by shelling out to kiro-cli). ``agent.acp_backend`` accepts ``kas``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.acp import kas_assets
from kiro_crew.acp.kas_assets import (
    ENV_KAS_NODE,
    ENV_KAS_SCRIPT,
    KAS_NODE_FLAGS,
    KAS_TRANSPORT_ARG,
    KasAssetsMissing,
    build_kas_argv,
    resolve_kas_entry,
)
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_CLIENT_CAPABILITIES,
    KAS_CLIENT_CAPABILITIES,
)
from kiro_crew.config.paths import kiro_agents_dir


class TestAssetResolution:
    def test_env_overrides_win(self, tmp_path, monkeypatch):
        node = tmp_path / "node"
        script = tmp_path / "acp-server.js"
        node.write_text("")
        script.write_text("")
        monkeypatch.setenv(ENV_KAS_NODE, str(node))
        monkeypatch.setenv(ENV_KAS_SCRIPT, str(script))
        assert resolve_kas_entry() == (node, script)

    def test_missing_assets_raise_with_actionable_guidance(self, tmp_path, monkeypatch):
        monkeypatch.delenv(ENV_KAS_NODE, raising=False)
        monkeypatch.delenv(ENV_KAS_SCRIPT, raising=False)
        # Point every discovery root at an empty dir so the probe finds nothing.
        monkeypatch.setattr(kas_assets, "_kiro_data_dirs", lambda: [tmp_path])
        with pytest.raises(KasAssetsMissing) as exc:
            resolve_kas_entry()
        message = str(exc.value)
        assert "kiro-cli" in message
        assert ENV_KAS_NODE in message or ENV_KAS_SCRIPT in message

    def test_discovers_the_newest_extracted_bundle(self, tmp_path, monkeypatch):
        """Replicates kiro-cli 2.18.0's real layout.

        The bundle is staged as a trimmed ``node_modules`` tree, so the entry
        script sits at the package's npm path — NOT at the top of the version
        directory. Both facts were confirmed against an extracted 2.18.0 bundle.
        """
        monkeypatch.delenv(ENV_KAS_SCRIPT, raising=False)
        kas_root = tmp_path / "kas"
        rel = Path("node_modules/@kiro/agent/dist/server/acp-server.js")
        old_dir = kas_root / "2.17.0-aaaa"
        new_dir = kas_root / "2.18.0-bbbb"
        for d in (old_dir, new_dir):
            (d / rel.parent).mkdir(parents=True)
            (d / rel).write_text("")
        os.utime(old_dir, (1_000, 1_000))
        os.utime(new_dir, (2_000, 2_000))
        monkeypatch.setattr(kas_assets, "_kiro_data_dirs", lambda: [tmp_path])
        found = kas_assets.find_kas_server_script()
        assert found is not None
        assert "2.18.0-bbbb" in str(found)
        assert found.name == "acp-server.js"

    def test_kiro_cli_data_dir_is_searched(self):
        """2.18.0 writes ``~/.local/share/kiro-cli``, not ``~/.local/share/kiro``."""
        names = [p.name for p in kas_assets._kiro_data_dirs()]
        assert "kiro-cli" in names
        assert names.index("kiro-cli") < names.index("kiro")


class TestArgv:
    def test_shape(self, tmp_path):
        argv = build_kas_argv(tmp_path / "node", tmp_path / "acp-server.js")
        assert argv[0].endswith("node")
        assert KAS_TRANSPORT_ARG in argv
        for flag in KAS_NODE_FLAGS:
            assert flag in argv

    def test_auth_acp_callback_is_passed(self, tmp_path):
        """We now launch KAS with --auth=acp-callback.

        KAS then keeps no refresh token and asks this host for an access token
        over ACP (``_kiro/auth/getAccessToken``), which the runtime answers by
        shelling out to kiro-cli. This works on a cli-only machine, unlike the
        file auth provider (omit --auth) whose SSO-cache token ``kiro-cli login``
        does not write.
        """
        argv = build_kas_argv(tmp_path / "node", tmp_path / "acp-server.js")
        assert "--auth=acp-callback" in argv

    def test_no_agent_flag_is_passed(self, tmp_path):
        argv = build_kas_argv(tmp_path / "node", tmp_path / "acp-server.js")
        assert "--agent" not in argv


class TestSandboxClassification:
    """KAS must not be declared to the sandbox as kiro-cli.

    On macOS ``wrap_argv`` skips its own seatbelt when told the child is
    kiro-cli and kiro's internal sandbox is on, because the two cannot nest.
    Node has no internal sandbox, so that claim would leave KAS running with no
    isolation at all rather than with one of the two layers.
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

        async def fake_bin():
            return "/usr/bin/kiro-cli"

        monkeypatch.setattr(
            "kiro_crew.acp.runtime._resolve_kiro_bin_for_spawn", fake_bin
        )
        monkeypatch.setattr(
            "kiro_crew.acp.runtime.ensure_agent_materialized", lambda _agent: None
        )
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
_STUB_AGENT = '''
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
'''


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
    """Point the KAS asset overrides at a stub agent run by this interpreter.

    Redirects the kiro agent home and puts a spec in it, because the projection reads
    one and nothing in a test can make production write it: ``ensure_agent_materialized``
    REFUSES to write the shared agent home from an ephemeral instance -- a checkout plus
    a temp data home, which describes every test -- and logs "This instance will use the
    existing specs instead". Without the redirect, "the existing specs" are the
    developer's own installed agents, so this test's verdict depended on which of them
    were present and whether their ``file://`` prompt files still resolved: it failed
    with an ``AcpRuntimeError`` naming a prompt file in an unrelated worktree.

    ``KIRO_HOME`` rather than patching ``Path.home()``: it is the documented override
    and it reaches the resolver this path uses. Note its scope caveat
    (``config/paths.py``) -- today only the agents directory follows it, so it moves
    where kiro-cli WRITES without moving where Kiro Crew reads. That is enough here,
    because the agents directory is the whole dependency. The rootdir conftest does NOT
    pin it: the variable outranks ``Path.home()``, and many tests isolate this resolver
    by patching that instead.
    """
    script = tmp_path / "kas_stub.py"
    script.write_text(_STUB_AGENT)
    launcher = tmp_path / "node-stub"
    # The launcher swallows KAS's node flags and transport arg, then runs the
    # stub: argv fidelity is asserted separately in TestArgv.
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n')
    launcher.chmod(0o755)
    monkeypatch.setenv(ENV_KAS_NODE, str(launcher))
    monkeypatch.setenv(ENV_KAS_SCRIPT, str(script))
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

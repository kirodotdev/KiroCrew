"""Agent binding on KAS: inject the definition, then activate it as a mode.

KAS has no ``--agent`` flag and advertises only its own built-in modes, so a
``session/set_mode`` naming Crew's agent fails against a stock KAS. The agent has
to travel on ``session/new`` as ``_meta.kiro.customAgents``; KAS registers it, it
surfaces as a mode, and the ordinary activation then works.

The stub here mirrors that mechanism rather than accepting everything: it
advertises only its built-ins plus whatever the client injected, and rejects a
``set_mode`` for an unknown mode. A test that passes against a permissive stub
would prove nothing about the binding.
"""

from __future__ import annotations

import json
import sys

import pytest

from kiro_crew.acp.kas_agents import _KAS_FALLBACK_PROMPT
from kiro_crew.acp.kas_assets import ENV_KAS_NODE, ENV_KAS_SCRIPT
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.types import ACP_BACKEND_KAS

#: Records what the client injected so a test can read it back, and gates
#: ``session/set_mode`` on the resulting mode list the way KAS does.
_MODE_STUB = '''
import json, os, sys

BUILTIN = ["vibe", "spec", "plan"]
state = {"modes": list(BUILTIN), "injected": [], "set_mode": None}
RECORD = os.environ["KAS_STUB_RECORD"]

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

def record():
    with open(RECORD, "w", encoding="utf-8") as fh:
        json.dump(state, fh)

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion"),
            "agentCapabilities": {"loadSession": True},
        }})
    elif method == "session/new":
        agents = ((params.get("_meta") or {}).get("kiro") or {}).get("customAgents") or []
        state["injected"] = agents
        state["modes"] = list(BUILTIN) + [a.get("id") for a in agents if a.get("id")]
        record()
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "sessionId": "kas-mode-session",
            "modes": {
                "currentModeId": "vibe",
                "availableModes": [{"id": m, "name": m} for m in state["modes"]],
            },
        }})
    elif method == "session/set_mode":
        requested = params.get("modeId")
        state["set_mode"] = requested
        record()
        if requested in state["modes"]:
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {
                "code": -32603, "message": "Mode '%s' not found" % requested}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
'''


@pytest.fixture
def mode_stub(tmp_path, monkeypatch):
    """A KAS-shaped agent that only accepts modes it actually advertises."""
    script = tmp_path / "kas_mode_stub.py"
    script.write_text(_MODE_STUB)
    record = tmp_path / "stub-record.json"
    launcher = tmp_path / "node-stub"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n')
    launcher.chmod(0o755)
    monkeypatch.setenv(ENV_KAS_NODE, str(launcher))
    monkeypatch.setenv(ENV_KAS_SCRIPT, str(script))
    monkeypatch.setenv("KAS_STUB_RECORD", str(record))
    return record


@pytest.fixture
def crew_agent(tmp_path, monkeypatch):
    """A materialized agent spec the projection can read, in an isolated dir."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "kirocrew.json").write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "description": "test crew agent",
                "prompt": "You are Kiro.",
                "tools": ["fs_read", "@kirocrew-core"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.acp.runtime.kiro_agents_dir", lambda: agents_dir)
    monkeypatch.setattr("kiro_crew.acp.runtime.ensure_agent_materialized", lambda _a: True)
    return agents_dir


@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
class TestModeBinding:
    @pytest.mark.asyncio
    async def test_injected_agent_becomes_the_active_mode(
        self, mode_stub, crew_agent, tmp_path
    ):
        """The whole chain: inject -> advertised as a mode -> activated."""
        runtime = AcpRuntime(
            work_dir=tmp_path / "ws",
            agent="kirocrew",
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        try:
            await runtime.spawn()
            handle = await runtime.create_session(cwd=tmp_path / "ws", agent="kirocrew")
            assert handle is not None
        finally:
            await runtime.kill()

        seen = json.loads(mode_stub.read_text(encoding="utf-8"))
        assert [a["id"] for a in seen["injected"]] == ["kirocrew"]
        assert seen["injected"][0]["prompt"] == "You are Kiro."
        assert seen["injected"][0]["tools"] == ["fs_read", "@kirocrew-core"]
        # Activation had to happen, and had to name the injected agent — not a
        # built-in that KAS would have run in its place.
        assert seen["set_mode"] == "kirocrew"

    @pytest.mark.asyncio
    async def test_runtime_default_agent_is_activated_without_explicit_request(
        self, mode_stub, crew_agent, tmp_path
    ):
        """A KAS session created with no explicit agent must still ACTIVATE the
        runtime default. KAS has no --agent flag, so injecting the default via
        customAgents without a following set_mode would leave the session on
        KAS's own default mode — the injection would be inert. Injection and
        activation must resolve the SAME agent.
        """
        runtime = AcpRuntime(
            work_dir=tmp_path / "wsd",
            agent="kirocrew",
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        try:
            await runtime.spawn()
            # No agent= passed: both injection and activation must fall back to
            # the runtime default.
            await runtime.create_session(cwd=tmp_path / "wsd")
        finally:
            await runtime.kill()

        seen = json.loads(mode_stub.read_text(encoding="utf-8"))
        assert [a["id"] for a in seen["injected"]] == ["kirocrew"]
        assert seen["set_mode"] == "kirocrew"

    @pytest.mark.asyncio
    async def test_prompt_is_inlined_not_sent_as_a_file_uri(
        self, mode_stub, crew_agent, tmp_path
    ):
        """KAS rejects ``file://`` here; the client owns the read."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("inlined from disk", encoding="utf-8")
        (crew_agent / "kirocrew.json").write_text(
            json.dumps(
                {"name": "kirocrew", "prompt": f"file://{prompt_file}", "tools": ["fs_read"]}
            ),
            encoding="utf-8",
        )
        runtime = AcpRuntime(
            work_dir=tmp_path / "ws2",
            agent="kirocrew",
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        try:
            await runtime.spawn()
            await runtime.create_session(cwd=tmp_path / "ws2", agent="kirocrew")
        finally:
            await runtime.kill()

        seen = json.loads(mode_stub.read_text(encoding="utf-8"))
        assert seen["injected"][0]["prompt"] == "inlined from disk"

    @pytest.mark.asyncio
    async def test_a_prompt_less_agent_falls_back_to_the_kas_prompt(
        self, mode_stub, crew_agent, tmp_path
    ):
        """KAS requires a non-empty prompt where kiro-cli tolerates an empty
        one. Crew's own prompt-less utility agents (e.g. ``kirocrew-lite``, which
        ships ``"prompt": ""``) must fall back to the small inline KAS prompt
        rather than crash the session. The tool allowlist still comes from the
        spec, so the fallback never widens the agent's capabilities.
        """
        (crew_agent / "kirocrew.json").write_text(
            json.dumps({"name": "kirocrew", "tools": ["fs_read"], "prompt": ""}),
            encoding="utf-8",
        )
        runtime = AcpRuntime(
            work_dir=tmp_path / "ws3",
            agent="kirocrew",
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        try:
            await runtime.spawn()
            await runtime.create_session(cwd=tmp_path / "ws3", agent="kirocrew")
        finally:
            await runtime.kill()

        seen = json.loads(mode_stub.read_text(encoding="utf-8"))
        assert seen["injected"][0]["prompt"] == _KAS_FALLBACK_PROMPT
        # Tool restriction is preserved — the fallback only supplies a prompt.
        assert seen["injected"][0]["tools"] == ["fs_read"]

    @pytest.mark.asyncio
    async def test_an_unprojectable_agent_fails_loud(self, mode_stub, crew_agent, tmp_path):
        """A prompt that cannot be resolved AT ALL still fails loud.

        The base-prompt fallback only covers an empty/absent prompt. A
        ``file://`` prompt pointing at a missing file is a genuine translation
        failure: silently continuing would run KAS's default mode instead, which
        for a restricted app or subagent agent means a BROADER agent than the
        caller asked for, so this must raise rather than degrade.
        """
        (crew_agent / "kirocrew.json").write_text(
            json.dumps(
                {
                    "name": "kirocrew",
                    "tools": ["fs_read"],
                    "prompt": f"file://{tmp_path / 'does-not-exist.md'}",
                }
            ),
            encoding="utf-8",
        )
        runtime = AcpRuntime(
            work_dir=tmp_path / "ws3",
            agent="kirocrew",
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        try:
            await runtime.spawn()
            with pytest.raises(Exception) as exc:
                await runtime.create_session(cwd=tmp_path / "ws3", agent="kirocrew")
            assert "onto KAS" in str(exc.value)
        finally:
            await runtime.kill()


@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
class TestKiroPathUntouched:
    """The kiro backend must not gain a customAgents payload.

    kiro-cli reads its agent from disk via ``--agent``; injecting definitions
    would be a second, competing source of truth.
    """

    @pytest.mark.asyncio
    async def test_no_custom_agents_for_the_kiro_backend(self, mode_stub, crew_agent, tmp_path):
        runtime = AcpRuntime(
            work_dir=tmp_path / "ws4",
            agent="kirocrew",
            sandbox_mode="off",
        )
        assert await runtime._kas_custom_agents("kirocrew") is None

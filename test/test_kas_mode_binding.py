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

import copy
import json
import sys

import pytest

from kiro_crew.acp import session_handle as sh
from kiro_crew.acp import skill_projection  # bound before a fixture patches its paths
from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.harness.kas_cloud import CLOUD_SANDBOX_TARGET, KasCloudSandboxHarness
from kiro_crew.acp.kas_start_agent import START_AGENT_ID
from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeError
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
)


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


#: Records what the client injected so a test can read it back, and gates
#: ``session/set_mode`` on the resulting mode list the way KAS does.
_MODE_STUB = """
import json, os, sys

# ``review`` stands in for a built-in id the engine keeps for itself that Crew's
# reserved set does not know about (the measured ones go out under a wire alias).
BUILTIN = ["vibe", "spec", "plan", "review"]
# Accepted in a batch and then left out of availableModes (measured on 2.23.0).
# ``dropped-agent`` stands in for an entry the engine drops whole, as the 0.66.33
# bundle drops one with no prompt. A test can name one more through the environment.
DROPPED = ["default", "dropped-agent"] + [d for d in os.environ.get("KAS_STUB_DROP", "").split(",") if d]
state = {"modes": list(BUILTIN), "injected": [], "set_mode": None, "start_mode": {}}

def mode_entry(mode_id):
    # The v3 engine stamps who supplied each definition; a client entry that
    # collides with a built-in id is discarded and the built-in stays stamped
    # as its own (measured on kiro-cli 2.23.0).
    origin = "bundled" if mode_id in BUILTIN else "client"
    return {"id": mode_id, "name": mode_id,
            "_meta": {"kiro": {"resource": {"resourceType": "agent", "source": {"origin": origin}}}}}
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
    elif method in ("session/new", "session/load"):
        kiro = (params.get("_meta") or {}).get("kiro") or {}
        agents = kiro.get("customAgents") or []
        state["injected"] = agents
        state["start_mode"][method] = kiro.get("modeId")
        state["modes"] = list(BUILTIN) + [
            a.get("id") for a in agents
            if a.get("id") and a.get("id") not in BUILTIN and a.get("id") not in DROPPED]
        record()
        if os.environ.get("KAS_STUB_STATUS_BEFORE_ANSWER"):
            # A report the engine sends while session/new is still in flight.
            send({"jsonrpc": "2.0", "method": "_kiro/mcp/status",
                  "params": {"sessionId": "kas-mode-session", "servers": []}})
        # The session starts in the mode its modeId names when that mode exists,
        # else in vibe (both measured on the 0.66.33 bundle).
        current = kiro.get("modeId") if kiro.get("modeId") in state["modes"] else "vibe"
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "sessionId": "kas-mode-session",
            "modes": {
                "currentModeId": current,
                "availableModes": [mode_entry(m) for m in state["modes"]],
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
"""


@pytest.fixture
def mode_stub(tmp_path, monkeypatch):
    """A KAS-shaped agent that only accepts modes it actually advertises."""
    script = tmp_path / "kas_mode_stub.py"
    script.write_text(_MODE_STUB)
    record = tmp_path / "stub-record.json"
    # Stands in for the kiro-cli binary the KAS relay argv is built around; the
    # launcher ignores `acp --agent-engine v3 --auth-method cli` and runs the
    # stub. Argv fidelity is asserted in test_kas_spawn.py, not here.
    launcher = tmp_path / "kiro-cli-stub"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n')
    launcher.chmod(0o755)

    async def fake_bin(*, environ=None, home=None) -> str:
        return str(launcher)

    monkeypatch.setattr("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", fake_bin)
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
    monkeypatch.setattr("kiro_crew.config.paths.kiro_agents_dir", lambda: agents_dir)
    monkeypatch.setattr("kiro_crew.agent.ensure_agent_materialized", lambda _a: True)
    # The stub launches no MCP server, so a custom agent's default Crew servers
    # (acp/default_crew_servers.py) would never report ready. These tests pin mode
    # binding; the defaults are pinned in test_default_crew_servers.py.
    monkeypatch.setattr("kiro_crew.acp.default_crew_servers.DEFAULT_CREW_SERVERS", ())
    return agents_dir


@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
class TestModeBinding:
    @pytest.mark.asyncio
    async def test_injected_agent_becomes_the_active_mode(self, mode_stub, crew_agent, tmp_path):
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
        # The spec's kiro-cli name travels with its KAS family beside it; the
        # MCP ref is untouched.
        assert seen["injected"][0]["tools"] == [
            "fs_read",
            "read_file",
            "list_directory",
            "@kirocrew-core",
        ]
        # Activation had to happen, and had to name the injected agent — not a
        # built-in that KAS would have run in its place.
        assert seen["set_mode"] == "kirocrew"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("backend", [ACP_BACKEND_KAS, ACP_BACKEND_KIRO], ids=["kas", "kiro"])
    async def test_only_a_kas_session_records_its_projected_batch(
        self, mode_stub, crew_agent, tmp_path, monkeypatch, backend
    ):
        """The projection bookkeeping is KAS-only: a kiro-cli handle keeps the defaults."""
        from kiro_crew.acp import kas_agents

        recorded: list = []
        real = kas_agents.projected_auto_approved

        def spy(custom_agents, active_agent):
            recorded.append(active_agent)
            return real(custom_agents, active_agent)

        # Only the KAS harness records the projection; the kiro one never reads it.
        monkeypatch.setattr(kas_agents, "projected_auto_approved", spy)
        if backend == ACP_BACKEND_KIRO:
            # The kiro spawn's skill-view projection is not what this test is about.
            monkeypatch.setattr(
                skill_projection,
                "prepare_native_skill_projection",
                lambda work_dir, **_kwargs: skill_projection.NativeSkillProjection(
                    {"vibe": "vibe"}
                ),
            )
        kas = backend == ACP_BACKEND_KAS
        # kiro-cli takes its agent at spawn: the stub's own current mode stands in.
        runtime = AcpRuntime(
            work_dir=tmp_path / "wsp",
            agent="kirocrew" if kas else "vibe",
            sandbox_mode="off",
            acp_backend=backend,
        )
        try:
            await runtime.spawn()
            handle = await runtime.create_session(
                cwd=tmp_path / "wsp", agent="kirocrew" if kas else None
            )
        finally:
            await runtime.kill()
        if kas:
            assert recorded == ["kirocrew"]
            assert handle.kas_projected_agent == "kirocrew"
            assert isinstance(handle.kas_auto_approved, frozenset)
        else:
            assert recorded == []
            assert handle.kas_auto_approved is None
            assert handle.kas_projected_agent == ""

    @pytest.mark.asyncio
    async def test_a_resumed_kas_session_records_its_re_registered_batch(
        self, mode_stub, crew_agent, tmp_path, monkeypatch
    ):
        """session/load re-registers the batch, and the resumed handle records it."""
        from kiro_crew.acp import kas_agents

        recorded: list = []
        real = kas_agents.projected_auto_approved

        def spy(custom_agents, active_agent):
            recorded.append(active_agent)
            return real(custom_agents, active_agent)

        runtime = AcpRuntime(
            work_dir=tmp_path / "wsl",
            agent="kirocrew",
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        session_file = tmp_path / "prior.jsonl"
        session_file.write_text("", encoding="utf-8")
        try:
            await runtime.spawn()
            monkeypatch.setattr(kas_agents, "projected_auto_approved", spy)
            handle = await runtime.load_session(
                str(session_file), "kas-mode-session", cwd=tmp_path / "wsl", agent="kirocrew"
            )
        finally:
            await runtime.kill()
        assert recorded == ["kirocrew"]
        assert handle.kas_projected_agent == "kirocrew"
        assert [a["id"] for a in handle.kas_registered_agents] == ["kirocrew"]

    @pytest.mark.asyncio
    async def test_new_and_resumed_sessions_carry_the_notice_their_spawn_recorded(
        self, mode_stub, crew_agent, tmp_path, monkeypatch
    ):
        """The runtime keeps the harness that spawned its process, so the notice the
        relay spawn recorded for kiro-cli 2.32.0 reaches session/new and session/load."""
        from kiro_crew.acp.harness import kas as kas_harness

        monkeypatch.setattr(kas_harness, "_bundle_route_selected", lambda: False)
        monkeypatch.setattr(kas_harness, "installed_kiro_cli_version", lambda: (2, 32, 0))
        runtime = AcpRuntime(
            work_dir=tmp_path / "wsn", agent="kirocrew", sandbox_mode="off", acp_backend="kas"
        )
        session_file = tmp_path / "prior.jsonl"
        session_file.write_text("", encoding="utf-8")
        try:
            await runtime.spawn()
            new = await runtime.create_session(cwd=tmp_path / "wsn", agent="kirocrew")
            loaded = await runtime.load_session(
                str(session_file), "kas-mode-session", cwd=tmp_path / "wsn", agent="kirocrew"
            )
        finally:
            await runtime.kill()
        for handle in (new, loaded):
            assert "kiro-cli 2.32.0 is older than 2.33.1" in handle.kas_agent_prompt_notice

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("version", "recorded"),
        [((2, 32, 0), None), (None, None), ((2, 33, 1), "You are Kiro.")],
        ids=["dropping-release", "unknown-release", "applying-release"],
    )
    async def test_the_prompt_is_recorded_as_held_only_where_kas_applies_it(
        self, mode_stub, crew_agent, tmp_path, monkeypatch, version, recorded
    ):
        """Only a KAS known to apply a custom agent's prompt holds a copy of it; for
        any other the session records none and the prompt builder keeps its block."""
        from kiro_crew.acp.harness import kas as kas_harness

        monkeypatch.setattr(kas_harness, "_bundle_route_selected", lambda: False)
        monkeypatch.setattr(kas_harness, "installed_kiro_cli_version", lambda: version)
        runtime = AcpRuntime(
            work_dir=tmp_path / "wsp", agent="kirocrew", sandbox_mode="off", acp_backend="kas"
        )
        try:
            await runtime.spawn()
            handle = await runtime.create_session(cwd=tmp_path / "wsp", agent="kirocrew")
        finally:
            await runtime.kill()
        assert handle.native_context_documents.get("template://kirocrew#prompt") == recorded

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
    @pytest.mark.parametrize("agent", ["kirocrew", None], ids=["explicit-agent", "runtime-default"])
    async def test_session_new_starts_in_the_injected_agent(
        self, mode_stub, crew_agent, tmp_path, agent
    ):
        """session/new names the one injected agent as the mode to start in.

        Without ``_meta.kiro.modeId`` KAS starts the session in its own ``vibe``
        mode and starts every ``~/.kiro/settings/mcp.json`` server for it until
        set_mode switches away. The id is read from the batch, so a session on the
        runtime default (no caller agent) names it too, and set_mode still follows.
        """
        runtime = AcpRuntime(
            work_dir=tmp_path / "wsm",
            agent="kirocrew",
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        try:
            await runtime.spawn()
            handle = await runtime.create_session(cwd=tmp_path / "wsm", agent=agent)
            assert handle.active_agent == "kirocrew"
        finally:
            await runtime.kill()

        seen = json.loads(mode_stub.read_text(encoding="utf-8"))
        assert seen["start_mode"] == {"session/new": "kirocrew"}
        assert seen["set_mode"] == "kirocrew"

    @pytest.mark.asyncio
    async def test_reports_sent_during_session_new_count_as_the_agents_own(
        self, mode_stub, crew_agent, tmp_path, monkeypatch
    ):
        """A session that starts in its agent has no pre-switch roster to discount.

        A report the engine sends while session/new is in flight describes the
        agent's own servers once the session starts in that agent, so the readiness
        drain is told that no queued frame is stale.
        """
        monkeypatch.setenv("KAS_STUB_STATUS_BEFORE_ANSWER", "1")
        stale: list[int] = []
        real = AcpRuntime._wait_managed_mcp

        async def spy(self, handle, params, agent, timeout, stale_report_frames):
            stale.append(stale_report_frames)
            await real(self, handle, params, agent, timeout, stale_report_frames)

        monkeypatch.setattr(AcpRuntime, "_wait_managed_mcp", spy)
        runtime = AcpRuntime(
            work_dir=tmp_path / "wsr",
            agent="kirocrew",
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        try:
            await runtime.spawn()
            await runtime.create_session(cwd=tmp_path / "wsr", agent="kirocrew")
        finally:
            await runtime.kill()

        seen = json.loads(mode_stub.read_text(encoding="utf-8"))
        assert seen["set_mode"] == "kirocrew"
        assert stale == [0]

    @pytest.mark.asyncio
    async def test_an_entry_the_engine_drops_is_refused_and_its_session_ended(
        self, mode_stub, crew_agent, tmp_path, monkeypatch
    ):
        """Guard (A): the engine drops the named entry and starts in vibe.

        session/new names the agent, but an id the engine cannot resolve falls
        back to vibe rather than failing the request, so the agent is not
        advertised. The session the engine already started is ended, and no
        set_mode is sent.
        """
        (crew_agent / "dropped-agent.json").write_text(
            json.dumps({"name": "dropped-agent", "tools": ["fs_read"], "prompt": "Dropped."}),
            encoding="utf-8",
        )
        runtime = AcpRuntime(
            work_dir=tmp_path / "ws7",
            agent="dropped-agent",
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        terminated: list[str] = []
        try:
            await runtime.spawn()
            real = runtime.terminate_session

            async def spy(session_id, **kwargs):
                terminated.append(session_id)
                await real(session_id, **kwargs)

            monkeypatch.setattr(runtime, "terminate_session", spy)
            with pytest.raises(
                AcpRuntimeError, match="Agent mode 'dropped-agent' is not available"
            ):
                await runtime.create_session(cwd=tmp_path / "ws7", agent="dropped-agent")
        finally:
            await runtime.kill()

        seen = json.loads(mode_stub.read_text(encoding="utf-8"))
        assert seen["start_mode"] == {"session/new": "dropped-agent"}
        assert seen["set_mode"] is None
        assert terminated == ["kas-mode-session"]

    @pytest.mark.asyncio
    async def test_prompt_is_inlined_not_sent_as_a_file_uri(self, mode_stub, crew_agent, tmp_path):
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
    async def test_a_prompt_less_agent_goes_out_with_an_empty_prompt(
        self, mode_stub, crew_agent, tmp_path
    ):
        """A prompt-less spec runs on kiro-cli with kiro-cli's own default prompt
        and nothing added, so on KAS it goes out with ``"prompt": ""`` and runs on
        KAS's default alone -- not with Crew's background-worker brief,
        which tells the model it has no tools. The tool allowlist still comes
        from the spec.
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
        assert seen["injected"][0]["prompt"] == ""
        assert "prompt" in seen["injected"][0]
        assert seen["injected"][0]["tools"] == ["fs_read", "read_file", "list_directory"]

    @pytest.mark.asyncio
    async def test_a_missing_prompt_file_still_binds_the_agent(
        self, mode_stub, crew_agent, tmp_path
    ):
        """A ``file://`` prompt naming a missing file runs the agent with no prompt.

        kiro-cli runs such an agent with no prompt (``resolve_global_prompt``).
        The agent is still the one registered and activated, with its own tool
        allowlist, so nothing broader than the spec runs; only its prompt is
        empty, and the session-start notice says so.
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
            await runtime.create_session(cwd=tmp_path / "ws3", agent="kirocrew")
        finally:
            await runtime.kill()

        seen = json.loads(mode_stub.read_text(encoding="utf-8"))
        assert seen["injected"][0]["prompt"] == ""
        assert seen["injected"][0]["tools"] == ["fs_read", "read_file", "list_directory"]

    @pytest.mark.asyncio
    async def test_a_reserved_id_runs_under_its_wire_alias(self, mode_stub, crew_agent, tmp_path):
        """``default`` -- the seeded crewmate's id -- is one the engine accepts in a
        batch and then leaves out of ``availableModes``, as the stub does. The
        crewmate's own definition registers and activates under its wire alias
        instead, while everything Crew records keeps the crewmate's id."""
        (crew_agent / "default.json").write_text(
            json.dumps({"name": "default", "tools": ["fs_read"], "prompt": "You are Default."}),
            encoding="utf-8",
        )
        runtime = AcpRuntime(
            work_dir=tmp_path / "ws4",
            agent="default",
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        try:
            await runtime.spawn()
            handle = await runtime.create_session(cwd=tmp_path / "ws4", agent="default")
            assert handle.active_agent == "default"
            assert handle.kas_projected_agent == "default"
            assert [a["id"] for a in handle.kas_registered_agents] == ["default"]
        finally:
            await runtime.kill()
        seen = json.loads(mode_stub.read_text(encoding="utf-8"))
        assert [a["id"] for a in seen["injected"]] == ["default-crewmate"]
        assert seen["injected"][0]["prompt"] == "You are Default."
        assert seen["start_mode"] == {"session/new": "default-crewmate"}
        assert seen["set_mode"] == "default-crewmate"

    @pytest.mark.asyncio
    async def test_a_resumed_reserved_id_runs_under_its_wire_alias(
        self, mode_stub, crew_agent, tmp_path
    ):
        """session/load re-sends the batch, so the alias rides the resume too.

        ``plan`` is a built-in the stub stamps ``bundled``, as the engine does, so
        sending it under its own id would trip Guard (C)."""
        (crew_agent / "plan.json").write_text(
            json.dumps({"name": "plan", "tools": ["fs_read"], "prompt": "You are Plan."}),
            encoding="utf-8",
        )
        runtime = AcpRuntime(
            work_dir=tmp_path / "ws6",
            agent="plan",
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        session_file = tmp_path / "prior-plan.jsonl"
        session_file.write_text("", encoding="utf-8")
        try:
            await runtime.spawn()
            handle = await runtime.load_session(
                str(session_file), "kas-mode-session", cwd=tmp_path / "ws6", agent="plan"
            )
            assert handle.active_agent == "plan"
            assert handle.kas_projected_agent == "plan"
        finally:
            await runtime.kill()
        seen = json.loads(mode_stub.read_text(encoding="utf-8"))
        assert [a["id"] for a in seen["injected"]] == ["plan-crewmate"]
        # A resumed session already has its mode, so the load names none.
        assert seen["start_mode"] == {"session/load": None}
        assert seen["set_mode"] == "plan-crewmate"

    @pytest.mark.asyncio
    async def test_an_id_the_host_keeps_for_itself_is_refused_not_activated(
        self, mode_stub, crew_agent, tmp_path, monkeypatch
    ):
        """Guard (C): the id IS advertised, but stamped as the host's own agent.

        A set_mode would succeed and run the engine's built-in under the
        crewmate's name -- the silent-substitution shape the reserved set's
        wire alias cannot cover for a built-in id it has not measured. The wire stamp
        (``origin: bundled`` instead of ``client``) is what catches it. session/new
        named the id, so the session may already run that built-in: the refusal
        ends it before any prompt.
        """
        (crew_agent / "review.json").write_text(
            json.dumps({"name": "review", "tools": ["fs_read"], "prompt": "You review."}),
            encoding="utf-8",
        )
        runtime = AcpRuntime(
            work_dir=tmp_path / "ws5",
            agent="review",
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        terminated: list[str] = []
        try:
            await runtime.spawn()
            real = runtime.terminate_session

            async def spy(session_id, **kwargs):
                terminated.append(session_id)
                await real(session_id, **kwargs)

            monkeypatch.setattr(runtime, "terminate_session", spy)
            with pytest.raises(Exception) as exc:
                await runtime.create_session(cwd=tmp_path / "ws5", agent="review")
            assert str(exc.value).startswith("Rename this Bot's template: “review” is reserved")
            assert "Agent Template tab" in str(exc.value)
            assert "kas-mode-session" not in str(exc.value)
        finally:
            await runtime.kill()
        seen = json.loads(mode_stub.read_text(encoding="utf-8"))
        assert [a["id"] for a in seen["injected"]] == ["review"]
        assert seen["start_mode"] == {"session/new": "review"}
        # Never activated: that is the whole point.
        assert seen["set_mode"] is None
        assert terminated == ["kas-mode-session"]


#: A member's private copy, named the way a Capabilities save names it.
_SAVED_TEMPLATE = "crew-0123456789abcdef01234567"


def _write_saved_spec(agents_dir, server_entry):
    (agents_dir / f"{_SAVED_TEMPLATE}.json").write_text(
        json.dumps(
            {
                "name": _SAVED_TEMPLATE,
                "prompt": "You are the member.",
                "tools": ["fs_read", "@extra"],
                "mcpServers": {"extra": server_entry},
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
class TestSavedMemberSpecOnKas:
    """A member's saved capability spec loads on KAS, the default member backend."""

    @pytest.mark.asyncio
    async def test_a_saved_spec_is_loaded_and_confirmed(self, mode_stub, crew_agent, tmp_path):
        """The whole member chain: provider support, injection, activation, evidence."""
        from kiro_crew.acp.session_provider import AcpSessionProvider

        _write_saved_spec(crew_agent, {"command": "extra-server"})
        runtime = AcpRuntime(
            work_dir=tmp_path / "ws",
            agent=_SAVED_TEMPLATE,
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        try:
            await runtime.spawn()
            handle = await runtime.create_session(cwd=tmp_path / "ws")
            provider = AcpSessionProvider(handle, runtime, owns_runtime=True)
            # Asked BEFORE start in session_allocation; False was the whole bug.
            assert provider.member_capabilities_supported is True
            # The evidence loaded_stamp() requires after start.
            assert provider.loaded_capability_template == _SAVED_TEMPLATE
        finally:
            await runtime.kill()
        seen = json.loads(mode_stub.read_text(encoding="utf-8"))
        # The spec declares a server, so the session starts in the start agent
        # registered after it and set_mode switches to the member's own.
        assert [a["id"] for a in seen["injected"]] == [_SAVED_TEMPLATE, START_AGENT_ID]
        assert seen["start_mode"][METHOD_SESSION_NEW] == START_AGENT_ID
        assert seen["injected"][0]["prompt"] == "You are the member."
        assert "extra" in seen["injected"][0]["mcpServers"]
        assert seen["set_mode"] == _SAVED_TEMPLATE

    @pytest.mark.asyncio
    async def test_an_engine_that_drops_the_start_agent_still_runs_the_members_own(
        self, mode_stub, crew_agent, tmp_path, monkeypatch
    ):
        """The session then starts in the engine's ``vibe`` mode, as it did before a
        start mode was named, and ``set_mode`` still activates the member's agent."""
        monkeypatch.setenv("KAS_STUB_DROP", START_AGENT_ID)
        _write_saved_spec(crew_agent, {"command": "extra-server"})
        runtime = AcpRuntime(
            work_dir=tmp_path / "ws",
            agent=_SAVED_TEMPLATE,
            sandbox_mode="off",
            acp_backend=ACP_BACKEND_KAS,
        )
        try:
            await runtime.spawn()
            handle = await runtime.create_session(cwd=tmp_path / "ws")
            assert handle.active_agent == _SAVED_TEMPLATE
        finally:
            await runtime.kill()
        seen = json.loads(mode_stub.read_text(encoding="utf-8"))
        assert seen["start_mode"][METHOD_SESSION_NEW] == START_AGENT_ID
        assert START_AGENT_ID not in seen["modes"]
        assert seen["set_mode"] == _SAVED_TEMPLATE


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
        assert (await runtime._kas_custom_agents("kirocrew")).custom_agents is None


def _batch_request(*entries: object) -> dict:
    return {"cwd": "/w", "mcpServers": [], "_meta": {"kiro": {"customAgents": list(entries)}}}


class TestSessionNewModeId:
    """The KAS request seam names the batch's first agent on session/new, and only there."""

    @pytest.mark.parametrize("agent", ["default", None], ids=["explicit-agent", "runtime-default"])
    def test_the_single_entry_names_the_mode_by_its_wire_id(self, agent):
        """The id comes from the batch, already renamed for the wire, never from the
        caller's override; the caller's agent and dicts come back untouched."""
        params = _batch_request({"id": "default-crewmate", "prompt": ""})
        before = copy.deepcopy(params)
        out, activated = harness_for(ACP_BACKEND_KAS).session_request(
            METHOD_SESSION_NEW, params, agent, agent_capabilities={}
        )
        assert out["_meta"]["kiro"] == {**before["_meta"]["kiro"], "modeId": "default-crewmate"}
        assert {k: v for k, v in out.items() if k != "_meta"} == {
            k: v for k, v in before.items() if k != "_meta"
        }
        assert activated == agent
        assert params == before

    @pytest.mark.parametrize(
        "params",
        [
            {"cwd": "/w", "mcpServers": []},
            {"cwd": "/w", "mcpServers": [], "_meta": {"claudeCode": {"options": {}}}},
            _batch_request(),
            _batch_request({"name": "kirocrew"}),
            _batch_request({"name": "kirocrew"}, {"id": "slack-agent"}),
            _batch_request({"id": ""}),
            _batch_request("kirocrew"),
        ],
        ids=["no-meta", "other-meta", "empty", "no-id", "no-id-first", "blank-id", "not-a-dict"],
    )
    def test_a_batch_whose_first_entry_has_no_id_is_sent_as_built(self, params):
        out, activated = harness_for(ACP_BACKEND_KAS).session_request(
            METHOD_SESSION_NEW, params, "kirocrew", agent_capabilities={}
        )
        assert out is params
        assert activated == "kirocrew"

    def test_a_registered_sub_agent_does_not_take_the_sessions_mode(self):
        """Sub-agent targets follow the session's own entry, which names the mode.

        A delegating agent registers each agent its sub-agent tool may name beside
        its own entry (kas_subagent_targets), so its batch has more than one entry;
        the session still starts in its own agent, not in vibe or in a target.
        """
        params = _batch_request({"id": "delegator"}, {"id": "slack-agent"})
        before = copy.deepcopy(params)
        out, activated = harness_for(ACP_BACKEND_KAS).session_request(
            METHOD_SESSION_NEW, params, None, agent_capabilities={}
        )
        assert out["_meta"]["kiro"] == {**before["_meta"]["kiro"], "modeId": "delegator"}
        assert activated is None
        assert params == before

    def test_a_load_names_no_mode(self):
        """A resumed session already has its mode."""
        params = {**_batch_request({"id": "kirocrew"}), "sessionId": "s1"}
        out, activated = harness_for(ACP_BACKEND_KAS).session_request(
            METHOD_SESSION_LOAD, params, "kirocrew", agent_capabilities={}
        )
        assert out is params
        assert activated == "kirocrew"

    def test_the_kiro_host_sends_the_request_as_built(self):
        params = _batch_request({"id": "kirocrew"})
        out, activated = harness_for(ACP_BACKEND_KIRO).session_request(
            METHOD_SESSION_NEW, params, "kirocrew", agent_capabilities={}
        )
        assert out is params
        assert activated == "kirocrew"

    def test_a_cloud_sandbox_names_no_mode(self):
        """The cloud harness activates nothing, so it names no mode either."""
        caps = {"_meta": {"kiro": {"executionTargets": [CLOUD_SANDBOX_TARGET]}}}
        out, activated = KasCloudSandboxHarness().session_request(
            METHOD_SESSION_NEW,
            _batch_request({"id": "kirocrew"}),
            "kirocrew",
            agent_capabilities=caps,
        )
        assert "modeId" not in out["_meta"]["kiro"]
        assert activated is None

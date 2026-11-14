"""Worker stubs reach mirrored hosts through the worker's session projection."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import codex_session_mcp_helpers
import pytest

from kiro_crew import mate_worker
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX
from kiro_crew.config import paths
from kiro_crew.mate_worker import MATE_WORKER_AGENT
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.mcp_gateway.hashing import expand_stub_flags
from kiro_crew.mcp_gateway.rewriter import rewrite_agents
from kiro_crew.mcp_gateway.session_servers import injection_server_names, pooled_session_servers
from kiro_crew.providers.mirrors import mirror_for

agents_dir = codex_session_mcp_helpers.agents_dir
WORK = "kirocrew-work"
KEY = "worker:own"
TOKEN = "worker-token"


@pytest.fixture
def worker(agents_dir, tmp_path, monkeypatch):
    mates = tmp_path / "mates"
    mates.mkdir()
    base = {
        "name": MATE_WORKER_AGENT,
        "prompt": "Worker",
        "tools": [f"@{WORK}"],
        "mcpServers": {
            WORK: {"command": "/opt/kirocrew", "args": ["mcp-work"], "autoApprove": ["*"]}
        },
    }
    base_path = mates / f"{MATE_WORKER_AGENT}.json"
    base_path.write_text(json.dumps(base), encoding="utf-8")
    monkeypatch.setattr(paths, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member, config=None: None)
    monkeypatch.setattr(mate_worker, "globally_disabled_refs", lambda names: [])
    records = {
        KEY: SimpleNamespace(
            selection_kind="template", template_id=MATE_WORKER_AGENT, member_id="bot"
        ),
        "chat:unrelated": SimpleNamespace(
            selection_kind="member", template_id="other", member_id="other"
        ),
    }
    monkeypatch.setattr("kiro_crew.execution_context.read_session_execution", records.get)
    overlay = tmp_path / "overlay"
    rewrite_agents(
        source_dir=agents_dir,
        overlay_dir=overlay,
        socket_path=tmp_path / "gateway.sock",
        work_dir=tmp_path,
        stub_servers=frozenset({WORK}),
        mate_source_dir=mates,
    )
    return SimpleNamespace(overlay=overlay, base=base, path=base_path, records=records)


def _project(worker, backend, *, key=KEY, token=TOKEN, governed=True):
    return mirror_for(backend).session_projection(
        MATE_WORKER_AGENT,
        session_key=key,
        session_token=token,
        worker_overlay_dir=worker.overlay,
        permission_surface_owned=governed,
    )


def _work_stub(elements):
    found = [e for e in elements if e["name"] == WORK]
    assert len(found) == 1
    entry = found[0]
    flags = expand_stub_flags(entry["args"])
    assert flags[flags.index("--agent") + 1] == MATE_WORKER_AGENT
    assert "autoApprove" not in entry
    return entry


@pytest.mark.parametrize("backend", [ACP_BACKEND_CODEX, ACP_BACKEND_CLAUDE])
def test_worker_session_projection_mounts_its_bound_stub(worker, backend):
    projection = _project(worker, backend)
    entry = _work_stub(projection.params["mcpServers"])
    assert {p["name"]: p["value"] for p in entry["env"]}[STUB_SESSION_TOKEN_ENV] == TOKEN


def test_by_name_worker_lookup_never_returns_stubs(worker):
    assert pooled_session_servers(worker.overlay, MATE_WORKER_AGENT) == []
    assert injection_server_names(worker.overlay, MATE_WORKER_AGENT) == frozenset()


@pytest.mark.parametrize("backend", [ACP_BACKEND_CODEX, ACP_BACKEND_CLAUDE])
@pytest.mark.parametrize("key,token", [("", TOKEN), ("chat:unrelated", TOKEN), (KEY, "")])
def test_no_worker_authority_gets_no_worker_stub(worker, backend, key, token):
    elements = _project(worker, backend, key=key, token=token).params["mcpServers"]
    assert all("--agent" not in expand_stub_flags(e.get("args", [])) for e in elements)


@pytest.mark.parametrize("backend", [ACP_BACKEND_CODEX, ACP_BACKEND_CLAUDE])
@pytest.mark.parametrize("restriction", ["disabled", "ungranted", "registry", "removed"])
def test_worker_stubs_follow_the_current_session_spec(worker, backend, restriction):
    if restriction == "ungranted":
        worker.base["tools"] = []
    elif restriction == "removed":
        worker.base["mcpServers"].pop(WORK)
    else:
        worker.base["mcpServers"][WORK].update(
            {"disabled": True} if restriction == "disabled" else {"type": "registry"}
        )
    worker.path.write_text(json.dumps(worker.base), encoding="utf-8")
    assert WORK not in {e["name"] for e in _project(worker, backend).params["mcpServers"]}


def test_claude_requires_a_governed_permission_surface(worker):
    assert _project(worker, ACP_BACKEND_CLAUDE, governed=False).params["mcpServers"] == []


@pytest.mark.parametrize("backend", [ACP_BACKEND_CODEX, ACP_BACKEND_CLAUDE])
def test_unknown_session_refuses_the_worker(worker, backend):
    with pytest.raises(mate_worker.WorkerKitUnreadable):
        _project(worker, backend, key="worker:unknown")


@pytest.mark.parametrize("backend", [ACP_BACKEND_CODEX, ACP_BACKEND_CLAUDE])
def test_registry_ceiling_withholds_worker_stubs(worker, backend, monkeypatch):
    monkeypatch.setattr("kiro_crew.agent._mcp_registry_mode", lambda: True)
    monkeypatch.setattr("kiro_crew.acp.session_mcp._mcp_registry_mode", lambda: True)
    assert WORK not in {e["name"] for e in _project(worker, backend).params["mcpServers"]}


@pytest.mark.parametrize("backend", [ACP_BACKEND_CODEX, ACP_BACKEND_CLAUDE])
@pytest.mark.parametrize("damage", ["missing", "malformed", "foreign"])
def test_unverifiable_overlay_supplies_no_worker_stub(worker, backend, damage):
    path = worker.overlay / f"{MATE_WORKER_AGENT}.json"
    if damage == "missing":
        path.unlink()
    elif damage == "malformed":
        path.write_text("{", encoding="utf-8")
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["name"] = "other"
        path.write_text(json.dumps(data), encoding="utf-8")
    elements = _project(worker, backend).params["mcpServers"]
    assert all("--agent" not in expand_stub_flags(e.get("args", [])) for e in elements)


def test_worker_tool_restrictions_survive_stub_projection(worker):
    from kiro_crew.acp.session_mcp_crewmate import worker_deny_rules

    worker.base["mcpServers"][WORK]["disabledTools"] = ["work_report"]
    worker.path.write_text(json.dumps(worker.base), encoding="utf-8")
    codex = _project(worker, ACP_BACKEND_CODEX)
    assert WORK not in {e["name"] for e in codex.params["mcpServers"]}
    claude = _project(worker, ACP_BACKEND_CLAUDE)
    assert _work_stub(claude.params["mcpServers"])["disabledTools"] == ["work_report"]
    assert "mcp__kirocrew-work__work_report" in worker_deny_rules(frozenset(), claude.agent_spec)


@pytest.mark.parametrize("backend", [ACP_BACKEND_CODEX, ACP_BACKEND_CLAUDE])
def test_worker_wire_projection_keeps_session_binding(worker, backend):
    params = mirror_for(backend).session_params(
        MATE_WORKER_AGENT,
        session_key=KEY,
        session_token=TOKEN,
        worker_overlay_dir=worker.overlay,
        permission_surface_owned=True,
    )
    _work_stub(params["mcpServers"])


def test_each_worker_uses_its_own_kit_and_token(worker, monkeypatch):
    from kiro_crew.acp.session_mcp_crewmate import worker_deny_rules
    from kiro_crew.session_capabilities import CapabilityStartupError

    limited_key = "worker:limited"
    worker.records[limited_key] = SimpleNamespace(
        selection_kind="template", template_id=MATE_WORKER_AGENT, member_id="limited"
    )
    (worker.path.parent / "limited.json").write_text(
        json.dumps({"name": "limited", "excludedTools": [f"@{WORK}/work_report"]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        mate_worker,
        "mate_file_for_member",
        lambda member, config=None: "limited" if member == "limited" else None,
    )
    ordinary = _project(worker, ACP_BACKEND_CODEX)
    assert _work_stub(ordinary.params["mcpServers"])
    with pytest.raises(CapabilityStartupError, match="capability_tool_limits_unsupported"):
        _project(worker, ACP_BACKEND_CODEX, key=limited_key)
    limited = _project(worker, ACP_BACKEND_CLAUDE, key=limited_key, token="limited-token")
    entry = _work_stub(limited.params["mcpServers"])
    assert {p["name"]: p["value"] for p in entry["env"]}[STUB_SESSION_TOKEN_ENV] == "limited-token"
    assert "mcp__kirocrew-work__work_report" in worker_deny_rules(frozenset(), limited.agent_spec)
    assert "mcp__kirocrew-work__work_report" not in worker_deny_rules(
        frozenset(), ordinary.agent_spec
    )


def test_stub_mounts_use_the_projection_spec_without_a_second_read(worker, monkeypatch):
    from kiro_crew.acp import session_mcp

    original = session_mcp._crewmate_worker_spec

    def resolve_then_change(*args, **kwargs):
        spec = original(*args, **kwargs)
        worker.base["mcpServers"].clear()
        worker.path.write_text(json.dumps(worker.base), encoding="utf-8")
        return spec

    monkeypatch.setattr(session_mcp, "_crewmate_worker_spec", resolve_then_change)
    projection = _project(worker, ACP_BACKEND_CODEX)
    assert WORK in projection.agent_spec["mcpServers"]
    _work_stub(projection.params["mcpServers"])


def test_codex_runtime_uses_the_child_session_projection(worker, tmp_path):
    runtime = AcpRuntime(work_dir=str(tmp_path), acp_backend=ACP_BACKEND_CODEX)
    runtime._mcp_gateway_overlay = str(worker.overlay)
    projected = asyncio.run(
        runtime._mirrored_session_mcp(
            MATE_WORKER_AGENT, work_dir=tmp_path, session_key=KEY, channel_id="worker-channel"
        )
    )
    entry = _work_stub(projected.servers)
    assert {p["name"]: p["value"] for p in entry["env"]}[
        STUB_SESSION_TOKEN_ENV
    ] == projected.stub_token


@pytest.fixture
def claude_client(worker, tmp_path):
    client = AcpClient(work_dir=tmp_path, agent=MATE_WORKER_AGENT, acp_backend=ACP_BACKEND_CLAUDE)
    client._session_key = KEY
    client._mcp_gateway_overlay = str(worker.overlay)
    try:
        client._write_claude_local_settings()
        assert client._permission_surface_governed
        yield client
    finally:
        try:
            asyncio.run(client._discard_claude_settings_seed())
        finally:
            client._reset_state()


def test_claude_client_uses_the_worker_session_projection(claude_client):
    entry = _work_stub(claude_client._resolve_session_mcp_servers())
    assert {p["name"]: p["value"] for p in entry["env"]}[
        STUB_SESSION_TOKEN_ENV
    ] == claude_client._stub_session_token


def test_claude_client_withholds_worker_stubs_when_seed_lags(worker, claude_client):
    worker.base["mcpServers"][WORK]["disabledTools"] = ["work_report"]
    worker.path.write_text(json.dumps(worker.base), encoding="utf-8")
    assert claude_client._resolve_session_mcp_servers() == []

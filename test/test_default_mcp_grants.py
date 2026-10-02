"""Settings > Developer: add or remove the dashboard and debug MCP sets on the default agent.

The default agent's spec is the only state. Pinned here: ``add`` mounts both
entries and refs with nothing pre-approved, ``remove`` takes them out (a hand-added
entry included), both are idempotent and survive a rebuild, the worker never gets
either set, and the route reports the step that failed.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import agent as agent_mod
from kiro_crew.agent_files import WORKER_AGENT_FILENAME
from kiro_crew.dashboard.handlers import default_mcp_grants as grants

DASH = "kirocrew-dashboard"
DEBUG = "kirocrew-debug"
BOTH = (DASH, DEBUG)


@pytest.fixture()
def env(tmp_path, monkeypatch) -> Path:
    """A throwaway project, data home and agents dir for ``rebuild_agent_config``."""
    from kiro_crew.apps import bridges

    project = tmp_path / "project" / "agents"
    project.mkdir(parents=True)
    (project / "defaults.json").write_text(
        json.dumps({"name": "kirocrew", "tools": ["@kirocrew-core"]}), encoding="utf-8"
    )
    (project / "prompt.md").write_text("prompt", encoding="utf-8")
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path / "project"))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))

    kiro_dir = tmp_path / ".kiro" / "agents"
    kiro_dir.mkdir(parents=True)
    spec_path = kiro_dir / "kirocrew.json"
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: kiro_dir)
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "absent-kiro.json")
    monkeypatch.setattr(agent_mod, "_CC_MCP_JSON", tmp_path / "absent-cc.json")
    monkeypatch.setattr(bridges, "_mcp_json_path", lambda: spec_path)
    agent_mod.rebuild_agent_config()
    return spec_path


def _spec(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, spec: dict) -> None:
    path.write_text(json.dumps(spec), encoding="utf-8")


def _mounted(spec: dict, server: str) -> bool:
    return server in spec.get("mcpServers", {}) and f"@{server}" in spec.get("tools", [])


def _refs(spec: dict, key: str, server: str) -> list[str]:
    return [r for r in spec.get(key, []) if str(r).startswith(f"@{server}")]


def test_the_two_servers_are_opt_in_managed_servers():
    for name, subcommand in grants.SERVERS.items():
        assert agent_mod._MANAGED_MCP_SERVERS[name].get("opt_in") is True
        assert agent_mod._managed_opt_in_entry(subcommand)["args"][-1] == subcommand


def test_nothing_is_mounted_by_default(env):
    assert grants.mounted_state(_spec(env)) == {DASH: False, DEBUG: False}


def test_add_mounts_both_without_pre_approval_and_survives_a_rebuild(env):
    assert grants.run_action("add") == ["write_spec", "rebuild", "verify"]
    agent_mod.rebuild_agent_config()
    spec = _spec(env)
    for name in BOTH:
        assert _mounted(spec, name)
        assert "autoApprove" not in spec["mcpServers"][name]
        assert _refs(spec, "allowedTools", name) == []


def test_add_is_idempotent(env):
    grants.run_action("add")
    first = _spec(env)
    grants.run_action("add")
    assert _spec(env) == first


def test_add_drops_pre_approvals_and_keeps_operator_fields(env, monkeypatch):
    records: list[dict] = []
    monkeypatch.setattr(
        grants,
        "sel",
        lambda: type("S", (), {"log_api_access": lambda self, **kw: records.append(kw)})(),
    )
    spec = _spec(env)
    entry = agent_mod._managed_opt_in_entry("mcp-debug")
    entry.update({"autoApprove": ["*"], "timeout": 90000})
    spec["mcpServers"][DEBUG] = entry
    spec.setdefault("allowedTools", []).extend([f"@{DEBUG}", f"@{DASH}/session_send"])
    _write(env, spec)

    grants.run_action("add")
    spec = _spec(env)
    assert "autoApprove" not in spec["mcpServers"][DEBUG]
    assert spec["mcpServers"][DEBUG]["timeout"] == 90000
    for name in BOTH:
        assert _refs(spec, "allowedTools", name) == []
    # The dropped approvals are recorded, not lost silently.
    [rec] = records
    assert rec["resources"] == (
        f"add dropped approvals: @{DASH}/session_send, {DEBUG}.autoApprove, @{DEBUG}"
    )


def test_remove_takes_out_a_hand_added_entry_and_every_ref(env):
    spec = _spec(env)
    spec["mcpServers"][DEBUG] = {"command": "/opt/custom/kirocrew", "args": ["mcp-debug"]}
    spec["tools"].extend([f"@{DEBUG}", f"@{DEBUG}/debug_threads"])
    spec.setdefault("allowedTools", []).append(f"@{DEBUG}")
    _write(env, spec)

    assert grants.run_action("remove") == ["write_spec", "rebuild", "verify"]
    spec = _spec(env)
    for name in BOTH:
        assert name not in spec.get("mcpServers", {})
        assert _refs(spec, "tools", name) == []
        assert _refs(spec, "allowedTools", name) == []


def test_remove_is_idempotent_and_reverses_add(env):
    before = _spec(env)
    grants.run_action("add")
    grants.run_action("remove")
    once = _spec(env)
    grants.run_action("remove")
    assert _spec(env) == once
    assert grants.mounted_state(once) == grants.mounted_state(before)


def test_the_worker_never_gets_either_set(env):
    grants.run_action("add")
    worker = json.loads((env.parent / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
    for name in BOTH:
        assert name not in worker.get("mcpServers", {})
        assert not any(str(r).startswith(f"@{name}") for r in worker.get("tools", []))


def test_an_unreadable_spec_fails_at_write_spec(env):
    env.write_text("{not json", encoding="utf-8")
    with pytest.raises(grants.StepFailed) as err:
        grants.run_action("add")
    assert err.value.step == "write_spec"


def test_a_refused_rebuild_fails_at_rebuild(env, monkeypatch):
    monkeypatch.setattr(agent_mod, "rebuild_agent_config_reporting", lambda: (env, False))
    with pytest.raises(grants.StepFailed) as err:
        grants.run_action("add")
    assert err.value.step == "rebuild"


def test_a_spec_that_does_not_show_the_change_fails_at_verify(env, monkeypatch):
    monkeypatch.setattr(grants, "_end_state_holds", lambda _spec, _action: False)
    with pytest.raises(grants.StepFailed) as err:
        grants.run_action("add")
    assert err.value.step == "verify"


# -- the route ---------------------------------------------------------------


@asynccontextmanager
async def _client(monkeypatch, *, owner: bool = True):
    """A started client that always closes (an ``async with`` helper, by this repo's
    convention: the pinned pytest-asyncio does not collect async-generator fixtures
    declared with plain ``@pytest.fixture``)."""
    from kiro_crew.dashboard.handlers import source_providers

    monkeypatch.setattr(source_providers, "is_owner_dashboard_request", lambda _r: owner)
    app = web.Application()
    app.router.add_get("/api/agent/default-mcp-grants", grants.api_default_mcp_grants_get)
    app.router.add_post("/api/agent/default-mcp-grants", grants.api_default_mcp_grants_set)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        yield c
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_route_adds_removes_and_reports_state(env, monkeypatch):
    async with _client(monkeypatch) as client:
        body = await (await client.get("/api/agent/default-mcp-grants")).json()
        assert {s["name"]: s["mounted"] for s in body["servers"]} == {DASH: False, DEBUG: False}
        assert body["session_control"] is True

        resp = await client.post("/api/agent/default-mcp-grants", json={"action": "add"})
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["steps"] == ["write_spec", "rebuild", "verify"]
        assert all(s["mounted"] for s in body["servers"])

        resp = await client.post("/api/agent/default-mcp-grants", json={"action": "remove"})
        assert resp.status == 200
        assert not any(s["mounted"] for s in (await resp.json())["servers"])


@pytest.mark.asyncio
async def test_route_names_the_failed_step(env, monkeypatch):
    monkeypatch.setattr(agent_mod, "rebuild_agent_config_reporting", lambda: (env, False))
    async with _client(monkeypatch) as client:
        resp = await client.post("/api/agent/default-mcp-grants", json={"action": "add"})
        assert resp.status == 500
        body = await resp.json()
        assert body["code"] == "step_failed"
        assert body["failed_step"] == "rebuild"


@pytest.mark.asyncio
async def test_route_refuses_an_unknown_action(env, monkeypatch):
    async with _client(monkeypatch) as client:
        resp = await client.post("/api/agent/default-mcp-grants", json={"action": "grant"})
        assert resp.status == 400
        assert (await resp.json())["code"] == "action_invalid"


@pytest.mark.asyncio
async def test_route_refuses_a_non_owner(env, monkeypatch):
    before = _spec(env)
    async with _client(monkeypatch, owner=False) as client:
        resp = await client.post("/api/agent/default-mcp-grants", json={"action": "add"})
        assert resp.status in (401, 403)
    assert _spec(env) == before

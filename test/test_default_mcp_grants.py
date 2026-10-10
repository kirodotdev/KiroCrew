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


def test_add_keeps_owner_written_approvals_and_fields(env):
    # rfc-owner-written-mcp-auto-approve: an approval the owner wrote is kept.
    spec = _spec(env)
    entry = agent_mod._managed_opt_in_entry("mcp-debug")
    entry.update({"autoApprove": ["debug_threads"], "timeout": 90000})
    spec["mcpServers"][DEBUG] = entry
    spec.setdefault("allowedTools", []).append(f"@{DASH}/session_send")
    _write(env, spec)

    grants.run_action("add")
    spec = _spec(env)
    assert spec["mcpServers"][DEBUG]["timeout"] == 90000
    assert spec["mcpServers"][DEBUG]["autoApprove"] == ["debug_threads"]
    assert _refs(spec, "allowedTools", DASH) == [f"@{DASH}/session_send"]
    assert "autoApprove" not in spec["mcpServers"][DASH]


def test_add_merges_an_owner_env_map_with_the_managed_bindings(monkeypatch):
    # A relocated home gives the managed entry a non-empty env; the owner's own
    # variables on that entry must survive, with the managed bindings winning.
    real = agent_mod._managed_opt_in_entry

    def relocated(subcommand):
        entry = real(subcommand)
        entry["env"] = {"KIROCREW_HOME": "/relocated"}
        return entry

    monkeypatch.setattr(agent_mod, "_managed_opt_in_entry", relocated)
    spec = {"mcpServers": {DEBUG: {"env": {"OWNER_VAR": "kept", "KIROCREW_HOME": "/stale"}}}}
    grants.apply_to_spec(spec, "add")
    assert spec["mcpServers"][DEBUG]["env"] == {"OWNER_VAR": "kept", "KIROCREW_HOME": "/relocated"}
    assert spec["mcpServers"][DASH]["env"] == {"KIROCREW_HOME": "/relocated"}


def test_a_shared_agent_home_this_instance_must_not_own_writes_nothing(env, monkeypatch):
    before = env.read_bytes()
    monkeypatch.setattr(agent_mod, "_decline_shared_agent_home", lambda **_kw: env)
    with pytest.raises(grants.StepFailed) as err:
        grants.run_action("add")
    assert err.value.step == "write_spec"
    assert env.read_bytes() == before


def test_a_rebuild_that_snapshotted_before_remove_does_not_restore_the_sets(env, monkeypatch):
    # A concurrent rebuild (an agent.model change) snapshots the spec before
    # Remove, and commits after Remove has written. The opt-in sets are held by
    # the spec alone, so the commit must take their state from disk.
    from kiro_crew.agent_materialization import default_spec_commit

    grants.run_action("add")
    real = default_spec_commit.write_default_spec

    def remove_lands_first(*args, **kwargs):
        spec = _spec(env)
        grants.apply_to_spec(spec, "remove")
        _write(env, spec)
        return real(*args, **kwargs)

    monkeypatch.setattr(default_spec_commit, "write_default_spec", remove_lands_first)
    agent_mod.rebuild_agent_config_reporting()
    spec = _spec(env)
    for name in BOTH:
        assert name not in spec.get("mcpServers", {})
        assert _refs(spec, "tools", name) == []
        assert _refs(spec, "allowedTools", name) == []


def test_a_rebuild_that_snapshotted_before_add_keeps_the_sets(env, monkeypatch):
    from kiro_crew.agent_materialization import default_spec_commit

    real = default_spec_commit.write_default_spec

    def add_lands_first(*args, **kwargs):
        spec = _spec(env)
        grants.apply_to_spec(spec, "add")
        _write(env, spec)
        return real(*args, **kwargs)

    monkeypatch.setattr(default_spec_commit, "write_default_spec", add_lands_first)
    agent_mod.rebuild_agent_config_reporting()
    assert all(grants.mounted_state(_spec(env)).values())


def test_the_disk_reconcile_does_not_restore_a_ceiling_withheld_approval(env, monkeypatch):
    # An owner-written approval for a mounted opt-in set, then a governance
    # ceiling that denies it: the rebuild's final ceiling pass withholds the
    # ref, and the locked disk reconcile must not append it back.
    from kiro_crew.agent_materialization import auto_approve

    grants.run_action("add")
    spec = _spec(env)
    spec.setdefault("allowedTools", []).append(f"@{DASH}/session_stop")
    _write(env, spec)
    monkeypatch.setattr(
        auto_approve, "_may_auto_approve", lambda ref: not str(ref).startswith(f"@{DASH}")
    )

    agent_mod.rebuild_agent_config_reporting()
    spec = _spec(env)
    assert _mounted(spec, DASH)
    assert _refs(spec, "allowedTools", DASH) == []


def test_a_rebuild_that_snapshotted_an_approval_does_not_restore_it_after_remove_and_add(
    env, monkeypatch
):
    # The rebuild reads the spec while the owner's autoApprove is still there;
    # Remove then Add land before it commits, so the entry on disk has none. The
    # commit takes the owner fields from disk and must not write the stale one back.
    from kiro_crew.agent_materialization import default_spec_commit

    grants.run_action("add")
    spec = _spec(env)
    spec["mcpServers"][DEBUG]["autoApprove"] = ["debug_threads"]
    _write(env, spec)
    real = default_spec_commit.write_default_spec

    def remove_then_add_land_first(*args, **kwargs):
        for action in ("remove", "add"):
            spec = _spec(env)
            grants.apply_to_spec(spec, action)
            _write(env, spec)
        return real(*args, **kwargs)

    monkeypatch.setattr(default_spec_commit, "write_default_spec", remove_then_add_land_first)
    agent_mod.rebuild_agent_config_reporting()
    spec = _spec(env)
    assert _mounted(spec, DEBUG)
    assert "autoApprove" not in spec["mcpServers"][DEBUG]


def test_the_disk_reconcile_keeps_the_managed_entry_sanitized(env):
    # The reconcile reads each opt-in entry from disk under the lock. That copy is
    # whatever was hand-written there, so it has to go through the same ownership
    # pass the rebuild applies: a stray key makes kiro-cli reject the whole agent,
    # and a loader env key must never reach a managed server.
    grants.run_action("add")
    spec = _spec(env)
    entry = spec["mcpServers"][DASH]
    entry["cwd"] = "/tmp"
    entry.setdefault("env", {})["LD_PRELOAD"] = "/tmp/evil.so"
    entry["timeout"] = 120000
    spec["mcpServers"][DEBUG]["disabled"] = "false"
    _write(env, spec)
    agent_mod.rebuild_agent_config_reporting()
    assert "disabled" not in _spec(env)["mcpServers"][DEBUG]
    entry = _spec(env)["mcpServers"][DASH]
    assert "cwd" not in entry
    assert "LD_PRELOAD" not in entry.get("env", {})
    assert entry["timeout"] == 120000


def test_an_unreadable_locked_reread_keeps_the_sets(env, monkeypatch):
    # The lenient reader turns an OSError into {}; reconciling against that would
    # delete both sets. The rebuild must keep the entries it already has.
    from kiro_crew.apps import bridges

    grants.run_action("add")

    def unreadable(*, strict: bool = False):
        if strict:
            raise OSError("simulated read failure")
        return {}

    monkeypatch.setattr(bridges, "_read_mcp_json_unlocked", unreadable)
    agent_mod.rebuild_agent_config_reporting()
    assert all(grants.mounted_state(_spec(env)).values())


def test_a_non_clean_rebuild_still_repairs_a_corrupt_spec(env):
    # A hand-edit typo leaves kirocrew.json unparseable. The rebuild falls back to
    # a fresh config and must still write it over the corrupt file.
    env.write_text("{not json", encoding="utf-8")
    agent_mod.rebuild_agent_config_reporting()
    assert _spec(env)["name"] == "kirocrew"


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
        assert "pi" in body["unreached_backends"]  # NO_CHANNEL in PROJECTIONS

        resp = await client.post("/api/agent/default-mcp-grants", json={"action": "add"})
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert set(body) == {"servers", "session_control", "unreached_backends"}
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

"""KAS projection, broker overlays and hook reads select the same spec source."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew.acp.harness.kas import KasHarness, resolve_projected_spec
from kiro_crew.acp.kas_agents import KasAgentTranslationError
from kiro_crew.acp.runtime_session_setup import _pooled_session_servers_and_ref_spec
from kiro_crew.agent_materialization.worker_agent import _DERIVED_AGENTS, DerivedSpecSnapshot
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.config import paths
from kiro_crew.mcp_gateway import session_servers
from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER


@pytest.fixture
def layout(tmp_path, monkeypatch):
    user, mates, overlay = (tmp_path / name for name in ("agents", "mates", "overlay"))
    for directory in (user, mates, overlay):
        directory.mkdir()
    monkeypatch.setattr(paths, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(paths, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", user)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda agent: None)
    return SimpleNamespace(user=user, mates=mates, overlay=overlay, checkout=tmp_path)


def _write(directory: Path, stem: str, spec: dict, rank: int = 0) -> None:
    path = directory / f"{stem}.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    stamp = 1_700_000_000_000_000_000 + rank * 1_000_000_000
    os.utime(path, ns=(stamp, stamp))


def _claimants(layout, agent: str) -> dict:
    canonical = {}
    for rank, (stem, label) in enumerate(((agent, "canonical"), (f"Package-{agent}", "package"))):
        spec = {
            "name": agent,
            "prompt": label,
            "tools": ["@service"],
            "mcpServers": {"service": {"command": f"{label}-server"}},
            "hooks": {"preToolUse": [{"matcher": "*", "command": f"{label}-hook"}]},
        }
        _write(layout.user, stem, spec, rank)
        _write(
            layout.overlay,
            stem,
            {
                "name": agent,
                "mcpServers": {
                    "service": {"command": f"{label}-stub", "args": [], _WRAPPER_MARKER: True}
                },
            },
            rank,
        )
        if rank == 0:
            canonical = spec
    return canonical


@pytest.mark.parametrize("agent", sorted(_DERIVED_AGENTS))
@pytest.mark.asyncio
async def test_derived_projection_and_overlay_keep_the_canonical_source(layout, monkeypatch, agent):
    canonical = _claimants(layout, agent)
    snapshot = DerivedSpecSnapshot("identity", "fingerprint", canonical, agent)
    monkeypatch.setattr(agent_mod, "require_fresh_derived_spec", lambda *args: snapshot)

    servers, _ = _pooled_session_servers_and_ref_spec(
        layout.overlay, agent, ACP_BACKEND_KAS, layout.checkout, ref_spec=canonical
    )
    assert [(entry["name"], entry["command"]) for entry in servers] == [
        ("service", "canonical-stub")
    ]
    assert (
        session_servers._load_overlay_for_agent(layout.overlay, agent, prefer_newest=True)[
            "mcpServers"
        ]["service"]["command"]
        == "canonical-stub"
    )
    extras = await KasHarness().session_extras(
        agent, work_dir=str(layout.checkout), mcp_gateway_overlay=layout.overlay
    )
    [projected] = extras.custom_agents
    assert projected["prompt"] == "canonical"
    assert "service" not in projected.get("mcpServers", {})


@pytest.mark.parametrize("agent", sorted(_DERIVED_AGENTS))
def test_derived_hooks_keep_the_canonical_source(layout, agent):
    _claimants(layout, agent)
    hooks, _, _ = spec_hooks.crew_fired_spec_hooks(agent, prefer_newest=True)
    assert [hook.command for hook in hooks] == ["canonical-hook"]


def test_user_projection_overlay_and_hooks_keep_the_newest_source(layout):
    agent = "custom-agent"
    _claimants(layout, agent)
    spec, anchor = resolve_projected_spec(layout.user, agent, layout.checkout)
    assert spec["prompt"] == "package"
    assert anchor == layout.user
    servers, _ = _pooled_session_servers_and_ref_spec(
        layout.overlay, agent, ACP_BACKEND_KAS, layout.checkout, ref_spec=spec
    )
    assert [entry["command"] for entry in servers] == ["package-stub"]
    hooks, _, _ = spec_hooks.crew_fired_spec_hooks(agent, prefer_newest=True)
    assert [hook.command for hook in hooks] == ["package-hook"]


def test_mate_service_projection_and_hooks_remain_strict(layout):
    agent = "kirocrew-mate-conductor"
    _claimants(layout, agent)
    for rank, stem in enumerate((agent, f"Package-{agent}")):
        _write(layout.mates, stem, {"name": agent, "prompt": stem}, rank)
    with pytest.raises(KasAgentTranslationError, match="differing"):
        resolve_projected_spec(layout.user, agent, layout.checkout)
    with pytest.raises(KasAgentTranslationError, match="differing"):
        spec_hooks.crew_fired_spec_hooks(agent, prefer_newest=True)
    overlay = session_servers._load_overlay_for_agent(layout.overlay, agent, prefer_newest=True)
    assert overlay["mcpServers"]["service"]["command"] == "canonical-stub"


@pytest.mark.parametrize("agent", sorted(_DERIVED_AGENTS))
def test_derived_load_probe_reads_the_canonical_file(layout, agent):
    _claimants(layout, agent)
    (layout.user / f"{agent}.json").write_text("{invalid", encoding="utf-8")
    assert spec_hooks.kas_spec_load_error(layout.user, agent) is not None


@pytest.mark.parametrize("agent", sorted(_DERIVED_AGENTS))
def test_non_kas_derived_hook_reads_still_refuse_ambiguity(layout, agent):
    _claimants(layout, agent)
    with pytest.raises(KasAgentTranslationError, match="differing"):
        spec_hooks.crew_fired_spec_hooks(agent)


def test_newest_predicate_is_limited_to_user_level_projection(layout):
    from kiro_crew.agent_sdk.kas_spec_selection import kas_projects_newest

    assert kas_projects_newest(None) is False
    assert kas_projects_newest("missing") is False
    for agent in (*sorted(_DERIVED_AGENTS), "kirocrew-mate-conductor", "kirocrew-mate-worker"):
        _claimants(layout, agent)
        assert kas_projects_newest(agent) is False
    _write(layout.mates, "my-mate", {"name": "my-mate", "prompt": "mate"})
    assert kas_projects_newest("my-mate") is False
    project = layout.checkout / ".kiro" / "agents"
    project.mkdir(parents=True)
    _write(project, "project-agent", {"name": "project-agent", "prompt": "project"})
    assert kas_projects_newest("project-agent") is False
    _claimants(layout, "custom-agent")
    assert kas_projects_newest("custom-agent") is True
    assert kas_projects_newest("custom-agent", agents_dir=layout.mates) is False
    assert kas_projects_newest("custom-agent", agents_dir=project) is False
    # User-level presence outranks a same-name crewmate or checkout file.
    _write(layout.mates, "custom-agent", {"name": "custom-agent", "prompt": "mate"})
    _write(project, "custom-agent", {"name": "custom-agent", "prompt": "project"})
    assert kas_projects_newest("custom-agent") is True


@pytest.mark.parametrize("agent", sorted(_DERIVED_AGENTS))
@pytest.mark.asyncio
async def test_derived_hook_staleness_uses_canonical_coverage(layout, monkeypatch, agent):
    from kiro_crew.acp import kas_agents
    from kiro_crew.agent_sdk.capabilities import capabilities_for
    from kiro_crew.hooks import ScriptHookStore

    canonical = _claimants(layout, agent)
    _write(layout.user, f"Package-{agent}", {**canonical, "hooks": {}}, rank=1)
    store = ScriptHookStore(config_dir=layout.checkout / "hooks")
    monkeypatch.setattr(kas_agents, "get_global_hook_store", lambda: store)
    monkeypatch.setattr(kas_agents, "user_hook_files", lambda: [])
    live = SimpleNamespace(
        capabilities=capabilities_for(ACP_BACKEND_KAS),
        kas_auto_approved_capabilities=frozenset({"web_fetch"}),
    )
    assert await spec_hooks.hook_projection_stale(live, agent) is True

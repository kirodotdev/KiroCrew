"""KAS pooled stubs and projection subtraction follow the source spec's pick."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kiro_crew.acp import runtime_session_setup
from kiro_crew.acp.harness import kas
from kiro_crew.config import paths
from kiro_crew.mcp_gateway import session_servers
from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER


def _write(path: Path, value: dict, stamp: int) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
    os.utime(path, ns=(stamp, stamp))


@pytest.fixture
def overlay_pair(tmp_path, monkeypatch):
    agents = tmp_path / "sources"
    overlay = tmp_path / "overlays"
    agents.mkdir()
    overlay.mkdir()
    monkeypatch.setattr(paths, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(runtime_session_setup, "_ref_spec_snapshot", lambda *_a: None)
    monkeypatch.setattr(session_servers, "_registry_ceiling_exemptions", lambda: None)
    monkeypatch.setattr(kas.agent_mod, "ensure_agent_materialized", lambda *_a: None)
    monkeypatch.setattr(kas.agent_mod, "require_fork_governance", lambda *_a: None)
    monkeypatch.setattr(kas.agent_mod, "require_fresh_derived_spec", lambda *_a: None)
    monkeypatch.setattr(kas, "with_default_crew_servers", lambda _a, spec: spec)
    monkeypatch.setattr(kas, "_skill_scope_rules", lambda *_a: ())
    monkeypatch.setattr("kiro_crew.template_mcp_additions.merge_at_spawn", lambda _a, spec: spec)
    monkeypatch.setattr("kiro_crew.agent_sdk.kas_model_catalog.vault_identity", lambda: None)
    return agents, overlay


def _pair(agents, overlay, *, tie=False, first="Alpha-shared.json", second="Beta-shared.json"):
    for index, filename in enumerate((first, second)):
        label = ("alpha", "beta")[index]
        spec = {
            "name": "shared",
            "prompt": label,
            "mcpServers": {
                "common": {"command": label},
                label: {"command": label},
            },
        }
        _write(agents / filename, spec, 1_000_000_000 if tie else (index + 1) * 1_000_000_000)
        # The rewriter publishes JSON under the source stem. Overlay timestamps
        # deliberately rank opposite to source timestamps.
        rewritten = {
            **spec,
            "mcpServers": {
                name: {**entry, _WRAPPER_MARKER: True, "args": [], "env": {}}
                for name, entry in spec["mcpServers"].items()
            },
        }
        _write(overlay / filename, rewritten, (2 - index) * 1_000_000_000)


@pytest.mark.parametrize("tie", [False, True])
@pytest.mark.parametrize("first", ["Alpha-shared.json", "shared.json"])
@pytest.mark.asyncio
async def test_kas_injection_and_subtraction_follow_source_pick(
    overlay_pair, monkeypatch, tie, first
):
    agents, overlay = overlay_pair
    _pair(agents, overlay, tie=tie, first=first)
    # Beta sorts before the bare filename, so the tie must follow Beta then.
    winner = "beta" if not tie or first == "shared.json" else "alpha"
    pooled, _ = runtime_session_setup._pooled_session_servers_and_ref_spec(
        overlay, "shared", "kas", agents.parent, session_key="chat-overlay"
    )
    assert {entry["command"] for entry in pooled} == {winner}
    assert {entry["name"] for entry in pooled} == {"common", winner}
    captured = []

    def build(_dir, _agent, spec, *, stub_server_names, **_kwargs):
        captured.append((spec["prompt"], stub_server_names))
        return []

    monkeypatch.setattr(kas.kas_agents_mod, "build_kas_custom_agents", build)
    await kas.KasHarness().session_extras("shared", work_dir=None, mcp_gateway_overlay=str(overlay))
    assert captured == [(winner, frozenset({"common", winner}))]


def test_non_kas_keeps_filename_order(overlay_pair):
    agents, overlay = overlay_pair
    _pair(agents, overlay)
    pooled, _ = runtime_session_setup._pooled_session_servers_and_ref_spec(
        overlay, "shared", "", agents.parent
    )
    assert {entry["command"] for entry in pooled} == {"alpha"}
    assert session_servers.injection_server_names(overlay, "shared") == {"common", "alpha"}


def test_missing_selected_overlay_never_falls_back_to_other_claimant(overlay_pair):
    agents, overlay = overlay_pair
    _pair(agents, overlay)
    (overlay / "Beta-shared.json").unlink()
    pooled, _ = runtime_session_setup._pooled_session_servers_and_ref_spec(
        overlay, "shared", "kas", agents.parent
    )
    assert pooled == []


def test_declared_name_needs_no_matching_filename_suffix(overlay_pair):
    agents, overlay = overlay_pair
    _pair(agents, overlay, second="other-filename.json")
    pooled, _ = runtime_session_setup._pooled_session_servers_and_ref_spec(
        overlay, "shared", "kas", agents.parent
    )
    assert {entry["command"] for entry in pooled} == {"beta"}

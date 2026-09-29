"""Tests for the select_crew MCP tool (kiro_crew.mcp_core._do_select_crew)."""

from __future__ import annotations

import json
import unittest.mock
from pathlib import Path

import kiro_crew.mcp_core as mcp_core


def _write_cfg(tmp_path: Path) -> Path:
    data = {
        "agents": {
            "default": {
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "memory_store": "default",
            },
            "oncall": {
                "member_id": "member-oncall",
                "kiro_agent": "oncall-agent",
                "workspace": "oncall-ws",
                "memory_store": "oncall-mem",
                "triggers": "incident, prod outage",
            },
            "research": {"kiro_agent": "kirocrew", "description": "deep research crew"},
        },
        "default_agent": "default",
        "workspaces": {"default": {"dir": "workspace"}, "oncall-ws": {"dir": "oncall"}},
        "memory_stores": {
            "default": {},
            "oncall-mem": {
                "owner_member": "oncall",
                "owner_member_id": "member-oncall",
                "memory_version": 2,
            },
        },
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_roster_mode_lists_only_crews_with_triggers(tmp_path):
    p = _write_cfg(tmp_path)
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=p):
        out = json.loads(mcp_core._do_select_crew(""))
    assert out["default_agent"] == "default"
    crews = {c["name"]: c for c in out["crews"]}
    # The default crew is the caller — never in the roster.
    assert "default" not in crews
    # A crew WITH triggers is selectable, listed by its raw triggers.
    assert crews["oncall"]["triggers"] == "incident, prod outage"
    # A crew with NO triggers is not a routing candidate — excluded entirely
    # (no fallback to its description).
    assert "research" not in crews
    # The response carries the default fallback + high-confidence guidance.
    assert "default" in out["default_agent"]
    assert "guidance" in out and "high confidence" in out["guidance"]


def test_named_crew_returns_bindings(tmp_path):
    p = _write_cfg(tmp_path)
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=p):
        out = json.loads(mcp_core._do_select_crew("oncall"))
    assert out["crew"] == "oncall"
    assert out["bound"]["kiro_agent"] == "oncall-agent"
    assert out["bound"]["memory_store"] == "oncall-mem"
    assert out["bound"]["workspace"].endswith("oncall")


def test_unknown_crew_returns_error(tmp_path):
    p = _write_cfg(tmp_path)
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=p):
        out = json.loads(mcp_core._do_select_crew("ghost"))
    assert "error" in out
    assert "ghost" in out["error"]
    assert "oncall" in out["available"]


def test_schema_accepts_crew_names_with_spaces_and_dots():
    # Crew creation only strips the name (no regex), so the select_crew schema
    # must not reject a legitimately-named crew — otherwise it would list the
    # crew in its roster but fail to bind it. The membership check in
    # _do_select_crew is the real deny-by-default gate.
    from kiro_crew.validation import SELECT_CREW_SCHEMA, validate_tool_args

    for name in ("on call", "crew.v2", "team-a"):
        cleaned = validate_tool_args({"crew": name}, SELECT_CREW_SCHEMA)
        assert cleaned["crew"] == name


def _labelled_cfg(tmp_path: Path) -> Path:
    p = _write_cfg(tmp_path)
    data = json.loads(p.read_text(encoding="utf-8"))
    # A crew whose label differs from its id.
    data["agents"]["launch-notes"] = {
        "kiro_agent": "kirocrew",
        "display_name": "Release Notes",
        "triggers": "release notes, changelog",
    }
    # A label equal to its key carries nothing new and is not repeated.
    data["agents"]["oncall"]["display_name"] = "oncall"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_roster_carries_display_name_beside_the_dispatch_key(tmp_path):
    p = _labelled_cfg(tmp_path)
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=p):
        out = json.loads(mcp_core._do_select_crew(""))
    crews = {c["name"]: c for c in out["crews"]}
    assert crews["launch-notes"]["display_name"] == "Release Notes"
    assert "display_name" not in crews["oncall"]
    assert "display_name" in out["guidance"]


def test_route_crew_matches_carry_display_name(tmp_path):
    p = _labelled_cfg(tmp_path)
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=p):
        out = json.loads(mcp_core._do_route_crew("write the release notes"))
    top = out["matches"][0]
    assert (top["crew"], top["display_name"]) == ("launch-notes", "Release Notes")


def test_route_crew_unavailable_entries_carry_display_name(tmp_path):
    p = _labelled_cfg(tmp_path)
    data = json.loads(p.read_text(encoding="utf-8"))
    data["agents"]["launch-notes"]["memory_store"] = "missing-store"
    p.write_text(json.dumps(data), encoding="utf-8")
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=p):
        out = json.loads(mcp_core._do_route_crew("write the release notes"))
    refused = {u["crew"]: u for u in out["unavailable"]}
    assert refused["launch-notes"]["display_name"] == "Release Notes"


def test_unknown_crew_error_lists_labels_beside_keys(tmp_path):
    p = _labelled_cfg(tmp_path)
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=p):
        out = json.loads(mcp_core._do_select_crew("Release Notes"))
    assert "error" in out
    assert "launch-notes (Release Notes)" in out["available"]
    assert "oncall (oncall)" not in out["available"]

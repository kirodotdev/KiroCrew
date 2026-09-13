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
                "kiro_agent": "oncall-agent",
                "workspace": "oncall-ws",
                "memory_store": "oncall-mem",
                "triggers": "incident, prod outage",
            },
            "research": {"kiro_agent": "kirocrew", "description": "deep research crew"},
            # A member whose free-text name was re-keyed to a minted id, plus
            # two members sharing one display name.
            "case-competition": {
                "kiro_agent": "kirocrew",
                "display_name": "case competition",
                "triggers": "case study",
            },
            "judge-a": {"kiro_agent": "kirocrew", "display_name": "Judge"},
            "judge-b": {"kiro_agent": "kirocrew", "display_name": "Judge"},
        },
        "default_agent": "default",
        "workspaces": {"default": {"dir": "workspace"}, "oncall-ws": {"dir": "oncall"}},
        "memory_stores": {
            "default": {},
            "oncall-mem": {"owner_member": "oncall", "memory_version": 2},
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


def test_old_free_text_handle_resolves_through_the_display_name(tmp_path):
    """The name that WAS the key before the id split still selects the member.

    A skill or steering file written as ``select_crew("case competition")``
    keeps routing after the migration minted ``case-competition``; the bound
    payload reports the id, which is what the caller must pass to ``spawn_run``.
    """
    p = _write_cfg(tmp_path)
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=p):
        out = json.loads(mcp_core._do_select_crew("case competition"))
    assert "error" not in out
    assert out["crew"] == "case-competition"
    assert out["bound"]["kiro_agent"] == "kirocrew"


def test_shared_display_name_is_refused_as_ambiguous_not_guessed(tmp_path):
    p = _write_cfg(tmp_path)
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=p):
        out = json.loads(mcp_core._do_select_crew("Judge"))
    assert "bound" not in out
    assert out["error"].startswith("ambiguous crew 'Judge'")
    assert "judge-a, judge-b" in out["error"]
    assert "case-competition" in out["available"]


def test_display_name_fallback_is_exact(tmp_path):
    p = _write_cfg(tmp_path)
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=p):
        out = json.loads(mcp_core._do_select_crew("Case Competition"))
    assert out["error"] == "unknown crew 'Case Competition'"

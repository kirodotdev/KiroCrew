"""Tests for the select_crew MCP tool (kiro_crew.mcp_core._do_select_crew)."""

from __future__ import annotations

import json
import logging
import unittest.mock
from pathlib import Path

import pytest

import kiro_crew.mcp_core as mcp_core
from kiro_crew.mcp_tools import control


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
    # The roster's default crewmate is never in the roster (RFC rule 5: the one
    # select_crew keeps for itself), triggers or not -- see the case below.
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


def test_the_default_crewmate_stays_out_of_routing_even_with_triggers(tmp_path):
    """RFC rule 5: the roster's default crewmate is the one ``select_crew`` keeps
    for itself. A plain session is a template session, not that crewmate, so this
    is a routing rule rather than "the caller itself" -- and it holds whether or
    not the operator gave that row triggers."""
    p = _write_cfg(tmp_path)
    data = json.loads(p.read_text(encoding="utf-8"))
    data["agents"]["default"]["triggers"] = "billing, invoices"
    p.write_text(json.dumps(data), encoding="utf-8")
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=p):
        roster = json.loads(mcp_core._do_select_crew(""))
        ranked = json.loads(mcp_core._do_route_crew("an invoices question"))
    assert {c["name"] for c in roster["crews"]} == {"oncall"}
    assert [m["crew"] for m in ranked["matches"]] == []


def test_a_named_crew_hands_its_routing_decision_to_the_gateway(tmp_path, gateway_posts):
    p = _write_cfg(tmp_path)
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=p):
        control.select_crew("select_crew", {"crew": "oncall"})
    assert gateway_posts == [("/api/member-routing/decision", {"crew": "oncall"})]


def test_the_roster_and_an_unknown_crew_record_no_routing_decision(tmp_path, gateway_posts):
    p = _write_cfg(tmp_path)
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=p):
        control.select_crew("select_crew", {})
        control.select_crew("select_crew", {"crew": "ghost"})
    assert gateway_posts == []


def test_the_routing_post_is_bounded_like_its_best_effort_siblings(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(mcp_core, "_post", lambda path, body, **kw: calls.append(kw) or {})
    p = _write_cfg(tmp_path)
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=p):
        control.select_crew("select_crew", {"crew": "oncall"})
    assert calls == [{"timeout": control._WAIT_PING_TIMEOUT_SECS}]


_REFUSED = {"error": "this session could not be verified", "code": "unattested_caller"}


def _select_oncall(tmp_path, monkeypatch, reply):
    monkeypatch.setattr(mcp_core, "_post", lambda path, body, **kw: reply)
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:chat-1")
    p = _write_cfg(tmp_path)
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=p):
        return control.select_crew("select_crew", {"crew": "oncall"})


def _warnings(caplog):
    return [
        r
        for r in caplog.records
        if r.name == "kiro_crew.mcp_tools.control" and r.levelno == logging.WARNING
    ]


def test_a_refused_routing_post_is_logged_and_the_bind_still_returns(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_tools.control"):
        out = _select_oncall(tmp_path, monkeypatch, _REFUSED)
    assert json.loads(out)["bound"]["kiro_agent"] == "oncall-agent"
    assert len(_warnings(caplog)) == 1
    [rec] = _warnings(caplog)
    msg = rec.getMessage()
    assert "oncall" in msg and "unattested_caller" in msg
    assert "dashboard:chat-1" not in msg


def test_a_routing_post_with_no_error_code_is_logged_as_unreachable(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_tools.control"):
        _select_oncall(tmp_path, monkeypatch, {"error": "timed out"})
    assert len(_warnings(caplog)) == 1
    assert "unreachable" in _warnings(caplog)[0].getMessage()


@pytest.mark.parametrize("recorded", [True, False])
def test_a_delivered_routing_decision_logs_no_warning(tmp_path, monkeypatch, caplog, recorded):
    with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_tools.control"):
        _select_oncall(tmp_path, monkeypatch, {"recorded": recorded})
    assert _warnings(caplog) == []

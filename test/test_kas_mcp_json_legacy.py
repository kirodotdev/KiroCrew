"""Agent files that existed before the KAS mcp.json exclusion keep KAS's earlier behaviour."""

from __future__ import annotations

import json
import logging

import pytest

from kiro_crew.acp import kas_mcp_json_legacy as legacy


@pytest.fixture
def agents(tmp_path):
    root = tmp_path / "agents"
    root.mkdir()
    return root


def _write(path, data):
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def test_a_file_without_the_key_is_kept_by_stem_and_declared_name(agents):
    _write(agents / "pkg-helper.json", {"name": "helper", "tools": ["*"]})
    assert legacy.legacy_mcp_json("helper", agents, {})
    assert legacy.legacy_mcp_json("pkg-helper", agents, {})


@pytest.mark.parametrize(
    "key", [{"includeMcpJson": False}, {"includeMcpJson": True}, {"useLegacyMcpJson": False}]
)
def test_a_file_that_states_the_key_is_not_kept(agents, key):
    _write(agents / "a.json", {"name": "a", **key})
    assert not legacy.legacy_mcp_json("a", agents, {})


def test_the_list_is_recorded_once(agents):
    _write(agents / "old.json", {"name": "old"})
    assert legacy.legacy_mcp_json("old", agents, {})
    recorded = legacy.list_path().read_text(encoding="utf-8")
    _write(agents / "new.json", {"name": "new"})
    assert not legacy.legacy_mcp_json("new", agents, {})
    assert legacy.legacy_mcp_json("old", agents, {})
    assert legacy.list_path().read_text(encoding="utf-8") == recorded


def test_a_bad_file_is_skipped_and_the_rest_recorded(agents):
    _write(agents / "broken.json", "{ not json")
    _write(agents / "good.json", {"name": "good"})
    assert legacy.legacy_mcp_json("good", agents, {})
    assert not legacy.legacy_mcp_json("broken", agents, {})


def test_a_malformed_list_keeps_nobody_and_is_not_rebuilt(agents):
    _write(agents / "old.json", {"name": "old"})
    legacy.list_path().parent.mkdir(parents=True, exist_ok=True)
    _write(legacy.list_path(), "[]")
    assert not legacy.legacy_mcp_json("old", agents, {})
    assert legacy.list_path().read_text(encoding="utf-8") == "[]"


def test_no_agents_directory_records_an_empty_list(tmp_path):
    assert not legacy.legacy_mcp_json("a", tmp_path / "absent", {})
    assert json.loads(legacy.list_path().read_text(encoding="utf-8")) == {"agents": []}


def test_a_listed_file_that_now_states_the_key_follows_it(agents):
    _write(agents / "a.json", {"name": "a"})
    assert legacy.legacy_mcp_json("a", agents, {})
    assert not legacy.legacy_mcp_json("a", agents, {"includeMcpJson": False})
    assert not legacy.legacy_mcp_json("a", agents, {"useLegacyMcpJson": False})


def test_a_directory_that_cannot_be_listed_records_nothing(agents, monkeypatch):
    from kiro_crew import agent_spec_format

    _write(agents / "old.json", {"name": "old"})
    real = agent_spec_format.iter_agent_spec_files_strict

    def refuse(_directory):
        raise PermissionError("listing refused")

    monkeypatch.setattr(agent_spec_format, "iter_agent_spec_files_strict", refuse)
    assert not legacy.legacy_mcp_json("old", agents, {})
    assert not legacy.list_path().exists()
    monkeypatch.setattr(agent_spec_format, "iter_agent_spec_files_strict", real)
    assert legacy.legacy_mcp_json("old", agents, {})


def test_a_session_on_an_opted_out_agent_still_records_the_list(agents):
    _write(agents / "old.json", {"name": "old"})
    assert not legacy.legacy_mcp_json("kirocrew", agents, {"includeMcpJson": False})
    _write(agents / "new.json", {"name": "new"})
    assert legacy.legacy_mcp_json("old", agents, {})
    assert not legacy.legacy_mcp_json("new", agents, {})


def test_an_agent_file_name_cannot_forge_a_log_line(agents, caplog, monkeypatch):
    from kiro_crew import agent_discovery, agent_spec_format

    # Faked listing: Windows refuses a newline in a real file name.
    kept, bad = agents / "x\nERROR forged.json", agents / "y\nERROR forged.json"

    def _read(path, **_kw):
        if path == bad:
            raise ValueError("not json")
        return {"name": "x"}

    monkeypatch.setattr(agent_spec_format, "iter_agent_spec_files_strict", lambda _d: [kept, bad])
    monkeypatch.setattr(agent_discovery, "read_agent_spec_strict", _read)
    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.kas_mcp_json_legacy"):
        legacy.legacy_mcp_json("x", agents, {})
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("agent file")]
    assert len(lines) == 2 and all("\n" not in line for line in lines)


def test_a_read_error_cannot_forge_a_log_line(agents, caplog, monkeypatch):
    from kiro_crew import agent_discovery

    def _raise(path, **_kw):
        raise ValueError(f"{path}\nERROR forged")

    monkeypatch.setattr(agent_discovery, "read_agent_spec_strict", _raise)
    _write(agents / "a.json", {"name": "a"})
    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.kas_mcp_json_legacy"):
        legacy.legacy_mcp_json("a", agents, {})
    lines = [r.getMessage() for r in caplog.records if "skipped" in r.getMessage()]
    assert lines and all("\n" not in line for line in lines)

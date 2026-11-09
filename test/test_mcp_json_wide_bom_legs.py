"""A UTF-16 global ``mcp.json`` means the same thing on every leg that reads it.

The session leg refuses a UTF-16 file with a fixed reason. The Connections
(discovery) leg and the spec-rebuild leg read the same file, so they must reach
the same result and log that reason, once per refusal, and again after a good read.
"""

from __future__ import annotations

import codecs
import logging
from pathlib import Path

import pytest

from kiro_crew import agent, mcp_discovery_server_info
from kiro_crew.acp import session_mcp
from kiro_crew.mcp_discovery_config import _load_mcp_json_by_source
from kiro_crew.mcp_discovery_server_info import SCOPE_KIRO_GLOBAL, SCOPE_KIROCREW

_DOC = '{"mcpServers": {"probe": {"command": "x"}}}'
_REASON = "it is saved as UTF-16; re-save it as UTF-8"


@pytest.fixture(params=["utf-16-le", "utf-16-be"])
def global_file(request, tmp_path: Path, monkeypatch) -> Path:
    bom = codecs.BOM_UTF16_LE if request.param == "utf-16-le" else codecs.BOM_UTF16_BE
    path = tmp_path / "settings" / "mcp.json"
    path.parent.mkdir()
    path.write_bytes(bom + _DOC.encode(request.param))
    crew = tmp_path / "crew" / "mcp.json"
    crew.parent.mkdir()
    crew.write_text('{"mcpServers": {"live": {"command": "y"}}}', encoding="utf-8")
    monkeypatch.setattr(agent, "_KIRO_MCP_JSON", path)
    monkeypatch.setattr(
        mcp_discovery_server_info,
        "_MCP_SOURCES",
        ((crew, SCOPE_KIROCREW), (path, SCOPE_KIRO_GLOBAL)),
    )
    monkeypatch.setattr(mcp_discovery_server_info, "_MCP_JSON_PATHS", None)
    monkeypatch.setattr(session_mcp, "_global_refusal_logged", [None], raising=False)
    return path


def _reason_logs(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if _REASON in r.getMessage()]


def test_the_session_leg_refuses_the_file(global_file):
    with pytest.raises(ValueError) as info:
        session_mcp._read_mcp_settings(global_file)
    assert getattr(info.value, "reason", None) == _REASON
    assert session_mcp._global_settings() == {}


def test_the_connections_leg_matches_and_names_the_reason(global_file, caplog):
    with caplog.at_level(logging.WARNING):
        by_source = _load_mcp_json_by_source()
        _load_mcp_json_by_source()

    assert by_source[SCOPE_KIRO_GLOBAL] == {}
    assert set(by_source[SCOPE_KIROCREW]) == {"live"}
    assert len(_reason_logs(caplog)) == 1


def test_the_spec_rebuild_leg_matches_and_names_the_reason(global_file, caplog):
    from kiro_crew.agent_materialization import mcp_sources

    with caplog.at_level(logging.WARNING):
        config: dict = {}
        mcp_sources.merge_mcp_sources(config)
        mcp_sources.merge_mcp_sources({})

    assert "probe" not in config.get("mcpServers", {})
    assert len(_reason_logs(caplog)) == 1


def test_a_refusal_is_named_again_after_a_good_read(global_file, caplog):
    with caplog.at_level(logging.WARNING):
        session_mcp._global_settings()
        good = global_file.read_bytes()
        global_file.write_text(_DOC, encoding="utf-8")
        assert set(session_mcp._global_settings()["mcpServers"]) == {"probe"}
        global_file.write_bytes(good)
        session_mcp._global_settings()

    assert len(_reason_logs(caplog)) == 2

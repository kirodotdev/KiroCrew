"""MCP config files saved with a UTF-8 byte-order mark still load.

Windows editors save UTF-8 "with BOM" by default, and ``json.loads`` refuses a
leading U+FEFF ("Unexpected UTF-8 BOM"). These tests write each user-owned MCP
config with that mark and drive the readers that parse it.
"""

from __future__ import annotations

import codecs
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.user_json import (
    load_mcp_servers,
    load_user_json_object,
    loads_mcp_config,
    loads_user_json,
    strip_utf8_bom,
)

_BOM = codecs.BOM_UTF8


def _write_bom_json(path: Path, doc: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_BOM + json.dumps(doc).encode("utf-8"))


class TestLoadsUserJson:
    def test_text_with_bom_parses(self) -> None:
        assert loads_user_json('\ufeff{"a": 1}') == {"a": 1}

    def test_plain_text_is_unchanged(self) -> None:
        assert loads_user_json('{"a": "\ufeff"}') == {"a": "\ufeff"}

    def test_only_one_mark_is_dropped(self) -> None:
        assert strip_utf8_bom("\ufeff\ufeffx") == "\ufeffx"


class TestLoadsMcpConfig:
    def test_bom_object_parses(self) -> None:
        assert loads_mcp_config('\ufeff{"mcpServers": {"a": {}}}') == {"mcpServers": {"a": {}}}

    @pytest.mark.parametrize(
        "text",
        ["\ufeff[]", '"x"', '{"mcpServers": []}', '\ufeff{"mcpServers": "x"}'],
        ids=["bom_list_root", "scalar_root", "list_servers", "bom_scalar_servers"],
    )
    def test_a_shape_readers_cannot_index_reads_as_unparseable(self, text: str) -> None:
        # The readers' existing "cannot parse" branch then handles it, rather
        # than a ``.get`` on a list raising further down.
        with pytest.raises(json.JSONDecodeError):
            loads_mcp_config(text)


class TestLoadUserJsonObject:
    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert load_user_json_object(tmp_path / "absent.json") == {}

    def test_malformed_file_is_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "mcp.json"
        path.write_bytes(_BOM + b"{not json")
        assert load_user_json_object(path) == {}

    def test_non_object_root_is_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "mcp.json"
        _write_bom_json(path, ["a"])
        assert load_user_json_object(path) == {}


class TestLoadMcpServers:
    def test_bom_file_yields_its_servers(self, tmp_path: Path) -> None:
        path = tmp_path / "mcp.json"
        _write_bom_json(path, {"mcpServers": {"a": {"command": "a"}}})
        assert load_mcp_servers(path) == {"a": {"command": "a"}}

    def test_non_object_servers_contribute_nothing(self, tmp_path: Path) -> None:
        # Materialization iterates the map; a list here must not abort it.
        path = tmp_path / "mcp.json"
        _write_bom_json(path, {"mcpServers": [{"command": "a"}]})
        assert load_mcp_servers(path) == {}


class TestWritersPreserveAWrongShape:
    def test_the_cc_sidecar_writer_never_replaces_a_file_it_cannot_index(
        self, tmp_path: Path
    ) -> None:
        # A parseable file with a list ``mcpServers`` must not be read as empty
        # and overwritten with only the new entry.
        from kiro_crew import mcp_discovery

        path = tmp_path / ".mcp.json"
        raw = _BOM + json.dumps({"mcpServers": [{"command": "x"}], "keep": 1}).encode("utf-8")
        path.write_bytes(raw)
        server = mcp_discovery.McpServerInfo(name="n", command="c", args=[])
        with pytest.raises((AttributeError, TypeError)):
            mcp_discovery.register_servers_for_cc([server], mcp_json_path=path)
        assert path.read_bytes() == raw


@pytest.fixture()
def bridges_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from kiro_crew.apps import bridges

    agents = tmp_path / ".kiro" / "agents"
    settings = tmp_path / ".kiro" / "settings"
    agents.mkdir(parents=True)
    settings.mkdir(parents=True)
    monkeypatch.setattr(bridges, "_mcp_json_path", lambda: agents / "kirocrew.json")
    monkeypatch.setattr(bridges, "_LEGACY_SHARED_MCP_PATH", settings / "mcp.json")
    return tmp_path


class TestBridgesSetup:
    def test_legacy_scrub_reads_a_bom_file_and_writes_it_back_plain(
        self, bridges_home: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from kiro_crew.apps import bridges

        legacy = bridges._LEGACY_SHARED_MCP_PATH
        _write_bom_json(legacy, {"mcpServers": {"tunnel:srv": {"command": "x"}, "keep": {}}})

        with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.bridges"):
            assert bridges._scrub_legacy_shared_mcp("tunnel") == 1

        assert "Failed to scrub" not in caplog.text
        raw = legacy.read_bytes()
        assert not raw.startswith(_BOM)
        assert json.loads(raw) == {"mcpServers": {"keep": {}}}

    def test_registration_reads_a_bom_agent_config(self, bridges_home: Path) -> None:
        from kiro_crew.apps import bridges

        _write_bom_json(bridges._mcp_json_path(), {"mcpServers": {"user": {"command": "u"}}})

        # strict is the read-modify-write mode registration uses: an unreadable
        # file aborts the write, so a BOM here would stop app setup.
        cfg = bridges._read_mcp_json_unlocked(strict=True)
        assert cfg["mcpServers"] == {"user": {"command": "u"}}

    def test_a_bom_list_root_agent_config_is_refused_not_crashed_on(
        self, bridges_home: Path
    ) -> None:
        from kiro_crew.apps import bridges

        _write_bom_json(bridges._mcp_json_path(), [])
        assert bridges._read_mcp_json_unlocked() == {}
        with pytest.raises(json.JSONDecodeError):
            bridges._read_mcp_json_unlocked(strict=True)

    def test_an_installed_app_agent_spec_with_bom_keeps_its_user_edits(
        self, tmp_path: Path
    ) -> None:
        from kiro_crew.apps import bridges

        path = tmp_path / "app--agent.json"
        _write_bom_json(path, {"name": "app--agent", "model": "user-pick"})
        assert bridges._read_agent_config(path) == {"name": "app--agent", "model": "user-pick"}

    def test_global_policy_specs_read_a_bom_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.apps import bridges

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _write_bom_json(
            tmp_path / ".kiro" / "settings" / "mcp.json",
            {"mcpServers": {"g": {"command": "g"}}},
        )
        assert bridges._global_mcp_specs() == {"g": {"command": "g"}}


class TestChatStartLoading:
    def test_agent_materialization_loader_reads_a_bom_mcp_json(self, tmp_path: Path) -> None:
        from kiro_crew.user_json import load_user_json_object

        path = tmp_path / "mcp.json"
        _write_bom_json(path, {"mcpServers": {"s": {"command": "s"}}})
        assert load_user_json_object(path) == {"mcpServers": {"s": {"command": "s"}}}

    def test_agent_loader_keeps_judging_crew_config_like_the_config_loader(
        self, tmp_path: Path
    ) -> None:
        # ``KiroCrewConfig.load`` accepts a BOM'd ``config.json``, so the agent's
        # own loader must too: if the two judged one file differently, the spec
        # builder would treat a config the gateway loaded as absent.
        from kiro_crew import agent

        path = tmp_path / "config.json"
        _write_bom_json(path, {"model": "m"})
        assert agent._load_json(path) == {"model": "m"}

    def test_doctor_reads_a_bom_agent_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from kiro_crew import cli_doctor

        class _Stop(Exception):
            pass

        def _stop(**_kw: object) -> None:
            raise _Stop

        # Stop right after the parse verdict: the rest of doctor probes servers.
        monkeypatch.setattr(cli_doctor._agent, "_decline_shared_agent_home", _stop)
        path = tmp_path / "kirocrew.json"
        # Valid JSON that is not an object: doctor names it only when the parse
        # got past the byte-order mark.
        _write_bom_json(path, ["not", "an", "object"])
        with pytest.raises(_Stop):
            cli_doctor._doctor_mcp_tools(path, [])
        assert "agent spec is not a JSON object" in capsys.readouterr().out

    def test_json_agent_spec_with_bom_parses(self) -> None:
        from kiro_crew.agent_spec_format import parse_agent_spec_bytes

        spec = parse_agent_spec_bytes(_BOM + b'{"name": "a"}', "a.json")
        assert spec == {"name": "a"}

    def test_discovery_reads_a_bom_mcp_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import mcp_discovery

        path = tmp_path / "crew" / "mcp.json"
        _write_bom_json(path, {"mcpServers": {"d": {"command": "d"}}})
        monkeypatch.setattr(mcp_discovery, "_MCP_JSON_PATHS", (path,))
        monkeypatch.setattr(mcp_discovery, "_extra_scope_sources", lambda: [])

        merged = mcp_discovery._load_mcp_json()
        assert merged == {"d": {"command": "d"}}


class TestForkRefreshReadsTheSpecStrictly:
    """The fork refresh reads a BOM'd fork spec, and never rewrites one it cannot parse."""

    @pytest.fixture
    def one_fork(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        from types import SimpleNamespace

        from kiro_crew import agent, agent_state
        from kiro_crew.config.loader import KiroCrewConfig

        monkeypatch.setattr(agent, "_fork_refresh_failed", frozenset())
        monkeypatch.setattr(agent, "_fork_refresh_pending", 0)
        monkeypatch.setattr(
            agent_state,
            "all_fork_info",
            lambda: {"crewfork": {"private_to": "crew", "forked_from": "kirocrew"}},
        )
        monkeypatch.setattr(agent_state, "get_capabilities", lambda _name: None)
        agents = {"crew": SimpleNamespace(kiro_agent="crewfork")}
        monkeypatch.setattr(
            KiroCrewConfig, "load", classmethod(lambda cls: SimpleNamespace(agents=agents))
        )
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
        return tmp_path

    def test_an_unparseable_fork_spec_stays_blocked_and_is_not_rewritten(
        self, one_fork: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import agent

        spec = one_fork / "crewfork.json"
        spec.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(agent, "agent_spec_path", lambda _name: spec)
        written: list[dict[str, Any]] = []
        monkeypatch.setattr(
            agent, "_atomic_json_write", lambda _path, config: written.append(config)
        )
        agent._refresh_forked_templates_locked(gated_off=frozenset())
        assert written == []
        assert agent._fork_refresh_failed == frozenset({"crewfork"})

    def test_a_fork_spec_saved_with_a_byte_order_mark_is_refreshed_whole(
        self, one_fork: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import agent

        spec = one_fork / "crewfork.json"
        doc = {"name": "crewfork", "tools": [], "allowedTools": [], "prompt": "keep me"}
        spec.write_bytes(b"\xef\xbb\xbf" + json.dumps(doc).encode("utf-8"))
        monkeypatch.setattr(agent, "agent_spec_path", lambda _name: spec)
        monkeypatch.setattr(agent, "_refresh_dynamic_fields", lambda *_a, **_k: None)
        written: list[dict[str, Any]] = []
        monkeypatch.setattr(
            agent, "_atomic_json_write", lambda _path, config: written.append(config)
        )
        agent._refresh_forked_templates_locked(gated_off=frozenset())
        assert [config.get("prompt") for config in written] == ["keep me"]
        assert agent._fork_refresh_failed == frozenset()

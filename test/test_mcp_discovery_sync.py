"""MCP discovery: proposing servers to sync, and writing them to the agent config.

``discover_servers_to_sync`` flags a new server, or one whose command, environment, remote
URL, headers or OAuth hints diverge from the agent's entry, and command spellings that
resolve to the same binary do not count as diverging; the sync trigger fires on exactly the
keys the merge reconciles; ``sync_to_agent_config`` writes local and remote servers without
launching kiro-cli; and a ``disabled`` flag in any scope marks the listed row.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from mcp_discovery_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _clear_cache,
    _passthrough_sandbox,
    _stub_remote_probe_ssrf_vet,
)

from conftest import host_abs
from kiro_crew import platform_compat
from kiro_crew.mcp_discovery import (
    SCOPE_KIRO_GLOBAL,
    McpServerInfo,
    discover_servers_to_sync,
    list_servers,
    probe_server,
    sync_to_agent_config,
)


def _make_executable(path: Path) -> Path:
    """Create *path* as a runnable file, parents included.

    Runnable by the predicate the code under test applies, ``isfile`` plus
    ``X_OK``. Windows answers ``X_OK`` for any existing file but wants an
    executable SUFFIX, so give it one ``PATHEXT`` lists; POSIX needs the mode
    bit and ignores the suffix.
    """
    if platform_compat.IS_WINDOWS:
        path = path.with_suffix(".exe")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    path.chmod(0o755)
    return path


class TestDiscoverNew:
    def test_discover_new(self, tmp_path, monkeypatch) -> None:
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {"mcpServers": {"existing": {"command": "a"}}}
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "existing": {"command": "a"},
                        "brand-new": {"command": "b"},
                    }
                }
            )
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        new = discover_servers_to_sync()
        assert len(new) == 1
        assert new[0].name == "brand-new"
        assert new[0].source == "discovered"

    def test_discover_new_remote_preserves_url_and_headers(self, tmp_path, monkeypatch) -> None:
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(json.dumps({"mcpServers": {}}))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        headers = {"Authorization": "Bearer sync-secret", "X-Tenant": "acme"}
        mcp_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "remote": {
                            "url": "https://mcp.example.com/v1",
                            "headers": headers,
                        }
                    }
                }
            )
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))

        result = discover_servers_to_sync()

        assert len(result) == 1
        assert result[0].name == "remote"
        assert result[0].is_remote is True
        assert result[0].command == ""
        assert result[0].url == "https://mcp.example.com/v1"
        assert result[0].headers == headers

    def test_discover_flags_existing_remote_url_change(self, tmp_path, monkeypatch) -> None:
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        headers = {"Authorization": "Bearer sync-secret"}
        cfg = {"mcpServers": {"remote": {"url": "https://mcp.example.com/v1", "headers": headers}}}
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "remote": {
                            "url": "https://mcp.example.com/v2",
                            "headers": headers,
                        }
                    }
                }
            )
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))

        result = discover_servers_to_sync()

        assert len(result) == 1
        assert result[0].url == "https://mcp.example.com/v2"

    def test_discover_flags_existing_remote_headers_change(self, tmp_path, monkeypatch) -> None:
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {
            "mcpServers": {
                "remote": {
                    "url": "https://mcp.example.com/v1",
                    "headers": {"Authorization": "Bearer old"},
                }
            }
        }
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "remote": {
                            "url": "https://mcp.example.com/v1",
                            "headers": {"Authorization": "Bearer new"},
                        }
                    }
                }
            )
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))

        result = discover_servers_to_sync()

        assert len(result) == 1
        assert result[0].headers == {"Authorization": "Bearer new"}

    def test_discover_new_remote_preserves_scopes_and_client_id(
        self, tmp_path, monkeypatch
    ) -> None:
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(json.dumps({"mcpServers": {}}))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "remote": {
                            "url": "https://mcp.example.com/v1",
                            "scopes": ["read:user", "read:org"],
                            "clientId": "public-client-id",
                        }
                    }
                }
            )
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))

        result = discover_servers_to_sync()

        assert len(result) == 1
        assert result[0].scopes == ["read:user", "read:org"]
        assert result[0].client_id == "public-client-id"

    def test_discover_flags_existing_remote_scopes_change(self, tmp_path, monkeypatch) -> None:
        """A Connect that widens or narrows scopes must re-sync, not be ignored."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {"mcpServers": {"remote": {"url": "https://mcp.example.com/v1", "scopes": ["read"]}}}
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "remote": {
                            "url": "https://mcp.example.com/v1",
                            "scopes": ["read", "write"],
                        }
                    }
                }
            )
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))

        result = discover_servers_to_sync()

        assert len(result) == 1
        assert result[0].scopes == ["read", "write"]

    def test_discover_flags_existing_remote_client_id_change(self, tmp_path, monkeypatch) -> None:
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {
            "mcpServers": {"remote": {"url": "https://mcp.example.com/v1", "clientId": "old-id"}}
        }
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "remote": {"url": "https://mcp.example.com/v1", "clientId": "new-id"}
                    }
                }
            )
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))

        result = discover_servers_to_sync()

        assert len(result) == 1
        assert result[0].client_id == "new-id"

    def test_discover_no_resync_when_oauth_hints_match(self, tmp_path, monkeypatch) -> None:
        """Equal hints must not churn: an unchanged entry stays out of the sync set."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        entry = {
            "url": "https://mcp.example.com/v1",
            "scopes": ["read"],
            "clientId": "public-client-id",
        }
        (agent_dir / "defaults.json").write_text(json.dumps({"mcpServers": {"remote": entry}}))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(json.dumps({"mcpServers": {"remote": entry}}))
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))

        assert discover_servers_to_sync() == []

    @pytest.mark.parametrize(
        "spec_extra,expected_scopes,expected_client_id",
        [
            ({"scopes": "read"}, [], ""),
            # A partially-valid list degrades to NO scopes, never to its
            # well-formed subset: truncating it would propagate a request the
            # file never made, and would disagree with the emit path, which
            # omits the field entirely on any malformed member.
            ({"scopes": ["read", 7]}, [], ""),
            ({"scopes": ["read", "  "]}, [], ""),
            ({"scopes": None}, [], ""),
            ({"clientId": 42}, [], ""),
            ({"clientId": "   "}, [], ""),
            ({"clientId": None}, [], ""),
        ],
    )
    def test_discover_degrades_malformed_oauth_hints(
        self, tmp_path, monkeypatch, spec_extra, expected_scopes, expected_client_id
    ) -> None:
        """Hand-edited mcp.json must not propagate a bad shape into the agent config."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(json.dumps({"mcpServers": {}}))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        spec = {"url": "https://mcp.example.com/v1", **spec_extra}
        mcp_json.write_text(json.dumps({"mcpServers": {"remote": spec}}))
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))

        result = discover_servers_to_sync()

        assert len(result) == 1
        assert result[0].scopes == expected_scopes
        assert result[0].client_id == expected_client_id

    def test_discover_none_when_all_known(self, tmp_path, monkeypatch) -> None:
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {"mcpServers": {"srv": {"command": "a"}}}
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(json.dumps({"mcpServers": {"srv": {"command": "a"}}}))
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        new = discover_servers_to_sync()
        assert new == []

    def test_discover_includes_existing_with_divergent_env(self, tmp_path, monkeypatch) -> None:
        """Existing servers with new env keys in mcp.json are included."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {"mcpServers": {"srv": {"command": "a", "env": {}}}}
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps({"mcpServers": {"srv": {"command": "a", "env": {"KEY": "val"}}}})
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        result = discover_servers_to_sync()
        assert len(result) == 1
        assert result[0].name == "srv"
        assert result[0].env == {"KEY": "val"}

    def test_discover_skips_existing_with_expanded_path(self, tmp_path, monkeypatch) -> None:
        """An expanded env.PATH in the agent config is not a divergence.

        install_agent writes the effective PATH while mcp.json keeps the
        fragment the user authored — the same resolved-vs-authored asymmetry
        ``_commands_diverged`` already absorbs for commands. Comparing the raw
        strings would flag every synced server on every refresh, re-syncing
        forever.
        """
        from kiro_crew.env import spec_env_path

        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        expanded = spec_env_path("/opt/shims")
        cfg = {"mcpServers": {"srv": {"command": "a", "env": {"PATH": expanded}}}}
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps({"mcpServers": {"srv": {"command": "a", "env": {"PATH": "/opt/shims"}}}})
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        assert discover_servers_to_sync() == []

    def test_discover_flags_changed_path_fragment(self, tmp_path, monkeypatch) -> None:
        """A genuinely edited env.PATH still triggers a re-sync."""
        from kiro_crew.env import spec_env_path

        # Spelled for the host (conftest.host_abs): a fragment that fails
        # ``os.path.isabs`` is dropped by the expansion, and from Python 3.13 a bare
        # ``/opt/old`` is not absolute under ntpath -- then old and new both expand
        # to the bare augmentation and the edit is invisible.
        old, new = host_abs("opt", "old"), host_abs("opt", "new")
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {"mcpServers": {"srv": {"command": "a", "env": {"PATH": spec_env_path(old)}}}}
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps({"mcpServers": {"srv": {"command": "a", "env": {"PATH": new}}}})
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        result = discover_servers_to_sync()
        assert len(result) == 1
        assert result[0].env == {"PATH": new}

    def test_discover_skips_existing_with_identical_env(self, tmp_path, monkeypatch) -> None:
        """Existing servers with identical env are not flagged for sync."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {"mcpServers": {"srv": {"command": "a", "env": {"KEY": "val"}}}}
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps({"mcpServers": {"srv": {"command": "a", "env": {"KEY": "val"}}}})
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        result = discover_servers_to_sync()
        assert result == []

    def test_discover_skips_existing_when_source_env_is_subset(self, tmp_path, monkeypatch) -> None:
        """Server not flagged when all mcp.json env keys already exist in agent config."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {"mcpServers": {"srv": {"command": "a", "env": {"EXISTING": "keep", "NEW": "val"}}}}
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps({"mcpServers": {"srv": {"command": "a", "env": {"NEW": "val"}}}})
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        result = discover_servers_to_sync()
        assert result == []

    def test_discover_skips_existing_with_divergent_args(self, tmp_path, monkeypatch) -> None:
        """Existing servers with different args are NOT flagged for sync.

        Args are user-customizable (e.g. --include-tools additions).
        Since install_agent() preserves user args via setdefault merge,
        flagging on args divergence only wastes a full config rebuild.
        """
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {
            "mcpServers": {
                "srv": {
                    "command": "srv-cmd",
                    "args": ["--include-tools=ReadInternalWebsites,TicketingWriteActions"],
                }
            }
        }
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "srv": {
                            "command": "srv-cmd",
                            "args": ["--include-tools=ReadInternalWebsites"],
                        }
                    }
                }
            )
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        result = discover_servers_to_sync()
        assert result == []

    def test_discover_skips_disabled_servers(self, tmp_path, monkeypatch) -> None:
        """Disabled servers are never included in sync results."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        cfg = {"mcpServers": {}}
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "disabled-srv": {"command": "x", "disabled": True},
                        "enabled-srv": {"command": "y"},
                    }
                }
            )
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        result = discover_servers_to_sync()
        assert len(result) == 1
        assert result[0].name == "enabled-srv"

    def test_discover_skips_resolved_path_match(self, tmp_path, monkeypatch) -> None:
        """Short command name matching the basename of the agent's resolved path is not flagged."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        pinned = _make_executable(tmp_path / "bin" / "my-server")
        cfg = {"mcpServers": {"srv": {"command": str(pinned)}}}
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(json.dumps({"mcpServers": {"srv": {"command": "my-server"}}}))
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        result = discover_servers_to_sync()
        assert result == []

    def test_discover_proposes_resync_for_vanished_pinned_path(self, tmp_path, monkeypatch) -> None:
        """A pinned absolute command whose file is gone must reach the sync list.

        This is the user-visible half of the basename heuristic's blind spot: an
        agent entry pinned to a version-stamped path keeps that basename after
        the release is deleted, so with no liveness probe the row reads as
        already-synced and the server fails at spawn time with no warning.
        """
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        pinned = _make_executable(tmp_path / "tools" / "my-server" / "1.0.1" / "my-server")
        cfg = {"mcpServers": {"srv": {"command": str(pinned)}}}
        (agent_dir / "defaults.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(json.dumps({"mcpServers": {"srv": {"command": "my-server"}}}))
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        assert discover_servers_to_sync() == []

        shutil.rmtree(pinned.parent)
        result = discover_servers_to_sync()
        assert [s.name for s in result] == ["srv"]


class TestSyncTriggerMatchesTheMerge:
    """The trigger must fire exactly on the keys ``_merge_source_owned`` reconciles.

    A rebuild reconciles ``agent._SOURCE_OWNED_MCP_KEYS`` onto an existing entry.
    A key in that set and absent from the trigger is a change the dashboard never
    offers; a key outside it and present in the trigger is a sync offer that
    converges on nothing.
    """

    @staticmethod
    def _write(tmp_path, monkeypatch, agent_spec: dict, source_spec: dict) -> Path:
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir(exist_ok=True)
        (agent_dir / "defaults.json").write_text(json.dumps({"mcpServers": {"srv": agent_spec}}))
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(json.dumps({"mcpServers": {"srv": source_spec}}))
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
        return mcp_json

    def test_local_timeout_change_fires(self, tmp_path, monkeypatch) -> None:
        self._write(
            tmp_path, monkeypatch, {"command": "a", "timeout": 60}, {"command": "a", "timeout": 120}
        )
        assert [s.name for s in discover_servers_to_sync()] == ["srv"]

    def test_remote_timeout_change_fires(self, tmp_path, monkeypatch) -> None:
        url = "https://mcp.example.com/v1"
        self._write(
            tmp_path, monkeypatch, {"url": url, "timeout": 60}, {"url": url, "timeout": 120}
        )
        assert [s.name for s in discover_servers_to_sync()] == ["srv"]

    def test_retired_key_does_not_fire(self, tmp_path, monkeypatch) -> None:
        """A key the source stopped declaring is not a convergent sync.

        Only ``_merge_source_owned`` pops such a key, and the kirocrew scope is
        merged with ``dict.update`` instead, so a server declared there alone
        would be offered the same sync on every poll forever.
        """
        self._write(tmp_path, monkeypatch, {"command": "a", "timeout": 60}, {"command": "a"})
        assert discover_servers_to_sync() == []

    def test_declared_disabled_false_against_a_disabled_entry_fires(
        self, tmp_path, monkeypatch
    ) -> None:
        """A source that declares the server back on must reach the entry."""
        self._write(
            tmp_path,
            monkeypatch,
            {"command": "a", "disabled": True},
            {"command": "a", "disabled": False},
        )
        assert [s.name for s in discover_servers_to_sync()] == ["srv"]

    def test_args_change_does_not_fire(self, tmp_path, monkeypatch) -> None:
        """``args`` is not reconciled, so offering a sync for it converges on nothing."""
        self._write(
            tmp_path,
            monkeypatch,
            {"command": "a", "args": ["--old"]},
            {"command": "a", "args": ["--new"]},
        )
        assert discover_servers_to_sync() == []

    def test_agreeing_source_owned_keys_do_not_fire(self, tmp_path, monkeypatch) -> None:
        self._write(
            tmp_path, monkeypatch, {"command": "a", "timeout": 60}, {"command": "a", "timeout": 60}
        )
        assert discover_servers_to_sync() == []

    def test_another_scopes_disabled_does_not_fire(self, tmp_path, monkeypatch) -> None:
        """``McpServerInfo.disabled`` is an aggregate across scopes; the trigger is not.

        A lower-priority scope switching the server off never reaches the winning
        spec, so comparing the aggregate would fire a sync that cannot converge.
        """
        self._write(tmp_path, monkeypatch, {"command": "a"}, {"command": "a"})
        other = tmp_path / "other-mcp.json"
        other.write_text(json.dumps({"mcpServers": {"srv": {"command": "a", "disabled": True}}}))
        monkeypatch.setattr(
            "kiro_crew.mcp_discovery._extra_scope_sources", lambda: [(other, "ccGlobal")]
        )
        assert discover_servers_to_sync() == []

    def test_key_set_is_read_from_agent_not_restated(self, tmp_path, monkeypatch) -> None:
        """Mutation guard: a hardcoded local key list would survive the drift.

        Narrowing ``agent._SOURCE_OWNED_MCP_KEYS`` must silence the trigger for the
        dropped key in the same commit, which is only true if the trigger imports it.
        """
        import kiro_crew.agent as agent_mod

        self._write(
            tmp_path, monkeypatch, {"command": "a", "timeout": 60}, {"command": "a", "timeout": 120}
        )
        assert [s.name for s in discover_servers_to_sync()] == ["srv"]

        monkeypatch.setattr(agent_mod, "_SOURCE_OWNED_MCP_KEYS", ("disabled",))
        assert discover_servers_to_sync() == []


class TestCommandsDiverged:
    @pytest.mark.parametrize(
        "old,new,diverged",
        [
            pytest.param("foo", "foo", False, id="identical-commands"),
            pytest.param("/usr/bin/server", "server", False, id="resolved-vs-short"),
            pytest.param("old-server", "new-server", True, id="genuinely-different-commands"),
            # Two different binaries with the same file name are NOT the same server.
            pytest.param(
                "/opt/a/bin/srv",
                "/opt/b/bin/srv",
                True,
                id="distinct-absolute-paths-sharing-a-basename-diverge",
            ),
        ],
    )
    def test_command_spellings(self, old, new, diverged) -> None:
        from kiro_crew.mcp_discovery import _commands_diverged

        assert _commands_diverged(old, new) is diverged

    def test_short_vs_resolved_path(self, tmp_path) -> None:
        """A bare name and the live absolute path it resolved to are one server."""
        from kiro_crew.mcp_discovery import _commands_diverged

        pinned = _make_executable(tmp_path / "bin" / "deep-research")
        assert _commands_diverged("deep-research", str(pinned)) is False

    def test_short_vs_vanished_resolved_path_diverges(self, tmp_path) -> None:
        """The basename match is void once the pinned path stops resolving.

        A version-stamped pin keeps its basename after a tool update removes the
        directory that held it, so a name-only comparison can never propose the
        re-sync that would repair the entry.
        """
        from kiro_crew.mcp_discovery import _commands_diverged

        pinned = _make_executable(tmp_path / "tools" / "deep-research" / "1.0.1" / "deep-research")
        assert _commands_diverged("deep-research", str(pinned)) is False

        shutil.rmtree(pinned.parent)
        assert _commands_diverged("deep-research", str(pinned)) is True

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS,
        reason="POSIX-only: clearing the execute bit does not make a file unrunnable on Windows.",
    )
    def test_present_but_non_executable_pin_diverges(self, tmp_path) -> None:
        """A pin the process cannot execute is as unusable as an absent one."""
        from kiro_crew.mcp_discovery import _commands_diverged

        pinned = _make_executable(tmp_path / "bin" / "srv")
        pinned.chmod(0o444)
        assert _commands_diverged("srv", str(pinned)) is True

    def test_distinct_existing_paths_sharing_a_basename_diverge(self, tmp_path) -> None:
        """Two live files with one name are two servers, not two spellings of one."""
        from kiro_crew.mcp_discovery import _commands_diverged

        first = _make_executable(tmp_path / "a" / "srv")
        second = _make_executable(tmp_path / "b" / "srv")
        assert _commands_diverged(str(first), str(second)) is True

    def test_relative_path_does_not_match_unrelated_rooted_path(self) -> None:
        """A CWD-relative path names a specific file, not a PATH lookup.

        ``bin/srv`` resolves against the working directory, so it is not the bare
        name that ``PATH`` lookup turned into ``/usr/bin/srv`` — treating it as
        one would silently skip syncing a genuinely changed command.
        """
        from kiro_crew.mcp_discovery import _commands_diverged

        assert _commands_diverged("bin/srv", "/usr/bin/srv") is True
        assert _commands_diverged("./srv", "/usr/bin/srv") is True
        assert _commands_diverged("/usr/bin/srv", "bin/srv") is True

    @pytest.mark.skipif(
        not platform_compat.IS_WINDOWS,
        reason="Windows-only: PATHEXT suffixes are only stripped there.",
    )
    def test_differing_pathext_suffixes_diverge(self) -> None:
        """``foo.bat`` and ``foo.cmd`` are different files, not two spellings of one.

        Only the ``shutil.which``-resolved (rooted) side may shed its suffix;
        folding it off both sides would collapse distinct executables.
        """
        from kiro_crew.mcp_discovery import _commands_diverged

        assert _commands_diverged("foo.bat", r"C:\x\foo.cmd") is True
        assert _commands_diverged("myserver.js", r"C:\x\myserver.exe") is True

    @pytest.mark.skipif(
        not platform_compat.IS_WINDOWS,
        reason="Windows-only: PATHEXT suffixing and case/separator-insensitive paths.",
    )
    def test_pathext_resolved_command_does_not_diverge(self, tmp_path, monkeypatch) -> None:
        """A bare name matches the ``shutil.which`` result that carries a PATHEXT suffix.

        ``agent._resolve_command`` resolves ``npx`` to ``...\\npx.CMD`` because
        ``shutil.which`` spells the extension as ``PATHEXT`` does. Treating that as
        divergence would re-sync and reset every session on every startup.
        """
        from kiro_crew.mcp_discovery import _commands_diverged

        monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
        # A real file, because the resolved side is now probed for liveness: a
        # fabricated path under Program Files would make this assertion depend on
        # what the runner image has installed.
        resolved = tmp_path / "npx.CMD"
        resolved.write_text("")
        assert _commands_diverged("npx", str(resolved)) is False
        assert _commands_diverged(r"C:\tools\my-server.exe", "my-server") is False

    @pytest.mark.skipif(
        not platform_compat.IS_WINDOWS,
        reason="Windows-only: paths are case-insensitive and accept either separator.",
    )
    def test_separator_and_case_variants_do_not_diverge(self) -> None:
        from kiro_crew.mcp_discovery import _commands_diverged

        assert _commands_diverged(r"C:\tools\srv.exe", "C:/Tools/SRV.exe") is False

    @pytest.mark.skipif(
        not platform_compat.IS_WINDOWS,
        reason="Windows-only: a driveless root is not ntpath.isabs but still names a path.",
    )
    def test_driveless_rooted_path_matches_bare_name(self) -> None:
        """A POSIX-shaped ``mcp.json`` copied onto Windows still resolves by basename.

        ``ntpath.isabs('/usr/bin/srv')`` is False (no drive), so a rooted-path check
        alone would read the whole string as a bare command name.
        """
        from kiro_crew.mcp_discovery import _commands_diverged

        assert _commands_diverged("srv", "/usr/bin/srv") is False
        assert _commands_diverged(r"\tools\srv", "srv") is False

    def test_other_os_path_spelling_is_not_probed(self) -> None:
        """A path this host does not address is never called stale.

        The filesystem cannot answer for the other OS's spelling, nor for a
        driveless root that resolves against whichever drive is current, so
        neither may be reported as a vanished pin -- doing so would re-sync a
        portable config on every pass.
        """
        from kiro_crew.mcp_discovery import _pinned_command_missing

        foreign = r"C:\tools\nope\srv" if not platform_compat.IS_WINDOWS else "/usr/bin/nope/srv"
        assert _pinned_command_missing(foreign) is False
        assert _pinned_command_missing("srv") is False

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS,
        reason="POSIX-only: filenames are case-sensitive there, unlike Windows.",
    )
    def test_case_differing_commands_diverge_on_posix(self) -> None:
        from kiro_crew.mcp_discovery import _commands_diverged

        assert _commands_diverged("Server", "/usr/bin/server") is True


class TestSyncToAgentConfig:
    def test_sync_never_launches_kiro_cli(self, tmp_path, monkeypatch) -> None:
        """sync_to_agent_config launches no subprocess, even with kiro-cli on PATH.

        The ``kiro-cli mcp add`` side channel was an unsynchronized second
        writer of the agent config whose output ``install_agent()`` rewrote
        moments later; the sync is install_agent() alone now, so a reappearing
        Popen here is a regression to the two-writer design.
        """
        calls: list[list[str]] = []

        def mock_which(x: str, **kw: object) -> str | None:
            return "/usr/bin/kiro-cli" if x == "kiro-cli" else None

        class MockPopen:
            returncode = 0

            def __init__(self, cmd: list[str], **kwargs: object) -> None:
                calls.append(list(cmd))

            def communicate(self, **kwargs: object) -> tuple[bytes, bytes]:
                return b"", b""

        monkeypatch.setattr("shutil.which", mock_which)
        monkeypatch.setattr("subprocess.Popen", MockPopen)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)
        config_path = kiro_dir / "kirocrew.json"
        config_path.write_text(json.dumps({"mcpServers": {}, "tools": [], "allowedTools": []}))

        install_called = []
        monkeypatch.setattr(
            "kiro_crew.agent.install_agent",
            lambda **kw: install_called.append(True) or config_path,
        )

        new_srv = McpServerInfo(name="new-srv", command="b", args=["--x"])
        ok = sync_to_agent_config([new_srv])
        assert ok is True
        assert install_called, "install_agent() is the one write path"
        assert calls == [], "no subprocess may be launched by the sync"

    def test_sync_fallback_writes_json(self, tmp_path, monkeypatch) -> None:
        """Without kiro-cli, delegates to install_agent() for config merge."""
        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)
        cfg = {
            "mcpServers": {"existing": {"command": "a"}},
            "tools": ["execute_bash"],
            "allowedTools": [],
        }
        config_path = kiro_dir / "kirocrew.json"
        config_path.write_text(json.dumps(cfg))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        monkeypatch.setattr("shutil.which", lambda x, **kw: None)

        install_called = []
        monkeypatch.setattr(
            "kiro_crew.agent.install_agent",
            lambda **kw: install_called.append(True) or config_path,
        )

        new_srv = McpServerInfo(name="new-srv", command="b", args=["--x"])
        ok = sync_to_agent_config([new_srv])
        assert ok is True
        assert install_called, "install_agent() should be called"

    def test_sync_no_installed_config(self, tmp_path, monkeypatch) -> None:
        """Works even when no config exists yet — install_agent creates it."""
        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)
        config_path = kiro_dir / "kirocrew.json"
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        monkeypatch.setattr("shutil.which", lambda x, **kw: None)

        install_called = []
        monkeypatch.setattr(
            "kiro_crew.agent.install_agent",
            lambda **kw: install_called.append(True) or config_path,
        )

        srv = McpServerInfo(name="srv", command="x")
        ok = sync_to_agent_config([srv])
        assert ok is True
        assert install_called

    def test_sync_remote_server_writes_url(self, tmp_path, monkeypatch) -> None:
        """Remote servers are handled by install_agent() via source file merge."""
        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)
        cfg: dict = {"mcpServers": {}, "tools": [], "allowedTools": []}
        config_path = kiro_dir / "kirocrew.json"
        config_path.write_text(json.dumps(cfg))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        monkeypatch.setattr("shutil.which", lambda x, **kw: None)

        install_called = []
        monkeypatch.setattr(
            "kiro_crew.agent.install_agent",
            lambda **kw: install_called.append(True) or config_path,
        )

        srv = McpServerInfo(
            name="deepwiki",
            url="https://mcp.deepwiki.com/mcp",
            headers={"X-Key": "val"},
        )
        ok = sync_to_agent_config([srv])
        assert ok is True
        assert install_called

    def test_sync_mixed_servers_launch_nothing(self, tmp_path, monkeypatch) -> None:
        """A mixed remote+local set syncs through install_agent() with no subprocess."""
        calls: list[list[str]] = []

        def mock_which(x: str, **kw: object) -> str | None:
            return "/usr/bin/kiro-cli" if x == "kiro-cli" else None

        class MockPopen:
            returncode = 0

            def __init__(self, cmd: list[str], **kwargs: object) -> None:
                calls.append(list(cmd))

            def communicate(self, **kwargs: object) -> tuple[bytes, bytes]:
                return b"", b""

        monkeypatch.setattr("shutil.which", mock_which)
        monkeypatch.setattr("subprocess.Popen", MockPopen)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)
        config_path = kiro_dir / "kirocrew.json"
        config_path.write_text(json.dumps({"mcpServers": {}, "tools": [], "allowedTools": []}))

        install_called = []
        monkeypatch.setattr(
            "kiro_crew.agent.install_agent",
            lambda **kw: install_called.append(True) or config_path,
        )

        remote = McpServerInfo(name="deepwiki", url="https://mcp.deepwiki.com/mcp")
        local = McpServerInfo(name="local-srv", command="some-cmd")
        assert sync_to_agent_config([remote, local]) is True
        assert install_called
        assert calls == []

    def test_sync_merges_env_for_existing_local_server(self, tmp_path, monkeypatch) -> None:
        """Existing server env changes are handled by install_agent() re-merge."""
        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)
        cfg = {
            "mcpServers": {
                "aws-outlook-mcp": {"command": "node", "args": ["server.js"], "env": {}}
            },
            "tools": ["@aws-outlook-mcp"],
            "allowedTools": ["@aws-outlook-mcp"],
        }
        config_path = kiro_dir / "kirocrew.json"
        config_path.write_text(json.dumps(cfg))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        monkeypatch.setattr("shutil.which", lambda x, **kw: None)

        install_called = []
        monkeypatch.setattr(
            "kiro_crew.agent.install_agent",
            lambda **kw: install_called.append(True) or config_path,
        )

        srv = McpServerInfo(
            name="aws-outlook-mcp",
            command="node",
            args=["server.js"],
            env={"OUTLOOK_MCP_ENABLE_WRITES": "true"},
        )
        sync_to_agent_config([srv])
        assert install_called, "install_agent() handles env merge"

    def test_sync_preserves_existing_env_keys(self, tmp_path, monkeypatch) -> None:
        """Env merge is handled by install_agent() reading source files."""
        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)
        cfg = {
            "mcpServers": {
                "my-mcp": {
                    "command": "node",
                    "args": [],
                    "env": {"EXISTING_KEY": "keep"},
                }
            },
            "tools": ["@my-mcp"],
            "allowedTools": ["@my-mcp"],
        }
        config_path = kiro_dir / "kirocrew.json"
        config_path.write_text(json.dumps(cfg))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        monkeypatch.setattr("shutil.which", lambda x, **kw: None)

        install_called = []
        monkeypatch.setattr(
            "kiro_crew.agent.install_agent",
            lambda **kw: install_called.append(True) or config_path,
        )

        srv = McpServerInfo(
            name="my-mcp",
            command="node",
            args=[],
            env={"NEW_KEY": "val"},
        )
        sync_to_agent_config([srv])
        assert install_called, "install_agent() handles env merge"

    def test_sync_updates_command_for_existing_local_server(self, tmp_path, monkeypatch) -> None:
        """Existing servers are refreshed via install_agent() which reads source files."""
        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)
        cfg = {
            "mcpServers": {"my-mcp": {"command": "old-cmd", "args": ["--old"], "env": {}}},
            "tools": ["@my-mcp"],
            "allowedTools": ["@my-mcp"],
        }
        config_path = kiro_dir / "kirocrew.json"
        config_path.write_text(json.dumps(cfg))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        monkeypatch.setattr("shutil.which", lambda x, **kw: None)

        # install_agent() is called internally — mock it to verify delegation
        install_called = []
        monkeypatch.setattr(
            "kiro_crew.agent.install_agent",
            lambda **kw: install_called.append(True) or config_path,
        )

        srv = McpServerInfo(name="my-mcp", command="new-cmd", args=["--new"])
        result = sync_to_agent_config([srv])
        assert result is True
        assert install_called, "install_agent() should be called to re-merge config"

    def test_sync_source_env_overrides_existing_on_conflict(self, tmp_path, monkeypatch) -> None:
        """Config changes are handled by install_agent() re-merge, not direct edit."""
        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)
        cfg = {
            "mcpServers": {
                "my-mcp": {
                    "command": "node",
                    "args": [],
                    "env": {"SHARED": "old", "ONLY_EXISTING": "keep"},
                }
            },
            "tools": ["@my-mcp"],
            "allowedTools": ["@my-mcp"],
        }
        config_path = kiro_dir / "kirocrew.json"
        config_path.write_text(json.dumps(cfg))
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        monkeypatch.setattr("shutil.which", lambda x, **kw: None)

        install_called = []
        monkeypatch.setattr(
            "kiro_crew.agent.install_agent",
            lambda **kw: install_called.append(True) or config_path,
        )

        srv = McpServerInfo(
            name="my-mcp",
            command="node",
            args=[],
            env={"SHARED": "new", "ONLY_SOURCE": "added"},
        )
        result = sync_to_agent_config([srv])
        assert result is True
        assert install_called, "install_agent() should be called to re-merge config"

    def test_sync_skips_disabled_server_in_kiro_cli_add(self, tmp_path, monkeypatch) -> None:
        """Defense-in-depth: disabled servers are not registered via kiro-cli mcp add."""
        calls: list[list[str]] = []

        def mock_which(x: str, **kw: object) -> str | None:
            return "/usr/bin/kiro-cli" if x == "kiro-cli" else None

        class MockPopen:
            returncode = 0

            def __init__(self, cmd: list[str], **kwargs: object) -> None:
                calls.append(list(cmd))

            def communicate(self, **kwargs: object) -> tuple[bytes, bytes]:
                return b"", b""

        monkeypatch.setattr("shutil.which", mock_which)
        monkeypatch.setattr("subprocess.Popen", MockPopen)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)
        config_path = kiro_dir / "kirocrew.json"
        config_path.write_text(json.dumps({"mcpServers": {}, "tools": [], "allowedTools": []}))

        monkeypatch.setattr(
            "kiro_crew.agent.install_agent",
            lambda **kw: config_path,
        )

        # Source mcp.json marks this server as disabled
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            json.dumps({"mcpServers": {"disabled-srv": {"command": "x", "disabled": True}}})
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))

        disabled_srv = McpServerInfo(name="disabled-srv", command="x")
        sync_to_agent_config([disabled_srv])

        # kiro-cli mcp add should NOT have been called for the disabled server
        for call in calls:
            assert (
                "disabled-srv" not in call
            ), f"disabled server should not be registered via kiro-cli: {call}"


class TestDisabledIsCrossScope:
    """``McpServerInfo.disabled`` must reflect a ``disabled: true`` in ANY scope.

    ``/api/mcp/toggle`` writes the flag into the Kiro-global ``mcp.json``, but the
    merge only marked rows introduced from the Kiro Crew scope. A server also
    present in the agent config was therefore introduced first with
    ``disabled = False`` and stayed probeable after the user switched it off —
    and now that ``probe_server`` keys its refusal on this flag, under-reporting
    it is the whole bypass.
    """

    def setup_method(self) -> None:
        _clear_cache()

    @staticmethod
    def _env(tmp_path, monkeypatch, *, agent_spec, global_spec):
        """Agent config introduces the row; Kiro-global mcp.json disables it."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(
            json.dumps({"mcpServers": {"srv": agent_spec}}), encoding="utf-8"
        )
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        kiro_mcp = tmp_path / "kiro-mcp.json"
        kiro_mcp.write_text(json.dumps({"mcpServers": {"srv": global_spec}}), encoding="utf-8")
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (kiro_mcp,))
        monkeypatch.setattr(
            "kiro_crew.mcp_discovery._MCP_SOURCES", ((kiro_mcp, SCOPE_KIRO_GLOBAL),)
        )

    def test_kiro_global_disable_marks_an_agent_introduced_row(self, tmp_path, monkeypatch) -> None:
        self._env(
            tmp_path,
            monkeypatch,
            agent_spec={"command": "node"},
            global_spec={"command": "node", "disabled": True},
        )
        rows = {s.name: s for s in list_servers()}
        assert "srv" in rows, "the row must still be listed so it can be re-enabled"
        assert rows["srv"].disabled is True

    def test_enabled_everywhere_stays_enabled(self, tmp_path, monkeypatch) -> None:
        """Guard against the fix over-reaching into a false positive."""
        self._env(
            tmp_path,
            monkeypatch,
            agent_spec={"command": "node"},
            global_spec={"command": "node"},
        )
        rows = {s.name: s for s in list_servers()}
        assert rows["srv"].disabled is False

    @pytest.mark.asyncio
    async def test_probe_server_refuses_a_cross_scope_disabled_row(
        self, tmp_path, monkeypatch
    ) -> None:
        """End-to-end: the populated flag reaches the chokepoint and withholds
        the spawn. Without the cross-scope fix the row arrives enabled and
        ``probe_server`` would run the command."""
        self._env(
            tmp_path,
            monkeypatch,
            agent_spec={"command": "node"},
            global_spec={"command": "node", "disabled": True},
        )
        spawned = []

        async def _no_spawn(*a, **k):
            spawned.append(a)
            raise AssertionError("a disabled server must never be spawned")

        # Patch the actual spawn primitive (the stdio path is inline in
        # probe_server, not a helper), so this asserts on the real side effect
        # consent gates rather than on a stand-in.
        monkeypatch.setattr("kiro_crew.mcp_discovery.create_subprocess_limited", _no_spawn)
        row = next(s for s in list_servers() if s.name == "srv")
        out = await probe_server(row)
        assert out.status == "disabled"
        assert spawned == []

    def test_raw_scoped_key_disable_marks_the_canonical_row(self, tmp_path, monkeypatch) -> None:
        """Row names are CANONICALIZED (step 3b): ``npm:@playwright/mcp`` is
        reported as ``playwright-mcp``. Scope dicts stay keyed by the raw name,
        so matching before canonicalization misses a raw-keyed disable whenever
        the agent config retained the canonical row — the row would arrive
        enabled and probe_server would spawn it."""
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "defaults.json").write_text(
            json.dumps({"mcpServers": {"playwright-mcp": {"command": "npx"}}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        kiro_mcp = tmp_path / "kiro-mcp.json"
        kiro_mcp.write_text(
            json.dumps(
                {"mcpServers": {"npm:@playwright/mcp": {"command": "npx", "disabled": True}}}
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (kiro_mcp,))
        monkeypatch.setattr(
            "kiro_crew.mcp_discovery._MCP_SOURCES", ((kiro_mcp, SCOPE_KIRO_GLOBAL),)
        )
        rows = {s.name: s for s in list_servers()}
        assert "playwright-mcp" in rows
        assert rows["playwright-mcp"].disabled is True

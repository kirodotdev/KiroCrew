"""One answer to "is this MCP entry a remote server?" across every site that asks.

The agent spec schema requires ``command`` on every server and defaults it to
``""``, so a schema-valid remote entry carries one: a placeholder such as
``"http"``, or the empty default. The sharing rows, the gateway rewriter and the
probe must all read such an entry as remote; reading the key's presence as stdio
offers the server for a share that resolves no launch.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from handlers_mcp_coverage_helpers import agents_dir_fixture  # noqa: F401
from handlers_mcp_coverage_helpers import _payload, _request

from kiro_crew import mcp_cleanup
from kiro_crew.dashboard.handlers import mcp as mcp_mod
from kiro_crew.dashboard.handlers.gateway_footprint import _settings_rows
from kiro_crew.mcp_discovery_config import _server_from_spec
from kiro_crew.mcp_gateway.rewriter import (
    _WRAPPER_MARKER,
    _injectable_settings_servers,
    _rewrite_single_spec,
)

URL = "https://example.invalid/mcp"

#: Remote entries as agent specs in the wild spell them. The last one's command
#: resolves on PATH, so a rewriter that read it as stdio would wrap a stub that
#: launches it instead of leaving the url for the session to reach.
REMOTE_WITH_COMMAND = {
    "http-placeholder": {"command": "http", "type": "http", "url": URL},
    "http-empty-command": {"command": "", "type": "http", "url": URL},
    "untyped-empty-command": {"command": "", "url": URL},
    "sse-with-command": {"command": "anything", "type": "sse", "url": URL},
    "http-resolvable-command": {"command": sys.executable, "type": "http", "url": URL},
}


@pytest.mark.parametrize(
    ("entry", "remote"),
    [
        pytest.param(REMOTE_WITH_COMMAND["http-placeholder"], True, id="type-http-placeholder"),
        pytest.param(REMOTE_WITH_COMMAND["http-empty-command"], True, id="type-http-empty"),
        pytest.param(REMOTE_WITH_COMMAND["untyped-empty-command"], True, id="untyped-empty"),
        pytest.param(REMOTE_WITH_COMMAND["sse-with-command"], True, id="type-sse"),
        pytest.param({"url": URL}, True, id="url-only"),
        pytest.param({"command": "  ", "url": URL}, True, id="blank-command"),
        pytest.param({"type": "stdio", "command": "", "url": URL}, True, id="stdio-blank-command"),
        # A hand-edited non-string type reads as no type instead of raising.
        pytest.param({"type": ["http"], "url": URL}, True, id="list-type"),
        pytest.param({"type": {"k": "v"}, "command": "run", "url": URL}, False, id="dict-type"),
        # Unchanged readings: a real command stays a process unless the type says
        # http or sse, and nothing is remote without a url.
        pytest.param({"command": "npx", "url": URL}, False, id="untyped-real-command"),
        pytest.param(
            {"type": "stdio", "command": "run", "url": URL}, False, id="stdio-real-command"
        ),
        pytest.param({"command": "run"}, False, id="stdio"),
        pytest.param({"type": "http", "command": "run"}, False, id="no-url"),
        pytest.param({"type": "http", "url": "   "}, False, id="blank-url"),
        pytest.param({"type": "registry"}, False, id="registry-pointer"),
        pytest.param("not-a-mapping", False, id="not-a-mapping"),
    ],
)
def test_mcp_entry_is_remote(entry: Any, remote: bool) -> None:
    assert mcp_cleanup.mcp_entry_is_remote(entry) is remote


class TestSharingRows:
    @pytest.fixture
    def fresh_gateway_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pin the gateway config the row builder reads to a fresh install's."""
        import kiro_crew.config.loader as loader

        cfg = SimpleNamespace(
            mcp_gateway=SimpleNamespace(
                stub_servers=[], socket_path="", enabled=False, forward_declared_env=False
            )
        )
        monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(lambda: cfg))

    @pytest.mark.asyncio
    async def test_a_remote_entry_with_a_command_is_not_offered_for_sharing(
        self, agents_dir: Path, fresh_gateway_config: None
    ) -> None:
        """The row, and so Enable all shareable, reads the entry as HTTP."""
        servers = {**REMOTE_WITH_COMMAND, "local": {"command": "run"}}
        (agents_dir / "a.json").write_text(
            json.dumps({"name": "alpha", "mcpServers": servers}), encoding="utf-8"
        )

        resp = await mcp_mod.api_mcp_gateway_servers(_request())
        rows = {r["name"]: r for r in _payload(resp)["servers"]}

        for name in REMOTE_WITH_COMMAND:
            assert rows[name]["transport"] == "http", name
            assert rows[name]["can_stub"] is False, name
        assert rows["local"]["transport"] == "stdio"
        assert rows["local"]["can_stub"] is True


def test_the_footprint_does_not_count_a_remote_settings_server(tmp_path: Path) -> None:
    """The footprint's settings rows use the rewriter's filter, so no remote row."""
    settings = tmp_path / "mcp.json"
    servers = {**REMOTE_WITH_COMMAND, "local-mcp": {"command": "run"}}
    settings.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")

    assert set(_settings_rows(settings)) == {"local-mcp"}


class TestRewriter:
    def _rewrite(
        self,
        spec: dict,
        tmp_path: Path,
        *,
        stub_servers: frozenset[str],
        inject_servers: dict[str, Any] | None = None,
    ) -> tuple[dict, int]:
        return _rewrite_single_spec(
            spec,
            stubs_dir=tmp_path / "stubs",
            socket_path=tmp_path / "gw.sock",
            work_dir=tmp_path / "wd",
            sandbox_mode="auto",
            approval_mode="interactive",
            stub_servers=stub_servers,
            inject_servers=inject_servers,
        )

    def test_a_listed_remote_entry_passes_through_unwrapped(self, tmp_path: Path) -> None:
        """Opting a remote entry in leaves it as declared; a stdio one is still wrapped."""
        servers = {**REMOTE_WITH_COMMAND, "local-mcp": {"command": sys.executable}}
        spec = {"name": "alpha", "mcpServers": servers, "tools": ["*"]}

        new_spec, wrapped = self._rewrite(spec, tmp_path, stub_servers=frozenset(servers))

        assert wrapped == 1
        assert new_spec["mcpServers"]["local-mcp"].get(_WRAPPER_MARKER) is True
        for name, entry in REMOTE_WITH_COMMAND.items():
            assert new_spec["mcpServers"][name] == entry, name

    def test_a_remote_settings_server_is_never_injected(self, tmp_path: Path) -> None:
        """Neither the settings filter nor the per-agent injection wraps one."""
        settings = {"mcpServers": dict(REMOTE_WITH_COMMAND)}
        listed = frozenset(REMOTE_WITH_COMMAND)
        assert _injectable_settings_servers(settings, listed) == {}

        new_spec, wrapped = self._rewrite(
            {"name": "alpha", "mcpServers": {}, "tools": ["*"]},
            tmp_path,
            stub_servers=listed,
            inject_servers=dict(REMOTE_WITH_COMMAND),
        )
        assert wrapped == 0
        assert not set(REMOTE_WITH_COMMAND) & set(new_spec.get("mcpServers", {}))

    def test_a_malformed_type_does_not_abort_the_rewrite(self, tmp_path: Path) -> None:
        """A list ``type`` is read as no type, so the other servers are still wrapped."""
        servers = {
            "odd-type": {"command": "http", "type": ["http"], "url": URL},
            "local-mcp": {"command": sys.executable},
        }
        # Grant only local-mcp: an ``http`` executable on PATH would get a granted
        # odd-type wrapped too. Its malformed type is still read, before the grant.
        spec = {"name": "alpha", "mcpServers": servers, "tools": ["@local-mcp"]}

        new_spec, wrapped = self._rewrite(spec, tmp_path, stub_servers=frozenset(servers))

        assert wrapped == 1
        assert new_spec["mcpServers"]["local-mcp"].get(_WRAPPER_MARKER) is True


class TestProbeTransport:
    @pytest.mark.parametrize("name", sorted(REMOTE_WITH_COMMAND))
    def test_a_listed_remote_entry_is_probed_as_remote(self, name: str) -> None:
        info = _server_from_spec(name, REMOTE_WITH_COMMAND[name], "agent")
        assert info.is_remote is True

    def test_an_explicit_stdio_type_is_probed_locally(self) -> None:
        info = _server_from_spec("s", {"type": "stdio", "command": "run", "url": URL}, "agent")
        assert info.is_remote is False

    def test_the_declared_type_is_not_serialized(self) -> None:
        info = _server_from_spec("s", REMOTE_WITH_COMMAND["http-placeholder"], "agent")
        assert "declaredType" not in info.to_dict()
        assert "declared_type" not in info.to_dict()

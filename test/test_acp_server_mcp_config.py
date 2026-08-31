"""Per-session ACP MCP config parsing: stdio accepted, others rejected."""

from __future__ import annotations

import pytest

from kiro_crew.acp_server.mcp_config import (
    McpConfigError,
    StdioMcpServer,
    parse_mcp_servers,
)


class TestParseMcpServers:
    def test_absent_is_empty(self) -> None:
        assert parse_mcp_servers(None) == []

    def test_empty_list_is_empty(self) -> None:
        assert parse_mcp_servers([]) == []

    def test_minimal_stdio_server(self) -> None:
        servers = parse_mcp_servers([{"name": "fs", "command": "/usr/bin/mcp-fs"}])
        assert servers == [StdioMcpServer(name="fs", command="/usr/bin/mcp-fs")]

    def test_stdio_with_args_and_env_array(self) -> None:
        servers = parse_mcp_servers(
            [
                {
                    "name": "fs",
                    "command": "mcp-fs",
                    "args": ["--root", "/repo"],
                    "env": [{"name": "TOKEN", "value": "secret"}],
                }
            ]
        )
        assert servers[0].args == ["--root", "/repo"]
        assert servers[0].env == {"TOKEN": "secret"}

    def test_env_accepts_plain_object(self) -> None:
        servers = parse_mcp_servers(
            [{"name": "fs", "command": "mcp-fs", "env": {"A": "1", "B": "2"}}]
        )
        assert servers[0].env == {"A": "1", "B": "2"}

    def test_explicit_stdio_type(self) -> None:
        servers = parse_mcp_servers([{"name": "fs", "type": "stdio", "command": "mcp-fs"}])
        assert servers[0].command == "mcp-fs"

    # ── rejections ──

    def test_non_list_rejected(self) -> None:
        with pytest.raises(McpConfigError):
            parse_mcp_servers({"name": "fs", "command": "x"})

    def test_non_object_entry_rejected(self) -> None:
        with pytest.raises(McpConfigError):
            parse_mcp_servers(["not-an-object"])

    def test_http_url_transport_rejected(self) -> None:
        with pytest.raises(McpConfigError, match="unsupported transport"):
            parse_mcp_servers([{"name": "remote", "url": "https://mcp.example"}])

    def test_explicit_http_type_rejected(self) -> None:
        with pytest.raises(McpConfigError, match="unsupported transport"):
            parse_mcp_servers([{"name": "remote", "type": "http", "url": "https://mcp.example"}])

    def test_sse_type_rejected(self) -> None:
        with pytest.raises(McpConfigError, match="unsupported transport"):
            parse_mcp_servers([{"name": "remote", "type": "sse", "url": "https://x"}])

    def test_missing_command_and_type_rejected(self) -> None:
        with pytest.raises(McpConfigError):
            parse_mcp_servers([{"name": "fs"}])

    def test_missing_name_rejected(self) -> None:
        with pytest.raises(McpConfigError, match="name"):
            parse_mcp_servers([{"command": "mcp-fs"}])

    def test_empty_command_rejected(self) -> None:
        with pytest.raises(McpConfigError, match="command"):
            parse_mcp_servers([{"name": "fs", "command": ""}])

    def test_duplicate_name_rejected(self) -> None:
        with pytest.raises(McpConfigError, match="duplicate"):
            parse_mcp_servers(
                [
                    {"name": "fs", "command": "a"},
                    {"name": "fs", "command": "b"},
                ]
            )

    def test_non_string_args_rejected(self) -> None:
        with pytest.raises(McpConfigError, match="args"):
            parse_mcp_servers([{"name": "fs", "command": "a", "args": [1, 2]}])

    def test_malformed_env_entry_rejected(self) -> None:
        with pytest.raises(McpConfigError, match="env"):
            parse_mcp_servers([{"name": "fs", "command": "a", "env": [{"name": "K"}]}])

    def test_rejection_message_does_not_leak_env_value(self) -> None:
        # The message names the field, never a secret value.
        with pytest.raises(McpConfigError) as exc:
            parse_mcp_servers(
                [{"name": "fs", "command": "a", "env": [{"name": "K", "value": 123}]}]
            )
        assert "123" not in str(exc.value)

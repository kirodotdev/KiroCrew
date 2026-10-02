"""Neutral Gateway session-MCP parsing and canonical shaping."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kiro_crew.gateway.session_mcp import (
    MAX_ARG_CHARS,
    MAX_ARGS_PER_SERVER,
    MAX_COMMAND_CHARS,
    MAX_ENV_NAME_CHARS,
    MAX_ENV_VALUE_CHARS,
    MAX_ENV_VARS_PER_SERVER,
    MAX_SERVER_NAME_CHARS,
    McpConfigError,
    StdioMcpServer,
    parse_mcp_servers,
    servers_to_session_dicts,
)
from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS


class TestParseSessionMcpServers:
    def test_absent_and_empty_are_empty(self) -> None:
        assert parse_mcp_servers(None) == []
        assert parse_mcp_servers([]) == []

    def test_stdio_server_is_canonicalized(self) -> None:
        servers = parse_mcp_servers(
            [
                {
                    "name": "filesystem",
                    "command": "mcp-fs",
                    "args": ["--root", "/repo"],
                    "env": {"MODE": "readonly"},
                }
            ]
        )
        assert servers == [
            StdioMcpServer(
                name="filesystem",
                command="mcp-fs",
                args=["--root", "/repo"],
                env={"MODE": "readonly"},
            )
        ]
        assert servers_to_session_dicts(servers) == [
            {
                "name": "filesystem",
                "command": "mcp-fs",
                "args": ["--root", "/repo"],
                "env": [{"name": "MODE", "value": "readonly"}],
            }
        ]

    def test_http_transport_is_rejected(self) -> None:
        with pytest.raises(McpConfigError, match="unsupported transport"):
            parse_mcp_servers([{"name": "remote", "url": "https://mcp.example"}])

    def test_duplicate_name_is_case_insensitive(self) -> None:
        with pytest.raises(McpConfigError, match="duplicate"):
            parse_mcp_servers(
                [
                    {"name": "filesystem", "command": "a"},
                    {"name": "FileSystem", "command": "b"},
                ]
            )

    @pytest.mark.parametrize("name", KIROCREW_BIN_MCP_SERVERS)
    @pytest.mark.parametrize("transform", [str.lower, str.upper])
    def test_managed_name_is_reserved(self, name: str, transform: Callable[[str], str]) -> None:
        with pytest.raises(McpConfigError, match="reserved server name"):
            parse_mcp_servers([{"name": transform(name), "command": "untrusted"}])

    def test_error_does_not_repeat_an_env_value(self) -> None:
        with pytest.raises(McpConfigError) as exc:
            parse_mcp_servers(
                [
                    {
                        "name": "filesystem",
                        "command": "mcp-fs",
                        "env": [{"name": "ACCESS", "value": 123456789}],
                    }
                ]
            )
        assert "123456789" not in str(exc.value)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("name", "n" * (MAX_SERVER_NAME_CHARS + 1)),
            ("command", "c" * (MAX_COMMAND_CHARS + 1)),
        ],
    )
    def test_scalar_fields_are_bounded(self, field: str, value: str) -> None:
        entry = {"name": "server", "command": "cmd", field: value}
        with pytest.raises(McpConfigError, match="too long"):
            parse_mcp_servers([entry])

    @pytest.mark.parametrize(
        "args",
        [
            ["x"] * (MAX_ARGS_PER_SERVER + 1),
            ["x" * (MAX_ARG_CHARS + 1)],
        ],
    )
    def test_arguments_are_bounded(self, args: list[str]) -> None:
        with pytest.raises(McpConfigError, match="too many arguments|argument is too long"):
            parse_mcp_servers([{"name": "server", "command": "cmd", "args": args}])

    @pytest.mark.parametrize(
        "env",
        [
            {f"KEY_{index}": "x" for index in range(MAX_ENV_VARS_PER_SERVER + 1)},
            {"x" * (MAX_ENV_NAME_CHARS + 1): "value"},
            {"KEY": "x" * (MAX_ENV_VALUE_CHARS + 1)},
            [
                {"name": f"KEY_{index}", "value": "x"}
                for index in range(MAX_ENV_VARS_PER_SERVER + 1)
            ],
        ],
    )
    def test_environment_is_bounded(self, env: object) -> None:
        with pytest.raises(McpConfigError, match="too many environment|entry is too long"):
            parse_mcp_servers([{"name": "server", "command": "cmd", "env": env}])

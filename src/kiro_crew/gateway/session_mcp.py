"""Validation and canonical shaping for client-supplied session MCP servers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS

_RESERVED_SERVER_NAMES = frozenset(name.casefold() for name in KIROCREW_BIN_MCP_SERVERS)
TRANSPORT_STDIO = "stdio"
MAX_SERVER_NAME_CHARS = 128
MAX_COMMAND_CHARS = 4096
MAX_ARGS_PER_SERVER = 128
MAX_ARG_CHARS = 4096
MAX_ENV_VARS_PER_SERVER = 128
MAX_ENV_NAME_CHARS = 256
MAX_ENV_VALUE_CHARS = 8192
MAX_SERVERS_PER_SESSION = 16


class McpConfigError(ValueError):
    """A session MCP entry is malformed or uses an unsupported transport."""


@dataclass(frozen=True)
class StdioMcpServer:
    """A validated stdio MCP server definition scoped to one session."""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

    def to_session_dict(self) -> dict[str, Any]:
        """Return the canonical session-level MCP wire shape."""
        return {
            "name": self.name,
            "command": self.command,
            "args": list(self.args),
            "env": [{"name": key, "value": value} for key, value in self.env.items()],
        }


def servers_to_session_dicts(servers: list[StdioMcpServer]) -> list[dict[str, Any]]:
    """Return canonical session-level dictionaries for validated servers."""
    return [server.to_session_dict() for server in servers]


def parse_mcp_servers(raw: Any) -> list[StdioMcpServer]:
    """Validate a client-supplied MCP array without granting any server."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise McpConfigError("servers must be an array")
    if len(raw) > MAX_SERVERS_PER_SESSION:
        raise McpConfigError(
            f"too many MCP servers: {len(raw)} requested, "
            f"at most {MAX_SERVERS_PER_SESSION} per session"
        )

    servers: list[StdioMcpServer] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise McpConfigError(f"servers[{index}] must be an object")
        transport = _transport_of(entry, index)
        if transport != TRANSPORT_STDIO:
            raise McpConfigError(
                f"servers[{index}]: unsupported transport {transport!r}; "
                "only the stdio transport is supported"
            )

        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise McpConfigError(f"servers[{index}]: 'name' must be a non-empty string")
        if len(name) > MAX_SERVER_NAME_CHARS:
            raise McpConfigError(f"servers[{index}]: 'name' is too long")
        canonical_name = name.casefold()
        if canonical_name in _RESERVED_SERVER_NAMES or ":" in name:
            raise McpConfigError(f"servers[{index}]: reserved server name {name!r}")
        if canonical_name in seen:
            raise McpConfigError(f"servers[{index}]: duplicate server name {name!r}")
        seen.add(canonical_name)

        command = entry.get("command")
        if not isinstance(command, str) or not command:
            raise McpConfigError(f"servers[{index}] ({name}): 'command' must be a non-empty string")
        if len(command) > MAX_COMMAND_CHARS:
            raise McpConfigError(f"servers[{index}] ({name}): 'command' is too long")
        servers.append(
            StdioMcpServer(
                name=name,
                command=command,
                args=_parse_args(entry.get("args"), index, name),
                env=_parse_env(entry.get("env"), index, name),
            )
        )
    return servers


def _transport_of(entry: dict[str, Any], index: int) -> str:
    declared = entry.get("type")
    if isinstance(declared, str) and declared:
        return declared.strip().lower()
    if "command" in entry:
        return TRANSPORT_STDIO
    if "url" in entry:
        return "http"
    raise McpConfigError(f"servers[{index}]: must declare a 'command' or a 'type'")


def _parse_args(raw: Any, index: int, name: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(arg, str) for arg in raw):
        raise McpConfigError(f"servers[{index}] ({name}): 'args' must be an array of strings")
    if len(raw) > MAX_ARGS_PER_SERVER:
        raise McpConfigError(f"servers[{index}] ({name}): too many arguments")
    if any(len(arg) > MAX_ARG_CHARS for arg in raw):
        raise McpConfigError(f"servers[{index}] ({name}): an argument is too long")
    return list(raw)


def _parse_env(raw: Any, index: int, name: str) -> dict[str, str]:
    if raw is None:
        return {}
    env: dict[str, str] = {}
    items: Iterable[tuple[Any, Any]]
    if isinstance(raw, dict):
        if len(raw) > MAX_ENV_VARS_PER_SERVER:
            raise McpConfigError(f"servers[{index}] ({name}): too many environment entries")
        items = raw.items()
    elif isinstance(raw, list):
        if len(raw) > MAX_ENV_VARS_PER_SERVER:
            raise McpConfigError(f"servers[{index}] ({name}): too many environment entries")
        pairs: list[tuple[Any, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                raise McpConfigError(
                    f"servers[{index}] ({name}): each 'env' entry must be an object"
                )
            pairs.append((item.get("name"), item.get("value")))
        items = pairs
    else:
        raise McpConfigError(
            f"servers[{index}] ({name}): 'env' must be an object or an array of pairs"
        )
    for key, value in items:
        if not isinstance(key, str) or not isinstance(value, str):
            raise McpConfigError(f"servers[{index}] ({name}): 'env' values must be strings")
        if len(key) > MAX_ENV_NAME_CHARS or len(value) > MAX_ENV_VALUE_CHARS:
            raise McpConfigError(f"servers[{index}] ({name}): an 'env' entry is too long")
        env[key] = value
    return env

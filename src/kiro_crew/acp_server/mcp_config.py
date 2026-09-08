"""Per-ACP-session MCP server configuration: parse, validate, reject.

An ACP client passes ``mcpServers`` in ``session/new`` (and, for Kiro Crew,
``session/load`` / ``session/resume``). Baseline ACP v1 requires the **stdio**
transport — ``command`` + ``args`` + ``env`` — and Kiro Crew supports only that
for now. HTTP and SSE transports are deferred, so they are rejected explicitly
with ``-32602`` rather than silently ignored: a client that asked for a server
it will not get must be told, not left believing a tool is available.

This module is pure validation/shaping. It does not spawn anything; process
supervision is wired separately through the gateway's MCP lifecycle. Keeping the
parse here makes the wire contract testable without a running gateway and gives
the dispatch layer one place to turn an untrusted ``mcpServers`` array into a
typed, session-scoped config or a precise error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The only MCP transport Kiro Crew serves today. Others parse cleanly enough to
# be *named* in the rejection message, but never construct a config.
TRANSPORT_STDIO = "stdio"

# Upper bound on client-supplied stdio MCP servers per ACP session. Each server
# is a real sandboxed child + a supervised proxy socket, so an unbounded list is
# a resource-exhaustion / slow-DoS vector on session setup. A generous ceiling
# that no legitimate editor config approaches.
MAX_SERVERS_PER_SESSION = 16


class McpConfigError(ValueError):
    """An ``mcpServers`` entry was malformed or used an unsupported transport.

    The dispatch layer turns this into a JSON-RPC ``-32602 Invalid params``. The
    message is safe to return to the client — it names the offending index and
    field, never a secret value.
    """


@dataclass(frozen=True)
class StdioMcpServer:
    """A validated ACP stdio MCP server definition, scoped to one session."""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

    def to_acp_dict(self) -> dict[str, Any]:
        """Re-serialize to the canonical ACP v1 stdio ``mcpServers`` entry.

        This is the single normalized shape that crosses every boundary after
        validation: the daemon stores it per slot, and the provider hands it to
        kiro-cli's ``session/new``. ``env`` is emitted in the ACP
        array-of-``{name, value}`` form (never a bare object), so the wire shape
        is stable regardless of which form the editor originally sent.
        """
        return {
            "name": self.name,
            "command": self.command,
            "args": list(self.args),
            "env": [{"name": key, "value": value} for key, value in self.env.items()],
        }


def servers_to_acp_dicts(servers: list[StdioMcpServer]) -> list[dict[str, Any]]:
    """Canonical ACP ``mcpServers`` array for a validated server list."""
    return [server.to_acp_dict() for server in servers]


def parse_mcp_servers(raw: Any) -> list[StdioMcpServer]:
    """Validate an ACP ``mcpServers`` value into stdio server configs.

    ``None`` (the field is absent) yields an empty list — a session with no
    client-supplied MCP servers is valid. A present value MUST be an array of
    objects; anything else, an unsupported transport, a missing ``command``, or
    a duplicate ``name`` raises :class:`McpConfigError`.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise McpConfigError("mcpServers must be an array")
    if len(raw) > MAX_SERVERS_PER_SESSION:
        raise McpConfigError(
            f"too many MCP servers: {len(raw)} requested, "
            f"at most {MAX_SERVERS_PER_SESSION} per session"
        )

    servers: list[StdioMcpServer] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise McpConfigError(f"mcpServers[{index}] must be an object")

        transport = _transport_of(entry, index)
        if transport != TRANSPORT_STDIO:
            raise McpConfigError(
                f"mcpServers[{index}]: unsupported transport {transport!r}; "
                "only the stdio transport is supported"
            )

        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise McpConfigError(f"mcpServers[{index}]: 'name' must be a non-empty string")
        if name in seen:
            raise McpConfigError(f"mcpServers[{index}]: duplicate server name {name!r}")
        seen.add(name)

        command = entry.get("command")
        if not isinstance(command, str) or not command:
            raise McpConfigError(
                f"mcpServers[{index}] ({name}): 'command' must be a non-empty string"
            )

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
    """Classify an MCP entry's transport.

    An explicit ``type`` wins. Otherwise a ``command`` implies stdio and a
    ``url`` implies an HTTP/SSE transport; an entry with neither cannot be
    classified and is rejected.
    """
    declared = entry.get("type")
    if isinstance(declared, str) and declared:
        return declared.strip().lower()
    if "command" in entry:
        return TRANSPORT_STDIO
    if "url" in entry:
        # No explicit type but a url — an HTTP/SSE server. Named generically so
        # the rejection is honest without guessing the exact sub-variant.
        return "http"
    raise McpConfigError(f"mcpServers[{index}]: must declare a 'command' (stdio) or a 'type'")


def _parse_args(raw: Any, index: int, name: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(a, str) for a in raw):
        raise McpConfigError(f"mcpServers[{index}] ({name}): 'args' must be an array of strings")
    return list(raw)


def _parse_env(raw: Any, index: int, name: str) -> dict[str, str]:
    """Accept the ACP array-of-``{name,value}`` shape or a plain object."""
    if raw is None:
        return {}
    env: dict[str, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise McpConfigError(f"mcpServers[{index}] ({name}): 'env' values must be strings")
            env[key] = value
        return env
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                raise McpConfigError(
                    f"mcpServers[{index}] ({name}): each 'env' entry must be an object"
                )
            key = item.get("name")
            value = item.get("value")
            if not isinstance(key, str) or not isinstance(value, str):
                raise McpConfigError(
                    f"mcpServers[{index}] ({name}): 'env' entries need string " "'name' and 'value'"
                )
            env[key] = value
        return env
    raise McpConfigError(
        f"mcpServers[{index}] ({name}): 'env' must be an object or an array of "
        "{{name, value}} pairs"
    )

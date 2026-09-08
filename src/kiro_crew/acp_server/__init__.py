"""Kiro Crew as an ACP agent: serve the protocol to an editor over stdio."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.acp_server.gateway import GatewayServices, make_prompt_handler  # noqa: F401
    from kiro_crew.acp_server.http_backend import (  # noqa: F401
        AcpGatewayError,
        HttpGatewayBackend,
        default_base_url,
        default_secret_path,
    )
    from kiro_crew.acp_server.locations import extract_tool_locations  # noqa: F401
    from kiro_crew.acp_server.mcp_config import (  # noqa: F401
        McpConfigError,
        StdioMcpServer,
        parse_mcp_servers,
        servers_to_acp_dicts,
    )
    from kiro_crew.acp_server.mcp_supervisor import (  # noqa: F401
        McpSpawnError,
        SessionMcpSupervisor,
    )
    from kiro_crew.acp_server.server import (  # noqa: F401
        AcpAgentServer,
        PromptHandler,
        PromptRequest,
        SessionBackend,
        SessionSink,
        prompt_blocks_to_text,
    )
    from kiro_crew.acp_server.transport import AcpServerError, AgentTransport  # noqa: F401

_EXPORTS = {
    "AcpAgentServer": ("server", "AcpAgentServer"),
    "AcpGatewayError": ("http_backend", "AcpGatewayError"),
    "AcpServerError": ("transport", "AcpServerError"),
    "AgentTransport": ("transport", "AgentTransport"),
    "GatewayServices": ("gateway", "GatewayServices"),
    "HttpGatewayBackend": ("http_backend", "HttpGatewayBackend"),
    "McpConfigError": ("mcp_config", "McpConfigError"),
    "McpSpawnError": ("mcp_supervisor", "McpSpawnError"),
    "PromptHandler": ("server", "PromptHandler"),
    "PromptRequest": ("server", "PromptRequest"),
    "SessionBackend": ("server", "SessionBackend"),
    "SessionMcpSupervisor": ("mcp_supervisor", "SessionMcpSupervisor"),
    "SessionSink": ("server", "SessionSink"),
    "StdioMcpServer": ("mcp_config", "StdioMcpServer"),
    "default_base_url": ("http_backend", "default_base_url"),
    "default_secret_path": ("http_backend", "default_secret_path"),
    "extract_tool_locations": ("locations", "extract_tool_locations"),
    "make_prompt_handler": ("gateway", "make_prompt_handler"),
    "parse_mcp_servers": ("mcp_config", "parse_mcp_servers"),
    "prompt_blocks_to_text": ("server", "prompt_blocks_to_text"),
    "servers_to_acp_dicts": ("mcp_config", "servers_to_acp_dicts"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    """Load public server symbols only when an ACP caller requests them."""
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, symbol_name = target
    value = getattr(import_module(f"{__name__}.{module_name}"), symbol_name)
    globals()[name] = value
    return value

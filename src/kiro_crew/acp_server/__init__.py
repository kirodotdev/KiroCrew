"""Kiro Crew as an ACP agent: serve the protocol to an editor over stdio."""

from __future__ import annotations

from kiro_crew.acp_server.gateway import GatewayServices, make_prompt_handler
from kiro_crew.acp_server.http_backend import (
    AcpGatewayError,
    HttpGatewayBackend,
    default_base_url,
    default_secret_path,
)
from kiro_crew.acp_server.locations import extract_tool_locations
from kiro_crew.acp_server.mcp_config import (
    McpConfigError,
    StdioMcpServer,
    parse_mcp_servers,
    servers_to_acp_dicts,
)
from kiro_crew.acp_server.mcp_supervisor import McpSpawnError, SessionMcpSupervisor
from kiro_crew.acp_server.server import (
    AcpAgentServer,
    PromptHandler,
    PromptRequest,
    SessionBackend,
    SessionSink,
    prompt_blocks_to_text,
)
from kiro_crew.acp_server.transport import AcpServerError, AgentTransport

__all__ = [
    "AcpAgentServer",
    "AcpGatewayError",
    "GatewayServices",
    "AcpServerError",
    "AgentTransport",
    "HttpGatewayBackend",
    "McpConfigError",
    "McpSpawnError",
    "PromptHandler",
    "PromptRequest",
    "SessionBackend",
    "SessionMcpSupervisor",
    "SessionSink",
    "StdioMcpServer",
    "default_base_url",
    "default_secret_path",
    "extract_tool_locations",
    "make_prompt_handler",
    "parse_mcp_servers",
    "prompt_blocks_to_text",
    "servers_to_acp_dicts",
]

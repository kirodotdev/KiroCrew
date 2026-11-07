"""Agent Plugins (agent-plugins.org 1.0.0) client: discovery and validation.

Kiro Crew reads plugins from the folder Kiro IDE, kiro-cli and KAS install into
and validates each one against the spec. This package only reads; turning a
plugin on, and handing its skills and MCP servers to sessions, are separate
steps. Design: ``docs/request-for-change/rfc-agent-plugins-standard.md``; spec:
``docs/system-specs/modules/agent-plugins.md``.
"""

from __future__ import annotations

from kiro_crew.agent_plugins.discovery import discover_plugins, inspect_plugin, installed_names
from kiro_crew.agent_plugins.model import (
    Issue,
    Manifest,
    McpServer,
    PluginRecord,
    PluginState,
    RemoteServer,
    Skill,
    StdioServer,
)
from kiro_crew.agent_plugins.paths import plugin_data_dir, powers_home

__all__ = [
    "Issue",
    "Manifest",
    "McpServer",
    "PluginRecord",
    "PluginState",
    "RemoteServer",
    "Skill",
    "StdioServer",
    "discover_plugins",
    "inspect_plugin",
    "installed_names",
    "plugin_data_dir",
    "powers_home",
]

"""OpenCode's dormant mirror and its bounded MCP projection candidate.

An MCP descriptor is not an execution gate. OpenCode's native named ``allow``
rules can skip ``session/request_permission``, so neither an ``ask`` default nor
the Claude-specific ``permission_surface_owned`` flag establishes Crew's control
over its tools. The mirror therefore withholds its whole array unconditionally.

The separately callable candidate builder preserves the part that can be checked
without starting a harness: only Crew's authoritative core/cron invocations, only
whole-server grants, and no silently dropped per-tool restriction. It is NOT wired
to the mirror. An admission change needs independent enforcement proof before
these descriptors can be handed to a real session.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

from kiro_crew.acp import session_mcp
from kiro_crew.acp_backends import ACP_BACKEND_OPENCODE
from kiro_crew.providers.mirrors.base import (
    AgentConfigMirror,
    Concern,
    Disposition,
    Ruling,
)

# OpenCode joins its normalized server name and tool name with an underscore.
# Preserve the original descriptor name; this normalization is for detecting
# ambiguous wire prefixes, not for inventing a different governance identity.
_MCP_NAME_SANITIZE = re.compile(r"[^A-Za-z0-9_-]")
_PORTABLE_STDIO_KEYS = ("name", "command", "args", "env")
_D = Disposition


def opencode_mcp_wire_name(name: str) -> str:
    """The shared server-name normalization for candidate and identity checks."""
    return _MCP_NAME_SANITIZE.sub("_", name)


def _without_ambiguous_names(
    servers: Sequence[dict[str, Any]], reserved_server_names: Collection[str]
) -> list[dict[str, Any]]:
    """Withhold every descriptor whose wire prefix has another explanation.

    Exact normalization collisions and prefix overlaps are both ambiguous:
    ``a_b_tool`` can be tool ``b_tool`` on server ``a`` or tool ``tool`` on
    server ``a_b``. Names supplied by another injection source reserve that
    namespace too. This only checks the supplied names, not native OpenCode
    configuration; it cannot certify a session's permission identities.
    """
    names = [entry["name"] for entry in servers]
    names.extend(reserved_server_names)
    prefixes = [f"{opencode_mcp_wire_name(name)}_" for name in names]
    out: list[dict[str, Any]] = []
    for index, entry in enumerate(servers):
        prefix = prefixes[index]
        if any(
            other.startswith(prefix) or prefix.startswith(other)
            for other_index, other in enumerate(prefixes)
            if other_index != index
        ):
            continue
        out.append(entry)
    return out


def project_managed_servers(
    spec: object,
    managed_entries: Mapping[str, Any],
    *,
    reserved_server_names: Collection[str] = (),
) -> list[dict[str, Any]]:
    """Pure candidate projection, with no claim that the result is safe to mount.

    ``managed_entries`` supplies live invocations resolved by Crew, never commands
    copied from the hand-editable spec. Only the existing session-MCP control-plane
    set is eligible: computer use, opt-in sets, app and user servers stay withheld.
    The spec still owns exposure and restrictions. An absent/unreadable spec grants
    nothing here, and a per-tool grant or a nonempty/malformed ``disabledTools``
    list withholds the entire server because the portable array cannot narrow it.
    """
    if not isinstance(spec, dict):
        return []
    servers = spec.get("mcpServers")
    tools = spec.get("tools")
    if not isinstance(servers, dict) or not isinstance(tools, list):
        return []
    exposed = {tool for tool in tools if isinstance(tool, str)}
    out: list[dict[str, Any]] = []
    for name in sorted(session_mcp._CONTROL_PLANE_SERVERS):
        entry = servers.get(name)
        if not isinstance(entry, dict) or (f"@{name}" not in exposed and "*" not in exposed):
            continue
        if entry.get("disabled", False) is not False or entry.get("enabled", True) is not True:
            continue
        if "disabledTools" in entry and entry["disabledTools"] != []:
            continue
        shaped = session_mcp.acp_server_element(name, managed_entries.get(name))
        if shaped is None or shaped.get("type") != "stdio":
            continue
        out.append({key: shaped[key] for key in _PORTABLE_STDIO_KEYS})
    return _without_ambiguous_names(out, reserved_server_names)


def candidate_session_servers(
    agent: str | None,
    *,
    work_dir: str | Path | None = None,
    reserved_server_names: Collection[str] = (),
) -> list[dict[str, Any]]:
    """Resolve a candidate off-loop using the shared bounded, project-first reader.

    Kept outside ``session_params`` deliberately: a caller cannot turn this
    diagnostic projection into an admitted backend by setting an ownership bool.
    No OpenCode config, user server command, or native permission rule is copied.
    """
    if not agent:
        return []
    spec = session_mcp._agent_spec_for(agent, work_dir)
    if spec is None:
        return []
    managed = {
        name: session_mcp.managed_mcp_spec_entry(name)
        for name in session_mcp._CONTROL_PLANE_SERVERS
    }
    return project_managed_servers(spec, managed, reserved_server_names=reserved_server_names)


class OpenCodeMirror(AgentConfigMirror):
    """Declare the projection gaps without claiming a tool-enforcement guarantee."""

    backend = ACP_BACKEND_OPENCODE

    def rulings(self) -> Mapping[Concern, Ruling]:
        return {
            Concern.MCP_SERVERS: Ruling(
                _D.WITHHELD,
                "OpenCode is dormant until every tool execution can be held at Crew's "
                "gate. The managed-only candidate projection is not delivered by this "
                "mirror, even when a caller passes permission_surface_owned=True",
            ),
            Concern.TOOL_ALLOWLIST: Ruling(
                _D.WITHHELD,
                "no MCP array is delivered. The candidate requires an explicit whole-server "
                "grant or the spec's bare * grant; a per-tool grant cannot safely mount "
                "the whole server, and native OpenCode tools are outside this projection",
            ),
            Concern.DENIED_TOOLS: Ruling(
                _D.WITHHELD,
                "no MCP array is delivered. The candidate withholds a whole server for "
                "nonempty or malformed disabledTools instead of dropping a restriction",
            ),
            Concern.AUTO_APPROVE: Ruling(
                _D.WITHHELD,
                "native pre-approval can skip session/request_permission and Crew's gate; "
                "autoApprove and allowedTools are never translated into native grants",
            ),
            Concern.MODEL: Ruling(
                _D.WITHHELD,
                "no model is projected while this backend is dormant; admitting the "
                "transport must also bind its own advertised model namespace",
            ),
            Concern.MODEL_ALLOWLIST: Ruling(
                _D.WITHHELD,
                "Kiro's model catalog is not an OpenCode entitlement list; model "
                "capability enrollment remains deferred with backend admission",
            ),
            Concern.PERMISSION_MODE: Ruling(
                _D.NO_CHANNEL,
                "an ask default does not override named native allow rules. No verified "
                "host-owned execution gate is installed, so an ownership bool cannot "
                "certify that every tool call reaches Crew before execution",
                channel="OpenCode's execution-time permission or plugin boundary, with "
                "verified precedence and fail-closed delivery to Crew's PreToolUse gate",
            ),
            Concern.PROMPT: Ruling(
                _D.WITHHELD,
                "the shared prompt context pipeline owns agent instructions; this mirror "
                "does not create a second native prompt file",
            ),
            Concern.RESOURCES: Ruling(
                _D.WITHHELD,
                "the shared context pipeline owns resource delivery, not a second native "
                "OpenCode configuration projection",
            ),
            Concern.HOOKS: Ruling(
                _D.WITHHELD,
                "Kiro-native agent hooks are not copied into OpenCode configuration; "
                "no speculative translation is treated as proof of Crew's tool gate",
            ),
        }

    def session_params(self, agent: str | None, **kwargs: Any) -> dict[str, Any]:
        """Withhold tools unconditionally, without I/O or a caller-controlled bypass."""
        return {"mcpServers": []}

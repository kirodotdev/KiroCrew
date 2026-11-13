"""One table of the agent spec fields, read by the offline checker and the JSON Schema.

``kirocrew agent validate`` and ``kirocrew agent validate --print-schema`` both
answer from :data:`FIELDS`, so a field added to the table is checked and
published together and the two cannot disagree. The table lists the top-level
fields kiro-cli documents (``docs/reference/kiro-cli/custom-agents``) plus the
few Kiro Crew itself writes; it is not a claim that kiro-cli accepts nothing
else, which is why :func:`check_spec` can be told to let unknown fields pass.

Pure functions over a parsed spec dict: no file, config or network access. The
caller supplies the server names that count as configured, so the same checks
run in a test and on a host.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from kiro_crew.agent_sdk.mcp_refs import RESERVED_TOOL_NAMESPACES, parse_tools_refs
from kiro_crew.mcp_cleanup import mcp_entry_is_registry_governed

#: Value shapes a field can take. ``any`` states nothing about the value.
KINDS = ("string", "bool", "object", "string_list", "resources", "hooks", "any")


@dataclass(frozen=True)
class SpecField:
    """One top-level spec field: its key, the shape of its value and what it does."""

    name: str
    kind: str
    doc: str


FIELDS: tuple[SpecField, ...] = (
    SpecField("$schema", "string", "URL of the JSON Schema that describes this file."),
    SpecField("name", "string", "Agent identifier; defaults to the file name."),
    SpecField("description", "string", "Human-readable purpose."),
    SpecField("prompt", "string", "System prompt, inline or a file:// URI."),
    SpecField("mcpServers", "object", "MCP servers the agent defines, keyed by server name."),
    SpecField(
        "tools", "string_list", "Tools the agent can use; @server and @server/tool name MCP."
    ),
    SpecField("excludedTools", "string_list", "Tools removed even when tools allows them."),
    SpecField("toolAliases", "object", "Tool name remapping."),
    SpecField("allowedTools", "string_list", "Tools that run without a permission prompt."),
    SpecField("permissions", "object", "Inline capability-based allow and deny rules."),
    SpecField("toolsSettings", "object", "Per-tool configuration."),
    SpecField("resources", "resources", "Files, skills and knowledge bases."),
    SpecField("hooks", "hooks", "Lifecycle commands."),
    SpecField("includeMcpJson", "bool", "Also mount the servers of the mcp.json files."),
    SpecField("useLegacyMcpJson", "bool", "Older spelling of includeMcpJson."),
    SpecField("includePowers", "bool", "Include IDE-installed powers."),
    SpecField("model", "string", "Model id."),
    SpecField("keyboardShortcut", "string", "Quick-switch shortcut, such as ctrl+a."),
    SpecField("welcomeMessage", "string", "Message shown when the agent is selected."),
    SpecField("managedToolPolicy", "any", "Policy written by an administrator."),
)

_BY_NAME = {field.name: field for field in FIELDS}

#: The json-schema fragment for each kind that has one.
_SCHEMA_FRAGMENTS: dict[str, dict[str, Any]] = {
    "string": {"type": "string"},
    "bool": {"type": "boolean"},
    "object": {"type": "object"},
    "string_list": {"type": "array", "items": {"type": "string"}},
    "resources": {"type": "array", "items": {"type": ["string", "object"]}},
    "hooks": {"type": ["object", "array"]},
    "any": {},
}


def json_schema() -> dict[str, Any]:
    """The JSON Schema (draft 2020-12) of an agent spec, built from :data:`FIELDS`."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Agent spec",
        "type": "object",
        "properties": {f.name: {**_SCHEMA_FRAGMENTS[f.kind], "description": f.doc} for f in FIELDS},
        "additionalProperties": False,
    }


def _shape_problem(kind: str, value: Any) -> str:
    """Why *value* is not a *kind*, or ``""`` when it is."""
    if kind == "string":
        return "" if isinstance(value, str) else "must be a string"
    if kind == "bool":
        return "" if isinstance(value, bool) else "must be true or false"
    if kind == "object":
        return "" if isinstance(value, dict) else "must be an object"
    if kind == "string_list":
        if not isinstance(value, list):
            return "must be a list of strings"
        for index, item in enumerate(value):
            if not isinstance(item, str):
                return f"entry {index} must be a string"
        return ""
    if kind == "resources":
        if not isinstance(value, list):
            return "must be a list"
        for index, item in enumerate(value):
            if not isinstance(item, (str, dict)):
                return f"entry {index} must be a string or an object"
        return ""
    if kind == "hooks":
        return "" if isinstance(value, (dict, list)) else "must be an object or a list"
    return ""


def _tool_entry_problem(entry: str) -> str:
    """Why one ``tools`` / ``allowedTools`` entry is malformed, or ``""``."""
    if not entry.strip():
        return "is empty"
    if any(ch.isspace() for ch in entry):
        return "contains whitespace"
    if entry.startswith("@"):
        server, slash, tool = entry[1:].partition("/")
        if not server:
            return "names no server after @"
        if slash and not tool:
            return "names no tool after the /"
    return ""


def _server_problems(servers: Mapping[str, Any]) -> list[tuple[str, str]]:
    problems: list[tuple[str, str]] = []
    for name, entry in sorted(servers.items(), key=lambda kv: str(kv[0])):
        where = f"mcpServers.{name}"
        if not isinstance(entry, dict):
            problems.append((where, "must be an object"))
            continue
        command, url = entry.get("command"), entry.get("url")
        launchable = (isinstance(command, str) and command.strip()) or (
            isinstance(url, str) and url.strip()
        )
        if not launchable and not mcp_entry_is_registry_governed(entry):
            problems.append((where, "names neither a command to run nor a url to reach"))
        if "command" in entry and not isinstance(command, str):
            problems.append((f"{where}.command", "must be a string"))
        if "url" in entry and not isinstance(url, str):
            problems.append((f"{where}.url", "must be a string"))
        args = entry.get("args")
        if "args" in entry and not (
            isinstance(args, list) and all(isinstance(a, str) for a in args)
        ):
            problems.append((f"{where}.args", "must be a list of strings"))
        if "env" in entry and not isinstance(entry.get("env"), dict):
            problems.append((f"{where}.env", "must be an object"))
    return problems


def check_spec(
    spec: Any,
    *,
    configured_servers: Collection[str] | None = None,
    allow_unknown: bool = False,
) -> list[tuple[str, str]]:
    """Every problem in a parsed spec, as ``(field path, message)`` pairs in field order.

    *configured_servers* is the set of MCP server names the spec can reach (its
    own ``mcpServers``, the ``mcp.json`` servers it opts in to, and the servers
    Kiro Crew adds); ``None`` skips the server-reference check. *allow_unknown*
    stops an unlisted top-level field from being reported, for a kiro-cli newer
    than this table. Never raises.
    """
    if not isinstance(spec, dict):
        return [("", "the spec must be an object")]
    problems: list[tuple[str, str]] = []
    for key in sorted(spec, key=str):
        known = _BY_NAME.get(key) if isinstance(key, str) else None
        if known is None:
            if not allow_unknown:
                problems.append((str(key), "unknown field"))
            continue
        reason = _shape_problem(known.kind, spec[key])
        if reason:
            problems.append((key, reason))
    servers = spec.get("mcpServers")
    if isinstance(servers, dict):
        problems.extend(_server_problems(servers))
    for field_name in ("tools", "allowedTools"):
        entries = spec.get(field_name)
        if not isinstance(entries, list):
            continue
        for index, entry in enumerate(entries):
            reason = _tool_entry_problem(entry) if isinstance(entry, str) else ""
            if reason:
                problems.append((f"{field_name}[{index}]", f"{entry!r} {reason}"))
        if configured_servers is None:
            continue
        _all, refs = parse_tools_refs(entries)
        for server in refs:
            if server not in configured_servers and server not in RESERVED_TOOL_NAMESPACES:
                problems.append(
                    (field_name, f"names @{server}, which no server this spec can reach defines")
                )
    return problems

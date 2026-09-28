"""The bundle's ``agent.json`` and ``mcp.json``, derived from the source spec.

Ported from ``crew_export/spec.py`` and the reader guard in ``serving/smc/bundle.py``
(``validate_tool_refs``): the prompt is inlined, deployment-owned keys are dropped, MCP
servers are narrowed to the approved set and cleaned, and tool grants are narrowed to the
tools that survive.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import prompt as _prompt
from . import scan as _scan
from .contract import ExportRefused
from .crew import ResolvedCrew

# `@builtin` names kiro-cli's own native tool group, not an MCP server, so a
# tool reference to it is never treated as dangling. Ported from
# ``serving/smc/bundle.py:BUILTIN_TOOL_GROUPS``.
_BUILTIN_TOOL_GROUPS = frozenset({"builtin"})

# Spec keys dropped on export. Ported from ``crew_export/spec.py:_DROPPED_KEYS``:
# an inherited security posture or a file outside the bundle is a silent policy
# change in the deployment.
_DROPPED_SPEC_KEYS = ("hooks", "includeMcpJson")


def _clean_mcp_server(name: str, server: dict, notes: list[str]) -> dict:
    """Strip secret-bearing material from one server before it ships.

    ``env`` and ``headers`` are SUPPLEMENTARY and are dropped WHOLESALE, not
    scanned-and-kept. Two reasons this is stricter than
    ``crew_export/spec.py:_clean_mcp_server`` (which keeps benign env): the plan's
    own operator-facing note says "env, headers stripped on export", so keeping
    them contradicts what the owner was told; and a bespoke token format the
    scanner does not recognise would otherwise ship. Dropping them leaves a server
    that fails loudly at connect time -- the safe direction -- and the deployment
    re-supplies whatever the container genuinely needs. This tightening is called
    out in the track report.

    ``args`` and ``url`` are LOAD-BEARING: a credential there refuses the export
    rather than being edited out, because a server minus one arg connects and
    misbehaves. (Ported unchanged from spec.py.)
    """
    out = dict(server)
    for field_name in ("env", "headers"):
        # PRESENT, not "present and a non-empty dict". The type test was there to avoid a
        # note about a field that carried nothing, and it decided the strip as well: a
        # server with ``"env": "TOKEN=sk-live-..."`` or a list of pairs kept the field and
        # shipped it. A malformed value is exactly the one a scanner has no shape for, so
        # the case the type test skipped is the case that most needed dropping.
        if field_name not in out:
            continue
        block = out.pop(field_name)
        if not block:
            continue  # nothing to report, but it is still gone
        # ``len`` only for the shapes that have one. The whole point of this change is that
        # the value may be any type, so the note must not be the thing that raises.
        try:
            count = f"{len(block)} entr(y/ies)"
        except TypeError:
            count = f"a {type(block).__name__} value"
        notes.append(
            f"mcp/{name}: dropped {field_name} ({count}; supplementary and can bear a "
            f"credential, so re-supply via the deployment if needed)"
        )
    for field_name in ("args", "url"):
        value = out.get(field_name)
        if not value:
            continue
        if _scan.scan_text(json.dumps(value, ensure_ascii=False), f"mcp/{name}/{field_name}"):
            raise ExportRefused(
                f"MCP server {name!r} carries a credential in {field_name!r}. That "
                f"field cannot be stripped without breaking the server, so the export "
                f"refuses. Move the value into an env var or a vault reference and re-plan."
            )
    return out


@dataclass
class SpecResult:
    spec: dict
    mcp: dict
    notes: list[str] = field(default_factory=list)


def build_spec(
    crew: ResolvedCrew, agent_spec: dict, selected_mcp: set[str], agents_dir: Path
) -> SpecResult:
    """Produce the bundle's ``agent.json`` and ``mcp.json`` from a source spec."""
    notes: list[str] = []
    spec = json.loads(json.dumps(agent_spec))  # detach from the source mapping

    if spec.get("name") != crew.name:
        notes.append(f"renamed spec {spec.get('name')!r} -> {crew.name!r}")
    spec["name"] = crew.name

    _prompt._inline_prompt(spec, crew.name, agents_dir, notes)

    for key in _DROPPED_SPEC_KEYS:
        if key in spec:
            spec.pop(key)
            notes.append(f"dropped {key!r}: it is a deployment decision, not the owner's")

    # MCP: keep only what curation approved, cleaned of secret material.
    raw_servers = agent_spec.get("mcpServers")
    source_servers: dict = raw_servers if isinstance(raw_servers, dict) else {}
    mcp: dict[str, dict] = {}
    for name in sorted(selected_mcp):
        server = source_servers.get(name)
        if not isinstance(server, dict):
            raise ExportRefused(
                f"plan selects MCP server {name!r}, which the spec does not declare"
            )
        mcp[name] = _clean_mcp_server(name, server, notes)
    dropped = sorted(set(source_servers) - set(mcp))
    if dropped:
        notes.append(f"MCP servers not selected: {', '.join(dropped)}")

    # Both files are emitted from this one dict so they cannot drift within a build
    # (crew_export/spec.py records the bug where they did). agent.json stays
    # installable as-is.
    if mcp:
        spec["mcpServers"] = mcp
    else:
        spec.pop("mcpServers", None)

    # tools: a `@server` reference to a server curation removed leaves the crew
    # holding a tool that points at nothing (kiro-cli drops it silently at mount
    # time). `@builtin` is kiro-cli's native group and is NOT an orphan.
    removed_servers = set(source_servers) - set(mcp)

    def _is_orphan(entry: str) -> bool:
        if not entry.startswith("@"):
            return False
        server = entry[1:].split("/", 1)[0]
        return server not in _BUILTIN_TOOL_GROUPS and server in removed_servers

    tools = spec.get("tools")
    # Shape first, and REFUSE rather than ignore. The isinstance(list) branch below quietly
    # skipped a non-list, and then `set(spec.get("tools") or [])` a few lines down hit it
    # anyway: a truthy non-iterable such as `"tools": 3` raised an uncaught TypeError. That
    # crash is loud and happens before anything is written, so nothing was corrupted -- but
    # a traceback tells the operator nothing about which field of which file is wrong, and
    # silently ignoring the value would ship a spec whose tool list is not the one they
    # wrote. allowedTools is checked with it because it feeds the same expression.
    for field_name in ("tools", "allowedTools"):
        value = spec.get(field_name)
        if value is not None and not isinstance(value, list):
            raise ExportRefused(
                f"{field_name!r} in the agent spec is {type(value).__name__}, not a list. "
                f"The bundle's tool grants are computed from it, so a value of another "
                f"shape cannot be narrowed safely. Fix the spec."
            )
    if isinstance(tools, list):
        # REFUSE a non-string element, never ``str()`` it. Coercing a dict/int/list into a
        # tool id fabricates a capability grant nothing in the spec authorized, and the bundle
        # is then SIGNED with it -- worse than a missing tool, which fails visibly at use, an
        # invented one may succeed. Element type is invalid input, so it is named and refused.
        for e in tools:
            if not isinstance(e, str):
                raise ExportRefused(
                    f"'tools' contains a {type(e).__name__} entry ({e!r}), not a string. A "
                    f"tool grant is computed from it and would be fabricated by coercion; the "
                    f"bundle is signed, so an invented capability cannot be allowed. Fix the "
                    f"spec."
                )
        kept = [e for e in tools if not _is_orphan(e)]
        orphans = [e for e in tools if _is_orphan(e)]
        spec["tools"] = kept
        if orphans:
            notes.append("removed tool references with no surviving server: " + ", ".join(orphans))

    # allowedTools cannot inflate past the surviving tools: a grant for a tool the
    # bundle does not carry is dropped.
    final_tools = set(spec.get("tools") or [])
    # REFUSE a non-string allowedTools element rather than silently dropping it (the same
    # invented-vs-omitted concern as tools above: a dropped grant is a silent capability
    # change in a signed bundle). Shape of the list itself is checked at the top of this
    # function; here the elements are.
    raw_allowed = spec.get("allowedTools") or []
    for t in raw_allowed:
        if not isinstance(t, str):
            raise ExportRefused(
                f"'allowedTools' contains a {type(t).__name__} entry ({t!r}), not a string. "
                f"It grants a capability in a signed bundle and cannot be coerced or dropped "
                f"silently. Fix the spec."
            )
    granted = sorted(t for t in raw_allowed if t in final_tools)
    if sorted(raw_allowed) != granted:
        notes.append(f"allowedTools narrowed to surviving tools ({len(granted)} kept)")
    spec["allowedTools"] = granted

    rendered = json.dumps(spec, indent=2, ensure_ascii=False)
    if _scan.scan_text(rendered, "agent.json"):
        raise ExportRefused("the agent spec contains a credential after cleaning")

    return SpecResult(spec=spec, mcp=mcp, notes=notes)

"""Saved-agent projection for tool visibility and trusted auto-approval grants.

Kiro V3 separates *visibility* (agent ``tools`` / ``excludedTools``) from
*authorization*.  Most executable calls still return to Crew through
``session/request_permission``.  ``allowedTools`` is the deliberate exception:
by Kiro contract, a matching tool skips the permission prompt.  Crew authors the
session projection; Kiro BYO consumes that trusted per-session grant while its
local sandbox/deny policy remains authoritative.
"""
from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

_NATIVE_TAGS: dict[str, frozenset[str]] = {
    "read": frozenset({"read", "read_files", "list_directory", "glob", "grep", "code_symbols", "code_query"}),
    "write": frozenset({"write", "edit", "delete_file"}),
    "shell": frozenset({"shell", "execute_shell_process", "control_shell_process"}),
    "web": frozenset({"web_fetch", "web_search"}),
}
_ALL_NATIVE = frozenset().union(*_NATIVE_TAGS.values())

# Old and canonical Kiro spellings accepted when a projected Crew spec predates
# V3 tags.  This is visibility only; NativeToolRuntime still canonicalizes at
# invocation time and Crew still owns approval.
_NATIVE_ALIASES: dict[str, str] = {
    "fs_read": "read",
    "read_file": "read",
    "read_files": "read_files",
    "list_directory": "list_directory",
    "file_search": "glob",
    "grep_search": "grep",
    "fs_write": "write",
    "str_replace": "edit",
    "delete_file": "delete_file",
    "deleteFile": "delete_file",
    "web_fetch": "web_fetch",
    "webFetch": "web_fetch",
    "web_search": "web_search",
    "webSearch": "web_search",
    "execute_bash": "shell",
    "execute_cmd": "shell",
    "execute_bash_process": "execute_shell_process",
    "control_bash_process": "control_shell_process",
}


@dataclass(frozen=True)
class AgentProjection:
    agent_id: str = ""
    prompt: str = ""
    tools: tuple[str, ...] = ()
    tools_declared: bool = False
    excluded_tools: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return bool(self.agent_id)

    @property
    def unrestricted(self) -> bool:
        # A projected Saved Agent is an explicit capability boundary. KiroCrew
        # 0.10's KAS wire contract treats an omitted ``tools`` as NO tools, not
        # an invitation to the harness's default tool surface. Only sessions
        # without an active Saved Agent get the default host-governed surface.
        # Persisted pre-fix records with tools_declared=False also fail closed.
        return not self.active

    def allowed_native_names(self, available: set[str]) -> set[str]:
        allowed: set[str]
        if self.unrestricted:
            allowed = set(available)
        else:
            allowed = set()
            for token in self.tools:
                token = str(token).strip()
                if token in {"*", "@builtin"}:
                    allowed.update(available)
                elif token in _NATIVE_TAGS:
                    allowed.update(_NATIVE_TAGS[token])
                else:
                    canonical = _NATIVE_ALIASES.get(token, token)
                    if canonical in _ALL_NATIVE:
                        allowed.add(canonical)
                    elif any(ch in canonical for ch in "*?["):
                        # Visibility globs are bounded by the local native-tool
                        # catalogue; no unknown tool can be granted by a glob.
                        allowed.update(name for name in available
                                       if name in _ALL_NATIVE and fnmatchcase(name, canonical))
        for token in self.excluded_tools:
            token = str(token).strip()
            if token in {"*", "@builtin"}:
                allowed.clear()
                continue
            if token in _NATIVE_TAGS:
                allowed.difference_update(_NATIVE_TAGS[token])
                continue
            canonical = _NATIVE_ALIASES.get(token, token)
            # Kiro excludedTools may use shell-style tool-id patterns. A pattern
            # must filter the expanded canonical surface as well; otherwise a
            # saved Agent declaring excludedTools=["web_*"] can still run web tools.
            allowed = {
                name for name in allowed
                if not fnmatchcase(name, canonical)
                and not any(fnmatchcase(alias, token)
                            for alias, target in _NATIVE_ALIASES.items() if target == name)
            }
        return allowed.intersection(available)

    @staticmethod
    def _matches_any(patterns: tuple[str, ...], candidates: set[str]) -> bool:
        # Kiro allowedTools uses case-sensitive shell-style wildcards: ``*`` and
        # ``?``.  Match only trusted canonical/config spellings; model-authored
        # titles or arguments never participate in authorization.
        return any(fnmatchcase(candidate, pattern) for pattern in patterns for candidate in candidates)

    def auto_approves_native(self, name: str) -> bool:
        canonical = _NATIVE_ALIASES.get(str(name).strip(), str(name).strip())
        candidates = {canonical}
        # Accept old Kiro spellings as compatibility aliases, but do not expand
        # visibility tags (for example ``shell`` must not silently grant the new
        # long-running process tools).
        candidates.update(alias for alias, target in _NATIVE_ALIASES.items() if target == canonical)
        return self._matches_any(self.allowed_tools, candidates)

    def auto_approves_mcp(self, *, server_name: str, tool_name: str, public_name: str) -> bool:
        candidates = {
            f"@{server_name}",
            f"@{server_name}/{tool_name}",
            public_name,  # legacy foreign-harness spelling; exact/glob only.
        }
        return self._matches_any(self.allowed_tools, candidates)

    @staticmethod
    def _selector_matches(pattern: str, *, server_name: str, tool_name: str, public_name: str) -> bool:
        if pattern in {"*", "@mcp"}:
            return True
        # A bare @server applies to all tools of that server. Server/tool
        # wildcards match only qualified selectors, never another server's bare
        # tool name. This prevents exclusions such as @core/* from being ignored.
        if pattern == f"@{server_name}":
            return True
        return any(fnmatchcase(value, pattern) for value in (
            f"@{server_name}/{tool_name}", public_name,
            *(() if pattern.startswith(("@", "mcp__")) or pattern in _NATIVE_TAGS
               or _NATIVE_ALIASES.get(pattern, pattern) in _ALL_NATIVE else (tool_name,)),
        ))

    def excludes_mcp(self, *, server_name: str, tool_name: str, public_name: str) -> bool:
        """Explicit Agent exclusions remain authoritative over host mounts."""
        return any(self._selector_matches(str(token).strip(),
            server_name=server_name, tool_name=tool_name, public_name=public_name)
            for token in self.excluded_tools)

    def allows_any_mcp(self) -> bool:
        if self.unrestricted:
            return True
        # This is only an inexpensive search-surface prefilter. Exact allow and
        # deny checks MUST still be performed for each discovered tool.
        return any(
            str(token).strip() in {"*", "@mcp"}
            or str(token).strip().startswith(("@", "mcp__"))
            for token in self.tools
        ) or any(str(token).strip() and not str(token).strip() in _NATIVE_TAGS
                 and _NATIVE_ALIASES.get(str(token).strip(), str(token).strip()) not in _ALL_NATIVE
                 for token in self.tools)

    def allows_mcp(self, *, server_name: str, tool_name: str, public_name: str) -> bool:
        if self.unrestricted:
            visible = True
        else:
            visible = any(self._selector_matches(str(token).strip(),
                server_name=server_name, tool_name=tool_name, public_name=public_name)
                for token in self.tools)
        if not visible:
            return False
        return not self.excludes_mcp(server_name=server_name,
            tool_name=tool_name, public_name=public_name)


def from_session_params(params: dict[str, Any]) -> AgentProjection:
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return AgentProjection()
    kiro = meta.get("kiro")
    if not isinstance(kiro, dict):
        return AgentProjection()
    agents = kiro.get("customAgents")
    if agents is None:
        return AgentProjection()
    if not isinstance(agents, list):
        raise ValueError("Crew customAgents projection must be a list")
    if any(not isinstance(raw, dict) or not str(raw.get("id") or "").strip() for raw in agents):
        raise ValueError("Crew customAgents contains an invalid Agent definition")
    preferred = str(kiro.get("modeId") or "").strip()
    valid_agents = [raw for raw in agents if isinstance(raw, dict)
                    and str(raw.get("id") or "").strip()]
    if preferred:
        matches = [raw for raw in valid_agents
                   if str(raw.get("id") or "").strip() == preferred]
        if not matches and (valid_agents or preferred != "default"):
            raise ValueError(f"Crew requested saved Agent {preferred!r} but did not project its definition")
        ordered = matches
    elif len(valid_agents) <= 1:
        ordered = valid_agents
    else:
        # Without modeId the list is a catalogue, not authority to pick an
        # arbitrary Agent with potentially stronger prompts / tool permissions.
        raise ValueError("Crew projected multiple saved Agents without an active modeId")
    for raw in ordered:
        agent_id = str(raw.get("id") or "").strip()
        prompt = raw.get("prompt")
        # An authored Agent may intentionally have an empty prompt while still
        # carrying restrictive tools/excludedTools. Do not discard its identity
        # and accidentally widen the session back to default tool access.
        if prompt is None:
            prompt = ""
        if not isinstance(prompt, str):
            raise ValueError(f"Crew saved Agent {agent_id!r} has malformed prompt")
        tools_raw = raw.get("tools")
        excluded_raw = raw.get("excludedTools")
        allowed_raw = raw.get("allowedTools")
        for key, value in (("tools", tools_raw), ("excludedTools", excluded_raw),
                           ("allowedTools", allowed_raw)):
            if value is not None and (not isinstance(value, list)
                                      or any(not isinstance(item, str) for item in value)):
                raise ValueError(f"Crew saved Agent {agent_id!r} has malformed {key}")
        tools = tuple(str(x).strip() for x in tools_raw if isinstance(x, str) and x.strip()) if isinstance(tools_raw, list) else ()
        excluded = tuple(str(x).strip() for x in excluded_raw if isinstance(x, str) and x.strip()) if isinstance(excluded_raw, list) else ()
        allowed_tools = tuple(str(x).strip() for x in allowed_raw if isinstance(x, str) and x.strip()) if isinstance(allowed_raw, list) else ()
        return AgentProjection(
            agent_id=agent_id,
            prompt=prompt,
            tools=tools,
            # Keep wire provenance for diagnostics, but missing ``tools`` is
            # restrictive even when a historical record used the old default.
            tools_declared=isinstance(tools_raw, list),
            excluded_tools=excluded,
            allowed_tools=allowed_tools,
        )
    return AgentProjection()

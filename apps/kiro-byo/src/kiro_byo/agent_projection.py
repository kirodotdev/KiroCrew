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
        # Missing tools in old Crew projections historically meant the backend's
        # default tool surface.  An explicit [] means no tools.
        return not self.active or not self.tools_declared

    def allowed_native_names(self, available: set[str]) -> set[str]:
        if self.unrestricted:
            allowed = set(available)
        else:
            allowed: set[str] = set()
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
        for token in self.excluded_tools:
            token = str(token).strip()
            if token in {"*", "@builtin"}:
                allowed.clear()
                continue
            if token in _NATIVE_TAGS:
                allowed.difference_update(_NATIVE_TAGS[token])
                continue
            allowed.discard(_NATIVE_ALIASES.get(token, token))
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

    def allows_any_mcp(self) -> bool:
        if self.unrestricted:
            return True
        for token in self.tools:
            token = str(token).strip()
            if token in {"*", "@mcp"} or token.startswith("@") or token.startswith("mcp__"):
                if token not in set(self.excluded_tools):
                    return True
        return False

    def allows_mcp(self, *, server_name: str, tool_name: str, public_name: str) -> bool:
        if self.unrestricted:
            allowed = True
        else:
            allowed = False
            for token in self.tools:
                token = str(token).strip()
                if token in {"*", "@mcp"}:
                    allowed = True
                    break
                if token == f"@{server_name}" or token == f"@{server_name}/{tool_name}":
                    allowed = True
                    break
                if token in {public_name, tool_name}:
                    allowed = True
                    break
        if not allowed:
            return False
        for token in self.excluded_tools:
            token = str(token).strip()
            if token in {"*", "@mcp", f"@{server_name}", f"@{server_name}/{tool_name}", public_name, tool_name}:
                return False
        return True


def from_session_params(params: dict[str, Any]) -> AgentProjection:
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return AgentProjection()
    kiro = meta.get("kiro")
    if not isinstance(kiro, dict):
        return AgentProjection()
    agents = kiro.get("customAgents")
    if not isinstance(agents, list):
        return AgentProjection()
    preferred = str(kiro.get("modeId") or "").strip()
    ordered = list(agents)
    if preferred:
        ordered.sort(key=lambda raw: 0 if isinstance(raw, dict) and str(raw.get("id") or "") == preferred else 1)
    for raw in ordered:
        if not isinstance(raw, dict):
            continue
        agent_id = str(raw.get("id") or "").strip()
        prompt = raw.get("prompt")
        if not agent_id or not isinstance(prompt, str) or not prompt.strip():
            continue
        tools_raw = raw.get("tools")
        excluded_raw = raw.get("excludedTools")
        allowed_raw = raw.get("allowedTools")
        tools = tuple(str(x).strip() for x in tools_raw if str(x).strip()) if isinstance(tools_raw, list) else ()
        excluded = tuple(str(x).strip() for x in excluded_raw if str(x).strip()) if isinstance(excluded_raw, list) else ()
        allowed_tools = tuple(str(x).strip() for x in allowed_raw if str(x).strip()) if isinstance(allowed_raw, list) else ()
        return AgentProjection(
            agent_id=agent_id,
            prompt=prompt,
            tools=tools,
            tools_declared=isinstance(tools_raw, list),
            excluded_tools=excluded,
            allowed_tools=allowed_tools,
        )
    return AgentProjection()

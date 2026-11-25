"""Which ``autoApprove`` verbs are literal tool names, and their ``@server/tool`` refs.

THE one answer for every writer that turns a server's ``autoApprove`` into
``allowedTools`` grants: the KAS projection (``acp.kas_auto_approve``) at session
time, and an app agent's MCP grant (``apps.bridges_agents``) at registration. A
verb carrying a glob or path character is not a tool name and grants nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: Characters that make an ``autoApprove`` verb a pattern rather than one tool.
NON_LITERAL = frozenset("*?[]{}!/")


def literal_auto_approve_refs(servers: Mapping[str, Any]) -> list[str]:
    """``@<server>/<tool>`` for each literal ``autoApprove`` verb in *servers*, in order."""
    refs: list[str] = []
    for name, entry in servers.items():
        verbs = entry.get("autoApprove") if isinstance(entry, dict) else None
        if not isinstance(verbs, list):
            continue
        for verb in verbs:
            if not isinstance(verb, str):
                continue
            verb = verb.strip()
            if not verb or NON_LITERAL.intersection(verb):
                continue
            ref = f"@{name}/{verb}"
            if ref not in refs:
                refs.append(ref)
    return refs

"""A spec's MCP ``autoApprove`` lists as ``allowedTools`` entries, for KAS.

kiro-cli reads a server's ``autoApprove`` directly; KAS has no slot for it, and
its only auto-approval input is the ``permissions`` block Crew derives from
``allowedTools``. So the projection drops the key from the server entry and
feeds what survives into that derivation instead, where it meets the same
per-entry governance ceiling and PreToolUse-hook withholding every
``allowedTools`` grant meets.

What survives is decided by :func:`~kiro_crew.platform.governance.strip_ungoverned_auto_approve`,
the same predicate every kiro-cli spec writer runs: the ceiling on the server, the
``mcp.honour_auto_approve`` opt-in, the owner-written and ``list[str]`` shape tests,
and the verbs a managed spec declares. Nothing is granted here that the kiro-cli
route would not keep.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.platform.governance import strip_ungoverned_auto_approve

#: A verb carrying any of these is not one literal tool name. kiro-cli's
#: ``autoApprove`` names tools; a glob would become a ``<server>/*``-style rule on
#: KAS, so such a verb is left to prompt rather than read as a wider grant.
_NON_LITERAL = frozenset("*?[]{}!/")


def auto_approve_refs(spec: dict[str, Any]) -> list[str]:
    """``@<server>/<tool>`` for each governed ``autoApprove`` verb in *spec*.

    Order follows the spec. Malformed servers and verbs are skipped, never raised.
    """
    servers = spec.get("mcpServers")
    if not isinstance(servers, dict):
        return []
    asking = {
        name: entry
        for name, entry in servers.items()
        if isinstance(name, str) and name and isinstance(entry, dict) and "autoApprove" in entry
    }
    if not asking:
        return []
    # Audited here, not only at spec write: a hand-written spec reaches this
    # projection without ever passing a writer, and each kept list is a grant.
    kept = strip_ungoverned_auto_approve(asking, audit=True)
    refs: list[str] = []
    for name, entry in kept.items():
        verbs = entry.get("autoApprove") if isinstance(entry, dict) else None
        if not isinstance(verbs, list):
            continue
        for verb in verbs:
            if not isinstance(verb, str):
                continue
            verb = verb.strip()
            if not verb or _NON_LITERAL.intersection(verb):
                continue
            ref = f"@{name}/{verb}"
            if ref not in refs:
                refs.append(ref)
    return refs

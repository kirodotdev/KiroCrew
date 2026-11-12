"""A spec's MCP ``autoApprove`` lists as ``allowedTools`` entries, for KAS and kiro-cli.

Neither runtime reads the key. KAS has no slot for it, and its only auto-approval
input is the ``permissions`` block Crew derives from ``allowedTools``; kiro-cli has
no ``autoApprove`` field and ignores it, so only an ``allowedTools`` entry lets it
run an MCP tool without asking. So the verbs that survive governance travel as
``@<server>/<tool>`` entries on both. The KAS projection drops the key from the
server entry and feeds the refs into that derivation, where they meet the same
per-entry governance ceiling and PreToolUse-hook withholding every
``allowedTools`` grant meets. On kiro-cli they go into the ``allowedTools`` of the
native skill view (:func:`grant_in_skill_view`), the Crew-owned copy of the spec
kiro-cli runs, rebuilt from the spec at every spawn. No spec file is written, so a
grant lasts exactly as long as the verb that backs it.

What survives is decided by :func:`~kiro_crew.platform.governance.strip_ungoverned_auto_approve`,
the same predicate every kiro-cli spec writer runs: the ceiling on the server, the
``mcp.honour_auto_approve`` opt-in, the owner-written and ``list[str]`` shape tests,
and the verbs a managed spec declares.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from typing import Any

from kiro_crew.platform.governance import may_skip_gate_now, strip_ungoverned_auto_approve
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: A verb carrying any of these is not one literal tool name. ``autoApprove`` names
#: tools; a glob would become a ``<server>/*``-style grant, so such a verb is left to
#: prompt rather than read as a wider grant.
_NON_LITERAL = frozenset("*?[]{}!/")

#: Skill-view decisions already recorded in this process, so a view rebuilt at every
#: spawn is recorded once per distinct decision and not once per spawn. Entries are
#: fixed-size digests. Past the bound the memo is cleared and a decision may be
#: recorded again.
_RECORDED: set[str] = set()
_RECORDED_MAX = 256
_RECORDED_LOCK = threading.Lock()


def auto_approve_refs(spec: dict[str, Any], *, audit: bool = True) -> list[str]:
    """``@<server>/<tool>`` for each governed ``autoApprove`` verb in *spec*.

    Order follows the spec. Malformed servers and verbs are skipped, never raised.
    *audit* False is for a read-only check that projects nothing.
    """
    asking = _asking(spec)
    if not asking:
        return []
    # Audited here, not only at spec write: a hand-written spec reaches this
    # projection without ever passing a writer, and each kept list is a grant.
    return _literal_refs(strip_ungoverned_auto_approve(asking, audit=audit))


def _asking(spec: dict[str, Any]) -> dict[str, Any]:
    """*spec*'s well-formed MCP server entries that carry an ``autoApprove`` key."""
    servers = spec.get("mcpServers")
    if not isinstance(servers, dict):
        return {}
    return {
        name: entry
        for name, entry in servers.items()
        if isinstance(name, str) and name and isinstance(entry, dict) and "autoApprove" in entry
    }


def _literal_refs(servers: dict[str, Any]) -> list[str]:
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
            if not verb or _NON_LITERAL.intersection(verb):
                continue
            ref = f"@{name}/{verb}"
            if ref not in refs:
                refs.append(ref)
    return refs


def grant_in_skill_view(agent: str, view: dict[str, Any]) -> list[str]:
    """Add *view*'s governed ``autoApprove`` refs to its ``allowedTools``, in place.

    *view* is the native skill view kiro-cli runs for *agent*, a Crew-owned copy,
    never the authored file. The refs are :func:`auto_approve_refs`'s, each asked of
    the ceiling again (``may_skip_gate_now``): the two filters the KAS projection
    applies, so both runtimes grant the same tools. A view with no ``allowedTools``
    gets the list, which kiro-cli reads as empty when absent; a value that is not a
    list is left alone. Returns the refs added. Each distinct decision, granted or
    withheld by either filter, is recorded in the SEL once per process (:func:`_record`).
    """
    allowed = view.get("allowedTools", [])
    if not isinstance(allowed, list):
        return []
    # The first filter runs unaudited (the view is rebuilt at every spawn); what it
    # drops is recorded below through the deduplicated recorder instead.
    kept = set(auto_approve_refs(view, audit=False))
    granted: list[str] = []
    withheld: list[str] = []
    for ref in _literal_refs(_asking(view)):
        if ref in allowed:
            continue
        (granted if ref in kept and may_skip_gate_now(ref) else withheld).append(ref)
    if granted:
        view["allowedTools"] = [*allowed, *granted]
    if granted or withheld:
        _record(agent, granted, withheld)
    return granted


def _record(agent: str, granted: list[str], withheld: list[str]) -> None:
    """Record *agent*'s skill-view grants and withholds in the SEL. Best-effort."""
    canonical = json.dumps([agent, granted, withheld], separators=(",", ":")).encode("utf-8")
    key = hashlib.sha256(canonical).hexdigest()
    with _RECORDED_LOCK:
        if key in _RECORDED:
            return
        if len(_RECORDED) >= _RECORDED_MAX:
            _RECORDED.clear()
        _RECORDED.add(key)
    entries = []
    if granted:
        entries.append(
            (
                "mcp_auto_approve_honoured",
                f"{', '.join(granted)} auto-approved in the Kiro Crew skill view of agent "
                f"{agent!r} (a kept autoApprove; kiro-cli honours it only as allowedTools); "
                "these calls do not reach the tool gate",
            )
        )
    if withheld:
        entries.append(
            (
                "mcp_auto_approve_withheld",
                f"{', '.join(withheld)} not auto-approved in the Kiro Crew skill view of "
                f"agent {agent!r} (governance ceiling or the undeclared-grant floor); "
                "calls go through the approval gate",
            )
        )
    for operation, resources in entries:
        try:
            sel().log_api_access(
                caller="system",
                operation=operation,
                outcome="ok",
                source=f"skill-view:{agent}",
                resources=resources,
            )
        except Exception:  # noqa: BLE001 -- the audit must not break the projection
            logger.debug("SEL audit unavailable for a skill view grant", exc_info=True)

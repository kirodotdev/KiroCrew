"""The ``agentTools`` grant family: which of a third-party app's declared agent tools the owner approved.

A manifest's top-level ``agentTools`` block (``apps/manifest_agent_tools.py``) declares MCP
tools the app serves from its own process. A first-party app's tools are listed on
``kirocrew-core`` with no grant (``apps/app_agent_tools.py``). A third-party app's tool
is listed there only when the owner approved its entry in this family.

One entry is one tool: ``<tool name>@<digest>``. The digest is the first 12 hex
characters of SHA-256 over the canonical JSON of the block's route and the tool's
title, description, input schema, ``timeoutSeconds``, ``result``, ``strictSession`` and
``maxResultChars``. A change to any of them is a new entry, so an update that changes
a tool holds it back exactly like one that adds a tool, and the owner approves it
again.

The install card lists each declared tool and sends back the entries it showed;
installing approves only those (``install_app(agent_tools_consent=...)``). An install
no card preceded approves none, and the owner approves them on the app's page
(``manager_records._OWNER_APPROVED_FAMILIES``). The family is explicit-only
(``point_grants``): a set stored before this host knew the family approves no tool.

Nothing here names an app.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from kiro_crew.apps.manifest_agent_tools import AgentToolDecl, AgentToolsConfig

#: The manifest key, and the grant family that stores its entries.
GRANT_FAMILY = "agentTools"
AGENT_TOOL_GRANT_FAMILIES = (GRANT_FAMILY,)
#: Hex characters of the digest an entry carries.
DIGEST_CHARS = 12


def tool_entry(route: str, decl: AgentToolDecl) -> str:
    """The grant entry of *decl*, served from the block's *route*."""
    fields = {
        "route": route,
        "title": decl.title,
        "description": decl.description,
        "inputSchema": decl.input_schema,
        "timeoutSeconds": decl.timeout_seconds,
        "result": decl.result,
        "strictSession": decl.strict_session,
        "maxResultChars": decl.max_result_chars,
    }
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    # ``surrogatepass``: a lone surrogate (a valid JSON ``\ud800`` escape) in a declared
    # field must not raise; every other string encodes to the same UTF-8 bytes.
    digest = hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()[:DIGEST_CHARS]
    return f"{decl.name}@{digest}"


def _block(manifest: Any) -> AgentToolsConfig:
    """The ``agentTools`` block of a parsed manifest or a raw manifest dict."""
    if isinstance(manifest, dict):
        return AgentToolsConfig.from_value(manifest.get("agentTools"))
    block = getattr(manifest, "agentTools", None)
    return block if isinstance(block, AgentToolsConfig) else AgentToolsConfig()


def agent_tool_grant_entries(manifest: Any) -> dict[str, list[str]]:
    """The :data:`GRANT_FAMILY` entries a manifest declares, one per parsed tool."""
    block = _block(manifest)
    if not block.route:
        return {GRANT_FAMILY: []}
    return {GRANT_FAMILY: [tool_entry(block.route, tool) for tool in block.tools]}


def _rows(manifest: Any) -> list[dict[str, str]]:
    block = _block(manifest)
    if not block.route:
        return []
    return [
        {"name": t.name, "description": t.description, "entry": tool_entry(block.route, t)}
        for t in block.tools
    ]


def agent_tools_grant_row(name: str, manifest: Any) -> dict[str, Any]:
    """The ``agentToolsGrant`` key of an app row, absent when no tool is declared.

    ``approved`` lists the declared tools whose entry the owner's set holds. Of the
    rest, ``revoked`` lists the ones the owner turned off on the app's page (by name,
    so whatever their entry is now), which the page shows as off switches, and
    ``staged`` the ones an install or update holds back, which the page's approval
    notice offers. A revoked list that cannot be read counts every held-back tool as
    revoked, so the notice offers none of them. Each row is ``{name, description,
    entry}``, so the page shows the tool and never its digest.
    """
    rows = _rows(manifest)
    if not rows:
        return {}
    # Deferred: the facade's family imports ``point_grants``, which imports this module.
    from kiro_crew.apps import app_agent_tools  # noqa: PLC0415
    from kiro_crew.apps.manager import staged_app_grants  # noqa: PLC0415

    if not app_agent_tools.is_third_party(name):
        # A first-party app's tools need no grant, so there is nothing to approve.
        return {}

    from kiro_crew.apps import protected_app_state  # noqa: PLC0415

    approved = set(staged_app_grants(name, GRANT_FAMILY, lambda: [r["entry"] for r in rows]))
    held = [r for r in rows if r["entry"] not in approved]
    revoked = protected_app_state.revoked_agent_tools_or_none(name)
    if revoked is None:
        revoked = frozenset(r["name"] for r in held)
    return {
        "agentToolsGrant": {
            "approved": [r for r in rows if r["entry"] in approved],
            "staged": [r for r in held if r["name"] not in revoked],
            "revoked": [r for r in held if r["name"] in revoked],
        }
    }


__all__ = [
    "AGENT_TOOL_GRANT_FAMILIES",
    "DIGEST_CHARS",
    "GRANT_FAMILY",
    "agent_tool_grant_entries",
    "agent_tools_grant_row",
    "tool_entry",
]

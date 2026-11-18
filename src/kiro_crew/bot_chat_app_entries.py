"""How one app server is rendered for a Bot's chat (``bot_chat_app_tools``).

Split out of ``bot_chat_app_tools`` whole; it holds no state.
"""

from __future__ import annotations

import copy
import json
from typing import Any

#: Characters that make a ``disabledTools`` pattern a glob, which Claude deny rules
#: cannot express (``claude_mcp_tool_name`` sanitises them, producing a wrong name).
_GLOB_CHARS: frozenset[str] = frozenset("*?{[")


def _rendered_entry(app: str, agent: str, server: str) -> dict[str, Any] | None:
    """The entry a Bot chat mounts for *server*, from the app agent's RENDERED spec.

    ``None`` when the server is switched off whole, in that spec or on the MCP page,
    under its OWN name: the mount renames it ``<app>:<server>``, which no switch-off
    names. Rebuilt from launch keys alone (``template_mcp_additions.ENTRY_KEYS``), so
    no trust key such as ``autoApprove`` reaches the Bot's chat, then given the tools
    switched off for that server, in the spec or on the page, as ``disabledTools``.
    """
    from kiro_crew.agent_sdk.agent_specs import (
        global_mcp_servers,
        switched_off_tools,
        unusable_disabled_tools,
    )
    from kiro_crew.apps.bridges import _kiro_agents_dir, _namespace, _safe_link_name
    from kiro_crew.mate_worker import globally_disabled_refs
    from kiro_crew.mcp_cleanup import mcp_entry_is_muted, mcp_server_muted_in_any_store
    from kiro_crew.mcp_utils import mcp_server_alias
    from kiro_crew.template_mcp_additions import ENTRY_KEYS

    path = _kiro_agents_dir() / f"{_safe_link_name(_namespace(app, agent))}.json"
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    servers = spec.get("mcpServers") if isinstance(spec, dict) else None
    entry = servers.get(server) if isinstance(servers, dict) else None
    if not isinstance(entry, dict) or mcp_server_muted_in_any_store(server, entry):
        return None
    canonical = mcp_server_alias(server)
    if any(
        mcp_server_alias(str(name)) == canonical and mcp_entry_is_muted(row)
        for name, row in global_mcp_servers().items()
    ):
        return None
    if unusable_disabled_tools({"mcpServers": {server: entry}}):
        # A malformed restriction would be dropped by the rebuild below and the
        # server mount whole: refuse it, as KAS refuses such a spec.
        return None
    out = {key: copy.deepcopy(entry[key]) for key in ENTRY_KEYS if key in entry}
    # By the source name, canonically (the page row and the toggle's stub use the
    # slash-free alias): the spec's and Kiro-global switch-offs, then the MCP page's,
    # whichever Crew store it saved them to.
    off = {tool for name, tool in switched_off_tools(spec) if mcp_server_alias(name) == canonical}
    for ref in globally_disabled_refs({server, canonical}):
        name, _, tool = ref[1:].partition("/")
        if tool and mcp_server_alias(name) == canonical:
            off.add(tool)
    if off:
        if any(_GLOB_CHARS & set(tool) for tool in off):
            # A glob-pattern restriction cannot be translated to a Claude deny rule
            # (``claude_mcp_tool_name`` replaces glob chars with ``_``, producing a
            # rule for a nonexistent tool). Withhold the offer instead, as the worker
            # path already refuses ``uncarried_tool_limits_of`` for the same reason.
            return None
        out["disabledTools"] = sorted(off)
    return out

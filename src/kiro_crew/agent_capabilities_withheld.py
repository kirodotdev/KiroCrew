"""Name the approvals a capability draft sets that the saved spec will not carry.

Split out of :mod:`kiro_crew.agent_capabilities` (which keeps the import path
callers use) to hold that module under the file-line cap.
"""

from __future__ import annotations


def withheld_approvals(request: dict, spec: dict, intent: dict, catalog: dict) -> list[dict]:
    """The approvals this draft asks for that the saved spec will not carry.

    Three causes drop an approval the owner just set, and none is visible in
    the rows alone. Each reason is read from the projected spec, not inferred:
    ``governed``: the governance filter in ``_sanitize_projection`` withheld
    it and left a remove tombstone, so the row reads "Removed" after the save.
    ``not_connected``: the projected spec has no enabled MCP connection by that
    name (missing, or ``disabled``), so ``_materialize`` dropped it.
    ``managed``: the server is one of ``MEMBER_PLATFORM_SERVERS``, whose
    transport ``_maintain_owned`` rebuilds with no approvals.
    ``unexplained``: none of those applies; the approval is still named, without
    a guessed cause. The preview names each, so the owner learns why before
    saving instead of finding a silently removed row afterwards.
    """
    # Imported here: agent_capabilities imports this module at load time.
    from kiro_crew.agent_capabilities import MEMBER_PLATFORM_SERVERS, _rows

    rows = _rows(spec, catalog)
    withheld = []
    for op in request["operations"]:
        section, key = op["section"], op["id"]
        if section not in ("allowedTools", "autoApprove") or op["action"] != "set":
            continue
        if key in rows[section]:
            continue
        override = intent["overrides"][section].get(key) or {}
        server = key[1:].split("/", 1)[0] if key.startswith("@") else None
        transport = spec.get("mcpServers", {}).get(server) if server else None
        if override.get("action") == "remove":
            reason = "governed"
        elif server and (not isinstance(transport, dict) or transport.get("disabled")):
            reason = "not_connected"
        elif server in MEMBER_PLATFORM_SERVERS:
            reason = "managed"
        else:
            # No known drop site explains it; say so rather than guess a cause.
            reason = "unexplained"
        withheld.append({"section": section, "id": key, "reason": reason})
    return withheld

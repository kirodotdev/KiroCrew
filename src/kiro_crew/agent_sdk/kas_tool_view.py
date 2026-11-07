"""What a live KAS session's registered agent offers, as plain data for ``/tools``.

The dashboard's ``/tools`` on KAS (``dashboard/kas_local_commands.py``) lists the
agent entry the session runs now. Choosing that entry and reading its approvals
is ACP knowledge (``acp.kas_agents``, ``acp.kas_permissions``), so it happens
here and the dashboard gets names only. A server's ``env``, ``headers``,
``command`` and ``args`` are never read: only the ``mcpServers`` keys leave.
"""

from __future__ import annotations

from typing import Any


def clean_names(values: Any) -> list[str]:
    """The non-empty, stripped strings of *values*, in order, without repeats."""
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set, frozenset)):
        return []
    out: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip() and value.strip() not in out:
            out.append(value.strip())
    return out


def _auto_approved_names(permissions: Any) -> list[str]:
    """Each ``allow`` rule KAS still honours, as a capability or ``@server/tool`` pattern.

    An unscoped ``ask`` or ``deny`` cancels an ``allow`` of the same capability,
    as ``auto_approved_capabilities`` resolves it. A scoped ``ask``/``deny`` or an
    ``exclude`` narrows the grant without cancelling it; the name is then marked
    "(with exceptions)" rather than resolved here, so this summary never restates
    KAS's rule precedence.
    """
    from kiro_crew.acp.kas_permissions import auto_approved_capabilities

    approved = auto_approved_capabilities(permissions)
    raw_rules = permissions.get("rules") if isinstance(permissions, dict) else None
    rules = [r for r in raw_rules if isinstance(r, dict)] if isinstance(raw_rules, list) else []
    narrowed = {
        r.get("capability")
        for r in rules
        if r.get("effect") in ("ask", "deny") and (r.get("match") or r.get("exclude"))
    }
    out: list[str] = []
    for rule in rules:
        if rule.get("effect") != "allow":
            continue
        capability = rule.get("capability")
        if not isinstance(capability, str) or capability not in approved:
            continue
        suffix = " (with exceptions)" if rule.get("exclude") or capability in narrowed else ""
        match = clean_names(rule.get("match"))
        if capability == "mcp" and match:
            out.extend(f"@{pattern}{suffix}" for pattern in match)
        elif match:
            out.append(f"{capability} (some paths){suffix}")
        else:
            out.append(f"{capability}{suffix}")
    return clean_names(out)


def kas_tool_view(provider: object) -> dict[str, Any] | None:
    """The registered entry *provider*'s session runs now, as names.

    ``None`` when the provider holds no KAS batch at all. ``{"entry": None,
    "mode_id": ...}`` when it holds one but runs a mode Kiro Crew did not
    register (a KAS built-in). Otherwise ``entry`` carries ``id``, ``tools``
    (``"*"`` or a list), ``excluded``, ``servers``, ``approved`` and
    ``crew_answered``. The entry is chosen by ``active_registered_entry``: by wire
    id after a KAS mode switch, else the batch's own agent id exactly.
    """
    from kiro_crew.acp.kas_agents import active_registered_entry
    from kiro_crew.acp.kas_permissions import CREW_ANSWERED_BUILTINS_KEY

    entries = getattr(provider, "kas_registered_agents", None)
    if not isinstance(entries, list):
        return None
    agent_id = getattr(provider, "kas_projected_agent", "")
    mode_id = getattr(provider, "kas_active_mode_id", "")
    agent_id = agent_id if isinstance(agent_id, str) else ""
    mode_id = mode_id if isinstance(mode_id, str) else ""
    entry = active_registered_entry(entries, agent_id, mode_id)
    if entry is None:
        return {"entry": None, "mode_id": mode_id}
    tools = entry.get("tools")
    servers = entry.get("mcpServers")
    return {
        "entry": {
            "id": entry.get("id") if isinstance(entry.get("id"), str) else "",
            "tools": "*" if tools == "*" else clean_names(tools),
            "excluded": clean_names(entry.get("excludedTools")),
            "servers": clean_names(list(servers)) if isinstance(servers, dict) else [],
            "approved": _auto_approved_names(entry.get("permissions")),
            "crew_answered": clean_names(entry.get(CREW_ANSWERED_BUILTINS_KEY)),
        },
        "mode_id": mode_id,
    }


def permissions_block_readable(raw: Any) -> bool:
    """Whether KAS's projection would read the ``permissions`` block *raw* whole.

    A block it refuses (a named bundle, an unknown key, a rule it cannot parse)
    travels as no rule at all, its denies included. Read without auditing: this
    asks, it projects nothing.
    """
    from kiro_crew.acp import kas_permissions

    parsed = kas_permissions.parse_user_permissions(
        raw, audit_decision=kas_permissions._asking_only
    )
    return parsed is not None


def allow_granted_by_spec(rule: dict[str, Any], spec: dict[str, Any]) -> bool:
    """Whether *spec*'s own grants already cover allow *rule* on KAS.

    Read from the rules KAS's projection derives (``allowed_tools_to_permissions``)
    from the same input it uses: the ``allowedTools`` list plus each governed
    server ``autoApprove`` verb. A bare capability allow is granted when that
    input derives the same one, an ``mcp`` allow when every pattern it matches is
    covered by a pattern the input derives (:func:`_pattern_contains`).
    """
    from kiro_crew.acp import kas_permissions
    from kiro_crew.acp.kas_auto_approve import auto_approve_refs

    listed = spec.get("allowedTools")
    allowed = [*(listed if isinstance(listed, list) else [])]
    allowed.extend(r for r in auto_approve_refs(spec, audit=False) if r not in allowed)
    derived = kas_permissions.allowed_tools_to_permissions(allowed) or {}
    allows = [r for r in derived.get("rules", []) if r.get("effect") == "allow"]
    capability = rule.get("capability")
    if capability != "mcp":
        return any(r.get("capability") == capability and not r.get("match") for r in allows)
    granted = [p for r in allows if r.get("capability") == "mcp" for p in r.get("match") or []]
    wanted = rule.get("match") or []
    return bool(wanted) and all(
        isinstance(p, str) and any(_pattern_contains(g, p) for g in granted) for p in wanted
    )


def _pattern_contains(granted: str, wanted: str) -> bool:
    """Whether MCP pattern *granted* provably matches everything *wanted* matches.

    Only two shapes are proved: the same pattern, and a whole-server grant
    (``server/*``) over any pattern on that server. Anything else is not.
    """
    if granted == wanted:
        return True
    server, sep, tool = granted.partition("/")
    return bool(sep) and tool == "*" and wanted.startswith(server + "/")


def permission_allow_derivable(rule: dict[str, Any]) -> bool:
    """Whether KAS's projection treats ``permissions`` *rule* as an ``allowedTools``
    grant, which it drops from the block when the spec has that list."""
    from kiro_crew.acp import kas_permissions

    if not isinstance(rule.get("capability"), str) or not isinstance(rule.get("effect"), str):
        return False
    return kas_permissions._deriver_could_emit(rule)


def tools_settings_family(tool: Any) -> str:
    """The tool family a ``toolsSettings`` key belongs to (every kiro-cli spelling
    of one tool is one: ``execute_bash``, ``shell``, ``executeBash``), else the
    key itself."""
    from kiro_crew.platform import tools_settings_aliases as aliases

    if not isinstance(tool, str):
        return str(tool)
    name = aliases._TOOLS_SETTINGS_CANONICAL.get(tool, tool)
    family = aliases._TOOLS_SETTINGS_FAMILY.get(name)
    return family or name


def tools_settings_unenforced(raw: Any) -> list[str]:
    """The ``<tool>.<key>`` entries in ``toolsSettings`` *raw* KAS's translation drops.

    Read from KAS's own translation (``tools_settings_to_permissions``): a deny it
    cannot turn into a rule (a regex with no glob form, a URL pattern naming a
    path) and an ``allowedCommands`` entry Crew's gate cannot answer are named,
    and so is a duplicate alias key kiro-cli drops (``canonical_tools_settings``).
    """
    from kiro_crew.acp import kas_permissions

    settings, dropped = kas_permissions.canonical_tools_settings(raw)
    _block, not_carried = kas_permissions.tools_settings_to_permissions(settings)
    return sorted(set(not_carried) | set(dropped))

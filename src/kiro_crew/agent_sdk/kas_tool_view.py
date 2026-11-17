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


def allow_exclude_limit() -> int:
    """The most ``exclude`` patterns an ``allow`` rule may list and still reach KAS.

    KAS ANDs an exclude list onto every statement it compiles, so a longer one is
    not split the way a long ``match`` is: the projection leaves that allow out.
    """
    from kiro_crew.acp.kas_permissions import _MAX_PATTERNS_PER_RULE

    return _MAX_PATTERNS_PER_RULE


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


def builtin_allow_tool(rule: dict[str, Any]) -> str:
    """The ``allowedTools`` name KAS's projection derives built-in allow *rule* from.

    Only an unscoped allow (``capability`` and ``effect`` alone) on a capability
    exactly one Crew tool name stands for (``kas_permissions.CAPABILITY_BY_TOOL``,
    ``use_subagent`` for ``subagent``) has one; any other rule answers ``""``, so
    a name-scoped allow is never widened to the whole capability.
    """
    from kiro_crew.acp import kas_permissions

    if set(rule) != {"capability", "effect"} or rule.get("effect") != "allow":
        return ""
    names = [
        tool
        for tool, capability in kas_permissions.CAPABILITY_BY_TOOL.items()
        if capability == rule.get("capability")
    ]
    return names[0] if len(names) == 1 else ""


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
    So is an ``allowedCommands`` entry Crew reads by ASCII (``\\d``, ``\\w``,
    ``\\s``, ``\\b`` or ``(?i)``, ``Pattern.ascii_only``): kiro-cli reads those by
    Unicode, so it is not carried exactly.
    Each entry is named under the spelling *raw* wrote (``aliases.as_authored``).
    """
    from kiro_crew.acp import kas_permissions
    from kiro_crew.platform import allowed_commands
    from kiro_crew.platform import tools_settings_aliases as aliases

    settings, dropped = kas_permissions.canonical_tools_settings(raw)
    _block, not_carried = kas_permissions.tools_settings_to_permissions(settings)
    inexact = [
        f"{key}.allowedCommands"
        for key, entry in (settings.items() if isinstance(settings, dict) else ())
        if key in ("execute_bash", "shell")
        and isinstance(entry, dict)
        and isinstance(entry.get("allowedCommands"), list)
        and any(
            (parsed := allowed_commands.parse(p)) is not None and parsed.ascii_only
            for p in entry["allowedCommands"]
        )
    ]
    return sorted(
        {aliases.as_authored(raw, path) for path in (*not_carried, *inexact)} | set(dropped)
    )


def unreadable_allowed_commands(raw: Any) -> list[tuple[str, str, str]]:
    """``(shell key, entry, reason)`` for each ``allowedCommands`` entry in
    ``toolsSettings`` *raw* that Kiro Crew's gate cannot read
    (``kas_unread_commands.unreadable_allowed_commands``), read with one key per
    kiro-cli alias group as the gate reads it, and each key named under the
    spelling *raw* wrote (``aliases.as_authored``). For ``kirocrew doctor``."""
    from kiro_crew.acp import kas_permissions, kas_unread_commands
    from kiro_crew.platform import tools_settings_aliases as aliases

    settings, _dropped = kas_permissions.canonical_tools_settings(raw)
    return [
        (aliases.as_authored(raw, key), entry, reason)
        for key, entry, reason in kas_unread_commands.unreadable_allowed_commands(settings)
    ]


def spec_unjudged_denies(spec: dict[str, Any], *, conditional: bool = False) -> list[str]:
    """The deny entries neither KAS nor Crew's gate can fully judge for *spec*.

    Combine authored and translated rules as the worker projection does, then
    use its classifier so the Add-abilities notice names the same shell hold.
    With *conditional*, the entries only the gate checks, in a simple command or
    chain (``kas_spec_denies.unjudged_denied_commands``).
    """
    from kiro_crew.acp.kas_permissions import tools_settings_to_permissions
    from kiro_crew.acp.kas_spec_denies import unjudged_denied_commands
    from kiro_crew.platform.tools_settings_aliases import canonical_tools_settings

    settings, _dropped = canonical_tools_settings(spec.get("toolsSettings"))
    if not isinstance(settings, dict):
        return []
    translated, _lost = tools_settings_to_permissions(settings)
    permissions = spec.get("permissions")
    authored = permissions.get("rules") if isinstance(permissions, dict) else None
    rules = [
        *(authored if isinstance(authored, list) else []),
        *(translated or {}).get("rules", []),
    ]
    return unjudged_denied_commands(settings, {"rules": rules}, conditional=conditional)


def worker_deny_records(value: Any) -> tuple[dict[str, Any] | str, ...]:
    """Validate the Crew-only record; legacy strings and malformed items hold unconditionally."""
    if not isinstance(value, (list, tuple)):
        return (str(value),)
    return tuple(
        (
            dict(item)
            if (
                isinstance(item, dict)
                and isinstance(item.get("key"), str)
                and "entry" in item
                and all(
                    isinstance(item.get(key), bool)
                    for key in ("conditional", "past_bound", "whole")
                )
                and (
                    not item["conditional"]
                    or (
                        isinstance(item["entry"], str)
                        and not item["whole"]
                        and not item["past_bound"]
                    )
                )
            )
            else str(item)
        )
        for item in value
    )


def unjudged_denies_for_command(
    entries: tuple[dict[str, Any] | str, ...], command: str | None
) -> tuple[str, ...]:
    """Worker denies without a definite no-match, using structured classification."""
    from kiro_crew.acp.kas_spec_denies import deny_entry_text
    from kiro_crew.platform.spec_denied_gate import shell_denies_match

    return tuple(
        deny_entry_text(item) if isinstance(item, dict) else item
        for item in worker_deny_records(entries)
        if not isinstance(item, dict)
        or not item["conditional"]
        or shell_denies_match((item["entry"],), command) is not False
    )


def split_deny_notices(keys: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Render serialized deny records into judged/unjudged groups and leave other keys intact."""
    import json

    from kiro_crew.acp.kas_spec_denies import denied_command_notice

    judged: list[str] = []
    unjudged: list[str] = []
    plain: list[str] = []
    for key in keys:
        if key.startswith('{"deniedCommand":'):
            item = json.loads(key)["deniedCommand"]
            target = judged if item["conditional"] else unjudged
            target.append(denied_command_notice({**item, "conditional": False}))
        else:
            plain.append(key)
    return judged, unjudged, plain


def deny_sentences(agent: str, judged: list[str], unjudged: list[str], worker: bool) -> list[str]:
    """The session-start notice's sentences for rendered deny records.

    *judged* are gate-only entries; *unjudged* are ones nothing checks, passed
    only for a Bot's *worker* (``kirocrew-mate-worker``, the agent the KAS
    projection records them for, ``kas_spec_denies.with_unjudged_denies``). There
    the ``spec-unjudged-denies`` row refuses a shell command that is not read-only
    and that an entry cannot be checked against, so the notice says so instead of
    "no effect" or "runs without a refusal".
    """
    out = []
    if unjudged:
        out.append(
            f"Agent {agent} sets {' and '.join(unjudged)}, which this backend does not "
            "receive and Kiro Crew cannot check, so this Bot's worker refuses every "
            "shell command that is not read-only."
        )
    if judged:
        out.append(
            f"Agent {agent} sets {' and '.join(judged)}, which this backend does not "
            "receive. Kiro Crew's own approval gate refuses a plain command they name, "
            "and each command of a plain chain (&&, ||, ;, |). It does not judge any "
            "other command, such as one with $(...), a backtick, a redirect, a newline, "
            "a trailing &, a tab, a ( ) group, or a quote, # or [[ in a chain, so "
            + (
                "this Bot's worker refuses such a command unless it is read-only."
                if worker
                else "such a command runs without a refusal."
            )
        )
    return out

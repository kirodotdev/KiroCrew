"""A crewmate worker's spec policy: the ``hooks``, ``permissions`` and ``toolsSettings`` it runs under.

A template copied into the crewmate's file (``mate_worker_kit``) brings these three
fields along, and the crewmate's worker runs under them: the worker spec layers
them over the worker base (:func:`layer_policy`, called by
``mate_worker.merge_worker_spec``). Nothing from the file reaches the conductor
(the crewmate's own chat), which runs the stock spec (owner decision,
``docs/decisions``).

Carrying is exact or refused. A hook is added beside the base's, a permission
rule beside the base's rules, and a ``toolsSettings`` entry is copied whole for a
tool the base sets nothing for. An ``mcp`` allow the worker's ``allowedTools``
would otherwise drop also lands as that list's matching ``@server[/tool]`` refs,
whether or not the base declares the server, and an unscoped built-in allow
(``subagent``, ``skill``) as the one tool name KAS derives it from. A field
that cannot land exactly (an entry for a tool the base already sets
differently, a ``permissions`` block KAS would refuse whole) raises :class:`SpecPolicyUncarried` naming it. The governance ceiling still
applies to the merged spec, so a grant the template declares is never wider than
the ceiling allows; KAS's projection relays a ``deny``/``ask`` rule as written and
an ``allow`` only where the ceiling permits it (``kas_permissions.merge_user_permissions``).

Only KAS applies the three fields on a worker session (:func:`applies_spec_policy`),
so a crewmate on any other backend is refused a template that sets them
(:func:`backend_refusal`). Stateless.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

from kiro_crew.agent_sdk.kas_tool_view import (
    allow_granted_by_spec,
    builtin_allow_tool,
    permission_allow_derivable,
    permissions_block_readable,
    tools_settings_family,
    tools_settings_unenforced,
)
from kiro_crew.mate_worker import WorkerKitUnreadable
from kiro_crew.platform.tools_settings_aliases import canonical_tools_settings
from kiro_crew.user_json import copy_json_document

logger = logging.getLogger(__name__)

#: The spec fields a crewmate's file carries for its worker.
POLICY_KEYS = ("hooks", "permissions", "toolsSettings")


def applies_spec_policy(backend: str) -> bool:
    """Whether a crewmate's worker on *backend* runs its ``hooks``, ``permissions``
    and ``toolsSettings``.

    KAS only: it takes the merged spec over the wire, translates ``toolsSettings``
    and merges ``permissions`` (``kas_agents``), and Crew fires the hooks from the
    session's merged spec (``agent_sdk.spec_hooks``). claude runs none of the
    three, and kiro-cli reads its spec by name off disk, so a per-crewmate layer
    never reaches it.
    """
    from kiro_crew.acp_backends import ACP_BACKEND_KAS

    return backend == ACP_BACKEND_KAS


class SpecPolicyUncarried(WorkerKitUnreadable):
    """A crewmate's policy cannot be carried exactly; ``fields`` names what.

    A :class:`~kiro_crew.mate_worker.WorkerKitUnreadable`, so every caller that
    fails a session closed on an unreadable kit refuses this one the same way.
    """

    def __init__(self, fields: list[str], why: str = "") -> None:
        self.fields = list(dict.fromkeys(fields))
        detail = why or "they cannot be applied exactly"
        super().__init__(f"the Bot's {', '.join(self.fields)} cannot be carried: {detail}")


def uncarried(fields: list[str], why: str = "") -> SpecPolicyUncarried:
    """The refusal for *fields*, which could not be carried exactly."""
    return SpecPolicyUncarried(fields, why)


def _dicts(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def policy_of(spec: dict[str, Any]) -> dict[str, Any]:
    """The policy fields *spec* sets, each a copy; a field set to nothing is left out."""
    out: dict[str, Any] = {}
    for key in POLICY_KEYS:
        value = spec.get(key)
        if value is None or value == {} or value == []:
            continue
        out[key] = copy_json_document(value)
    return out


def without_held(policy: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """*policy* less every hook, permission rule and tool setting *base* already holds.

    What the worker base carries reaches the worker without the crewmate's file, so
    only what the template adds is written and judged. Compared as the layering
    dedupes: a hook per event, a rule, a tool's entry, each whole.
    """
    out: dict[str, Any] = {}
    hooks = policy.get("hooks")
    if isinstance(hooks, dict):
        held = _dicts(base.get("hooks"))
        kept_hooks: dict[str, Any] = {}
        for event, entries in hooks.items():
            have = held.get(event)
            if isinstance(have, list):
                held_entries = have
            elif have:
                held_entries = [have]
            else:
                held_entries = []
            seen = {_canon(e) for e in held_entries}
            listed = entries if isinstance(entries, list) else [entries]
            rest = [e for e in listed if _canon(e) not in seen]
            if rest:
                kept_hooks[event] = rest
        if kept_hooks:
            out["hooks"] = kept_hooks
    elif "hooks" in policy:
        out["hooks"] = hooks
    perms = policy.get("permissions")
    if (
        isinstance(perms, dict)
        and set(perms) <= {"rules"}
        and isinstance(perms.get("rules", []), list)
    ):
        held_rules = _dicts(base.get("permissions")).get("rules")
        seen = {_canon(r) for r in held_rules} if isinstance(held_rules, list) else set()
        rules = [r for r in perms.get("rules") or [] if _canon(r) not in seen]
        if rules:
            out["permissions"] = {"rules": rules}
    elif "permissions" in policy:
        out["permissions"] = perms
    settings = policy.get("toolsSettings")
    if isinstance(settings, dict):
        held_settings = _dicts(base.get("toolsSettings"))
        kept = {
            tool: entry
            for tool, entry in settings.items()
            if tool not in held_settings or _canon(held_settings[tool]) != _canon(entry)
        }
        if kept:
            out["toolsSettings"] = kept
    elif "toolsSettings" in policy:
        out["toolsSettings"] = settings
    return out


def _layer_hooks(target: dict[str, Any], hooks: Any, lost: list[str]) -> None:
    if not isinstance(hooks, dict):
        lost.append("hooks")
        return
    have = _dicts(target.get("hooks"))
    merged = {event: list(v) if isinstance(v, list) else [v] for event, v in have.items()}
    for event, entries in hooks.items():
        listed = merged.setdefault(str(event), [])
        seen = {_canon(e) for e in listed}
        for entry in entries if isinstance(entries, list) else [entries]:
            if _canon(entry) not in seen:
                listed.append(copy_json_document(entry))
                seen.add(_canon(entry))
    # Crew fires at most this many spec hooks and drops the rest, so a hook
    # layered past the cap would be written but never run.
    from kiro_crew.agent_sdk.spec_hooks import _MAX_SPEC_HOOKS

    if sum(len(v) for v in merged.values()) > _MAX_SPEC_HOOKS:
        lost.append("hooks")
        return
    target["hooks"] = merged


def _layer_permissions(
    target: dict[str, Any], perms: Any, lost: list[str], *, worker: bool = False
) -> None:
    if not (
        isinstance(perms, dict)
        and set(perms) <= {"rules"}
        and isinstance(perms.get("rules", []), list)
    ):
        # A block KAS would refuse whole (a named bundle, an unknown key, a rule
        # it cannot parse) carries none of its denies either.
        lost.append("permissions")
        return
    have = _dicts(target.get("permissions"))
    rules = list(have.get("rules") or []) if isinstance(have.get("rules"), list) else []
    seen = {_canon(r) for r in rules}
    for rule in perms.get("rules") or []:
        if _canon(rule) not in seen:
            rules.append(copy_json_document(rule))
            seen.add(_canon(rule))
    merged = {**copy_json_document(have), "rules": rules}
    if not permissions_block_readable(merged):
        lost.append("permissions")
        return
    allowed_tools = target.get("allowedTools")
    if worker and isinstance(allowed_tools, list):
        # With an ``allowedTools`` list present, KAS's projection drops an allow
        # that list could express itself. One the list already grants is no
        # loss; one it does not is carried as the list's own entries (``mcp``
        # refs, or a built-in tool name); any other would be written while the
        # worker still asks.
        held_rules = have.get("rules")
        held = {_canon(r) for r in held_rules} if isinstance(held_rules, list) else set()
        lost.extend(
            f"permissions rule {i} (allow {rule.get('capability')})"
            for i, rule in enumerate(perms.get("rules") or [])
            if isinstance(rule, dict)
            and _canon(rule) not in held
            and permission_allow_derivable(rule)
            and not allow_granted_by_spec(rule, target)
            and not _carry_mcp_allow(rule, target)
            and not _carry_builtin_allow(rule, target)
        )
    target["permissions"] = merged


def mcp_allow_refs(rule: Any) -> list[str] | None:
    """The ``allowedTools`` refs that express ``mcp`` *rule*'s ``match``, deduplicated.

    ``server/*`` becomes ``@server`` and any other pattern ``@<pattern>``, the
    refs KAS's projection derives back into the same rule. ``None`` for a rule
    that is not ``mcp``, or whose ``match`` is not a list of strings. The worker
    merge (:func:`_carry_mcp_allow`) and the Add-abilities preview
    (``mate_worker_kit``) both read a carried allow through this one mapping.
    """
    if not isinstance(rule, dict) or rule.get("capability") != "mcp":
        return None
    match = rule.get("match") or []
    if not isinstance(match, list):
        return None
    refs = []
    for pattern in match:
        if not isinstance(pattern, str):
            return None
        server, _, tool = pattern.partition("/")
        refs.append(f"@{server}" if tool == "*" else f"@{pattern}")
    return list(dict.fromkeys(refs))


def _carry_builtin_allow(rule: dict[str, Any], target: dict[str, Any]) -> bool:
    """Add the ``allowedTools`` name that grants built-in allow *rule*, exactly.

    The name is :func:`builtin_allow_tool`'s, the one entry KAS's projection
    derives back into the same rule (``use_subagent`` for ``subagent``,
    ``disclose_context`` for ``skill``), so nothing wider than the rule is
    written; the governance ceiling still runs over the merged spec afterwards.
    An excluded tool is proved but not written, as :func:`_carry_mcp_allow`
    treats one. Returns whether the rule is granted.
    """
    name = builtin_allow_tool(rule)
    if not name:
        return False
    have = list(target.get("allowedTools") or [])
    new = [] if name in have else [name]
    if not allow_granted_by_spec(rule, {**target, "allowedTools": [*have, *new]}):
        return False
    excluded = set(target.get("excludedTools") or [])
    target["allowedTools"] = [*have, *(n for n in new if n not in excluded)]
    return True


def _carry_mcp_allow(rule: dict[str, Any], target: dict[str, Any]) -> bool:
    """Add the ``allowedTools`` refs that grant ``mcp`` allow *rule*, exactly.

    The refs are :func:`mcp_allow_refs`. They are added only when
    :func:`allow_granted_by_spec` then proves the rule granted, so nothing
    wider than the rule is written; the governance ceiling still runs over the
    merged spec afterwards. A ref for an excluded tool or a muted server is
    proved but not written: the merge already took those grants away, and the
    tool they name never reaches the worker. Returns whether the rule is granted.
    """
    refs = mcp_allow_refs(rule)
    if refs is None:
        return False
    have = list(target.get("allowedTools") or [])
    new = [r for r in refs if r not in have]
    if not allow_granted_by_spec(rule, {**target, "allowedTools": [*have, *new]}):
        return False
    excluded = set(target.get("excludedTools") or [])
    servers = _dicts(target.get("mcpServers"))
    target["allowedTools"] = [
        *have,
        *(
            r
            for r in new
            if r not in excluded
            and _dicts(servers.get(r[1:].partition("/")[0])).get("disabled") is not True
        ),
    ]
    return True


def _layer_tools_settings(target: dict[str, Any], settings: Any, lost: list[str]) -> None:
    if not isinstance(settings, dict):
        lost.append("toolsSettings")
        return
    have = copy_json_document(_dicts(target.get("toolsSettings")))
    held_families = {tools_settings_family(t): t for t in have}
    for tool, entry in settings.items():
        alias = held_families.get(tools_settings_family(tool))
        if alias is not None and alias != tool and _canon(have[alias]) != _canon(entry):
            # Another spelling of the same tool (``shell`` for ``execute_bash``):
            # only the spelling kiro-cli reads first takes effect, so this one
            # could silently replace the other's limits.
            lost.append(f"toolsSettings.{tool}")
        elif tool not in have:
            have[tool] = copy_json_document(entry)
        elif _canon(have[tool]) != _canon(entry):
            # Two settings for one tool have no exact union: either one alone
            # would loosen or tighten the other.
            lost.append(f"toolsSettings.{tool}")
    lost.extend(_outside_allowlist(settings))
    # A listed key whose value KAS's translation drops would be copied but never
    # take effect.
    lost.extend(f"toolsSettings.{path}" for path in tools_settings_unenforced(settings))
    if have:
        target["toolsSettings"] = have


#: The ``toolsSettings`` keys carried onto a worker, by tool family: the ones KAS
#: or Crew's gate is shown to apply on a worker session. Every other key, in any
#: form, is refused by name (owner decision, ``docs/decisions``).
CARRIED_TOOLS_SETTINGS: dict[str, frozenset[str]] = {
    "shell": frozenset({"deniedCommands", "allowedCommands"}),
    "read": frozenset({"deniedPaths"}),
    "write": frozenset({"deniedPaths"}),
    "web_fetch": frozenset({"blocked"}),
    # KAS turns it into a ``subagent`` deny, and Crew's spawn gate reads the
    # worker's merged spec for it (``agent_policy.parent_template_allowlists``).
    "subagent": frozenset({"availableAgents"}),
}

#: Families carried under one spelling only. The spawn gate reads
#: ``availableAgents`` from the ``subagent`` key alone, so a ``use_subagent``
#: copy would reach KAS but not ``spawn_run``.
_CARRIED_SPELLINGS: dict[str, frozenset[str]] = {"subagent": frozenset({"subagent"})}


def _outside_allowlist(settings: dict[str, Any]) -> list[str]:
    """The ``toolsSettings`` paths in *settings* outside :data:`CARRIED_TOOLS_SETTINGS`."""
    out: list[str] = []
    for tool, entry in settings.items():
        family = tools_settings_family(tool)
        spellings = _CARRIED_SPELLINGS.get(family)
        allowed = (
            CARRIED_TOOLS_SETTINGS.get(family)
            if spellings is None or tool in spellings
            else frozenset()
        )
        if not isinstance(entry, dict) or (allowed is None and not entry):
            out.append(f"toolsSettings.{tool}")
            continue
        out.extend(f"toolsSettings.{tool}.{key}" for key in entry if key not in (allowed or ()))
    return out


#: A sub-agent name with no pattern in it: the only kind a ``subagent`` allow's
#: ``match`` is proved to cover by listing it.
_LITERAL_AGENT_NAME = re.compile(r"[A-Za-z0-9_.\-]+")


def _web_fetch_trust_held(value: Any, merged: dict[str, Any]) -> bool:
    """``web_fetch.trusted``: the merged worker already fetches every URL unasked.

    Proved by :func:`allow_granted_by_spec`: the worker's own ``allowedTools``
    derive a bare ``web_fetch`` allow, which covers any URL the list trusts.
    """
    return (
        isinstance(value, list)
        and all(isinstance(v, str) for v in value)
        and allow_granted_by_spec({"capability": "web_fetch", "effect": "allow"}, merged)
    )


def _trusted_agents_held(value: Any, merged: dict[str, Any]) -> bool:
    """``subagent.trustedAgents``: a ``subagent`` allow the merged worker carries
    names every trusted agent.

    KAS turns the key into that same allow, so it adds nothing. Only literal
    names are proved: a pattern, in the list or in the allow's ``match``, is not
    read as covering anything, and an allow with an ``exclude`` does not count.
    """
    if not isinstance(value, list) or not value:
        return False
    if not all(isinstance(v, str) and _LITERAL_AGENT_NAME.fullmatch(v) for v in value):
        return False
    rules = _dicts(merged.get("permissions")).get("rules")
    named: set[str] = set()
    for rule in rules if isinstance(rules, list) else []:
        if (
            isinstance(rule, dict)
            and rule.get("capability") == "subagent"
            and rule.get("effect") == "allow"
            and not rule.get("exclude")
            and isinstance(rule.get("match"), list)
        ):
            named.update(m for m in rule["match"] if isinstance(m, str))
    return all(v in named for v in value)


#: The grant-only ``toolsSettings`` keys Add abilities leaves out of a template's
#: copy when the merged worker already grants what they grant, by tool family,
#: each with its proof. Kit time only: the worker merge still refuses them in a
#: file, so a hand-edited one fails closed (owner decision, ``docs/decisions``).
HELD_GRANT_SETTINGS: dict[str, dict[str, Callable[[Any, dict[str, Any]], bool]]] = {
    "web_fetch": {"trusted": _web_fetch_trust_held},
    "subagent": {"trustedAgents": _trusted_agents_held},
}


def grant_settings_in(spec: dict[str, Any]) -> list[tuple[str, str]]:
    """The ``(tool, key)`` pairs of :data:`HELD_GRANT_SETTINGS` *spec*'s ``toolsSettings`` sets.

    Empty when one of those tool families is written under two spellings: kiro-cli
    drops one (``canonical_tools_settings``), and leaving a key out could change
    which one is kept, so the gates name the dropped spelling instead.
    """
    settings = spec.get("toolsSettings")
    if not isinstance(settings, dict):
        return []
    _folded, dropped = canonical_tools_settings(settings)
    if any(tools_settings_family(tool) in HELD_GRANT_SETTINGS for tool in dropped):
        return []
    out: list[tuple[str, str]] = []
    for tool, entry in settings.items():
        keys = HELD_GRANT_SETTINGS.get(tools_settings_family(tool), {})
        if isinstance(entry, dict):
            out.extend((str(tool), key) for key in entry if key in keys)
    return out


def held_grant_settings(spec: dict[str, Any], merged: dict[str, Any]) -> list[tuple[str, str]]:
    """The pairs of :func:`grant_settings_in` that merged worker spec *merged* already grants."""
    settings = _dicts(spec.get("toolsSettings"))
    return [
        (tool, key)
        for tool, key in grant_settings_in(spec)
        if HELD_GRANT_SETTINGS[tools_settings_family(tool)][key](settings[tool][key], merged)
    ]


def without_settings(spec: dict[str, Any], pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """A copy of *spec* less each ``(tool, key)`` of *pairs* in its ``toolsSettings``.

    A tool entry left empty is dropped, and so is a ``toolsSettings`` left empty.
    """
    out = copy_json_document(spec)
    settings = out.get("toolsSettings")
    if not isinstance(settings, dict):
        return out
    for tool, key in pairs:
        entry = settings.get(tool)
        if isinstance(entry, dict):
            entry.pop(key, None)
            if not entry:
                settings.pop(tool)
    if not settings:
        out.pop("toolsSettings")
    return out


def layer_policy(target: dict[str, Any], policy: dict[str, Any], *, worker: bool = False) -> None:
    """Layer *policy* (:func:`policy_of`) onto *target*, in place, exactly.

    Raises :func:`uncarried` naming every field that could not land as written;
    *target* is then partly layered and must be discarded. *worker* says *target*
    is the merged worker spec, whose ``allowedTools`` KAS's projection reads: an
    allow that list neither grants nor lets travel is then refused too.
    """
    lost: list[str] = []
    if "hooks" in policy:
        _layer_hooks(target, policy["hooks"], lost)
    if "permissions" in policy:
        _layer_permissions(target, policy["permissions"], lost, worker=worker)
    if "toolsSettings" in policy:
        _layer_tools_settings(target, policy["toolsSettings"], lost)
    if lost:
        raise uncarried(lost)


def backend_refusal(policy: dict[str, Any], backend: str) -> str | None:
    """Why a crewmate on *backend* cannot run a file carrying *policy*, else ``None``."""
    if not policy or applies_spec_policy(backend):
        return None
    from kiro_crew.acp_backends import backend_to_stored

    return (
        f"the Bot's {', '.join(sorted(policy))} cannot be applied on "
        f"{backend_to_stored(backend)}; run the Bot on kas"
    )

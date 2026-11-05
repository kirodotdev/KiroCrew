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
whether or not the base declares the server. A field that cannot land exactly (an entry for a
tool the base already sets differently, a ``permissions`` block KAS would refuse
whole) raises :class:`SpecPolicyUncarried` naming it. The governance ceiling still
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
from typing import Any

from kiro_crew.agent_sdk.kas_tool_view import (
    allow_granted_by_spec,
    permission_allow_derivable,
    permissions_block_readable,
    tools_settings_family,
    tools_settings_unenforced,
)
from kiro_crew.mate_worker import WorkerKitUnreadable
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
        # loss; an ``mcp`` one it does not is carried as the list's own refs;
        # any other would be written while the worker still asks.
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
        )
    target["permissions"] = merged


def _carry_mcp_allow(rule: dict[str, Any], target: dict[str, Any]) -> bool:
    """Add the ``allowedTools`` refs that grant ``mcp`` allow *rule*, exactly.

    ``server/*`` becomes ``@server`` and any other pattern ``@<pattern>``, the
    refs KAS's projection derives back into the same rule. They are added only
    when :func:`allow_granted_by_spec` then proves the rule granted, so nothing
    wider than the rule is written; the governance ceiling still runs over the
    merged spec afterwards. A ref for an excluded tool or a muted server is
    proved but not written: the merge already took those grants away, and the
    tool they name never reaches the worker. Returns whether the rule is granted.
    """
    if rule.get("capability") != "mcp":
        return False
    refs = []
    for pattern in rule.get("match") or []:
        if not isinstance(pattern, str):
            return False
        server, _, tool = pattern.partition("/")
        refs.append(f"@{server}" if tool == "*" else f"@{pattern}")
    have = list(target.get("allowedTools") or [])
    new = [r for r in dict.fromkeys(refs) if r not in have]
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
            # the gate unions both, so this one would widen the other's limits.
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
}


def _outside_allowlist(settings: dict[str, Any]) -> list[str]:
    """The ``toolsSettings`` paths in *settings* outside :data:`CARRIED_TOOLS_SETTINGS`."""
    out: list[str] = []
    for tool, entry in settings.items():
        allowed = CARRIED_TOOLS_SETTINGS.get(tools_settings_family(tool))
        if not isinstance(entry, dict) or (allowed is None and not entry):
            out.append(f"toolsSettings.{tool}")
            continue
        out.extend(f"toolsSettings.{tool}.{key}" for key in entry if key not in (allowed or ()))
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

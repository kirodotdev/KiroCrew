"""The KAS projection's record of grants Crew honours once it checks a spec's denies.

``kas_agents.to_client_custom_agent`` keeps a shell or write grant at the prompt
when the spec also sets ``deniedCommands`` or a write ``deniedPaths``
(``kas_permissions.withhold_answered_under_authored_rules``). When every entry of
that deny list is one Crew reads the way kiro-cli does, this module records the
grants the deny alone held back under :data:`CREW_SPEC_DENIES_KEY`, together with
the list, and the session handle reads them back as one
:class:`~kiro_crew.platform.spec_denied_grant.SpecDeniedGrant`. See that module for
how the gate uses it. Any other key beside a deny (``denyByDefault``,
``fallbackAction``, an authored ``ask``) keeps the grant withheld, as before.
"""

from __future__ import annotations

import os
from typing import Any, cast

from kiro_crew.acp.kas_grant_only_settings import without_grant_only_settings
from kiro_crew.acp.kas_permissions import (
    _SHELL_SETTINGS_KEYS,
    _WRITE_SETTINGS_KEYS,
    CREW_SPEC_DENIES_KEY,
    withhold_answered_under_authored_rules,
)
from kiro_crew.platform import allowed_commands, allowed_paths
from kiro_crew.platform.spec_denied_grant import SpecDeniedGrant

#: The built-ins a checked deny may lift: the ones whose calls the gate can test
#: against it (a shell call's command, a write's targets). ``code`` both edits and
#: runs commands, so it stays withheld.
_LIFTABLE: frozenset[str] = frozenset(
    {"execute_bash", "execute_pwsh", "fs_write", "fs_append", "str_replace"}
)

#: Bound on each recorded deny list, like the allowlists'.
_MAX_DENIES = 64


def _checkable(tools_settings: Any, keys: tuple[str, ...], field: str, read: Any) -> list[str]:
    """Every entry of *field* on *keys*, when *read* accepts each one; else ``[]``.

    ``[]`` when no key sets the field, when any value is not a list of strings, when
    any entry, blank-looking ones included, is outside what *read* accepts, or past
    the bound: then the
    deny is not lifted and keeps its grant at the prompt (fail toward ask).
    """
    if not isinstance(tools_settings, dict):
        return []
    out: list[str] = []
    for key in keys:
        settings = tools_settings.get(key)
        if not isinstance(settings, dict) or field not in settings:
            continue
        entries = settings[field]
        if not isinstance(entries, list):
            return []
        for raw in entries:
            # Kept verbatim: a blank-looking entry such as " " is a real path or
            # command to kiro-cli, so it is checked like any other, never dropped.
            if not isinstance(raw, str) or read(raw) is None:
                return []
            out.append(raw)
    out = list(dict.fromkeys(out))
    return out if len(out) <= _MAX_DENIES else []


def _without(tools_settings: Any, keys: tuple[str, ...], field: str) -> Any:
    """*tools_settings* without *field* on *keys*; an entry left empty goes too."""
    if not isinstance(tools_settings, dict):
        return tools_settings
    out = dict(tools_settings)
    for key in keys:
        entry = out.get(key)
        if isinstance(entry, dict) and field in entry:
            rest = {k: v for k, v in entry.items() if k != field}
            if rest:
                out[key] = rest
            else:
                del out[key]
    return out


def spec_denies_record(
    spec: dict[str, Any],
    *,
    candidates: frozenset[str],
    answered: frozenset[str],
    policy: Any,
    commands: list[str],
    commands_answered: bool,
) -> dict[str, Any] | None:
    """The :data:`CREW_SPEC_DENIES_KEY` value for *spec*, or ``None``.

    *candidates* are the built-ins the spec grants that the ceiling allows,
    *answered* those already answered unconditionally, *policy* the merged
    ``permissions`` BEFORE the translated ``toolsSettings`` (whose denies are the
    ones lifted here; an authored rule still counts), *commands* the usable
    ``allowedCommands`` and *commands_answered* whether they are already answered.
    """
    # shortcut: every alias spelling is read, as this branch's other readers do, so
    # a deny under a spelling kiro-cli discards still refuses, and any other key
    # under it still withholds (fail toward ask); fold aliases if one is reported.
    tools_settings = spec.get("toolsSettings")
    denied_commands = _checkable(
        tools_settings, _SHELL_SETTINGS_KEYS, "deniedCommands", allowed_commands.parse_deny
    )
    denied_paths = _checkable(
        tools_settings, _WRITE_SETTINGS_KEYS, "deniedPaths", allowed_paths.parse
    )
    if not (denied_commands or denied_paths):
        return None
    lifted_settings = without_grant_only_settings(tools_settings)
    if denied_commands:
        lifted_settings = _without(lifted_settings, _SHELL_SETTINGS_KEYS, "deniedCommands")
    if denied_paths:
        lifted_settings = _without(lifted_settings, _WRITE_SETTINGS_KEYS, "deniedPaths")
    lifted = withhold_answered_under_authored_rules(
        candidates | {"execute_bash"},
        policy,
        authored_permissions=spec.get("permissions"),
        tools_settings=lifted_settings,
    )
    builtins = sorted((lifted & candidates & _LIFTABLE) - answered)
    from kiro_crew.platform.governance import may_grant_builtin_at_gate_now

    lifted_commands = (
        commands
        if commands
        and not commands_answered
        and "execute_bash" in lifted
        and may_grant_builtin_at_gate_now("execute_bash")
        else []
    )
    if not (builtins or lifted_commands):
        return None
    return {
        "builtins": builtins,
        "commands": list(lifted_commands),
        "deniedCommands": denied_commands,
        "deniedPaths": denied_paths,
    }


def commands_lifted_by_denies(spec: dict[str, Any]) -> bool:
    """Whether *spec*'s ``allowedCommands`` are answered once its denies are
    checked, read with no merged policy, for the session-start notice."""
    from kiro_crew.acp.kas_permissions import crew_answered_commands

    record = spec_denies_record(
        spec,
        candidates=frozenset(),
        answered=frozenset(),
        policy=None,
        commands=crew_answered_commands(spec.get("toolsSettings")),
        commands_answered=False,
    )
    return bool(record and record["commands"])


def _entry_spec_denies(entry: Any, cwd: str) -> SpecDeniedGrant | None:
    """One projected entry's record as a :class:`SpecDeniedGrant`, anchored at *cwd*.

    ``None`` when the entry has none, or when any part of it is malformed: no
    grant, so the call keeps asking.
    """
    raw = entry.get(CREW_SPEC_DENIES_KEY) if isinstance(entry, dict) else None
    if not isinstance(raw, dict):
        return None
    fields = ("builtins", "commands", "deniedCommands", "deniedPaths")
    values = [raw.get(f) for f in fields]
    if not all(isinstance(v, list) and all(isinstance(x, str) for x in v) for v in values):
        return None
    builtins, commands, denied_commands, denied_paths = cast(list[list[str]], values)
    from kiro_crew.acp.kas_write_allowlist import anchor_write_allowlist

    per_entry = [anchor_write_allowlist([path], cwd) for path in dict.fromkeys(denied_paths)]
    if not all(per_entry):
        # An entry that cannot be anchored here (a relative one with no session
        # directory) checks nothing, so nothing it held back is granted.
        return None
    anchored = tuple(dict.fromkeys(glob for globs in per_entry for glob in globs))
    return SpecDeniedGrant(
        builtins=frozenset(builtins),
        commands=tuple(commands),
        denied_commands=tuple(denied_commands),
        denied_write_paths=anchored,
        cwd=cwd if os.path.isabs(cwd) else "",
    )


def projected_spec_denies(custom_agents: Any, agent_id: str, cwd: str) -> SpecDeniedGrant | None:
    """*agent_id*'s registered entry's record, matched like
    ``kas_agents.projected_crew_answered``."""
    from kiro_crew.acp.kas_agents import _registered_entry

    return _entry_spec_denies(_registered_entry(custom_agents, agent_id), cwd)


def switched_spec_denies(custom_agents: Any, mode_id: str, cwd: str) -> SpecDeniedGrant | None:
    """That record once a KAS mode switch moves the session to *mode_id*."""
    from kiro_crew.acp.kas_agents import _switched_entry

    return _entry_spec_denies(_switched_entry(custom_agents, mode_id), cwd)

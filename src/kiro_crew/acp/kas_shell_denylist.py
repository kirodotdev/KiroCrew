"""A KAS session's ``deniedCommands`` record, read back from its batch.

The projection (``kas_agents.to_client_custom_agent``) records the spec's
``toolsSettings`` shell ``deniedCommands`` as written under
:data:`~kiro_crew.acp.kas_permissions.CREW_SHELL_DENYLIST_KEY`. The session
handle reads them back here, for the agent it runs and again after a KAS mode
switch, and stamps them on its own requests; the gate's ``spec-shell-denylist``
row refuses what they name (:mod:`kiro_crew.platform.spec_shell_denylist`).
"""

from __future__ import annotations

from typing import Any

from kiro_crew.acp.kas_agents import _registered_entry, _switched_entry
from kiro_crew.acp.kas_permissions import CREW_SHELL_DENYLIST_KEY
from kiro_crew.platform.spec_shell_denylist import DENY_ENFORCED_PREFIX, DENY_UNREADABLE_PREFIX


def denylist_notice_keys(tools_settings: Any, not_carried: list[str]) -> list[str]:
    """*not_carried* without the ``<tool>.deniedCommands`` paths, plus one notice
    key per entry KAS never received.

    Crew's gate enforces every entry, so the field itself is not named as lost.
    Each entry with no faithful KAS rule (no glob spelling, or a redirect
    character KAS never sees) is named under :data:`DENY_ENFORCED_PREFIX`, or under
    :data:`DENY_UNREADABLE_PREFIX` when Crew cannot judge it either.
    """
    from kiro_crew.acp.kas_permissions import _REDIRECT_CHARACTERS, _regex_to_glob
    from kiro_crew.platform.spec_shell_denylist import spec_denied_commands, unreadable_entries

    keys = [k for k in not_carried if not k.endswith(".deniedCommands")]
    entries = spec_denied_commands(tools_settings) or []
    unreadable = set(unreadable_entries(entries))
    for raw in entries:
        label = raw if isinstance(raw, str) else repr(raw)
        if label in unreadable:
            keys.append(DENY_UNREADABLE_PREFIX + label)
            continue
        glob = _regex_to_glob(raw.strip()) if raw.strip() else ""
        if glob is None or set(glob) & _REDIRECT_CHARACTERS:
            keys.append(DENY_ENFORCED_PREFIX + label)
    return keys


def _entry_shell_denylist(entry: Any) -> tuple[Any, ...] | None:
    """The :data:`CREW_SHELL_DENYLIST_KEY` entries of one projected entry.

    ``None`` when the entry has no such key. A value that is not a list is kept
    as one entry, which the gate cannot read and so refuses on: a restriction is
    never lifted by a malformed record.
    """
    if not isinstance(entry, dict) or CREW_SHELL_DENYLIST_KEY not in entry:
        return None
    raw = entry[CREW_SHELL_DENYLIST_KEY]
    return tuple(raw) if isinstance(raw, list) else (raw,)


def projected_shell_denylist(custom_agents: Any, agent_id: str) -> tuple[Any, ...] | None:
    """*agent_id*'s registered entry's ``deniedCommands``, matched like
    ``kas_agents.projected_shell_allowlist``."""
    return _entry_shell_denylist(_registered_entry(custom_agents, agent_id))


def switched_shell_denylist(custom_agents: Any, mode_id: str) -> tuple[Any, ...] | None:
    """Those entries once a KAS mode switch moves the session to *mode_id*."""
    return _entry_shell_denylist(_switched_entry(custom_agents, mode_id))

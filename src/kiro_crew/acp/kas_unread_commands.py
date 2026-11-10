"""The ``allowedCommands`` entries of a spec that Kiro Crew cannot read, one by one.

On KAS (and claude) Kiro Crew answers ``toolsSettings.shell.allowedCommands`` at its
own gate, and refuses what they do not match under ``denyByDefault``, reading each
entry with :mod:`kiro_crew.platform.allowed_commands`. An entry outside what that
reader reads allows nothing: a command only it matches asks for approval, or is
refused under ``denyByDefault``. So each such entry is named, with the reason, in
the three places a person meets it:

* the session-start notice (:func:`name_unreadable_entries`, through
  ``kas_agents.spec_keys_without_carrier``);
* the ``denyByDefault`` refusal, which a cron run records: the entries ride the
  projected allowlist (:func:`unreadable_allowlist_entries`), match nothing there,
  and the gate's refusal names them (``allowed_commands.unreadable_note``);
* ``kirocrew doctor`` (:func:`unreadable_allowed_commands`, through
  ``agent_sdk.kas_tool_view``).

Every reader here takes ``toolsSettings`` with one key per kiro-cli alias group
(``canonical_tools_settings``), as the gate reads it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from kiro_crew.acp.kas_spec_grants import SHELL_SETTINGS_KEYS
from kiro_crew.platform import allowed_commands

#: Most unreadable entries one projected allowlist carries for its refusal to name.
_MAX_CARRIED = 64


def _unreadable(tools_settings: Any) -> Iterator[tuple[str, object, str]]:
    """``(shell key, entry as written, reason)`` for each entry the reader refuses."""
    if not isinstance(tools_settings, dict):
        return
    for key in SHELL_SETTINGS_KEYS:
        settings = tools_settings.get(key)
        entries = settings.get("allowedCommands") if isinstance(settings, dict) else None
        for raw in entries if isinstance(entries, list) else ():
            reason = allowed_commands.refusal_reason(raw)
            if reason is not None:
                yield key, raw, reason


def unreadable_allowed_commands(tools_settings: Any) -> list[tuple[str, str, str]]:
    """``(shell key, entry, reason)`` for each ``allowedCommands`` entry of
    *tools_settings* Kiro Crew cannot read, in the spec's order. An entry that is
    not a string, or holds a character a reader could not see, is shown as JSON."""
    return [
        (key, raw if isinstance(raw, str) and raw.isprintable() else json.dumps(raw), reason)
        for key, raw, reason in _unreadable(tools_settings)
    ]


def unreadable_allowlist_entries(tools_settings: Any) -> list[str]:
    """The unreadable entries a ``denyByDefault`` allowlist carries beside its usable
    ones, so its refusal can name them.

    Only an entry that is a printable string within the reader's length bound, each
    once: it is the spec's own text, matches nothing at the gate, and is shown as
    it is. Any other value never rides along, since its JSON form could read as a
    literal entry; the notice and ``kirocrew doctor`` still name it.
    """
    out = [
        raw
        for _key, raw, _reason in _unreadable(tools_settings)
        if isinstance(raw, str)
        and raw.isprintable()
        and 0 < len(raw) <= allowed_commands.MAX_PATTERN_LENGTH
    ]
    return list(dict.fromkeys(out))[:_MAX_CARRIED]


def name_unreadable_entries(tools_settings: Any, not_carried: list[str]) -> list[str]:
    """*not_carried*, ``tools_settings_to_permissions``'s ``<tool>.<key>`` paths,
    with each unreadable ``allowedCommands`` entry named on its own as
    ``<key>.allowedCommands: <entry> (<reason>)``
    (:data:`~kiro_crew.platform.allowed_commands.UNREADABLE_ENTRY_MARK`).

    The bare ``<key>.allowedCommands`` stays when something else named it too: a
    value that is not a list (it has no entries to name), or more usable entries
    than Kiro Crew keeps.
    """
    # circular import: kas_permissions imports this module for its allowlist.
    from kiro_crew.acp.kas_permissions import _MAX_ALLOWED_COMMANDS, _usable_allowed_commands

    unread = unreadable_allowed_commands(tools_settings)
    named = {f"{key}.allowedCommands" for key, _entry, _reason in unread}
    over_bound = len(_usable_allowed_commands(tools_settings)) > _MAX_ALLOWED_COMMANDS
    kept = [path for path in not_carried if over_bound or path not in named]
    return kept + [
        f"{key}{allowed_commands.UNREADABLE_ENTRY_MARK}{entry} ({reason})"
        for key, entry, reason in unread
    ]

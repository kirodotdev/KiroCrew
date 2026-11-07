"""``toolsSettings`` with the keys that only grant left out.

:func:`kas_permissions.withhold_answered_under_authored_rules` reads ANY entry for
a built-in as a restriction. These helpers drop the keys that are grants, so an
author who wrote one beside the tool in ``allowedTools`` is not read as having
restricted it. Pure; every other key, and any malformed value, is kept.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.acp.kas_permissions import _SHELL_SETTINGS_KEYS, _WRITE_SETTINGS_KEYS

#: The ``toolsSettings`` keys kiro-cli reads its file-read settings under.
_READ_SETTINGS_KEYS = ("fs_read", "read")


def without_allowed_commands(tools_settings: Any) -> Any:
    """*tools_settings* with each shell entry's ``allowedCommands`` key left out.

    For :func:`withhold_answered_under_authored_rules`, which reads ANY entry for a
    built-in as a restriction: the grant itself is not one. A shell entry that held
    only that key goes too; every other key, and any malformed value, is kept, so
    it still withholds.
    """
    if not isinstance(tools_settings, dict):
        return tools_settings
    out = dict(tools_settings)
    for key in _SHELL_SETTINGS_KEYS:
        entry = out.get(key)
        if isinstance(entry, dict) and "allowedCommands" in entry:
            rest = {k: v for k, v in entry.items() if k != "allowedCommands"}
            if rest:
                out[key] = rest
            else:
                del out[key]
    return out


def without_grant_only_settings(tools_settings: Any) -> Any:
    """*tools_settings* without the keys that only grant: ``allowedCommands`` on a
    shell entry (:func:`without_allowed_commands`) and ``allowedPaths`` on a read or
    write entry.

    For the ``allowedTools`` grant reading of
    :func:`withhold_answered_under_authored_rules`: on kiro-cli those keys only add
    work that runs unprompted, so an author who also lists the tool in
    ``allowedTools`` restricted nothing by writing them. An entry left empty goes
    too; every other key, and any malformed value, is kept, so it still withholds.
    """
    out = without_allowed_commands(tools_settings)
    if not isinstance(out, dict):
        return out
    out = dict(out)
    for key in (*_READ_SETTINGS_KEYS, *_WRITE_SETTINGS_KEYS):
        entry = out.get(key)
        if isinstance(entry, dict) and "allowedPaths" in entry:
            rest = {k: v for k, v in entry.items() if k != "allowedPaths"}
            if rest:
                out[key] = rest
            else:
                del out[key]
    return out

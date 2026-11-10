"""``toolsSettings`` with the keys that only grant left out.

:func:`kas_permissions.withhold_answered_under_authored_rules` reads ANY entry for
a built-in as a restriction. These helpers drop the keys that are grants, so an
author who wrote one beside the tool in ``allowedTools`` is not read as having
restricted it. Pure; every other key, and any malformed value, is kept.
"""

from __future__ import annotations

from collections.abc import Callable
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
    """*tools_settings* without the keys that only grant: ``allowedCommands`` and a
    boolean ``autoAllowReadonly`` on a shell entry, and ``allowedPaths`` on a read or
    write entry.

    For the ``allowedTools`` grant reading of
    :func:`withhold_answered_under_authored_rules`: on kiro-cli those keys only add
    work that runs unprompted, so an author who also lists the tool in
    ``allowedTools`` restricted nothing by writing them. An entry left empty goes
    too; every other key, and any malformed value, is kept, so it still withholds.
    """
    out = _without_keys(
        without_allowed_commands(tools_settings),
        _SHELL_SETTINGS_KEYS,
        lambda key, value: key == "autoAllowReadonly" and isinstance(value, bool),
    )
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


def _shell_key_grants_only(key: str, value: Any) -> bool:
    """Whether a shell entry's *key* leaves an ``allowedCommands`` match unasked.

    On kiro-cli none of these turns a command an ``allowedCommands`` entry matches
    into a prompt: ``allowedCommands`` is the grant; ``denyByDefault`` refuses only
    the commands no entry matches, and Crew's ``spec-shell-allowlist`` row refuses
    those before any grant row; ``autoAllowReadonly`` only adds read-only commands,
    which Crew's read-only row answers. A non-boolean ``autoAllowReadonly`` is not
    one of them, so it still withholds.
    """
    if key in ("allowedCommands", "denyByDefault"):
        return True
    return key == "autoAllowReadonly" and isinstance(value, bool)


def _write_key_grants_only(key: str, value: Any) -> bool:
    """Whether a write entry's *key* leaves a write ``allowedPaths`` covers unasked.

    On kiro-cli ``allowedPaths`` is the grant, and ``fallbackAction`` decides only
    what happens to a write no entry covers: ``"deny"`` is refused by Crew's
    ``spec-write-allowlist`` row before any grant row, anything else asks.
    """
    return key in ("allowedPaths", "fallbackAction")


def _without_keys(
    tools_settings: Any, tools: tuple[str, ...], grants_only: Callable[[str, Any], bool]
) -> Any:
    """*tools_settings* with every key *grants_only* accepts left out of each *tools*
    entry. An entry left empty goes too; any other key, and any malformed value, is
    kept, so it still withholds."""
    if not isinstance(tools_settings, dict):
        return tools_settings
    out = dict(tools_settings)
    for key in tools:
        entry = out.get(key)
        if not isinstance(entry, dict):
            continue
        rest = {k: v for k, v in entry.items() if not grants_only(k, v)}
        if rest:
            out[key] = rest
        else:
            del out[key]
    return out


def without_shell_grant_keys(tools_settings: Any) -> Any:
    """*tools_settings* with each shell entry's grant-only keys left out
    (:func:`_shell_key_grants_only`).

    For the ``allowedCommands`` reading of
    :func:`withhold_answered_under_authored_rules`: the grant itself, and the keys
    that never turn a matched command back into a prompt, are not a restriction.
    Wider than the ``allowedTools`` reading (:func:`without_grant_only_settings`),
    which keeps ``denyByDefault``, so a ``denyByDefault`` beside a whole-tool grant
    still keeps it at the prompt (fail toward ask).
    """
    return _without_keys(tools_settings, _SHELL_SETTINGS_KEYS, _shell_key_grants_only)


def without_write_grant_keys(tools_settings: Any) -> Any:
    """*tools_settings* with each write entry's grant-only keys left out
    (:func:`_write_key_grants_only`), for the write ``allowedPaths`` reading, like
    :func:`without_shell_grant_keys`."""
    return _without_keys(tools_settings, _WRITE_SETTINGS_KEYS, _write_key_grants_only)


def without_read_grant_keys(tools_settings: Any) -> Any:
    """*tools_settings* with the ``allowedPaths`` of each read, ``grep`` and ``glob``
    entry left out, for the read ``allowedPaths`` reading, like
    :func:`without_write_grant_keys`: on kiro-cli each only grants. Any other key on
    them (``deniedPaths``, ``allowReadOnly``) still withholds."""
    keys = (*_READ_SETTINGS_KEYS, "grep", "glob")
    return _without_keys(tools_settings, keys, lambda key, _v: key == "allowedPaths")

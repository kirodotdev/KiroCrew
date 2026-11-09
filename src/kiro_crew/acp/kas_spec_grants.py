"""The kiro-cli ``toolsSettings`` grants Crew carries to a KAS session itself.

KAS reads no ``toolsSettings``, and a shell or filesystem ``allow`` never reaches
its policy (``kas_permissions.WITHHELD_CAPABILITIES``), so Crew answers these
grants at its own gate, after every deny row and governance. This module holds
what the projection and the session record share about them: the Crew-only entry
keys and their readers. Which setting keys are grants rather than restrictions is
:mod:`kiro_crew.acp.kas_grant_only_settings`.
Imported by :mod:`kiro_crew.acp.kas_permissions`, which re-exports every name.
"""

from __future__ import annotations

from typing import Any

#: The ``toolsSettings`` keys kiro-cli reads its shell settings under. After
#: ``canonical_tools_settings`` at most one of them is present.
SHELL_SETTINGS_KEYS = ("execute_bash", "shell")

#: The ``toolsSettings`` keys kiro-cli reads its file-write settings under.
WRITE_SETTINGS_KEYS = ("fs_write", "write")

#: The key a projected entry carries its :func:`crew_answered_paths` under, like
#: :data:`CREW_ANSWERED_COMMANDS_KEY`: the write tool's ``allowedPaths``, which the
#: gate answers a covered write's permission request for.
CREW_ANSWERED_PATHS_KEY = "_crewAnsweredPaths"

#: The ``toolsSettings`` keys kiro-cli reads its file-read settings under. ``grep``
#: and ``glob`` have settings of their own on kiro-cli, so they are not here.
READ_SETTINGS_KEYS = ("fs_read", "read")

#: The same for the read tool's ``allowedPaths``: the gate answers a covered
#: read's permission request (``gate_tiers._spec_read_granted``).
CREW_ANSWERED_READ_PATHS_KEY = "_crewAnsweredReadPaths"

#: The key a projected entry carries ``True`` under when its spec sets the shell's
#: ``autoAllowReadonly: true`` beside ``denyByDefault``. The gate's
#: ``spec-shell-allowlist`` row then lets a read-only command past, as kiro-cli
#: does, for the read-only row to answer.
CREW_SHELL_READONLY_KEY = "_crewShellReadonly"


def _entry_shell_readonly(entry: Any) -> bool:
    """Whether one projected entry carries :data:`CREW_SHELL_READONLY_KEY` as ``True``.

    Anything else, a malformed value included, is ``False``: a ``denyByDefault``
    refusal is never lifted by a record Crew did not write.
    """
    return isinstance(entry, dict) and entry.get(CREW_SHELL_READONLY_KEY) is True


def _entry_crew_answered_paths(entry: Any, key: str = CREW_ANSWERED_PATHS_KEY) -> tuple[str, ...]:
    """The *key* (:data:`CREW_ANSWERED_PATHS_KEY` or
    :data:`CREW_ANSWERED_READ_PATHS_KEY`) entries of one projected entry, as the
    spec wrote them, read like :func:`_entry_crew_answered_commands`."""
    raw = entry.get(key) if isinstance(entry, dict) else None
    if not isinstance(raw, list):
        return ()
    return tuple(p for p in raw if isinstance(p, str) and p)


def shell_auto_allows_readonly(tools_settings: Any) -> bool:
    """Whether *tools_settings* sets ``autoAllowReadonly: true`` on either shell key.

    Only the literal ``True`` counts: it lifts a ``denyByDefault`` refusal for a
    read-only command, so a malformed value lifts nothing.
    """
    if not isinstance(tools_settings, dict):
        return False
    return any(
        isinstance(settings, dict) and settings.get("autoAllowReadonly") is True
        for settings in (tools_settings.get(key) for key in SHELL_SETTINGS_KEYS)
    )

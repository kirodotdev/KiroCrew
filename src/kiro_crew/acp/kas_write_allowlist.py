"""A KAS session's ``fallbackAction: "deny"`` write allowlist, read from its batch.

The projection (``kas_agents.to_client_custom_agent``) records a spec's usable
``toolsSettings.fs_write.allowedPaths`` under
:data:`~kiro_crew.acp.kas_permissions.CREW_WRITE_ALLOWLIST_KEY` as the spec wrote
them. The session handle reads them back here, for the agent it runs and again
after a KAS mode switch, and anchors them at the session's own directory, which
only the handle knows. The gate's ``spec-write-allowlist`` row matches a write's
targets against the result.
"""

from __future__ import annotations

import os
from typing import Any

from kiro_crew.acp.kas_agents import _registered_entry, _switched_entry
from kiro_crew.acp.kas_permissions import CREW_WRITE_ALLOWLIST_KEY


def _entry_write_allowlist(entry: Any, cwd: str) -> tuple[str, ...] | None:
    """The :data:`CREW_WRITE_ALLOWLIST_KEY` entries of one projected entry, anchored.

    ``None`` when the entry has no such key (no ``fallbackAction: "deny"``). A value
    that is present but not a list reads as ``()``, which covers no path: a
    restriction is never lifted by a malformed record. Each usable entry is
    anchored as kiro-cli anchors it (:func:`anchor_write_allowlist`).
    """
    if not isinstance(entry, dict) or CREW_WRITE_ALLOWLIST_KEY not in entry:
        return None
    raw = entry[CREW_WRITE_ALLOWLIST_KEY]
    if not isinstance(raw, list):
        return ()
    return anchor_write_allowlist(raw, cwd)


def anchor_write_allowlist(patterns: Any, cwd: str) -> tuple[str, ...]:
    """*patterns* (``allowedPaths`` entries) as the absolute globs the gate matches.

    ``~`` is the home directory, and a relative entry is joined to *cwd*, the
    session's own directory, as kiro-cli joins it to its working directory; with no
    *cwd* a relative entry covers nothing. An entry outside the subset
    :mod:`kiro_crew.platform.allowed_paths` reads covers nothing either.

    String work only: nothing here touches the filesystem, because it runs on the
    event loop at session start and on a mode switch. Symlinks in an entry's
    directory part are resolved by the gate, and only for a write that is not
    covered without them (``gate_tiers._write_target_uncovered``).
    """
    from kiro_crew.platform import allowed_paths

    home = os.path.expanduser("~")
    out: list[str] = []
    for raw in patterns if isinstance(patterns, (list, tuple)) else ():
        parsed = allowed_paths.parse(raw)
        anchored = allowed_paths.anchor(parsed, cwd, home) if parsed is not None else None
        if anchored is not None:
            out.append(anchored)
    return tuple(dict.fromkeys(out))


def projected_write_allowlist(
    custom_agents: Any, agent_id: str, cwd: str
) -> tuple[str, ...] | None:
    """The ``fallbackAction: "deny"`` write allowlist of *agent_id*'s registered
    entry, anchored at *cwd*, or ``None``, matched like
    ``kas_agents.projected_shell_allowlist``."""
    return _entry_write_allowlist(_registered_entry(custom_agents, agent_id), cwd)


def switched_write_allowlist(custom_agents: Any, mode_id: str, cwd: str) -> tuple[str, ...] | None:
    """That allowlist once a KAS mode switch moves the session to *mode_id*."""
    return _entry_write_allowlist(_switched_entry(custom_agents, mode_id), cwd)

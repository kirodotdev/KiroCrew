"""What of a kiro-cli agent spec KAS can carry: ``resources`` knowledge bases and ``toolAliases``.

Split out of :mod:`kiro_crew.acp.kas_agents`, which sits at the file-line cap.

* A ``{"type": "knowledgeBase", "source": "file://..."}`` entry in ``resources``
  has a slot on KAS's wire (``KnowledgeBaseResourceSchema`` in KAS's
  ``acp-type-covenant/session/agent-resources.ts``), so a valid one is sent with
  only the schema's keys. One KAS's schema would refuse drops the whole injected
  agent, so every entry is checked here first and a bad one is dropped and named.
* ``toolAliases`` has no slot, so a session runs without it and is told. The pairs
  Crew's own Connections pass wrote are not named: Crew put them there, so the
  user has nothing to fix (:mod:`kiro_crew.connections.alias_record`).
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Any

from kiro_crew.acp.kas_prompt_paths import is_unsafe_prompt_path

_FILE_SCHEME = "file://"
_KNOWLEDGE_BASE = "knowledgeBase"
_INDEX_TYPES = ("fast", "best")
_STRING_KEYS = ("name", "description")
_LIST_KEYS = ("include", "exclude")


def _source(raw: object) -> str | None:
    """*raw* as a source KAS may index, or ``None``.

    ``~`` and ``$VAR`` are expanded here, as kiro-cli expands them; the engine is
    not relied on to. An absolute source must not be a credential or governance
    location (:func:`~kiro_crew.acp.kas_prompt_paths.is_unsafe_prompt_path`). A
    relative one stays relative, so the engine anchors it to the session's folder
    as kiro-cli does, and may not climb out of it with ``..``.
    """
    if not isinstance(raw, str) or not raw.startswith(_FILE_SCHEME):
        return None
    ref = raw[len(_FILE_SCHEME) :].strip()
    if not ref:
        return None
    if ref.startswith("~") and not (ref == "~" or ref.startswith(("~/", "~\\"))):
        # ``~user``: POSIX raises for an unknown user and Windows guesses a sibling
        # of the home folder, so neither names a folder the user meant.
        return None
    try:
        path = Path(os.path.expandvars(ref)).expanduser()
        # ``anchor``, not ``is_absolute``: on Windows ``/x`` is rooted with no drive.
        if path.anchor and is_unsafe_prompt_path(path.resolve()):
            return None
    except (OSError, RuntimeError):
        # ``~nosuchuser`` cannot be expanded, a symlink loop cannot be resolved.
        return None
    if path.anchor:
        return _FILE_SCHEME + path.as_posix()
    if ".." in PurePosixPath(path.as_posix()).parts:
        return None
    return _FILE_SCHEME + path.as_posix()


def knowledge_base_entry(raw: dict[str, Any]) -> dict[str, Any] | None:
    """*raw* as KAS's knowledge-base resource, or ``None`` when it would be refused.

    Only the schema's keys are kept, and each must have the schema's type: an
    unknown key is dropped, and a wrong type rejects the entry.
    """
    if raw.get("type") != _KNOWLEDGE_BASE:
        return None
    source = _source(raw.get("source"))
    if source is None:
        return None
    out: dict[str, Any] = {"type": _KNOWLEDGE_BASE, "source": source}
    for key in _STRING_KEYS:
        if key in raw:
            if not isinstance(raw[key], str):
                return None
            out[key] = raw[key]
    if "indexType" in raw:
        if raw["indexType"] not in _INDEX_TYPES:
            return None
        out["indexType"] = raw["indexType"]
    for key in _LIST_KEYS:
        if key in raw:
            value = raw[key]
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                return None
            out[key] = value
    if "autoUpdate" in raw:
        if not isinstance(raw["autoUpdate"], bool):
            return None
        out["autoUpdate"] = raw["autoUpdate"]
    return out


def projected_resources(resources: object) -> tuple[list[Any], list[str]]:
    """The ``resources`` KAS is sent, and the dropped knowledge bases' labels.

    Strings pass as before. A label is ``resources[<index>]``, plus the entry's
    ``name`` when it is a string; it names no path.
    """
    if not isinstance(resources, list):
        return [], []
    kept: list[Any] = []
    dropped: list[str] = []
    for index, entry in enumerate(resources):
        if isinstance(entry, str):
            if entry:
                kept.append(entry)
            continue
        if not isinstance(entry, dict):
            continue
        projected = knowledge_base_entry(entry)
        if projected is not None:
            kept.append(projected)
            continue
        name = entry.get("name")
        dropped.append(f"resources[{index}]" + (f" ({name})" if isinstance(name, str) else ""))
    return kept, dropped


def user_tool_aliases(aliases: object) -> bool:
    """Whether *aliases* holds a pair Crew's Connections pass did not write.

    Only a pair the alias record proves Crew wrote, for the map exactly as it is,
    is left out; any other non-empty map counts as the user's.
    """
    if not isinstance(aliases, dict) or not aliases:
        return False
    # circular import: connections reaches the agent layer, which imports this package.
    from kiro_crew.connections.alias_record import (
        is_recorded_emission,
        load_claimed,
        spec_fingerprint,
    )

    claimed = load_claimed(spec_fingerprint(aliases))
    return any(not is_recorded_emission(claimed, ref, alias) for ref, alias in aliases.items())

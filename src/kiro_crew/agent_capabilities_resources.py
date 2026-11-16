"""Capability rows for a spec's ``resources`` list, strings and objects alike.

kiro-cli's ``resources`` holds ``file://`` and ``skill://`` strings and, for a
knowledge base, an object (``{"type": "knowledgeBase", "source": ..., "name": ...}``)
that has no string spelling. A capability row needs a string key, so a string is
its own key and an object is keyed by :func:`object_resource_key`. The row's value
is the object itself, so a change to it reads as a changed row and goes through
the same review as any other ``resources`` change.

Imports nothing from Kiro Crew: ``agent_state`` validates saved rows with it, and
sits below the resolver in the import graph.
"""

from __future__ import annotations

import copy
from typing import Any


def object_resource_key(entry: dict) -> str:
    """``<type>:<name>``, or ``<type>:<source>`` for an unnamed entry, else ``object``.

    Read from the object's identity, never its content, so editing a knowledge
    base keeps its row and the edit waits for approval as one changed row.
    """
    kind, name, source = entry.get("type"), entry.get("name"), entry.get("source")
    named = name if isinstance(name, str) and name else source
    if isinstance(kind, str) and kind and isinstance(named, str) and named:
        return f"{kind}:{named}"
    return "object"


def resource_row_key(entry: Any) -> str | None:
    """*entry*'s row key, or ``None`` for an entry that is neither a string nor an object."""
    if isinstance(entry, str):
        return entry
    return object_resource_key(entry) if isinstance(entry, dict) else None


def resource_rows(entries: Any, skills: dict[str, str]) -> tuple[dict, dict] | None:
    """*entries* split into skill rows and resource rows, or ``None`` when it has no row form.

    *skills* maps a catalog URI to its skill key. ``None`` is a non-list, an entry
    with no row key, or two different entries sharing one key: a row must keep its
    key whatever else the list holds, so a shared key is refused, not renamed.
    """
    if not isinstance(entries, list):
        return None
    skill_rows: dict[str, str] = {}
    rows: dict[str, Any] = {}
    claimed: dict[str, Any] = {}
    for entry in entries:
        key = resource_row_key(entry)
        if key is None or claimed.setdefault(key, entry) != entry:
            return None
        if isinstance(entry, str) and entry in skills:
            skill_rows[skills[entry]] = entry
        else:
            rows[key] = copy.deepcopy(entry)
    return skill_rows, rows


def resource_list(base: Any, wanted: dict[str, Any]) -> list:
    """*wanted* rows as a ``resources`` list: *base*'s order for what survives, then the rest.

    Each surviving entry takes its row's value, not *base*'s, so an accepted change
    to an object lands where the object already stood.
    """
    entries = base if isinstance(base, list) else []
    order = [key for key in dict.fromkeys(map(resource_row_key, entries)) if key is not None]
    kept = set(order)
    ordered = [key for key in order if key in wanted] + [k for k in wanted if k not in kept]
    return [copy.deepcopy(wanted[key]) for key in ordered]

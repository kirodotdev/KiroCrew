"""Find the UPDATE candidate already queued for a live auto-skill.

Consolidation merges a later update into the one already queued for the same
live skill, and staging replaces that queued candidate once the merge used it.
A queued candidate is a merge base only when the new candidate ships every
script it ships, since approval copies only the approved candidate's scripts.
These read the queue through the loader's public list and detail calls only.
"""

from __future__ import annotations

from typing import Any


def _queued_script_names(entry: dict, detail: dict) -> "set[str] | None":
    """Every script filename a queued candidate ships, or ``None`` if unknown.

    Read from the metadata ``scripts`` list and the detail's script tree. A
    ``scripts`` field that is not a list of strings, a script entry without a
    string filename, or ``has_scripts`` set with no names yields ``None``.
    """
    meta = detail.get("meta")
    listed = meta.get("scripts", []) if isinstance(meta, dict) else None
    if not isinstance(listed, list) or not all(isinstance(n, str) for n in listed):
        return None
    tree = detail.get("scripts", [])
    if not isinstance(tree, list):
        return None
    names = set(listed)
    for s in tree:
        if not isinstance(s, dict) or not isinstance(s.get("filename"), str):
            return None
        names.add(s["filename"])
    if not names and (entry.get("has_scripts") or meta.get("has_scripts")):
        return None
    return names


def queued_update_for(loader: Any, target_key: str, base_version: int) -> "dict | None":
    """The pending UPDATE candidate for ``target_key`` built on ``base_version``.

    Returns ``{"slug", "created_at", "content", "triggers", "description",
    "scripts"}`` read from the queue, or ``None`` when there is none or it
    cannot be read. ``slug`` and ``created_at`` identify the exact candidate,
    so the caller can supersede that one and no other. ``scripts`` is the set
    of script filenames it ships, or ``None`` when they cannot be determined;
    the caller does not merge from it unless the new candidate ships all of
    them. Best-effort: a failed read only means this candidate is merged over
    the live body.
    """
    try:
        pending = list(loader.list_pending_skills())
    except Exception:
        return None
    for p in pending:
        if p.get("kind") != "update" or p.get("target") != target_key:
            continue
        if p.get("base_version") != base_version:
            continue
        slug = str(p.get("slug") or "")
        if not slug:
            continue
        try:
            detail = loader.get_pending_skill(slug)
        except Exception:
            detail = None
        if not detail:
            continue
        return {
            "slug": slug,
            "created_at": str(p.get("created_at") or ""),
            "content": detail.get("content") or "",
            "triggers": p.get("triggers") or "",
            "description": p.get("description") or "",
            "scripts": _queued_script_names(p, detail),
        }
    return None


def live_target_of_pending_update(loader: Any, key: str) -> "str | None":
    """The live ``target`` of the pending UPDATE candidate named ``key``, else ``None``."""
    slug = key.split("/", 1)[-1]
    try:
        pending = list(loader.list_pending_skills())
    except Exception:
        return None
    for p in pending:
        if p.get("slug") == slug and p.get("kind") == "update" and p.get("target"):
            return str(p["target"])
    return None

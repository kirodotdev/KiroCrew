"""Find the UPDATE candidate already queued for a live auto-skill.

Consolidation merges a later update into the one already queued for the same
live skill, and staging replaces that queued candidate once the merge used it.
These read the queue through the loader's public list and detail calls only.
"""

from __future__ import annotations

from typing import Any


def queued_update_for(loader: Any, target_key: str, base_version: int) -> "dict | None":
    """The pending UPDATE candidate for ``target_key`` built on ``base_version``.

    Returns ``{"slug", "created_at", "content", "triggers", "description"}``
    read from the queue, or ``None`` when there is none or it cannot be read.
    ``slug`` and ``created_at`` identify the exact candidate, so the caller can
    supersede that one and no other. Best-effort: a failed read only means
    this candidate is merged over the live body.
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

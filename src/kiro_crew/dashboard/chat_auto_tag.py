"""Background auto-tagging — derive a tag from the session's project directory.

Mirrors the pattern of ``_maybe_auto_title`` in ``chat_title.py``: a fire-once
background task that never raises, guards on idempotency, and runs alongside the
first-message title generation.

v1 derivation is DETERMINISTIC: the tag name is ``os.path.basename(slot.project)``
(the repo/directory name). This requires zero LLM calls — if the project is
set, the tag is derived immediately.

# Future work: use a cheap model to extract 1–2 topic tags from the first
# user message (similar to how auto-title calls the LLM). That would add
# semantic tags like "oncall", "debugging", "code-review" alongside the
# deterministic project-name tag.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
from kiro_crew.dashboard.chat_tags import (
    _NAME_MAX,
    create_tag_definition,
    persist_tags_snapshot_unlocked,
    tags_write_lock,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)


async def maybe_auto_tag(state: Any, slot: Any) -> None:
    """Derive a tag from the slot's project directory and apply it.

    Never raises — all failures are debug-logged. Guards:
    - Once-per-slot: ``slot._auto_tagged`` flag (mirrors ``_titled`` pattern)
    - Non-empty ``slot.project`` that isn't trivial (".", "~")
    - Idempotent: if the derived tag id is already in slot.tags, no-op
    """
    # Once-guard: never re-run after the first attempt (even if the user
    # manually removes the tag later — respect the removal).
    if getattr(slot, "_auto_tagged", False):
        return
    try:
        await _auto_tag_inner(state, slot)
    finally:
        # Mark attempted regardless of success/failure (same as _titled).
        slot._auto_tagged = True
    return


async def _auto_tag_inner(state: Any, slot: Any) -> None:
    # Note: no slot-kind guard needed here — the only call site
    # (chat_handlers message path) fires exclusively for dashboard chat
    # slots, same as ``_maybe_auto_title``. Slot keys are BARE names; the
    # ``dashboard:`` prefix exists only on the derived session key
    # (``dashboard:{slot.key}``), never on ``slot.key`` itself.

    # Guard: must have a meaningful project
    project = getattr(slot, "project", "") or ""
    if not project:
        return
    tag_name = os.path.basename(project)
    if not tag_name or tag_name in (".", "~"):
        return

    # Redact credential/exfiltration patterns from derived name, then apply
    # the SAME normalization create_tag_definition uses (strip + _NAME_MAX
    # truncation) BEFORE matching — otherwise two long basenames that differ
    # only past the truncation point would each miss the lookup and create
    # duplicate definitions with identical persisted names.
    safe_name, _ = redact_exfiltration_urls(tag_name)
    safe_name, _ = redact_credentials(safe_name)
    safe_name = safe_name.strip()[:_NAME_MAX]
    if not safe_name:
        return

    async with tags_write_lock(state):
        # Build case-insensitive lookup
        existing_by_lower: dict[str, dict] = {}
        for t in state._tags:
            name_lower = (t.get("name") or "").lower()
            if name_lower and name_lower not in existing_by_lower:
                existing_by_lower[name_lower] = t

        lower = safe_name.lower()
        existing = existing_by_lower.get(lower)

        if existing:
            # NEVER apply status/workflow tags
            if existing.get("status"):
                return
            tag_id = existing["id"]
        else:
            # Create new tag definition (never status=True)
            new_tag = create_tag_definition(state, safe_name, status=False)
            tag_id = new_tag["id"]
            try:
                await persist_tags_snapshot_unlocked(state)
            except Exception:
                # Roll back the in-memory append: a definition that never
                # reached disk must not stay visible in the vocabulary
                # (retries are suppressed by the once-flag, so it would
                # otherwise linger unassigned until restart).
                state._tags = [t for t in state._tags if t.get("id") != tag_id]
                logger.debug(
                    "auto_tag: tag snapshot persist failed; rolled back %s",
                    tag_id,
                    exc_info=True,
                )
                return

        # Idempotency: already tagged? no-op
        current_tags: list[str] = list(getattr(slot, "tags", None) or [])
        if tag_id in current_tags:
            return

        # Additive merge. Set the once-flag BEFORE persisting: the slot save
        # below writes the metadata line, and the flag must be on it —
        # otherwise a restart loses the flag and a later message re-runs
        # auto-tag, silently re-adding a tag the user removed.
        current_tags.append(tag_id)
        slot.tags = current_tags
        slot._auto_tagged = True
        await save_slot_off_loop(state, slot, force=True)

        # Push update to connected clients
        push = getattr(state, "push_slots_update", None)
        if push is not None:
            try:
                push()
            except Exception:
                logger.debug("push_slots_update failed in auto_tag", exc_info=True)

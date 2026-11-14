"""What a Bot is told about the values a PERSON corrected on its Dashboard.

Split out of :mod:`kiro_crew.dashboard_agentic`, which re-exports
:func:`person_corrections_line`, so callers keep importing it from there. Feature
preview ``dashboard_edit_mode``.
"""

from __future__ import annotations

import json
import logging
from typing import Mapping, Sequence

logger = logging.getLogger("kiro_crew.dashboard_agentic")


def person_corrections_line(slug: str, member: str, names: Sequence[str]) -> str:
    """The line naming each field the PERSON corrected in Dashboard edit mode, or ``""``.

    A field whose latest write carries ``actor: person`` (feature preview
    ``dashboard_edit_mode``). The Bot may write over it, so it is told what the person
    set and why. TOTAL: an unreadable fold, the preview off, no member or no field
    names is no line.
    """
    if not member or not names:
        return ""
    try:
        from kiro_crew import dashboard_edit_mode

        if not dashboard_edit_mode.edit_mode_on():
            return ""
        from kiro_crew.crew_log import projection
        from kiro_crew.dashboard.handlers.member_dashboard import _dashboard_slot

        # Warm only: a prompt build never folds a slot from scratch. A cell not warm yet
        # raises SlotFoldNotReady, caught below, and the turn simply carries no line.
        slot = _dashboard_slot(member, slug)
        fold = projection.read_slot_projection(slot, "agentic", cold=False).value
        rows = fold.get("fields") if isinstance(fold, Mapping) else None
        if not isinstance(rows, Mapping):
            return ""
        parts: list[str] = []
        for name in names:
            row = rows.get(name)
            if not isinstance(row, Mapping) or row.get("actor") != "person":
                continue
            reason = str(row.get("reason") or "")
            parts.append(
                f"{name} = {json.dumps(row.get('value'), ensure_ascii=False)[:120]} "
                f"at {row.get('at') or '?'}" + (f' ("{reason}")' if reason else "")
            )
    except Exception:
        logger.debug("dashboard: %r's corrections could not be read", slug, exc_info=True)
        return ""
    if not parts:
        return ""
    return (
        "The person corrected these values on your Dashboard: "
        + "; ".join(parts)
        + ". Keep them unless you know better; if you write over one, say why in your reply."
    )

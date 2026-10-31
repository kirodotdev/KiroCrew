"""Which (slug, member) pair names a dashboard: the name check shared by the page and the board.

Split out of :mod:`kiro_crew.dashboard.handlers.member_dashboard`, which keeps importing and
re-exporting :class:`NameRefusal` and :func:`resolve_names` so existing callers are unchanged.
"""

from __future__ import annotations

import asyncio
from typing import NamedTuple

from kiro_crew import members as members_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers.members import _member_names_for_slug
from kiro_crew.members import MemberSlugError

__all__ = ["NameRefusal", "resolve_names"]


class NameRefusal(NamedTuple):
    """Why a (slug, member) pair named no dashboard: code, sentence, status."""

    code: str
    message: str
    status: int = 400


async def resolve_names(slug: str, member: str) -> tuple[str, str] | NameRefusal:
    """``(slug, canonical member)`` or the refusal; also used by the board's ``open``."""
    try:
        members_mod.validate_slug(slug)
    except MemberSlugError:
        return NameRefusal("invalid_member_slug", "invalid member slug")
    if not members_mod.is_dispatchable_member_name(member):
        return NameRefusal("missing_member", "member query parameter required")
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    # The ROSTER's spelling (`atlas` is `Atlas`), resolved ONCE before either check so the
    # slug check and the roster check ask about the same name.
    member = members_mod.canonical_member_key(member, cfg)
    try:
        if members_mod.member_slug(member, cfg) != slug:
            return NameRefusal("member_slug_mismatch", "member does not match slug")
    except MemberSlugError:
        return NameRefusal("member_slug_mismatch", "member does not match slug")
    if member not in cfg.agents:
        return NameRefusal("member_not_found", "no Bot for this slug", 404)
    if _member_names_for_slug(cfg, slug) != [member]:
        return NameRefusal(
            "dashboard_slug_ambiguous",
            "multiple crews share this slug; their dashboards would be ambiguous",
            409,
        )
    return slug, member

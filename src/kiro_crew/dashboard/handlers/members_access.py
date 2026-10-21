"""The gates and checks every Crew Members route shares.

The existence-hiding 404 for an app-token caller, the SEL row a granted owner read
leaves, the late-bound SEL accessor, and the name questions the routes ask of a slug:
which addressable crew names derive it, and whether any registered crew still owns it.

Served through :mod:`kiro_crew.dashboard.handlers.members`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import logging

from aiohttp import web

from kiro_crew import members as members_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.members import MemberSlugError

logger = logging.getLogger("kiro_crew.dashboard.handlers.members")


def _sel():
    """Late-binding _sel() for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg

    return _pkg.sel()


async def _deny_app_caller(request: web.Request, operation: str) -> web.Response | None:
    """404 for app-token callers; ``None`` for the dashboard user.

    404 rather than 403, matching the slot-access denials: a distinct status
    would confirm the surface exists to a caller that may not know about it.

    The audit is a bare enqueue: SEL is warmed at gateway startup
    (``sel.warm_sel_singleton``), so the first-touch filesystem initialization
    never runs on this call site. Guarded because a FAILED warm leaves
    construction to retry on this thread and possibly raise.
    """
    request_app = request.get("app", "")
    if not request_app:
        return None
    try:
        _sel().log_api_access(
            caller=request_app,
            operation=operation,
            outcome="denied",
            source="app_isolation",
            error="apps cannot access member threads",
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for %s app denial failed", operation, exc_info=True)
    return web.json_response({"error": "not found", "code": "not_found"}, status=404)


def _audit_owner_read(request: web.Request, operation: str) -> None:
    """Record an owner gate grant, as the briefing and rules reads do.

    A denied-only trail cannot answer who read the owner's view of the crew, so a
    granted read leaves an ``allowed`` record too. Best effort: an audit must never
    change the outcome.
    """
    try:
        _sel().log_api_access(
            caller=request.remote or "",
            operation=operation,
            outcome="allowed",
            source="dashboard",
            resources="owner_grant",
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for %s failed", operation, exc_info=True)


def _member_name_is_addressable(value: object) -> bool:
    return members_mod.is_dispatchable_member_name(value)


def _member_names_for_slug(cfg: KiroCrewConfig, slug: str) -> list[str]:
    """Addressable crew names for *slug*, in deterministic config order.

    Malformed hand-edited names remain unaddressable.
    """
    out: list[str] = []
    for name in cfg.agents:
        if not _member_name_is_addressable(name):
            continue
        try:
            if members_mod.member_slug(name, cfg) == slug:
                out.append(name)
        except MemberSlugError:
            continue
    return out


def _slug_is_claimed_by_any_member(cfg: KiroCrewConfig, slug: str, owner_key: str) -> bool:
    """Whether a registered crew still owns *slug* and *owner_key*.

    Unlike :func:`_member_names_for_slug`, this check includes malformed legacy
    names. Dropping a live owner would let a colliding crew take its record.

    This check uses ``member_slug`` rather than ``slug_for_name`` because a
    persisted ``member_id`` deliberately differs after member recreation.

    ``agent_panel`` is imported here because importing the optional panel
    subsystem during gateway boot delays readiness.
    """
    from kiro_crew import agent_panel as agent_panel_mod

    for name in cfg.agents:
        try:
            if members_mod.member_slug(name, cfg) != slug:
                continue
        except MemberSlugError:
            continue
        if agent_panel_mod.crew_key(name) == owner_key:
            return True
    return False

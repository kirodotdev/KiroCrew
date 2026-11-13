"""The gates and checks every Crew Members route shares.

The existence-hiding 404 for an app-token caller, the SEL row a granted owner read
leaves, the late-bound SEL accessor, and the name questions the routes ask of a slug:
which addressable crew names derive it, and whether any registered crew still owns it.

Served through :mod:`kiro_crew.dashboard.handlers.members`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
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


def _audit_teammate_read(request: web.Request, operation: str) -> None:
    """Record a relay teammate's granted request, as :func:`_audit_owner_read` records the owner's.

    Filed under the relay caller's own audit label (``relay:<Kiro user ID>``), so the
    trail says which teammate read or changed which Bot. Best effort, like the owner's row.
    """
    from kiro_crew.dashboard.kiro_relay.caller import published_relay_caller

    relayed = published_relay_caller(request)
    try:
        _sel().log_api_access(
            caller=relayed.audit_label if relayed is not None else "",
            operation=operation,
            outcome="allowed",
            source="dashboard",
            resources="relay_teammate",
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for %s failed", operation, exc_info=True)


def _audit_teammate_denied(request: web.Request, operation: str, resources: str) -> None:
    """Record a relay teammate's refused request, under their relay label. Best effort."""
    from kiro_crew.dashboard.kiro_relay.caller import published_relay_caller

    relayed = published_relay_caller(request)
    try:
        _sel().log_api_access(
            caller=relayed.audit_label if relayed is not None else "",
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources=resources,
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for %s failed", operation, exc_info=True)


def _teammate_reaches_bot(*, slug: str | None = None, name: str | None = None) -> bool:
    """Whether a relay teammate may reach the Bot *slug* (or crew *name*) names.

    The teammate's roster (``members_roster._teammate_roster``) lists only Bots a
    person created here, so these routes admit exactly those: every addressable name
    deriving the slug must be one, and at least one must exist. Blocking (it loads
    the config); call it off the event loop.
    """
    cfg = KiroCrewConfig.load()
    if name is not None:
        agent_cfg = cfg.agents.get(name)
        return agent_cfg is not None and members_mod.is_person_created_crewmate(name, agent_cfg)
    try:
        members_mod.validate_slug(slug or "")
    except MemberSlugError:
        return False
    names = _member_names_for_slug(cfg, slug or "")
    return bool(names) and all(
        members_mod.is_person_created_crewmate(n, cfg.agents[n]) for n in names
    )


async def _teammate_admitted(
    request: web.Request,
    operation: str,
    *,
    slug: str | None = None,
    name: str | None = None,
) -> bool:
    """Whether *request* is a team Crew teammate this Bot route admits; audits the grant.

    On a team Crew a Bot is shared like a session: a teammate the Kiro relay verified
    may use a person-created Bot as they may use a session. A route that admits them
    calls this before its owner gate and skips the gate when it answers ``True``.
    ``False`` for everyone else, the owner included, with no config read, so the
    owner's path through the route is unchanged. A teammate on a Bot outside their
    roster also gets ``False`` and meets the owner gate's refusal. A route that
    names no Bot yet (create) passes neither *slug* nor *name*, and only the
    caller is checked.
    """
    from kiro_crew.shared_crew.routes_identity import _relay_teammate

    if not _relay_teammate(request):
        return False
    if (slug is not None or name is not None) and not await asyncio.to_thread(
        _teammate_reaches_bot, slug=slug, name=name
    ):
        return False
    _audit_teammate_read(request, operation)
    return True


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

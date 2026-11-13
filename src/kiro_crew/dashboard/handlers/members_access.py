"""The gates and checks every Crew Members route shares.

The existence-hiding 404 for an app-token caller, the one scope gate an app token
passes to reach a Bot (``_app_bot_gate``), the SEL row a granted owner read
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


def _not_found() -> web.Response:
    return web.json_response({"error": "not found", "code": "not_found"}, status=404)


async def _app_bot_gate(
    request: web.Request, scope: str, operation: str, slug: str | None = None
) -> tuple[str, tuple[str, ...]] | web.Response:
    """``(app, ticked Bot slugs)`` for an app token holding *scope*, else the 404 to send.

    The one door an app token has into the Bots (``apps.bot_grants``): the app's
    approved ``permissions.bots`` must hold *scope*, and, when *slug* is given, the
    owner must have ticked that Bot for the app. Every refusal is the same 404 the
    app-caller guard sends, so an app cannot tell a Bot it was not given from one
    that does not exist. Only call it for an app-token request.
    """
    from kiro_crew.apps.bot_grants import app_bot_scopes, app_granted_bots

    request_app = request.get("app", "")
    if not request_app:
        return _not_found()
    scopes = await asyncio.to_thread(app_bot_scopes, request_app)
    if not scopes:
        # An app with no Bot scope at all is refused, and audited, exactly as before
        # the scopes existed.
        denied = await _deny_app_caller(request, operation)
        return denied if denied is not None else _not_found()
    ticked = await asyncio.to_thread(app_granted_bots, request_app) if scope in scopes else ()
    reason = ""
    if scope not in scopes:
        reason = f"permissions.bots does not grant {scope!r}"
    elif slug is not None and slug not in ticked:
        reason = "the owner has not given this Bot to the app"
    try:
        _sel().log_api_access(
            caller=request_app,
            operation=operation,
            outcome="denied" if reason else "allowed",
            source="app_bot_scope",
            resources=slug or "",
            error=reason,
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for %s failed", operation, exc_info=True)
    if reason:
        return _not_found()
    return request_app, ticked


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
    """Record a relay teammate's granted read, as :func:`_audit_owner_read` records the owner's.

    Filed under the relay caller's own audit label (``relay:<Kiro user ID>``), so the
    trail says which teammate read the roster. Best effort, like the owner's row.
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

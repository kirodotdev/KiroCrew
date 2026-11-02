"""Marketplace install routes over the ``CapabilityManager`` marketplace ops.

A marketplace listing (a skill, an MCP server or a theme, with any bundled
components and dependencies) is installed by the EDITION's own installer: the core
never downloads, verifies or places a file, so the files land exactly where that
installer puts them from its own command line. These routes only gate, validate
and pass through:

* ``GET  /api/capability/market`` -- ``{"available": bool}``.
* ``POST /api/capability/market/plan`` -- body ``{ref}``; the installer's plan.
* ``POST /api/capability/market/apply`` -- body ``{ref, plan_digest}``; installs
  only the plan that digest names.

The POSTs are human-only (an internal-secret caller is refused ``human_only``
before anything else is read) and owner-only, because an install places
third-party files, MCP server commands among them, that later sessions run.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Awaitable, Callable

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import (
    _capability_manager,
    read_bounded_json,
    rebuild_agent_spec,
    require_owner_dashboard_request,
)
from kiro_crew.external_text import redact_external_text as _redact_external
from kiro_crew.platform.interfaces import CapabilityResult
from kiro_crew.sel import sel as _sel

#: ``<namespace>/<name>`` and an optional ``@<version or bounded range>``
#: (``1.2.0``, ``^1.2.0``, ``>=1.0.0``). Each part starts alphanumeric, so the
#: value can never be read as an option by the edition's installer, and there is
#: no whitespace or shell metacharacter.
VALID_MARKET_REF_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
    r"(?:@[0-9A-Za-z^~<>=][0-9A-Za-z.^~<>=+-]{0,63})?$"
)
#: The digest naming one install plan: sha256, lower-case hex.
VALID_MARKET_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

_HUMAN_ONLY = (
    "Installing a marketplace listing is a user action: do it from the dashboard, "
    "where the plan is shown before anything is installed."
)
_UNAVAILABLE = "marketplace install not available"


def _audit(operation: str, outcome: str, ref: str) -> None:
    """A SEL line naming the listing; the audit middleware logs only the path."""
    try:
        _sel().log_api_access(
            caller="dashboard",
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources=f"market:{ref}",
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        pass


async def _gate(
    request: web.Request, operation: str, *, needs_digest: bool
) -> "tuple[str, str] | web.Response":
    """Human-only, then owner-only, then the body; returns ``(ref, digest)``."""
    if request.get("internal_auth"):
        _audit(operation, "denied", "internal_secret_caller")
        return web.json_response({"error": _HUMAN_ONLY, "code": "human_only"}, status=403)
    denied = await require_owner_dashboard_request(request, operation)
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    ref = body.get("ref")
    if not isinstance(ref, str) or ".." in ref or not VALID_MARKET_REF_RE.match(ref):
        return web.json_response(
            {
                "error": "ref must be <namespace>/<name> with an optional @<version>",
                "code": "invalid_market_ref",
            },
            status=400,
        )
    digest = ""
    if needs_digest:
        raw = body.get("plan_digest")
        if not isinstance(raw, str) or not VALID_MARKET_DIGEST_RE.match(raw):
            return web.json_response(
                {
                    "error": "plan_digest must be the planDigest of the plan that was shown",
                    "code": "invalid_plan_digest",
                },
                status=400,
            )
        digest = raw
    return ref, digest


def _strs(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


#: Every item field the consent dialog renders, each a string.
_ITEM_STR_FIELDS = ("ref", "kind", "semver", "sha256", "reason", "target", "action")


def _item_is_showable(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    publisher = item.get("publisher")
    return (
        all(isinstance(item.get(f), str) for f in _ITEM_STR_FIELDS)
        and isinstance(publisher, dict)
        and isinstance(publisher.get("display"), str)
        and _strs(item.get("requiredBy"))
    )


def _permissions_are_showable(perms: object) -> bool:
    if not isinstance(perms, dict):
        return False
    commands = perms.get("commands")
    network = perms.get("network")
    return (
        isinstance(commands, list)
        and all(_strs(c) for c in commands)
        and (network in ("none", "any") or _strs(network))
        and _strs(perms.get("files"))
        and _strs(perms.get("secrets"))
    )


def plan_is_showable(plan: object) -> bool:
    """Can the consent dialog show this plan in full? Never ask consent for less.

    Checks every field the dialog reads, so a plan missing one is a 502
    ``market_plan_malformed`` here rather than a dialog that fails to render.
    """
    if not isinstance(plan, dict):
        return False
    refusal = plan.get("refusal")
    items = plan.get("items")
    digest = plan.get("planDigest")
    return (
        isinstance(plan.get("installable"), bool)
        and (
            refusal is None
            or (isinstance(refusal, dict) and isinstance(refusal.get("message"), str))
        )
        and isinstance(items, list)
        and all(_item_is_showable(i) for i in items)
        and _permissions_are_showable(plan.get("permissions"))
        and _strs(plan.get("conflicts"))
        and isinstance(digest, str)
        and VALID_MARKET_DIGEST_RE.match(digest) is not None
    )


async def api_capability_market_status(request: web.Request) -> web.Response:
    """GET /api/capability/market -- does this build install marketplace listings?"""
    return web.json_response({"available": bool(_capability_manager().market_available())})


async def api_capability_market_plan(request: web.Request) -> web.Response:
    """POST /api/capability/market/plan -- what installing a listing would place.

    A POST because the installer resolves it live and the route is human-only.
    Writes nothing. A plan the dialog could not show in full is a 502.
    """
    gated = await _gate(request, "capability_market_plan", needs_digest=False)
    if isinstance(gated, web.Response):
        return gated
    ref, _ = gated
    mgr = _capability_manager()
    if not mgr.market_available():
        return web.json_response(
            {"error": _UNAVAILABLE, "code": "capability_unavailable"}, status=503
        )
    plan = await mgr.market_plan(ref)
    if not plan_is_showable(plan):
        _audit("capability_market_plan", "error", ref)
        return web.json_response(
            {
                "error": "the installer returned a plan that cannot be shown",
                "code": "market_plan_malformed",
            },
            status=502,
        )
    return web.json_response(plan)


async def _mutate(
    request: web.Request,
    operation: str,
    ref: str,
    call: Callable[[Any], Awaitable[CapabilityResult]],
) -> web.Response:
    mgr = _capability_manager()
    if not mgr.market_available():
        return web.json_response(
            {"error": _UNAVAILABLE, "code": "capability_unavailable"}, status=503
        )
    res = await call(mgr)
    if not res.ok:
        _audit(operation, "refused", ref)
        message = _redact_external((res.message or "refused")[:500])
        return web.json_response({"error": message, "code": "market_refused"}, status=409)
    _audit(operation, "ok", ref)
    # A listing can place skills and MCP servers, so the agent spec is rebuilt as
    # after a skill install; a declined rewrite is reported, not an error.
    spec_rewritten = await asyncio.to_thread(rebuild_agent_spec, operation)
    request.app["state"].push_refresh("agents")
    return web.json_response({"ok": True, "ref": ref, "agent_spec_rewritten": spec_rewritten})


async def api_capability_market_apply(request: web.Request) -> web.Response:
    """POST /api/capability/market/apply -- install the plan the user confirmed.

    The edition refuses (409 ``market_refused``) when the listing now resolves to
    a plan with another digest, so nothing the user did not see is installed.
    """
    gated = await _gate(request, "capability_market_apply", needs_digest=True)
    if isinstance(gated, web.Response):
        return gated
    ref, digest = gated
    return await _mutate(
        request, "capability_market_apply", ref, lambda mgr: mgr.market_apply(ref, digest)
    )

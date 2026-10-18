"""The access gates handler modules share: the owner check and capability admission.

``require_owner_dashboard_request`` refuses a dashboard request that does not come
from the owner, with the audited denial body; ``admits_registry`` and
``admits_cloud_deployment`` ask the edition's capability manager whether a registry
or a cloud deployment is admitted; ``_caller_bounds`` reads the bounds a credential
minted on the caller's authority inherits.

Served through :mod:`kiro_crew.dashboard.handlers._shared`, which callers and tests
import and patch; this module never imports it."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Callable

from aiohttp import web

from kiro_crew.dashboard.token_auth import (
    MAX_SESSION_TTL_SECS,
    _b64url_decode,
    required_peer_key_unverified,
)

if TYPE_CHECKING:
    from kiro_crew.platform.interfaces import CapabilityManager

logger = logging.getLogger("kiro_crew.dashboard.handlers._shared")


def _audit_admission(surface: str, resource: str, allowed: bool, error: str = "") -> None:
    """Record an external-access verdict in the security event log.

    BOTH outcomes are logged, not just denials. An admission is the security-
    relevant event here: "this deployment queried a public registry" and "this
    deployment provisioned cloud infrastructure" are exactly what an operator who
    restricted these surfaces needs to be able to prove afterwards, and a log that
    only carries denials cannot answer whether the permitted path was ever taken.

    Raises on failure — deliberately NOT best-effort, unlike most SEL call sites.
    An access grant that cannot be recorded is an unaccountable grant, so the
    caller converts a failed audit into a denial rather than proceeding unlogged.

    ``critical=True`` is what makes that possible. The default path QUEUES the
    event and swallows a write failure internally, so an exception handler around
    this call would never fire and the "fail closed" claim would be empty; the
    critical path writes synchronously and raises on a filesystem failure.
    """
    from kiro_crew.sel import sel as _sel  # circular import: sel imports config

    _sel().log_api_access(
        caller="system",
        operation=f"external_access:{surface}",
        outcome="allowed" if allowed else "denied",
        source="agent",
        resources=resource,
        error=error,
        critical=True,
    )


def _admits(surface: str, resource: str, probe: "Callable[[], bool]") -> bool:
    """Ask the composed policy one admission question, audited either way.

    Denies on a transient adapter failure rather than admitting. The only way to
    reach that fallback is for a COMPOSED policy to raise — a managed deployment
    whose intent was to restrict something — so admitting there would hand back
    the exact access the operator disabled. The public default cannot raise, so an
    ordinary install is unaffected, and ``PlatformCompositionError`` still
    propagates per the CPP fail-closed invariant.

    A FAILED AUDIT ALSO DENIES. If the verdict cannot be written to the security
    event log — an unwritable or corrupt SEL key — then proceeding would grant
    external access with no accountability record, which is the one thing this
    seam exists to make provable. Denying is the conservative direction: the
    operator loses a registry browser or a deploy button and gets a logged error,
    rather than silently gaining unaudited egress.

    SYNCHRONOUS BY DESIGN, and callers on the event loop must run it in a worker
    thread. SEL initialization does blocking filesystem work (trust-dir creation,
    key validation, and on Windows an owner-only DACL), so calling this inline
    from a coroutine would stall every request.
    """
    from kiro_crew.platform.context import safe_context_call

    failed: list[str] = []

    def _fallback() -> bool:
        failed.append("policy_error")
        return False

    allowed = safe_context_call(
        probe,
        fallback_factory=_fallback,
        log_message=f"external-access check failed for {surface} {resource!r}; denying",
    )
    try:
        _audit_admission(surface, resource, allowed, error="policy_error" if failed else "")
    except Exception:
        logger.error(
            "external-access verdict for %s %r could not be audited; denying",
            surface,
            resource,
            exc_info=True,
        )
        return False
    return allowed


def admits_registry(kind: str, name: str, api_base: str) -> bool:
    """Whether the composed platform admits an external discovery registry.

    The single call point for the registry half of the ``external_access`` seam, so
    both catalogs ask the question identically instead of each re-deriving the
    fail-closed idiom — the reason ``safe_context_call`` is centralized is that a
    hand-rolled ``except Exception`` at a call site silently swallows
    ``PlatformCompositionError``.
    """
    from kiro_crew.platform.context import current_context

    return _admits(
        f"registry:{kind}",
        api_base,
        lambda: current_context().external_access.admits_registry(kind, name, api_base),
    )


def admits_cloud_deployment(target: str = "aws") -> bool:
    """Whether the composed platform admits provisioning in a cloud account.

    Consulted by the deploy surface: a denied deployment reports itself disabled
    and refuses every mutating request.
    """
    from kiro_crew.platform.context import current_context

    return _admits(
        "cloud_deployment",
        target,
        lambda: current_context().external_access.admits_cloud_deployment(target),
    )


def _capability_manager() -> "CapabilityManager":
    """The edition's external capability manager (CPP seam).

    Lives in the shared layer (not a leaf handler) so every consumer —
    ``agents.py`` handlers, ``mcp.py`` uninstall, and the skill/prompt listers
    here — imports it DOWNWARD with no circular dependency. Operations-based: the
    edition owns its CLI grammar, output parsing, and error translation. Fails
    closed to an unavailable ``DefaultCapabilityManager`` so ``/api/capability/*``
    degrade to 503 rather than crashing.

    The returned manager is ALREADY LIVENESS-bounded: the context wraps every
    ``CapabilityManager`` in ``BoundedCapabilityManager`` at composition time
    (``PlatformContext.__post_init__``), so the ``asyncio.wait_for`` mutation
    bound is inherited by every reader of ``current_context().capability_manager``
    — not just callers who route through this accessor. The fallback
    ``DefaultCapabilityManager`` is bound here too so a context-lookup failure
    degrades to a wrapped (still unavailable) manager, keeping the return type
    uniform.
    """
    from kiro_crew.platform.capability_bound import bind_capability_manager
    from kiro_crew.platform.context import current_context, safe_context_call
    from kiro_crew.platform.defaults import DefaultCapabilityManager

    return safe_context_call(
        lambda: current_context().capability_manager,
        fallback_factory=lambda: bind_capability_manager(DefaultCapabilityManager()),
        log_message="capability_manager lookup failed; treating as unavailable",
    )


def _caller_bounds(request: web.Request) -> tuple[dict[str, str], int]:
    """Read the caller's own session bounds from the token that authenticated it.

    Shared by every handler that mints a NEW credential on the authority of an
    existing dashboard session (the mobile login link and the tailnet QR mint),
    so the two mint surfaces cannot drift apart on the invariant: the minted
    credential must never out-scope the session authorizing it.

    Returns ``(carried_claims, ttl_ceiling_seconds)``. ``ttl_ceiling`` is ``0``
    when the caller has no lifetime left to lend, which the handler refuses
    rather than minting against. Claims are carried, never re-derived: ``boot``
    copied verbatim (same rule as the link→session exchange in ``token_auth``),
    ``no_refresh`` copied so the recipient session never grows a refresh chain,
    and the remaining ``session_exp`` becomes the TTL ceiling so a short-lived
    caller cannot mint a longer-lived credential. ``require_peer`` and its
    signed ``peer_key`` move as one inseparable device bound. Fail-closed on an
    unreadable payload: a caller whose bounds cannot be established gets a
    bounded (no-refresh, default-TTL-capped) link rather than an unbounded one.

    **Read the credential the middleware VALIDATED, not a re-extracted one.**
    Only that credential has a verified signature; the other one was never
    checked. ``token_auth`` publishes it as ``request["auth_token"]`` for
    exactly this reason: its own extraction prefers ``?token=`` but falls back
    to the session cookie when the query token is invalid, so re-deriving with
    a fixed query-then-cookie order could pick the credential that was NOT
    validated — letting a request that authenticated with a bounded cookie have
    its bounds read from an unverified, attacker-settable query token, dropping
    ``no_refresh`` and raising the TTL ceiling to the full maximum, which is
    precisely the ceiling-escape this function exists to prevent. When no
    credential was published (a surface that authenticated by another means),
    the mint is bounded fail-closed the same way an unreadable payload is.

    **A non-positive remaining lifetime is never rounded up.** Clamping it to a
    floor of one second would let a caller whose own session has just run out
    mint a link that outlives it, and the exchange the recipient performs starts
    a fresh window — so repeating the mint would walk the expiry forward
    indefinitely from a session that should already be dead. Report ``0`` and
    let the caller be refused.
    """
    published = request.get("auth_token", "")
    token = published if isinstance(published, str) else ""
    carried: dict[str, str] = {}
    ttl_ceiling = MAX_SESSION_TTL_SECS
    if not token:
        # Authenticated without a readable token (unexpected on this surface):
        # fail closed by bounding the mint rather than trusting it.
        return {"no_refresh": "1"}, ttl_ceiling
    try:
        data = json.loads(_b64url_decode(token.split(".", 1)[0]))
        boot = str(data.get("boot", ""))
        if boot:
            carried["boot"] = boot
        if str(data.get("no_refresh", "")) == "1":
            carried["no_refresh"] = "1"
        if str(data.get("require_peer", "")) == "1":
            carried["require_peer"] = "1"
            # Middleware refuses a claimless require_peer cookie, so the
            # fallback is unreachable on a real authenticated request. Keep it
            # fail-closed for direct test doubles or future alternate auth:
            # an impossible key mints an unusable child instead of widening it.
            carried["peer_key"] = required_peer_key_unverified(token) or "unverified"
        session_exp = float(data.get("session_exp", 0.0))
        if session_exp:
            remaining = int(session_exp - time.time())
            if remaining <= 0:
                return carried, 0
            ttl_ceiling = min(ttl_ceiling, remaining)
    except Exception:
        return {"no_refresh": "1"}, ttl_ceiling
    return carried, ttl_ceiling


async def require_owner_dashboard_request(
    request: web.Request, operation: str
) -> web.Response | None:
    """Owner gate shared across dashboard handler modules.

    Returns ``None`` when the caller IS the dashboard owner, allowing the
    request to proceed.  Otherwise audits the denial via SEL (an enqueue —
    the singleton is warmed at startup, see ``sel.warm_sel_singleton``),
    checks for a stale pre-owner bootstrap subject (relabelling the denial
    to a 401), and falls back to a 403 with the standard ``owner_only`` code.

    Imports ``is_owner_dashboard_request`` and ``stale_owner_session_response``
    inside the function body to avoid a circular import: ``source_providers``
    imports chat-state helpers that reach back into sibling handler modules.
    """
    from kiro_crew.dashboard.handlers.source_providers import (
        is_owner_dashboard_request,
    )

    if is_owner_dashboard_request(request):
        return None

    # SEL is warmed at gateway startup (sel.warm_sel_singleton), so this
    # ``log_api_access`` only enqueues to the writer thread — no thread hop
    # needed. Guarded because a FAILED warm leaves construction to
    # retry here and possibly raise.
    caller = str(request.get("user") or "unknown")
    try:
        from kiro_crew.sel import sel as _sel

        _sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources="non_owner_block",
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for non-owner %s failed", operation, exc_info=True)

    # Deny decision made above; only the response label changes for a signed
    # pre-owner bootstrap subject (see stale_owner_session_response).
    return _owner_denial_response(request)


async def require_owner_for_dashboard_caller(
    request: web.Request, operation: str
) -> web.Response | None:
    """:func:`require_owner_dashboard_request` for the dashboard-user class only.

    A non-owner dashboard session (a Telegram, Teams or Webex allowlist link)
    holds the same empty app claim as the owner's own session, so an app guard
    alone admits it. This gate refuses it and audits the refusal. A caller whose
    app claim is not exactly ``""`` passes untouched and stays under the control
    that already governs it: an app token under its app-isolation check and
    manifest scope, an ``X-Internal-Secret`` caller under the internal transport
    (``token_auth`` leaves ``app`` absent for one acting as the person). It is a
    layer on top of those controls, never a replacement for them.
    """
    if request.get("app") != "":
        return None
    return await require_owner_dashboard_request(request, operation)


def _owner_denial_response(
    request: web.Request,
    error_message: str = "owner authorization required",
    error_code: str = "owner_only",
) -> web.Response:
    """Stale-session relabel + 403 denial -- the tail of every owner gate.

    Synchronous: ``stale_owner_session_response`` is a pure predicate over
    request attributes, so no I/O is involved.  Domain-specific wrappers that
    perform their own SEL/audit logging before reaching the denial response can
    call this directly instead of going through the full async
    ``require_owner_dashboard_request`` helper.

    Imports ``stale_owner_session_response`` inside the function body to avoid
    a circular import (same reason as the async helper above).
    """
    from kiro_crew.dashboard.handlers.source_providers import (
        stale_owner_session_response,
    )

    stale = stale_owner_session_response(request)
    if stale is not None:
        return stale
    return web.json_response(
        {"error": error_message, "code": error_code},
        status=403,
    )

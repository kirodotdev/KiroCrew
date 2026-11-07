"""What an app token may reach: its own namespace and the API prefixes its manifest declares.

Deny by default. The short-lived cache of each app's declared ``permissions.api``
allowlist, which paths an app owns, the implicit allowances every app token gets, and
the scope check the middleware applies to an app token's request, whose refusal tells
the app what clears it (:mod:`kiro_crew.dashboard.app_scope_refusal`).

Served through :mod:`kiro_crew.dashboard.token_auth`, which callers and tests import and
patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

from aiohttp import web

from kiro_crew.dashboard.app_scope_refusal import GrantFacts, ScopeRefusal, app_label, scope_refusal
from kiro_crew.dashboard.origin import is_proxied_request
from kiro_crew.dashboard.token_auth_paths import _deny, _log_auth
from kiro_crew.sel import sel as _sel_fn

logger = logging.getLogger("kiro_crew.dashboard.token_auth")


# -- App-token scope enforcement (CWE-269, least privilege) -------------------
#
# An app token (payload carries a non-empty ``app`` claim, minted by the
# X-App-Secret exchange at /api/apps/<name>/token) must NOT have the same
# reach as a dashboard-user token. Deny-by-default: an app token may only
# access (1) its own app namespace and (2) the API path prefixes the app
# declared in its manifest ``permissions.api`` allowlist. Everything else is
# rejected. Dashboard-user tokens (empty ``app`` claim) are never subject to
# this — the gate is a no-op for them.

# Short-TTL cache of each app's declared ``permissions.api`` allowlist so the
# hot auth path doesn't read app.json on every request. Permissions change
# rarely; a 30s TTL self-heals after enable/disable/update without any
# invalidation wiring.
_APP_PERMS_TTL = 30.0
_app_perms_cache: dict[str, tuple[float, tuple[str, ...]]] = {}
_app_perms_lock = threading.Lock()

# What a refusal tells the app about its own grant, stored by the same read as its
# allowlist entry and under the same timestamp, so the refusal path reads no disk.
# A refusal uses an entry only while that timestamp matches the allowlist entry's.
_app_grant_facts: dict[str, tuple[float, GrantFacts]] = {}


def _app_api_allowlist(app_name: str) -> tuple[str, ...]:
    """Return the app's declared ``permissions.api`` prefixes (cached, deny-safe).

    On any failure (app not installed, manifest unreadable) returns an empty
    tuple — i.e. deny-by-default: the app is confined to its own namespace only.
    """
    now = time.time()
    with _app_perms_lock:
        entry = _app_perms_cache.get(app_name)
        if entry is not None and now - entry[0] < _APP_PERMS_TTL:
            return entry[1]
    allow: tuple[str, ...] = ()
    seen: dict[str, tuple[str, tuple[str, ...]]] = {}
    try:
        # circular import: apps.manager imports generate_app_secret/
        # write_app_secret from token_auth, which loads this module, so a top-level
        # `import` here would form a cycle. Kept function-local deliberately.
        from kiro_crew.apps.manager import get_app_manifest, staged_app_grants

        def _declared() -> list[str] | None:
            manifest = get_app_manifest(app_name)
            if manifest is None:
                return None
            declared = [p for p in manifest.permissions.api if p]
            # Only words a refusal, so a missing name must not fail this read.
            seen["manifest"] = (getattr(manifest, "displayName", ""), tuple(declared))
            return declared

        # An update's added entries stay out until the owner approves them.
        allow = tuple(staged_app_grants(app_name, "api", _declared))
    except Exception:
        logger.warning(
            "app scope: could not load permissions for %r; denying by default",
            app_name,
            exc_info=True,
        )
        allow = ()
    facts = _read_grant_facts(app_name, seen.get("manifest"))
    with _app_perms_lock:
        _app_perms_cache[app_name] = (now, allow)
        _app_grant_facts[app_name] = (now, facts)
    return allow


def _read_grant_facts(app_name: str, manifest: tuple[str, tuple[str, ...]] | None) -> GrantFacts:
    """The grant facts a refusal of *app_name* words itself from.

    *manifest* is the display name and declared entries the allowlist read saw,
    ``None`` when it read no manifest. Never raises: these facts only word a
    refusal, so a failure here must not change the allowlist beside them.
    """
    if manifest is None:
        return GrantFacts(label=app_name, declared=None, held_back=())
    display_name, declared = manifest
    label = app_label(app_name, display_name)
    try:
        from kiro_crew.apps.manager import held_back_app_grants

        held = held_back_app_grants(app_name, "api", declared)
    except Exception:
        logger.debug("app scope: could not read held-back entries for %r", app_name, exc_info=True)
        held = None
    if held is None:
        # The app moved between the allowlist read and this one, or the read
        # failed: claim no cause rather than pair one manifest with another's grant.
        return GrantFacts(label=label, declared=None, held_back=())
    return GrantFacts(label=label, declared=declared, held_back=tuple(held))


# A primed entry is refreshed this long before it expires, so the synchronous
# read that follows the prime in the same request finds it fresh.
_APP_PERMS_PRIME_MARGIN = 5.0


async def _prime_app_api_allowlist(app_name: str) -> None:
    """Refresh *app_name*'s allowlist cache OFF the event loop when it is stale.

    A refresh reads the manifest, the install record and the app's directory
    (``staged_app_grants``); the async middleware awaits this before the
    synchronous ``_enforce_app_scope`` so that I/O never runs on the loop
    (no-blocking-call-on-event-loop). An entry within the margin of expiry is
    dropped first, so the scope check in the same request still finds it fresh.
    """
    if not app_name:
        return
    with _app_perms_lock:
        entry = _app_perms_cache.get(app_name)
        if entry is not None:
            if time.time() - entry[0] < _APP_PERMS_TTL - _APP_PERMS_PRIME_MARGIN:
                return
            del _app_perms_cache[app_name]
    await asyncio.to_thread(_app_api_allowlist, app_name)


# Literal first path segments registered under ``/api/apps/`` that are NOT the
# ``/api/apps/{name}`` catch-all. Source of truth is the route table in
# ``kiro_crew.apps.routes.setup_routes`` (the ``add_get``/``add_post`` calls for
# ``/api/apps/registry``, ``/api/apps/registries``, ``/api/apps/blob``,
# ``/api/apps/registry/install``, ``/api/apps/install`` and
# ``/api/apps/register``), all registered BEFORE ``/api/apps/{name}``.
#
# These segments resolve to SHARED literal routes (registry listing, registry
# install, blob proxy, install, self-registration, registry refresh — which
# triggers outbound git fetches of every configured registry). Without this
# carve-out an app that names itself after one of them (e.g. ``registries``)
# would implicitly own that route via ``_app_owns_path`` and could invoke it
# with no ``permissions.api`` grant (CWE-269 authorization bypass). The carve-out
# is the primary security boundary; ``apps.manifest.RESERVED_APP_PATH_SEGMENTS``
# mirrors this set to also refuse such names at manifest validation for NEW apps
# (defense-in-depth). Keep both lists in sync with the routes.py table.
RESERVED_APP_PATH_SEGMENTS: frozenset[str] = frozenset(
    {"registry", "registries", "blob", "install", "register"}
)


def _app_owns_path(app_name: str, path: str) -> bool:
    """True if *path* is within *app_name*'s own namespace.

    Covers the reverse-proxy + UI surface (``/apps/<name>/...``) and the
    per-app management/config surface (``/api/apps/<name>/...``). Membership is
    a path-boundary match so app ``foo`` cannot reach app ``foo-bar``.

    Carve-out: the ``/api/apps/<name>`` branch does NOT match when ``app_name``
    is one of ``RESERVED_APP_PATH_SEGMENTS`` — those segments resolve to shared
    literal routes registered before ``/api/apps/{name}``, so treating them as an
    app's own namespace would hand any app so named implicit ownership of those
    routes without a ``permissions.api`` grant. The ``/apps/<name>`` reverse-proxy
    branch is a separate namespace (its literal-page reservations live in
    ``apps.manifest.RESERVED_ROUTE_APP_NAMES``) and is intentionally left
    unchanged.
    """
    if path == f"/apps/{app_name}" or path.startswith(f"/apps/{app_name}/"):
        return True
    if app_name not in RESERVED_APP_PATH_SEGMENTS:
        api_base = f"/api/apps/{app_name}"
        if path == api_base or path.startswith(api_base + "/"):
            return True
    return False


def _api_pattern_matches(pattern: str, path: str) -> bool:
    """Match a ``permissions.api`` entry against a request path.

    Supports trailing ``/*`` and ``*`` wildcards; a bare prefix matches the
    exact path or any child under a path boundary (``/api/chat`` matches
    ``/api/chat`` and ``/api/chat/slots`` but NOT ``/api/chatx``).
    """
    pattern = pattern.strip()
    if not pattern:
        return False
    if pattern.endswith("/*"):
        base = pattern[:-2]
        return path == base or path.startswith(base + "/")
    if pattern.endswith("*"):
        return path.startswith(pattern[:-1])
    return path == pattern or path.startswith(pattern + "/")


# Protocol-layer paths every app token implicitly needs — connection
# infrastructure, not feature-level permissions. Requiring each app to declare
# them in ``permissions.api`` adds no security value and produces silent 403
# regressions whenever a new app forgets to list them.
#
# ``/api/ws`` is safe to allow implicitly ONLY because the WS layer applies
# per-app event scope filtering (``ws_event_scope.py``): a connected app token
# receives just the events matching its ``permissions.events`` declarations, so
# connecting does not grant the full event stream. Contrast with functional
# paths like /api/chat/* or /api/spawn/* — those grant real capabilities and
# MUST stay explicitly declared.
#
# ``/api/status`` is deliberately NOT here. It has no response-level filter to
# match what event scoping does for ``/api/ws``, and ``api_status`` returns far
# more than liveness: ``owner_id_hash``, host specs (os/arch/cpu/memory), cron
# and usage stats, and the live safety-override (``yolo_*``) state. The
# connect/reconnect poll that needs it is the DASHBOARD SPA
# (``useDashboardHealthProbe``), which runs on a dashboard-user token and never
# reaches this list. An app that genuinely wants it declares it in
# ``permissions.api`` — the shipped ``design_critique`` manifest does.
_APP_TOKEN_IMPLICIT_ALLOW: frozenset[str] = frozenset(
    {
        # Connecting grants no events by itself: the socket records the caller's
        # manifest declarations and every frame is filtered per socket, payload AND
        # envelope, in ws_event_scope.py / DashboardState._serialize_for_client.
        "/api/ws",
    }
)


def app_token_path_allowed(app_name: str, path: str) -> bool:
    """Return True if an app token for *app_name* may access *path*.

    Deny-by-default (CWE-269). Only call this for app tokens (non-empty
    ``app_name``); dashboard-user tokens must bypass it entirely.
    """
    if not app_name:
        # Defensive: a caller should never pass an empty app_name here, but if
        # it does, do NOT silently grant — that would turn the gate into a
        # no-op allow. Return False; the caller's non-empty guard is primary.
        return False
    if path in _APP_TOKEN_IMPLICIT_ALLOW:
        # Audit implicit grants so every app-token path decision is in the trail.
        try:
            _sel_fn().log_api_access(
                caller=app_name,
                operation="app_scope_check",
                outcome="granted_implicit",
                source="token_auth",
                resources=path,
            )
        except Exception as exc:
            # Security-relevant audit path (CWE-269 implicit grant); a persistent
            # SEL misconfiguration must be observable, so log rather than pass.
            logger.debug(
                # Message deliberately avoids the module-name prefix: Semgrep's
                # logger-credential-disclosure heuristic fires on the substring
                # alone. The logged values are an app name, a path, and an
                # exception -- no secret material.
                "SEL audit for implicit app-scope allow %s -> %s failed: %s",
                app_name,
                path,
                exc,
            )
        return True
    if _app_owns_path(app_name, path):
        return True
    # Notification push (RFC local notification bus, Phase 2): every app may
    # reach this single push-only endpoint — the handler independently
    # enforces app identity (from the verified token), manifest-declared
    # channels, and per-app rate limits, so the grant confers no cross-app
    # authority. Deliberately NOT /api/notifications: that path also serves
    # GET (read history) and DELETE, which app tokens must not reach.
    if path == "/api/notifications/push":
        return True
    return any(_api_pattern_matches(p, path) for p in _app_api_allowlist(app_name))


def _scope_refusal(app_name: str, path: str) -> ScopeRefusal:
    """Word the refusal of *app_name*'s token on *path* from the cached grant facts.

    Facts stored by a different read than the allowlist entry that refused the
    path are not used: they could name an entry the owner has since approved.
    """
    with _app_perms_lock:
        allow_entry = _app_perms_cache.get(app_name)
        facts_entry = _app_grant_facts.get(app_name)
    facts = None
    if allow_entry is not None and facts_entry is not None and facts_entry[0] == allow_entry[0]:
        facts = facts_entry[1]
    if facts is None or facts.declared is None:
        pending: tuple[str, ...] = ()
        declared: bool | None = None
    else:
        pending = tuple(e for e in facts.held_back if _api_pattern_matches(e, path))
        declared = any(_api_pattern_matches(e, path) for e in facts.declared)
    return scope_refusal(
        app_name,
        facts.label if facts is not None else app_name,
        path,
        pending_approval=pending,
        declared=declared,
        approval_delay_secs=int(_APP_PERMS_TTL),
    )


def _enforce_app_scope(request: web.Request, app_name: str, path: str) -> web.Response | None:
    """Return a 403 response if an app token is out of scope, else None.

    No-op for dashboard-user tokens (empty *app_name*). On an ``/api/`` path the
    body names why the path is refused and what clears it (:mod:`app_scope_refusal`);
    any other path keeps the shared sign-in page, which quotes only a fixed reason.
    """
    if not app_name:
        return None
    if app_token_path_allowed(app_name, path):
        return None
    # SEL audit for the permission decision (matches the sibling deny paths in
    # the middleware, which log_api_access in addition to _log_auth).
    _sel_fn().log_api_access(
        caller=app_name,
        forwarded=is_proxied_request(request),
        operation="app_scope_check",
        outcome="denied",
        source="token_auth",
        resources=path,
        error="app token out of scope",
    )
    _log_auth(request, app_name, "denied", f"app token out of scope: {path}")
    if not request.path.startswith("/api/"):
        return _deny(request, "app token not permitted for this endpoint")
    refusal = _scope_refusal(app_name, path)
    return web.json_response(
        {
            "error": refusal.message,
            "code": refusal.code,
            "pending_approval": list(refusal.pending_approval),
        },
        status=403,
        # The header every middleware refusal carries (``_deny``).
        headers={"X-Auth-Required": "true"},
    )

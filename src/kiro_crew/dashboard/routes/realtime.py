"""Route registration for websocket, status/system, capability inventory, suggestions and tips.

One contiguous slice of the dashboard's route table, kept in its original
order. aiohttp resolves routes in REGISTRATION order, and several routes here
rely on a literal path being registered before a pattern that would otherwise
swallow it, so neither the lines within this function nor the order in which
``server.start_dashboard`` calls the registrars may be rearranged.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.dashboard import handlers, ws
from kiro_crew.suggestions import api_suggestions
from kiro_crew.tips import api_tips_feedback, api_tips_next, api_tips_status


def register(app: web.Application) -> None:
    """Register the realtime routes on *app*."""
    app.router.add_get("/", handlers.index)
    app.router.add_get("/logo.png", handlers.logo)
    # A long tail of clients (bookmark managers, in-app browsers, uptime
    # monitors) never parse <link rel="icon"> and fetch /favicon.ico directly.
    # Without this route the request falls through to the SPA fallback and
    # returns 200 text/html, which fails image decoding downstream.
    app.router.add_get("/favicon.ico", handlers.logo)
    app.router.add_get(
        "/{name:manifest\\.json|sw\\.js|icon-\\d+\\.png|pcm-worklet\\.js}", handlers.pwa_file
    )

    # WebSocket (multiplexed real-time events)
    app.router.add_get("/api/ws", ws.api_ws)

    # Status / system
    app.router.add_get("/api/status", handlers.api_status)
    app.router.add_get("/api/system", handlers.api_system)
    app.router.add_get("/api/system/session-storage", handlers.api_session_storage)
    # The inventory list and its per-row detail. Registered before the {uid} route
    # so the literal path cannot be swallowed by the pattern.
    app.router.add_get("/api/system/session-storage/sessions", handlers.api_session_inventory)
    app.router.add_get(
        "/api/system/session-storage/sessions/{uid}", handlers.api_session_inventory_detail
    )
    app.router.add_post("/api/system/session-storage/trash", handlers.api_session_inventory_trash)
    app.router.add_post("/api/system/session-storage/cleanup", handlers.api_session_storage_cleanup)
    app.router.add_post("/api/system/session-storage/restore", handlers.api_session_storage_restore)
    app.router.add_post("/api/system/session-storage/empty", handlers.api_session_storage_empty)
    # Progress for the empty above. A GET on the same path, because it reports on
    # exactly the operation that POST starts.
    app.router.add_get(
        "/api/system/session-storage/empty", handlers.api_session_storage_empty_status
    )
    app.router.add_get("/api/stream", handlers.api_stream)
    app.router.add_get("/api/sso-ttl", handlers.api_sso_ttl)
    app.router.add_get("/api/dashboard/branding", handlers.api_branding)
    app.router.add_get("/api/health", handlers.api_health)
    app.router.add_get("/api/live", handlers.api_live)
    app.router.add_get("/api/ready", handlers.api_ready)
    app.router.add_get("/api/theme/boot", handlers.api_theme_boot)
    app.router.add_get("/api/admin/compliance/yolo-status", handlers.api_compliance_yolo_status)
    app.router.add_get(
        "/api/kiro-prerequisite",
        handlers.api_kiro_prerequisite_status,
    )
    # POST, not a flag on the status GET: csrf_middleware skips check_origin for
    # safe methods and sel_audit_middleware logs only mutating ones, so a spec
    # rewrite reached from the GET would be cross-site triggerable and unaudited.
    app.router.add_post(
        "/api/kiro-prerequisite/repair-specs",
        handlers.api_kiro_prerequisite_repair_specs,
    )
    # POST for the same reason as repair-specs above: it spawns `kiro-cli update`
    # on the host, so it must be origin-checked and audited.
    app.router.add_post(
        "/api/kiro-prerequisite/update-cli",
        handlers.api_kiro_prerequisite_update_cli,
    )

    # KAS-mode interactive login (no kiro-cli): status is a read; the device-code
    # begin/poll/logout mutations are POSTs so they stay origin-checked and audited.
    # The kas_login module (and the auth subsystem behind it) is imported on the
    # FIRST request, never at boot — route registration must add no gateway-boot
    # work for a subsystem most launches never touch. Python caches the module
    # after that first import, so the per-request cost is a dict lookup.
    def _lazy_kas(handler_name: str):
        async def _dispatch(request):
            from kiro_crew.dashboard.handlers import kas_login

            return await getattr(kas_login, handler_name)(request)

        return _dispatch

    app.router.add_get("/api/kas-login", _lazy_kas("api_kas_login_status"))
    app.router.add_post("/api/kas-login/device", _lazy_kas("api_kas_login_begin_device"))
    app.router.add_post("/api/kas-login/poll", _lazy_kas("api_kas_login_poll"))
    app.router.add_post("/api/kas-login/logout", _lazy_kas("api_kas_login_logout"))
    app.router.add_get("/api/governance/channels", handlers.api_governance_channels)

    # Suggestions (pre-computed contextual prompts)
    app.router.add_get("/api/suggestions", api_suggestions)

    # Tips (feature discovery)
    app.router.add_get("/api/tips/next", api_tips_next)
    app.router.add_get("/api/tips/status", api_tips_status)
    app.router.add_post("/api/tips/feedback", api_tips_feedback)

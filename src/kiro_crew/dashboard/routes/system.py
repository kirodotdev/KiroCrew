"""Route registration for notifications, restart, update, search, app platform and builtin apps.

One contiguous slice of the dashboard's route table, kept in its original
order. aiohttp resolves routes in REGISTRATION order, and several routes here
rely on a literal path being registered before a pattern that would otherwise
swallow it, so neither the lines within this function nor the order in which
``server.start_dashboard`` calls the registrars may be rearranged.
"""

from __future__ import annotations

import importlib
import os

from aiohttp import web

from kiro_crew.apps.builtins import BUILTIN_NAMES
from kiro_crew.apps.routes import register_app_routes
from kiro_crew.constants import env_flag_enabled
from kiro_crew.dashboard import handlers
from kiro_crew.dashboard.handlers.tunnel import api_tunnel_status


def register(app: web.Application) -> None:
    """Register the system routes on *app*."""
    # Misc (notifications GET/clear and send-message via _register_mcp_routes)
    app.router.add_get("/api/notifications", handlers.api_notifications)
    app.router.add_delete("/api/notifications", handlers.api_notification_delete)
    app.router.add_post("/api/notifications/ack", handlers.api_notification_ack)
    app.router.add_post("/api/notifications/unack", handlers.api_notification_unack)
    app.router.add_post("/api/notifications/ack-all", handlers.api_notifications_ack_all)
    app.router.add_get("/api/notifications/channels", handlers.api_notification_channels)
    app.router.add_put(
        "/api/notifications/channels/settings", handlers.api_notification_channel_settings
    )
    app.router.add_get("/api/update/check", handlers.api_update_check)
    app.router.add_get("/api/changelog", handlers.api_changelog)
    app.router.add_get("/api/releases", handlers.api_releases)
    app.router.add_post("/api/update", handlers.api_update_apply)
    app.router.add_post("/api/update/auto", handlers.api_update_auto)
    app.router.add_post("/api/update/channel", handlers.api_update_channel)
    app.router.add_post("/api/update/cancel", handlers.api_update_cancel)
    # Restart with no update. Sibling of /api/update rather than a mode of it:
    # /api/update refuses every layout that is not a git checkout, while a
    # restart is valid everywhere and is how a wheel install picks up code a
    # terminal-run installer already replaced on disk.
    app.router.add_post("/api/restart", handlers.api_gateway_restart)
    # Only expose the simulation endpoint in dev/debug environments
    _is_dev_env = os.environ.get("KIROCREW_HOME", "").endswith("-dev")
    if _is_dev_env or env_flag_enabled("KIROCREW_DEV_MODE"):
        app.router.add_post("/api/update/simulate", handlers.api_update_simulate)
    app.router.add_get("/api/sessions", handlers.api_sessions)
    app.router.add_delete("/api/sessions", handlers.api_sessions_clear)
    app.router.add_get("/api/sessions/context", handlers.api_sessions_context)
    app.router.add_get("/api/sessions/memory", handlers.api_sessions_memory)
    app.router.add_get("/api/sessions/health", handlers.api_sessions_health)
    app.router.add_get("/api/sessions/usage", handlers.api_sessions_usage)
    app.router.add_get("/api/usage/kiro", handlers.api_kiro_usage)
    app.router.add_get("/api/usage", handlers.api_usage)
    app.router.add_get("/api/telemetry/startup", handlers.api_telemetry_startup)
    app.router.add_get("/api/telemetry/context-trace", handlers.api_context_trace)
    app.router.add_get("/api/telemetry/beacon", handlers.api_beacon_status)
    app.router.add_get("/api/telemetry/collection", handlers.api_collection_status)
    app.router.add_get("/api/tailnet/status", handlers.api_tailnet_status)
    app.router.add_post("/api/sessions/restart", handlers.api_sessions_restart)
    # NOTE: /search must be registered before /{key} to avoid the path param catching "search"
    app.router.add_get("/api/sessions/search", handlers.api_sessions_search)
    app.router.add_post("/api/sessions/summarize", handlers.api_sessions_summarize)
    app.router.add_get("/api/sessions/{key}", handlers.api_session_detail)
    app.router.add_delete("/api/sessions/{key}", handlers.api_session_delete)
    app.router.add_get("/api/logs", handlers.api_logs)
    app.router.add_get("/api/logs/level", handlers.api_log_level_get)
    app.router.add_post("/api/logs/level", handlers.api_log_level)
    app.router.add_get("/api/sel/events", handlers.api_sel_events)
    app.router.add_get("/api/sel/verify", handlers.api_sel_verify)
    app.router.add_get("/api/security/stats", handlers.api_security_stats)
    app.router.add_get("/api/security/posture", handlers.api_security_posture)
    app.router.add_get("/api/security/denied-commands", handlers.api_denied_commands_list)
    app.router.add_patch(
        "/api/security/denied-commands/disable-all", handlers.api_denied_commands_disable_all
    )
    app.router.add_patch(
        "/api/security/denied-commands/builtins/{id}", handlers.api_denied_command_builtin_toggle
    )
    app.router.add_post("/api/security/denied-commands/user", handlers.api_denied_command_user_add)
    app.router.add_patch(
        "/api/security/denied-commands/user/{id}", handlers.api_denied_command_user_toggle
    )
    app.router.add_delete(
        "/api/security/denied-commands/user/{id}", handlers.api_denied_command_user_delete
    )
    # Per-app third-party execution grants (Settings > Security opt-IN). The
    # blanket flag is a PUT on a fixed sub-path; grant/revoke are POST/DELETE on
    # {name}, so the two never collide on method+path.
    app.router.add_get("/api/security/trusted-apps", handlers.api_trusted_apps_list)
    app.router.add_put("/api/security/trusted-apps/allow-all", handlers.api_trusted_apps_allow_all)
    app.router.add_post("/api/security/trusted-apps/{name}", handlers.api_trusted_app_grant)
    app.router.add_delete("/api/security/trusted-apps/{name}", handlers.api_trusted_app_revoke)
    # Read-only governance policy viewer — effective Level-1 ∩ Level-2 ceiling
    # across every governed scope (no write path; the ceiling is file-authored).
    app.router.add_get("/api/governance/policy", handlers.api_governance_policy)

    # Computer use (Settings > Computer Use). Browser-called and cookie-authed,
    # like the browser-config pair — deliberately NOT in
    # ``_STRICT_INTERNAL_API_PATHS``. The machine-only ``invoke`` leg IS in that
    # set and is registered in ``_register_mcp_routes``.
    app.router.add_get("/api/computer-use/config", handlers.api_computer_use_config_get)
    app.router.add_put("/api/computer-use/config", handlers.api_computer_use_config_save)
    app.router.add_get("/api/approvals", handlers.api_approvals)
    app.router.add_post("/api/approvals/{id}/{action}", handlers.api_approval_resolve)

    # Local token bootstrap (file-based secret auth in handler, bypasses middleware)
    app.router.add_get("/api/token/local", handlers.api_token_local)

    # Tunnel status
    app.router.add_get("/api/tunnel/status", api_tunnel_status)

    # Session revocation (called by `kirocrew logout` CLI)
    app.router.add_post("/api/logout", handlers.api_logout)
    app.router.add_post("/api/shutdown", handlers.api_shutdown)

    # Webhook hooks (external triggers)
    app.router.add_post("/api/hooks/agent", handlers.api_hooks_agent)

    # App Platform
    register_app_routes(app)

    # Built-in app routes — register at startup (handlers check enabled state)
    for _builtin_name in BUILTIN_NAMES:
        try:
            _mod = importlib.import_module(f"kiro_crew.apps.builtins.{_builtin_name}")
            if hasattr(_mod, "register_routes"):
                _mod.register_routes(app)
        except ModuleNotFoundError as exc:
            if exc.name != f"kiro_crew.apps.builtins.{_builtin_name}":
                raise

    # App token exchange (App Kit §5.1 — must be before auth middleware bypass)
    app.router.add_post("/api/apps/{name}/token", handlers.api_app_token)

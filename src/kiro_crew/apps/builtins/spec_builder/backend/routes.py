"""Compose and register the Spec Builder HTTP surface."""

from __future__ import annotations

import asyncio
import logging
from functools import wraps

from aiohttp import web

from kiro_crew.apps.manager import is_app_enabled

from .handlers import (
    _handle_approve,
    _handle_archive,
    _handle_browse,
    _handle_create,
    _handle_delete,
    _handle_duplicate,
    _handle_get,
    _handle_get_settings,
    _handle_handoff,
    _handle_list,
    _handle_message,
    _handle_messages,
    _handle_put_settings,
    _handle_recover_decision,
    _handle_repo_info,
    _handle_run_task,
    _handle_stop_execution,
    _handle_title,
)
from .repository import (
    _DUPLICATE_RECOVERY_STATE,
    APP_NAME,
    _DuplicateRecoveryState,
    _ensure_duplicate_recovery,
)

logger = logging.getLogger("kirocrew.app.spec-builder")


def _require_enabled(handler):
    """Deny requests when Spec Builder is disabled (deny-by-default). Routes are
    registered once at gateway startup, so a default-disabled / opt-in app would
    otherwise stay callable. ``is_app_enabled`` reads installed.json synchronously,
    so the lookup runs off the event loop."""

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.Response:
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            return web.json_response(
                {"code": "app_disabled", "error": "spec-builder is disabled"}, status=403
            )
        # Handlers own the 401 response, but an unauthenticated probe must not
        # trigger filesystem work before that gate runs.
        if request.get("user") is not None:
            await _ensure_duplicate_recovery(request.app)
        return await handler(request)

    return _wrapped


def register_routes(app: web.Application) -> None:
    """Register this app's routes without touching the filesystem.

    Registration runs on the gateway event loop. Eager state-directory creation
    could therefore stall startup on remote storage for an app that is never used;
    writers create their own directories off-loop instead.
    """
    base = f"/api/apps/{APP_NAME}"
    # Mutable per-Application state lets the first enabled request publish one
    # recovery task without mutating a frozen aiohttp Application. Registration
    # itself stays filesystem-free so gateway readiness never depends on this app.
    recovery: _DuplicateRecoveryState = {"task": None}
    app[_DUPLICATE_RECOVERY_STATE] = recovery
    app.router.add_get(f"{base}/settings", _require_enabled(_handle_get_settings))
    app.router.add_put(f"{base}/settings", _require_enabled(_handle_put_settings))
    app.router.add_post(f"{base}/settings", _require_enabled(_handle_put_settings))
    app.router.add_get(f"{base}/repo-info", _require_enabled(_handle_repo_info))
    app.router.add_get(f"{base}/browse", _require_enabled(_handle_browse))
    app.router.add_get(f"{base}/specs", _require_enabled(_handle_list))
    app.router.add_post(f"{base}/specs", _require_enabled(_handle_create))
    app.router.add_get(f"{base}/specs/{{name}}", _require_enabled(_handle_get))
    app.router.add_get(f"{base}/specs/{{name}}/messages", _require_enabled(_handle_messages))
    app.router.add_post(
        f"{base}/specs/{{name}}/recover-decision",
        _require_enabled(_handle_recover_decision),
    )
    app.router.add_post(f"{base}/specs/{{name}}/message", _require_enabled(_handle_message))
    app.router.add_post(f"{base}/specs/{{name}}/handoff", _require_enabled(_handle_handoff))
    # Alias: the SPA page calls this "execute".
    app.router.add_post(f"{base}/specs/{{name}}/execute", _require_enabled(_handle_handoff))
    app.router.add_post(f"{base}/specs/{{name}}/stop", _require_enabled(_handle_stop_execution))
    # Direct authority over the artifacts, rather than only the ability to ask the
    # agent for a change: record a phase approval, run one task, and manage the
    # label / archive / duplicate lifecycle.
    app.router.add_post(f"{base}/specs/{{name}}/approve", _require_enabled(_handle_approve))
    app.router.add_post(f"{base}/specs/{{name}}/task", _require_enabled(_handle_run_task))
    app.router.add_post(f"{base}/specs/{{name}}/title", _require_enabled(_handle_title))
    app.router.add_post(f"{base}/specs/{{name}}/archive", _require_enabled(_handle_archive))
    app.router.add_post(f"{base}/specs/{{name}}/duplicate", _require_enabled(_handle_duplicate))
    app.router.add_delete(f"{base}/specs/{{name}}", _require_enabled(_handle_delete))
    logger.info("spec-builder: registered app routes under %s", base)

"""Meetings — route registration.

Registered at gateway startup by ``dashboard/server.py``'s builtin loop (which
imports the app package and calls its ``register_routes``); the manifest field
``backend.routes = "backend.routes:register_routes"`` names the same entry point
for the generic App Kit loader.

Every handler lives under ``/api/apps/meetings/*`` on the gateway's OWN aiohttp
Application — same-origin, behind the dashboard's token auth. Upstream instead
ran ``web.run_app`` on its own port and called back into the gateway over
authenticated loopback HTTP; that whole second server (and its copy of the auth
path) is gone.

Handlers are wrapped by :func:`.._common.route`, which applies the
deny-by-default enable gate and turns validation failures into 4xx.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.domain import session as sess
from kiro_crew.apps.builtins.meetings.backend.routes import agents as agents_routes
from kiro_crew.apps.builtins.meetings.backend.routes import calendar as calendar_routes
from kiro_crew.apps.builtins.meetings.backend.routes import meeting_lifecycle as lifecycle_routes
from kiro_crew.apps.builtins.meetings.backend.routes import settings as settings_routes
from kiro_crew.apps.builtins.meetings.backend.routes import tasks as tasks_routes
from kiro_crew.apps.builtins.meetings.backend.routes._common import ACTIVE, route
from kiro_crew.executors import subprocess_executor

logger = logging.getLogger("kirocrew.app.meetings")

BASE = k.API_BASE


async def _on_startup(app: web.Application) -> None:
    """Create the data subtree and load the dictionary once at boot.

    This is the Python home of what upstream shipped as a multi-line ``mkdir -p``
    shell blob prepended to a cron message. Both steps touch the filesystem, so
    they run on an executor rather than the loop, and neither may break gateway
    startup.
    """
    def _init() -> int:
        root = app.get("_meetings_data_root")
        store.ensure_data_dirs(root)
        return len(sess.reload_dictionary(root).terms)

    try:
        count = await asyncio.get_running_loop().run_in_executor(subprocess_executor(), _init)
        logger.info("meetings: data dir ready, %d dictionary term(s) loaded", count)
    except Exception:  # pragma: no cover — defensive
        logger.warning("meetings: data-dir init failed", exc_info=True)


async def _on_cleanup(app: web.Application) -> None:
    """Flush a live meeting's queued transcript, then drop it, on shutdown."""
    try:
        # A restart is not a reason to lose what was said; the drain is bounded and
        # a failure inside it still tears the session down.
        await ACTIVE.drain_and_clear()
    except Exception:  # pragma: no cover — defensive
        logger.debug("meetings: cleanup failed", exc_info=True)


def register_routes(app: web.Application) -> None:
    """Register the Meetings app's routes on the gateway's aiohttp Application.

    Signature matches every other builtin app (see
    ``issue_radar/backend/routes.py:register_routes``): one argument, no base
    path passed in.
    """
    router = app.router

    # Config + dictionary
    router.add_get(f"{BASE}/config", route(settings_routes.handle_get_config))
    router.add_put(f"{BASE}/config", route(settings_routes.handle_put_config))
    router.add_get(f"{BASE}/dictionary", route(settings_routes.handle_get_dictionary))
    router.add_post(f"{BASE}/dictionary", route(settings_routes.handle_add_dictionary_term))
    router.add_post(
        f"{BASE}/dictionary/remove", route(settings_routes.handle_remove_dictionary_term)
    )
    router.add_post(
        f"{BASE}/dictionary/reload", route(settings_routes.handle_reload_dictionary)
    )

    # Calendar
    router.add_get(f"{BASE}/calendar", route(calendar_routes.handle_get_calendar))
    router.add_post(f"{BASE}/calendar/sync", route(calendar_routes.handle_calendar_sync))
    router.add_get(
        f"{BASE}/calendar/providers", route(calendar_routes.handle_calendar_providers)
    )

    # Agents + dispatcher
    router.add_get(f"{BASE}/agents", route(agents_routes.handle_get_agents))
    router.add_get(f"{BASE}/status", route(agents_routes.handle_status))
    router.add_get(f"{BASE}/task-providers", route(tasks_routes.handle_task_providers))

    # Meetings
    router.add_get(f"{BASE}/meetings", route(lifecycle_routes.handle_list_meetings))
    router.add_get(
        BASE + "/meetings/{meeting_id}", route(lifecycle_routes.handle_get_meeting)
    )
    router.add_post(
        BASE + "/meetings/{meeting_id}/init", route(lifecycle_routes.handle_meeting_init)
    )
    router.add_post(
        BASE + "/meetings/{meeting_id}/start", route(lifecycle_routes.handle_start_meeting)
    )
    router.add_post(
        BASE + "/meetings/{meeting_id}/status", route(lifecycle_routes.handle_meeting_status)
    )
    router.add_post(
        BASE + "/meetings/{meeting_id}/stop", route(lifecycle_routes.handle_stop_meeting)
    )
    router.add_get(
        BASE + "/meetings/{meeting_id}/outputs", route(lifecycle_routes.handle_get_outputs)
    )
    router.add_post(
        BASE + "/meetings/{meeting_id}/attachments",
        route(lifecycle_routes.handle_attachments),
    )

    # Per-meeting agent control
    router.add_post(
        BASE + "/meetings/{meeting_id}/agents", route(agents_routes.handle_toggle_agent)
    )
    router.add_post(
        BASE + "/meetings/{meeting_id}/mute", route(agents_routes.handle_mute_agent)
    )
    router.add_post(
        BASE + "/meetings/{meeting_id}/dispatch", route(agents_routes.handle_dispatch_text)
    )
    router.add_post(
        BASE + "/meetings/{meeting_id}/message", route(agents_routes.handle_agent_message)
    )
    router.add_post(
        BASE + "/meetings/{meeting_id}/reset", route(agents_routes.handle_reset_agents)
    )

    # Tasks
    router.add_get(BASE + "/meetings/{meeting_id}/tasks", route(tasks_routes.handle_get_tasks))
    router.add_post(BASE + "/meetings/{meeting_id}/tasks", route(tasks_routes.handle_add_task))
    router.add_patch(
        BASE + "/meetings/{meeting_id}/tasks", route(tasks_routes.handle_update_task)
    )
    router.add_delete(
        BASE + "/meetings/{meeting_id}/tasks", route(tasks_routes.handle_delete_task)
    )
    router.add_post(
        BASE + "/meetings/{meeting_id}/tasks/file", route(tasks_routes.handle_file_task)
    )
    router.add_post(
        BASE + "/meetings/{meeting_id}/tasks/review", route(tasks_routes.handle_review_task)
    )

    # register_routes runs before runner.setup() freezes the signal lists, so
    # these appends fire (same pattern as issue-radar's watcher hooks). Guarded
    # so a hook-append failure can never break gateway startup.
    try:
        app.on_startup.append(_on_startup)
        app.on_cleanup.append(_on_cleanup)
    except Exception:  # pragma: no cover — defensive
        logger.warning("meetings: could not register lifecycle hooks", exc_info=True)

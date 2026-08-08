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
from typing import Any

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


def _reconcile_orphaned_meetings(root: Any) -> list[str]:
    """End meetings left mid-flight by a process that never ran its cleanup. BLOCKING.

    ``_on_cleanup`` covers a GRACEFUL shutdown. It cannot cover a hard kill —
    ``SIGKILL``, an OOM, a crash, a power loss — and that is precisely when a
    meeting is most likely to be mid-flight. So the same false-``active`` state the
    cleanup hook exists to prevent is reachable by simply not letting the hook run.

    Startup is the only place to repair it, and it is a sound place: ``ACTIVE`` is
    empty by construction here (fresh process, no session installed yet), so ANY
    meeting on disk claiming a non-terminal status is provably orphaned — there is
    no live session it could belong to. That inference is only valid before the
    first start, which is why this runs at boot rather than on a timer.

    Returns the ids it ended, for the log line.
    """
    ended: list[str] = []
    for summary in store.list_meetings(root):
        status = str(summary.get("status") or "")
        # `idle` is NOT orphaned: a meeting can sit initialized-but-never-started
        # across any number of restarts, and ending those would mark every meeting
        # the user ever opened as finished.
        if status not in (k.STATUS_ACTIVE, k.STATUS_PAUSED, k.STATUS_REVIEWING):
            continue
        meeting_id = str(summary.get("event_id") or "")
        if not meeting_id:
            continue
        try:
            if sess.end_meeting_meta(meeting_id, root) is not None:
                ended.append(meeting_id)
        except Exception:  # pragma: no cover — one bad meeting must not stop the rest
            logger.debug("meetings: could not reconcile %s", meeting_id, exc_info=True)
    return ended


async def _on_startup(app: web.Application) -> None:
    """Create the data subtree, load the dictionary, and reconcile orphans at boot.

    This is the Python home of what upstream shipped as a multi-line ``mkdir -p``
    shell blob prepended to a cron message. Both steps touch the filesystem, so
    they run on an executor rather than the loop, and neither may break gateway
    startup.

    The reconcile pass is the other half of ``_on_cleanup``: that hook keeps a
    graceful shutdown honest, and this one repairs the case where it never ran.
    """

    def _init() -> tuple[int, list[str]]:
        root = app.get("_meetings_data_root")
        store.ensure_data_dirs(root)
        terms = len(sess.reload_dictionary(root).terms)
        return terms, _reconcile_orphaned_meetings(root)

    try:
        count, ended = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), _init
        )
        logger.info("meetings: data dir ready, %d dictionary term(s) loaded", count)
        if ended:
            # Loud, not debug: this means a previous run died mid-meeting, and the
            # ids are what a user would need to find the notes that stop early.
            logger.warning(
                "meetings: ended %d meeting(s) orphaned by a previous run: %s",
                len(ended),
                ", ".join(ended),
            )
    except Exception:  # pragma: no cover — defensive
        logger.warning("meetings: data-dir init failed", exc_info=True)


async def _on_cleanup(app: web.Application) -> None:
    """Flush a live meeting's queued transcript, mark it ended, then drop it.

    The live session lives only in this process's memory, but the meeting's status
    lives on disk — so dropping one without the other leaves the two disagreeing.
    A gateway restart during an ACTIVE meeting persisted ``active`` while ``ACTIVE``
    came back empty, and that combination is worse than either half:

    * the dashboard reads ``active`` and shows Live, and its transcription binding
      keys off exactly that status, so the browser keeps recording;
    * every resulting dispatch answers 409 ``no_active_meeting``, so the speech is
      dropped rather than queued — the notes just stop, mid-meeting, with the UI
      still claiming to be listening;
    * ``handle_start_meeting`` cannot recover it either, because ``idle -> active``
      is the only transition out of a fresh meeting and this one is not idle.

    Marking it ended is what makes the state honest: the meeting shows Ended, and
    ``ended -> active`` is an allowed transition, so the Restart button is exactly
    the affordance the user needs. That path re-runs ``init_agents``, which is why
    a restarted meeting's agents get their instructions back (see
    ``handle_start_meeting``).

    Ordered flush-then-mark: the flush is what saves the queued transcript, and it
    must happen while the session still exists. Neither step may break shutdown.
    """
    try:
        # A restart is not a reason to lose what was said; the drain is bounded and
        # a failure inside it still tears the session down.
        previous = await ACTIVE.drain_and_clear()
        if previous is not None:
            root = app.get("_meetings_data_root")
            # Off the loop: this is a metadata read-modify-write, and it takes the
            # metadata transaction internally.
            await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), sess.end_meeting_meta, previous.meeting_id, root
            )
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
        f"{BASE}/calendar/credentials",
        route(calendar_routes.handle_get_calendar_credentials),
    )
    router.add_put(
        f"{BASE}/calendar/credentials",
        route(calendar_routes.handle_put_calendar_credentials),
    )
    router.add_post(
        f"{BASE}/calendar/credentials/forget",
        route(calendar_routes.handle_forget_calendar_credentials),
    )
    router.add_post(
        f"{BASE}/calendar/oauth/start", route(calendar_routes.handle_oauth_start)
    )
    # The provider redirects the user's BROWSER here, so it answers HTML rather
    # than JSON. The path is built from the same constant the start handler uses
    # for the redirect URI, so the two cannot drift.
    router.add_get(
        f"{BASE}{calendar_routes.OAUTH_CALLBACK_PATH}",
        route(calendar_routes.handle_oauth_callback),
    )
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

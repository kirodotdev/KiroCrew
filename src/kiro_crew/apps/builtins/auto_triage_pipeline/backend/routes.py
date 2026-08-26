"""HTTP routes for the Auto Triage Pipeline view.

Three GET routes, one per level of the object model, and nothing else. There is
no POST, PATCH or DELETE here and there is deliberately no write path at all:
the pipeline is executed by its own scheduled jobs, which own every piece of
state this app reads. Keeping the app strictly a window means it can never act
on the repository, and a bug in the view can never corrupt a running pipeline.

Registered at gateway startup like every other builtin backend, so each handler
re-checks enablement itself (deny-by-default): an opt-in app whose routes are
mounted unconditionally would otherwise stay callable after being disabled.
"""

from __future__ import annotations

import asyncio
import logging
import re
from functools import wraps
from typing import Any, Awaitable, Callable

from aiohttp import web

from kiro_crew.apps.manager import is_app_enabled

from . import pipeline_fold as fold

logger = logging.getLogger(__name__)

APP_NAME = "auto-triage-pipeline"

#: Route prefix. Mirrors the app's manifest name so the manifest's
#: ``permissions.api`` entries and the mounted paths cannot drift.
PREFIX = f"/api/apps/{APP_NAME}"

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def _require_enabled(handler: Handler) -> Handler:
    """Deny requests while the app is disabled.

    ``is_app_enabled`` reads installed.json synchronously, so it runs off the
    event loop.
    """

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.StreamResponse:
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            return web.json_response(
                {"error": f"{APP_NAME} is disabled", "code": "app_disabled"}, status=403
            )
        return await handler(request)

    return _wrapped


def _bad_request(message: str, code: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=400)


#: Owner and repository names GitHub actually permits: letters, digits, and the
#: three punctuation marks it allows inside a name. Deliberately an ALLOW-list.
#: A deny-list here already missed one character: rejecting "/", "\\" and a leading
#: "." still accepted `D:foo`, which on Windows is drive-RELATIVE and resolves
#: against that drive's current directory rather than under the cache root, so the
#: value escaped the tree it was supposed to name a folder in.
_REPO_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")


def _repo_params(request: web.Request) -> tuple[str, str] | web.Response:
    """Resolve owner/repo from the query string.

    Validated rather than trusted: both become path segments when an issue cache
    entry is read, so anything that is not simply a name is refused here instead of
    being sanitized deeper where the intent is less obvious.
    """
    owner = (request.query.get("owner") or "").strip()
    repo = (request.query.get("repo") or "").strip()
    if not owner or not repo:
        return _bad_request("owner and repo are required", "repo_required")
    for value in (owner, repo):
        if not _REPO_NAME_RE.match(value):
            return _bad_request("owner or repo is not a valid name", "repo_invalid")
    return owner, repo


def _int_query(request: web.Request, name: str, default: int) -> int:
    raw = (request.query.get(name) or "").strip()
    if not raw or not raw.isdecimal() or len(raw) > 9:
        return default
    return int(raw)


async def _handle_overview(request: web.Request) -> web.StreamResponse:
    """GET {PREFIX}/overview — L0: the pipeline and its per-step throughput."""
    hours = _int_query(request, "hours", fold.DEFAULT_RECENT_HOURS)
    try:
        result = await asyncio.to_thread(fold.fold_pipeline, recent_hours=hours)
    except fold.FoldError as exc:
        # The message is authored by the fold layer and names no absolute path.
        return web.json_response({"error": str(exc), "code": "unreadable"}, status=503)
    except OSError:
        logger.warning("pipeline overview failed to read a data source", exc_info=True)
        return web.json_response(
            {"error": "a pipeline data source could not be read", "code": "unreadable"},
            status=503,
        )
    return web.json_response(result.to_dict())


async def _handle_step(request: web.Request) -> web.StreamResponse:
    """GET {PREFIX}/step?step=&owner=&repo= — L1: the items inside one step.

    ``owner``/``repo`` are used ONLY to locate the local issue cache that supplies
    titles, labels and assignees. They do NOT filter the item list: the pipeline's
    event trail carries no repository dimension at all, because these scheduled
    jobs run against one repository by construction. So passing a different
    owner/repo returns the SAME items with less enrichment, never a different
    pipeline -- which is also why the L0 overview takes no repo parameter. Anyone
    reading this as a repo filter would draw exactly the wrong conclusion from a
    two-repository install.
    """
    resolved = _repo_params(request)
    if isinstance(resolved, web.Response):
        return resolved
    owner, repo = resolved
    step = (request.query.get("step") or "").strip()
    if not step:
        return _bad_request("step is required", "step_required")
    limit = _int_query(request, "limit", fold.MAX_ROWS)
    try:
        rows = await asyncio.to_thread(
            fold.list_step_items, step, owner=owner, repo=repo, limit=limit
        )
    except fold.FoldError as exc:
        return web.json_response({"error": str(exc), "code": "bad_step"}, status=400)
    except OSError:
        logger.warning("pipeline step listing failed to read a data source", exc_info=True)
        return web.json_response(
            {"error": "a pipeline data source could not be read", "code": "unreadable"},
            status=503,
        )
    return web.json_response(
        {"step": step, "count": len(rows), "items": [r.to_dict() for r in rows]}
    )


async def _handle_item_sessions(request: web.Request) -> web.StreamResponse:
    """GET {PREFIX}/item/sessions?number= — L2: the sessions that worked an item.

    Spend is summed across the item's current slot AND every retired slot in the
    queue entry's ``previous_slots``. That is not a refinement: on the real trail
    one retried item reads 187 credits from its current slot alone against 4059
    across all three, so reporting only the live session would understate the
    expensive items by an order of magnitude.
    """
    raw = (request.query.get("number") or "").strip()
    if not raw or not raw.isdecimal() or len(raw) > 9:
        return _bad_request("a numeric item number is required", "number_required")
    try:
        rows = await asyncio.to_thread(fold.list_item_sessions, int(raw))
    except fold.FoldError as exc:
        return web.json_response({"error": str(exc), "code": "bad_item"}, status=400)
    except OSError:
        logger.warning("pipeline session listing failed to read a data source", exc_info=True)
        return web.json_response(
            {"error": "a pipeline data source could not be read", "code": "unreadable"},
            status=503,
        )
    payload: dict[str, Any] = {
        "number": int(raw),
        "count": len(rows),
        "sessions": [r.to_dict() for r in rows],
        # Which numeric columns actually carry data, so the table can omit the
        # ones that are structurally zero rather than printing a row of zeros
        # next to a real credit total.
        "populatedColumns": fold.populated_columns(rows),
    }
    return web.json_response(payload)


def register_routes(app: web.Application) -> None:
    """Mount the three read routes. No write route exists by design."""
    app.router.add_get(f"{PREFIX}/overview", _require_enabled(_handle_overview))
    app.router.add_get(f"{PREFIX}/step", _require_enabled(_handle_step))
    app.router.add_get(f"{PREFIX}/item/sessions", _require_enabled(_handle_item_sessions))

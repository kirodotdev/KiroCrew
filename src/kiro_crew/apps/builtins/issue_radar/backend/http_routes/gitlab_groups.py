"""GitLab groups: a group path the owner follows, and its review requests.

A group is stored as ``{host, path}`` only. Its review requests come from ONE
paginated ``groups/:id/merge_requests`` search with ``include_subgroups=true``
and the user's own GitLab username as the reviewer, so every project in the group
tree -- including projects created after the group was followed -- is covered
without a per-project list. Every provider call is a read through the user's
existing ``glab`` session; nothing is written to GitLab and no credential is
stored.

The read route serves only a followed group (the same gate the per-repo routes
apply to connected repos); following and unfollowing are owner only, like
connecting and disconnecting a repo. Every error answer carries a ``code`` the UI
turns into a sentence that says what to do.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial

from aiohttp import web

from kiro_crew.apps.builtins.issue_radar.backend import gitlab_client, store

from ..errors import RepoUrlError

logger = logging.getLogger("kirocrew.app.issue-radar")


def _group_error(message: str, code: str, status: int) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


def _provider_error_response(exc: Exception) -> web.Response:
    """Map a ``glab`` failure on a group read to a status the UI can show per group."""
    from .. import routes  # circular import: backend.routes imports this module

    if isinstance(exc, routes.GhSetupError):
        return _group_error(str(exc), "setup_required", 502)
    if isinstance(exc, gitlab_client.ProviderNotFoundError):
        return _group_error(str(exc), "group_not_found", 404)
    if isinstance(exc, routes.GhPermissionError):
        return _group_error(
            "this GitLab group is not visible to your glab session", "group_forbidden", 403
        )
    logger.warning("issue-radar gitlab group provider error: %s", exc)
    return _group_error("upstream provider error", "provider_error", 502)


def _query_group(request: web.Request) -> tuple[str, str] | web.Response:
    """Read and validate ``?host=&path=``; a malformed path is a 400."""
    host = (request.query.get("host") or "").strip()
    path = (request.query.get("path") or "").strip()
    if not host or not path:
        return _group_error("missing ?host= and ?path=", "missing_group", 400)
    try:
        gitlab_client.validate_group_path(path)
    except RepoUrlError as exc:
        return _group_error(str(exc), "invalid_group", 400)
    return host, path


async def _handle_gitlab_groups(request: web.Request) -> web.Response:
    """GET /gitlab-groups — the followed GitLab groups. Local read, no provider call."""
    del request
    groups = await asyncio.to_thread(store.list_gitlab_groups)
    return web.json_response({"groups": groups})


async def _handle_add_gitlab_group(request: web.Request) -> web.Response:
    """POST /gitlab-groups ``{"group": <link or path>}`` — follow a group.

    A bare path is a gitlab.com group; a pasted link names its own instance. The
    group must exist and be readable by the user's ``glab`` session: a malformed
    path is a 400 and an unknown one a 404, and neither is stored.
    """
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    # Following a group lets the read route search it with the OWNER's glab session.
    owner_denied = await require_owner_dashboard_request(request, "issue_radar.gitlab_group_add")
    if owner_denied is not None:
        return owner_denied

    try:
        body = await request.json()
    except Exception:
        return _group_error("request body must be JSON", "invalid_json", 400)
    if not isinstance(body, dict):
        return _group_error("request body must be a JSON object", "invalid_json", 400)
    value = body.get("group")
    if not isinstance(value, str) or not value.strip():
        return _group_error("missing 'group'", "missing_group", 400)

    try:
        # Off-loop: reading the dashboard.gitlab_hosts allowlist loads config.
        allowed = await asyncio.to_thread(gitlab_client.allowed_hosts)
        host, path = gitlab_client.parse_gitlab_group(value, allowed_hosts=allowed)
    except RepoUrlError as exc:
        return _group_error(str(exc), "invalid_group", 400)

    from .. import routes  # circular import: backend.routes imports this module

    try:
        await asyncio.to_thread(partial(gitlab_client.verify_group_access, path, host=host))
    except routes.GhCliError as exc:
        return _provider_error_response(exc)

    await asyncio.to_thread(store.add_gitlab_group, host, path)
    return web.json_response({"host": host, "path": path})


async def _handle_remove_gitlab_group(request: web.Request) -> web.Response:
    """DELETE /gitlab-groups?host=&path= — unfollow a group. Local only."""
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    owner_denied = await require_owner_dashboard_request(request, "issue_radar.gitlab_group_remove")
    if owner_denied is not None:
        return owner_denied

    parsed = _query_group(request)
    if isinstance(parsed, web.Response):
        return parsed
    host, path = parsed
    removed = await asyncio.to_thread(store.remove_gitlab_group, host, path)
    if not removed:
        return _group_error(f"{path} is not a followed group", "group_not_added", 404)
    return web.json_response({"host": host, "path": path})


async def _handle_gitlab_group_reviews(request: web.Request) -> web.Response:
    """GET /gitlab-groups/review-requests?host=&path= — open merge requests across
    the group and its subgroups where the ``glab`` user is a requested reviewer.

    Live call, capped like /pulls/search: ``truncated`` says the newest
    ``limit`` rows are shown and more exist.
    """
    from .. import routes  # circular import: backend.routes imports this module

    parsed = _query_group(request)
    if isinstance(parsed, web.Response):
        return parsed
    host, path = parsed
    if not await asyncio.to_thread(store.is_gitlab_group_added, host, path):
        return _group_error(f"{path} is not a followed group", "group_not_added", 404)

    try:
        login = await asyncio.to_thread(partial(gitlab_client.get_current_login, host=host))
    except routes.GhCliError as exc:
        return _provider_error_response(exc)
    if not login:
        return _group_error("could not read your GitLab username from glab", "no_login", 502)

    search_max = gitlab_client.PR_SEARCH_MAX
    try:
        pulls = await asyncio.to_thread(
            partial(gitlab_client.search_group_reviews, path, host=host),
            reviewer=login,
            limit=search_max + 1,
        )
    except routes.PrSearchError as exc:
        return _group_error(str(exc), "invalid_search", 400)
    except routes.GhCliError as exc:
        return _provider_error_response(exc)

    return web.json_response(
        {
            "host": host,
            "path": path,
            "login": login,
            "pulls": pulls[:search_max],
            "truncated": len(pulls) > search_max,
            "limit": search_max,
        }
    )

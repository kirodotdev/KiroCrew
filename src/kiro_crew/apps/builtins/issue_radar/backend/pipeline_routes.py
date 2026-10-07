"""HTTP routes for Issue Radar's pipeline dashboard.

Three GET routes, one per level of the object model, and nothing else. There is
no POST, PATCH or DELETE here and there is deliberately no write path at all:
the pipeline is executed by its own scheduled jobs, which own every piece of
state this dashboard reads. Keeping it strictly a window means it can never act
on the repository, and a bug in the view can never corrupt a running pipeline.

Mounted by Issue Radar's own ``register_routes``, and gated on Issue Radar's
enablement rather than its own: the pipeline is one of Issue Radar's dashboards,
not a separately installable app, so there is no second thing for a user to
enable and no state in which the dashboard exists while its host does not.
"""

from __future__ import annotations

import asyncio
import logging
import re
from functools import wraps
from typing import Any, Awaitable, Callable

from aiohttp import web

from kiro_crew.apps.manager import is_app_enabled

from . import gitlab_transport
from . import pipeline_fold as fold
from . import provider, store
from .errors import ProviderCliError

logger = logging.getLogger(__name__)

#: Derived from Issue Radar's own app name rather than spelled again. The clients
#: for these routes are forward-tolerant by design, so a drifted literal would
#: not surface as an error -- it would render as a pipeline that has never run.
APP_NAME = store.APP_NAME

#: Route prefix, nested under the host app so the ownership is legible from the
#: URL alone.
PREFIX = f"/api/apps/{APP_NAME}/pipeline"

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

#: One GitLab path segment, as the GitLab client itself admits it when a project is
#: connected (``gitlab_transport.SEGMENT_RE``: the same character class, up to
#: ``GITLAB_MAX_SEGMENT_CHARS``), minus the two names that are not names at all: ``.``
#: and ``..`` would walk the issue-cache tree. Built FROM the transport's constants
#: rather than restated so a connected project can never be refused here: a segment
#: starting with ``_`` or ``.``, or longer than a GitHub name, is legal on GitLab.
_GITLAB_SEGMENT = (
    r"(?!\.\.?(?:/|\Z))[A-Za-z0-9._-]{1,%d}" % gitlab_transport.GITLAB_MAX_SEGMENT_CHARS
)

#: A GitLab owner is a group path: segments joined by ``/`` (GitLab's namespace
#: separator, a URL path, not a filesystem one), at most ``MAX_NAMESPACE_SEGMENTS``
#: deep. One regex rather than a split-and-check so every segment is judged by the
#: same rule and an empty segment (``a//b``, a leading or trailing ``/``) cannot slip by.
_GITLAB_OWNER_RE = re.compile(
    r"\A%s(?:/%s){0,%d}\Z"
    % (_GITLAB_SEGMENT, _GITLAB_SEGMENT, gitlab_transport.MAX_NAMESPACE_SEGMENTS - 1)
)
_GITLAB_REPO_RE = re.compile(r"\A%s\Z" % _GITLAB_SEGMENT)


def _unsupported_forge() -> web.Response:
    return _bad_request(
        "the triage pipeline is not available for this repository's forge",
        "repo_provider_unsupported",
    )


def _forge_params(request: web.Request) -> fold.Forge | web.Response:
    """Resolve which forge the request names, or refuse one the pipeline has no data for.

    The pipeline's files are split BY FORGE (see ``pipeline_fold.Forge``), so a
    repository is only ever read from its own forge's audit log, queue shards and
    issue cache. That split is what makes it safe to serve more than public GitHub:
    the previous rule refused every other forge because the data was keyed on
    ``owner/repo`` alone, and a same-slug GitLab project would have been shown the
    GitHub repository's events, titles and credit costs under its own heading.

    Public GitHub (no provider, or ``github`` with no host or a GitHub host) and GitLab
    with an explicit host are served. Anything else -- Azure, GitHub Enterprise, a
    GitLab request with no host -- is refused with ``repo_provider_unsupported``, which
    the client renders as a stable "unsupported forge" state and stops polling on.

    This reads only which forge is named. Whether the host is one ``glab`` may talk to
    is decided in :func:`_reject_unservable` by the GitLab client's own host rule, off
    the event loop (it reads config) and before any of the forge's files are opened.
    Authorization is unchanged and still comes from the connected-repo gate, which
    matches provider + host + owner + repo.

    Absent ``provider`` means public GitHub, matching every other Issue Radar caller,
    so an existing request that names no provider is unaffected.
    """
    provider_name = (request.query.get("provider") or "").strip().lower()
    host = (request.query.get("host") or "").strip().lower()
    unsupported = _unsupported_forge()
    if provider_name in ("", "github"):
        # A host is only ever set for a self-hosted forge; public GitHub carries none.
        # Without this, provider=github&host=ghe.internal would pass as public GitHub.
        # The set of hosts that ARE public GitHub comes from the module that already
        # owns that fact for this app, so there is one list rather than a second one.
        if host and host not in provider.GITHUB_URL_HOSTS:
            return unsupported
        return fold.GITHUB
    if provider_name != "gitlab":
        return unsupported
    host = provider.normalize_host(host, provider_name)
    if not host:
        return unsupported
    return fold.Forge(provider=provider_name, host=host)


def _repo_params(request: web.Request) -> tuple[str, str, fold.Forge] | web.Response:
    """Resolve the forge and owner/repo from the query string.

    Validated rather than trusted: both become path segments when an issue cache
    entry is read, so anything that is not simply a name is refused here instead of
    being sanitized deeper where the intent is less obvious.
    """
    owner = (request.query.get("owner") or "").strip()
    repo = (request.query.get("repo") or "").strip()
    if not owner or not repo:
        return _bad_request("owner and repo are required", "repo_required")
    # The forge check comes BEFORE the name rule, because the name rule is per-forge.
    # GitLab nests a group path, so its `owner` legitimately holds slashes, while a
    # GitHub owner never does. Validating names first would answer `repo_invalid` (a
    # generic, retryable error the client offers a Retry for) for a forge the client
    # should instead be told is unsupported. Safe to reorder: this guard reads only
    # `provider` and `host`, never the names below it.
    forge = _forge_params(request)
    if isinstance(forge, web.Response):
        return forge
    owner_re, repo_re = (
        (_REPO_NAME_RE, _REPO_NAME_RE) if forge.is_github else (_GITLAB_OWNER_RE, _GITLAB_REPO_RE)
    )
    if not owner_re.match(owner) or not repo_re.match(repo):
        return _bad_request("owner or repo is not a valid name", "repo_invalid")
    return owner, repo, forge


def _servable_host(forge: fold.Forge) -> bool:
    """May the pipeline read this forge's files? GitHub always; GitLab exactly when
    ``glab`` would be allowed to call the host.

    The rule is the GitLab client's, not a copy: ``gitlab_transport.resolve_host``
    over ``gitlab_client.allowed_hosts()`` is the same check every ``glab`` call makes,
    so the Pipeline tab and the GitLab calls cannot disagree about a host. gitlab.com
    is always accepted there and never appears in ``dashboard.gitlab_hosts`` (the
    coercer drops it), which a membership test on the allowlist would get wrong. A
    host that passes has either that fixed spelling or one the config coercer already
    held to its label-and-port grammar, so it is safe as a filename segment and an
    issue-cache directory without a second grammar here.

    ``gitlab_transport`` is a module-level import because ``_GITLAB_SEGMENT`` reads
    its segment grammar at load time. Only ``gitlab_client`` is deferred: it carries
    the ``glab`` executable lookup and subprocess spawn, the config loader and the SEL
    audit sink, none of which this read-only view should load at import time.
    """
    if forge.is_github:
        return True
    from . import gitlab_client

    try:
        gitlab_transport.resolve_host(forge.host, allowed_hosts=gitlab_client.allowed_hosts())
    except ProviderCliError:
        return False
    return True


async def _reject_unservable(
    owner: str, repo: str, forge: fold.Forge = fold.GITHUB
) -> web.Response | None:
    """Refuse a forge whose host the operator does not list, then a repository Issue
    Radar is not connected to. None means the request may be served.

    The host check comes first so removing a host from ``dashboard.gitlab_hosts``
    takes effect on the Pipeline tab at once, as it does on every ``glab`` call.
    Without it, an already-connected project on a de-listed host would keep reading
    its audit log, queue shard and issue cache from disk. It answers
    ``repo_provider_unsupported`` so the client lands on the same stable "unsupported
    forge" state it shows for a host that never qualified.

    The connected-repo gate is the HOST APP's authorization gate, not a courtesy: every
    repo-scoped read in ``http_routes`` is gated on ``store.is_repo_connected`` through
    ``routes._connected``, and ``routes.py``'s own note names that call as "the gate
    that actually decides whether this request may touch a repo". These three
    handlers read the SAME per-repository data -- the issue cache under the repo's
    own directory, and its queue shard -- so skipping the gate would make them the
    one door in the app that opens on a repository the operator has not connected.

    Disconnecting does not erase what is on disk. The trail keeps its events, the
    issue cache keeps its titles, labels and assignees, and the queue shard keeps
    its slots and credit costs, so a name-only check would still serve all of it
    for a repository that has been removed. 404 rather than 403 because a
    connected/not-connected distinction is itself worth not confirming, and it is
    the status and code the sibling handlers already return.

    Off the event loop: both checks read JSON from disk (the config, then the store),
    exactly as ``http_routes`` does at each of its ``routes._connected`` call sites.
    """
    if not await asyncio.to_thread(_servable_host, forge):
        return _unsupported_forge()
    if await asyncio.to_thread(
        store.is_repo_connected, owner, repo, provider=forge.provider, host=forge.host
    ):
        return None
    return web.json_response(
        {
            "error": f"{owner}/{repo} is not connected -- call /connect first",
            "code": "repo_not_connected",
        },
        status=404,
    )


def _repo_key(owner: str, repo: str) -> str:
    """Join a VALIDATED pair into the ``"owner/repo"`` key the fold is scoped by.

    Only ever called with what ``_repo_params`` returned, which is why it can be a
    plain join: every segment has matched its forge's name rule. Dropping provider/host
    here is sound because the FORGE selects the files the key is looked up in (see
    ``pipeline_fold.Forge``), so within one forge ``owner/repo`` is the whole identity.
    """
    # A repository KEY handed to the fold, not a response body: this is not a route
    # (the _handle_* functions are), the server is aiohttp rather than Flask, and
    # both halves are already matched against their forge's name rule, whose
    # character class admits nothing that could reach markup.
    # nosemgrep: python.flask.security.audit.directly-returned-format-string.directly-returned-format-string
    return f"{owner}/{repo}"


def _int_query(request: web.Request, name: str, default: int) -> int:
    raw = (request.query.get(name) or "").strip()
    if not raw or not raw.isdecimal() or len(raw) > 9:
        return default
    return int(raw)


async def _handle_overview(request: web.Request) -> web.StreamResponse:
    """GET {PREFIX}/overview?owner=&repo=&hours= — L0: per-step throughput.

    ``owner``/``repo`` are REQUIRED and narrow the step counters to that one
    repository. A bare request is refused rather than widened: there is no
    repository whose numbers are the right ones to return by default, and the
    caller that would have relied on such a default does not exist -- this route
    is new.

    What never narrows is the disclosure: ``repos`` and ``unattributedEvents``
    stay whole-trail even for a scoped request, because a scoped board still has
    to be able to say which repositories the trail contains and how much of the
    history predates attribution.
    """
    hours = _int_query(request, "hours", fold.DEFAULT_RECENT_HOURS)
    resolved = _repo_params(request)
    if isinstance(resolved, web.Response):
        return resolved
    owner, repo, forge = resolved
    unconnected = await _reject_unservable(owner, repo, forge)
    if unconnected is not None:
        return unconnected
    scope = _repo_key(owner, repo)
    try:
        result = await asyncio.to_thread(
            fold.fold_pipeline, recent_hours=hours, repo=scope, forge=forge
        )
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

    ``owner``/``repo`` are REQUIRED here and do two jobs: they locate the local
    issue cache that supplies titles, labels and assignees, and they scope the
    list to that repository.

    Both halves narrowing together is what makes the heading truthful. While the
    dispatch queue was a single file keyed on the issue number alone, filtering
    here would have narrowed the rows without narrowing the joins, so a
    two-repository install would have shown one repository's sessions and costs
    under the other's filtered heading. The queue is now one file per repository,
    so the join is as narrow as the filter.

    An item whose events carry no repository appears on NO scoped list. It cannot be
    placed, and this listing enriches every row by its issue number from the selected
    repository's cache and shard -- so including one would render it wearing another
    repository's identity. The count of what was left out stays on the fold's census.
    """
    resolved = _repo_params(request)
    if isinstance(resolved, web.Response):
        return resolved
    owner, repo, forge = resolved
    unconnected = await _reject_unservable(owner, repo, forge)
    if unconnected is not None:
        return unconnected
    step = (request.query.get("step") or "").strip()
    if not step:
        return _bad_request("step is required", "step_required")
    limit = _int_query(request, "limit", fold.MAX_ROWS)
    try:
        rows = await asyncio.to_thread(
            fold.list_step_items, step, owner=owner, repo=repo, limit=limit, forge=forge
        )
    except fold.QueueMigrationPending as exc:
        # Not a bad request: the operator has to run the migration. 503 so the
        # client retries rather than treating it as a permanently invalid step.
        return web.json_response({"error": str(exc), "code": "queue_migration_pending"}, status=503)
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
    """GET {PREFIX}/item/sessions?number=&owner=&repo= — L2: an item's sessions.

    Spend is summed across the item's current slot AND every retired slot in the
    queue entry's ``previous_slots``. That is not a refinement: on the real trail
    one retried item reads 187 credits from its current slot alone against 4059
    across all three, so reporting only the live session would understate the
    expensive items by an order of magnitude.

    ``owner``/``repo`` are REQUIRED and select which repository's queue shard the
    slots come from. They matter most here of anywhere: issue numbers are
    per-repository, so before the queue was sharded this lookup could return
    another repository's slots -- and so another repository's sessions and credit
    costs -- for an item that merely shared a number. Reading some default shard
    instead of refusing would recreate exactly that, which is why there is no
    default to read.
    """
    raw = (request.query.get("number") or "").strip()
    if not raw or not raw.isdecimal() or len(raw) > 9:
        return _bad_request("a numeric item number is required", "number_required")
    resolved = _repo_params(request)
    if isinstance(resolved, web.Response):
        return resolved
    owner, repo, forge = resolved
    unconnected = await _reject_unservable(owner, repo, forge)
    if unconnected is not None:
        return unconnected
    try:
        rows = await asyncio.to_thread(
            fold.list_item_sessions, int(raw), repo=_repo_key(owner, repo), forge=forge
        )
    except fold.QueueMigrationPending as exc:
        return web.json_response({"error": str(exc), "code": "queue_migration_pending"}, status=503)
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

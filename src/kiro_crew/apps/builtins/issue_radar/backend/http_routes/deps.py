"""``/deps``: the repo's dependency graph, served stale while it revalidates behind.

Owns the two per-app registries: ``_DEPS_REFRESH_TASKS_APP_KEY`` holds at most one
in-flight background rebuild per repo, and ``_DEPS_REBUILD_LOCKS_APP_KEY`` the
per-repo rebuild mutex. ``_stop_deps_refreshes`` is the ``on_cleanup`` hook that
cancels whatever is still running, so no refresh outlives its aiohttp app.
"""

from __future__ import annotations

import asyncio
import logging
import time
from functools import partial

from aiohttp import web

from kiro_crew.apps.builtins.issue_radar.backend import github_client, provider, store

logger = logging.getLogger("kirocrew.app.issue-radar")


def _deps_node_hints(
    key: provider.RepoKey, issues: list[dict], pulls: list[dict]
) -> dict[int, dict]:
    """Seed ``number -> {kind, state, title}`` from the issues/pulls list caches.

    Every open item already in a cache costs NO extra API call when building the
    graph — only a number that appears in an edge yet is in neither cache falls
    back to a per-node ``get_ref_summary`` inside the fetcher. A PR carrying a
    ``merged_at`` is recorded as ``merged`` so the graph and the unlock semantics
    agree on merged-vs-closed.
    """
    hints: dict[int, dict] = {}
    for row in issues or []:
        num = row.get("number") if isinstance(row, dict) else None
        if isinstance(num, int) and num > 0:
            hints[num] = {
                "kind": "issue",
                "state": github_client._dep_node_state(row.get("state")),
                "title": str(row.get("title") or ""),
            }
    for row in pulls or []:
        num = row.get("number") if isinstance(row, dict) else None
        if isinstance(num, int) and num > 0:
            hints[num] = {
                "kind": "pull",
                "state": github_client._dep_node_state(row.get("state"), row.get("merged_at")),
                "title": str(row.get("title") or ""),
            }
    return hints


# ── /deps serve-stale-revalidate-behind ─────────────────────────────────────
#
# App-state key under which the in-flight background deps-refresh tasks live, one
# per repo. A typed ``web.AppKey`` (same pattern as spec_builder) so the registry
# shares the app's lifetime and shutdown can cancel every outstanding task — see
# ``_stop_deps_refreshes`` (registered as an ``on_cleanup`` hook).
_DepsRefreshTasks = dict[str, "asyncio.Task"]
_DEPS_REFRESH_TASKS_APP_KEY: web.AppKey[_DepsRefreshTasks] = web.AppKey(
    "issue_radar_deps_refresh_tasks", dict
)

# Per-repo rebuild mutex. Coalescing (above) only stops a SECOND BACKGROUND
# refresh; this lock keeps a background rebuild and a synchronous ``refresh=1``
# rebuild (or two concurrent ``refresh=1`` calls) from fetching the same graph
# concurrently. Ordering their WRITES is not its job: the store's compare-and-set on
# ``fetched_at`` decides which graph is newer, and it also covers the sweep's
# thread-side write, which never takes this mutex.
_DepsRebuildLocks = dict[str, "asyncio.Lock"]
_DEPS_REBUILD_LOCKS_APP_KEY: web.AppKey[_DepsRebuildLocks] = web.AppKey(
    "issue_radar_deps_rebuild_locks", dict
)


def _deps_reg_key(key: provider.RepoKey) -> str:
    """The per-repo registry key shared by the refresh-task and lock registries."""
    return f"{key.provider}:{key.host}:{key.owner}/{key.repo}"


def _deps_refresh_registry(app: web.Application) -> _DepsRefreshTasks:
    """The per-app ``repo-key -> in-flight refresh Task`` map, created on first use."""
    reg = app.get(_DEPS_REFRESH_TASKS_APP_KEY)
    if reg is None:
        reg = {}
        app[_DEPS_REFRESH_TASKS_APP_KEY] = reg
    return reg


def _deps_rebuild_lock(app: web.Application, key: provider.RepoKey) -> "asyncio.Lock":
    """The per-repo rebuild mutex, created on first use.

    Bound to the app (hence to one event loop), so this is a plain
    :class:`asyncio.Lock` rather than a loop-bound one: it is never a module
    global shared across loops.
    """
    locks = app.get(_DEPS_REBUILD_LOCKS_APP_KEY)
    if locks is None:
        locks = {}
        app[_DEPS_REBUILD_LOCKS_APP_KEY] = locks
    reg_key = _deps_reg_key(key)
    lock = locks.get(reg_key)
    if lock is None:
        lock = asyncio.Lock()
        locks[reg_key] = lock
    return lock


class _DepsScopeUnavailable(github_client.GhCliError):
    """The open-issue scope a deps rebuild needs could not be read.

    A dedicated type rather than a message pattern: the route maps this to the
    ``deps_issue_scope_unavailable`` code and a plain :class:`GhCliError` to
    ``deps_fetch_failed``. One helper owns the whole build, so the distinction
    has to travel with the exception rather than with which ``try`` block caught
    it, and sniffing the message text would silently reclassify every scope
    failure whose wording does not happen to mention issues
    (``gh api ... failed`` mentions neither).
    """


async def _rebuild_deps(app: web.Application, key: provider.RepoKey) -> dict:
    """Rebuild the dependency graph for ``key`` and persist it, returning the
    normalized stored shape ``{"edges", "nodes", ...}``.

    The single build path shared by the synchronous route (cold cache /
    ``refresh=1``) and the background revalidation. Reads the open issues (the
    graph's scope) plus the open pulls (node hints) from the caches the app
    already keeps, syncs the native + inferred edges via
    ``github_client.fetch_dependency_edges``, writes the deps cache, and re-reads
    it so the caller gets the normalized/deduped shape a later cache hit would.

    Holds the repo's rebuild mutex across fetch AND write, so a slow rebuild can
    never land on top of a newer one and re-stamp older edges as fresh.

    Raises :class:`_DepsScopeUnavailable` when the issue scope cannot be read and
    a plain ``GhCliError`` when the edge fetch fails, so the synchronous caller
    can keep the two 502 codes callers already see; the background caller catches
    every exception instead (a background failure must leave the previous good
    cache intact).
    """
    from .. import routes  # circular import: backend.routes imports this module

    owner, repo = key.owner, key.repo
    async with _deps_rebuild_lock(app, key):
        # Captured BEFORE the issue snapshot is loaded — the snapshot is the
        # graph's SCOPE, so the rebuilt graph is as old as its oldest input,
        # not as old as the edge fetch alone. A later stamp would let a rebuild
        # scoped by a stale snapshot outrank a concurrent producer (the sweep's
        # thread-side write, which never takes the mutex above) that used a
        # fresher one, and suppress its scope changes for the TTL.
        # Under-claiming age is the safe direction: this rebuild can only lose
        # a CAS race it might have won, never persist stale data as fresh.
        fetch_started = time.time()
        try:
            issues = await routes._load_open_issues_for_reco(key)
        except routes.GhCliError as exc:
            raise _DepsScopeUnavailable(str(exc)) from exc
        pulls = await routes._st(key, store.read_pulls_cache, owner, repo, state="open") or []
        hints = _deps_node_hints(key, issues, pulls)
        edges, nodes = await asyncio.to_thread(
            partial(github_client.fetch_dependency_edges, owner, repo, issues, hints)
        )
        await routes._st(
            key,
            store.write_deps_cache,
            owner,
            repo,
            edges,
            nodes,
            fetched_at=fetch_started,
        )
        stored = await routes._st(key, store.read_deps_cache, owner, repo)
    if stored is not None:
        return stored
    return {"edges": edges, "nodes": nodes}


def _schedule_deps_refresh(app: web.Application, key: provider.RepoKey) -> None:
    """Kick a background deps rebuild for ``key``, at most one in flight per repo.

    Coalesces concurrent stale callers: while a refresh is running, later stale
    requests see the live task in the registry and do NOT spawn a second — they
    just serve their own stale copy. The task removes itself from the registry
    when it finishes (in a ``finally`` so a crash cannot wedge the slot), and a
    failure is logged and swallowed so the previous good cache stays intact. The
    task is registered on ``app`` so shutdown can cancel it (no leaked task).
    """
    from .. import routes  # circular import: backend.routes imports this module

    registry = _deps_refresh_registry(app)
    reg_key = _deps_reg_key(key)
    existing = registry.get(reg_key)
    if existing is not None and not existing.done():
        return  # a refresh for this repo is already in flight — coalesce

    async def _run() -> None:
        try:
            await routes._rebuild_deps(app, key)
        except asyncio.CancelledError:
            raise
        except Exception:  # never crash the loop; keep the prior good cache
            logger.debug(
                "issue-radar: background deps refresh failed for %s/%s; keeping cached graph",
                key.owner,
                key.repo,
                exc_info=True,
            )
        finally:
            # Drop ourselves only if we are still the registered task (a cancel
            # during shutdown may have already cleared the slot).
            if registry.get(reg_key) is task:
                registry.pop(reg_key, None)

    task = asyncio.create_task(_run(), name=f"issue-radar-deps-refresh:{reg_key}")
    registry[reg_key] = task


async def _stop_deps_refreshes(app: web.Application) -> None:
    """``app.on_cleanup`` hook — cancel any outstanding background deps refreshes
    on gateway shutdown so no unawaited task outlives the app."""
    registry = app.get(_DEPS_REFRESH_TASKS_APP_KEY)
    if not registry:
        return
    tasks = [t for t in registry.values() if not t.done()]
    registry.clear()
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover - defensive
            logger.debug("issue-radar deps-refresh shutdown raised", exc_info=True)


async def _handle_deps(request: web.Request) -> web.Response:
    """GET /deps?owner=<o>&repo=<r>[&refresh=1] — the repo's dependency graph.

    Returns ``{owner, repo, provider, host, edges, nodes, from_cache}`` where an
    edge is ``{blocked, blocker, source: "native"|"inferred"}`` and ``nodes`` maps
    every number appearing in an edge to ``{kind, state, title}``.

    Cache-first, serve-stale-revalidate-behind: a FRESH cache (younger than
    ``store.DEPS_CACHE_TTL_SEC``) is served as-is; a STALE cache is served
    IMMEDIATELY while a single coalesced background refresh rebuilds it off the
    request path — the ~11s rebuild never blocks a user. Only a genuinely
    never-synced repo (no cache at all) still blocks on a build. ``refresh=1``
    forces a SYNCHRONOUS rebuild so a user-initiated refresh returns fresh data.
    A rebuild reads the open issues (the graph's scope) plus the pulls cache
    (node hints) and syncs the native + inferred edges via
    ``github_client.fetch_dependency_edges``.

    The response shape is deliberately UNCHANGED by serve-stale. Whether the
    served graph was fresh or aged is not reported, because no client reads such
    a signal today; staleness stays an internal scheduling decision rather than
    part of the contract.

    Dependency edges are a GitHub-native feature (the ``dependencies`` API);
    non-GitHub providers answer an empty graph rather than an error, so the M1
    frontend can call ``/deps`` uniformly and simply render nothing for a GitLab
    project (cross-provider parity is out of scope for M1).
    """
    from .. import routes  # circular import: backend.routes imports this module

    key = routes._key_from_request(request)
    owner, repo = key.owner, key.repo
    if not owner or not repo:
        return web.json_response(
            {"error": "missing ?owner= and ?repo=", "code": "missing_repo_params"},
            status=400,
        )

    if not await asyncio.to_thread(routes._connected, key):
        return web.json_response(
            {
                "error": f"{owner}/{repo} is not connected — call /connect first",
                "code": "repo_not_connected",
            },
            status=404,
        )

    # GitHub-native only in M1. A non-GitHub key returns an empty graph so the
    # client renders an empty dependency surface instead of an error.
    if key.provider != provider.GITHUB:
        return web.json_response(
            {
                **routes._identity(key),
                "edges": [],
                "nodes": {},
                "from_cache": False,
            }
        )

    force_refresh = request.query.get("refresh") == "1"
    if not force_refresh:
        cached = await routes._st(key, store.read_deps_cache, owner, repo)
        if cached is not None:
            fresh = (time.time() - cached["fetched_at"]) < store.DEPS_CACHE_TTL_SEC
            if not fresh:
                # Serve stale, revalidate behind: hand back the aged graph now and
                # kick a single coalesced background rebuild. The ~11s fetch never
                # blocks this request; a subsequent visit gets the refreshed graph.
                routes._schedule_deps_refresh(request.app, key)
            return web.json_response(
                {
                    **routes._identity(key),
                    "edges": cached["edges"],
                    "nodes": cached["nodes"],
                    "from_cache": True,
                }
            )

    # No cache at all (a never-synced repo) or a forced refresh: build inline.
    # Build from the caches the app already keeps: open issues are the graph's
    # scope, and both lists seed the node hints so a cached item costs no API call.
    # A MISSING issues cache is unknown, not empty: building from it would persist
    # a wrong-empty graph for the TTL. ``_load_open_issues_for_reco`` is the
    # existing cache-first loader (fetch + cache on miss, provider-routed) the
    # tagging queue uses; a cold repo's first /deps call warms both caches.
    try:
        stored = await routes._rebuild_deps(request.app, key)
    except _DepsScopeUnavailable as exc:
        return web.json_response(
            {"error": str(exc), "code": "deps_issue_scope_unavailable"}, status=502
        )
    except routes.GhCliError as exc:
        return web.json_response({"error": str(exc), "code": "deps_fetch_failed"}, status=502)

    return web.json_response(
        {
            **routes._identity(key),
            "edges": stored["edges"],
            "nodes": stored["nodes"],
            "from_cache": False,
        }
    )

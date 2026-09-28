"""Issue and pull-request reads: the lists, their first page, search, detail and ``/ref``.

Every read answers 400 on missing identity, then 404 unless the repo is connected.
All but ``/pulls/search`` (a live provider search) are served cache-first.
``poll=1`` hands the refetch decision to ``backend.routes._poll_can_serve_cache``;
``first_page=1`` is a read-only fast path that never writes the durable cache.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial

from aiohttp import web

from kiro_crew.apps.builtins.issue_radar.backend import provider, store

from .pr_actions import _BULK_PR_MAX

logger = logging.getLogger("kirocrew.app.issue-radar")


async def _handle_issues(request: web.Request) -> web.Response:
    """GET /issues?owner=<o>&repo=<r>[&refresh=1][&poll=1][&first_page=1] — list open issues.

    Serves the local cache by default; ``refresh=1`` forces a fresh `gh` fetch.
    ``poll=1`` is the CLIENT-POLL intent: it wants current data but delegates the
    cost policy to this handler, which answers with one cheap probe call and only
    pays the paginated fetch when the probe says something moved (see
    ``_poll_can_serve_cache``).

    ``first_page=1`` is the PROGRESSIVE-PAINT intent, open state only. On a warm
    cache it serves the full cached rows unchanged; on a COLD cache it fetches only
    the newest single page in ONE request and returns it with ``partial: true``,
    WITHOUT writing the cache. That first page paints in one round-trip instead of
    blocking on the tens of paginated requests ``list_open_issues`` needs for a
    large repo; the client then runs the ordinary full fetch (which owns the
    durable cache) and swaps the complete set in behind it. See
    ``_handle_issues_first_page``.
    """
    from .. import routes  # circular import: backend.routes imports this module

    key = routes._key_from_request(request)
    owner, repo = key.owner, key.repo
    client = provider.client_for(key)
    pkw = provider.call_kwargs(key)
    if not owner or not repo:
        return web.json_response({"error": "missing ?owner= and ?repo="}, status=400)

    state = (request.query.get("state") or "open").strip().lower()
    if state not in ("open", "closed"):
        return web.json_response({"error": "state must be 'open' or 'closed'"}, status=400)

    if not await asyncio.to_thread(routes._connected, key):
        return web.json_response(
            {"error": f"{owner}/{repo} is not connected — call /connect first"},
            status=404,
        )

    # Progressive first paint takes its own branch BEFORE the poll/refresh logic:
    # it is a read-only fast path (never writes the cache, never probes) whose only
    # job is to get something on screen while the authoritative fetch runs.
    if request.query.get("first_page") == "1" and state == "open":
        return await routes._handle_issues_first_page(key, client, pkw)

    force_refresh = request.query.get("refresh") == "1"
    is_poll = request.query.get("poll") == "1"
    snapshot = (
        None
        if force_refresh
        else await routes._st(key, store.read_issues_snapshot, owner, repo, state=state)
    )
    probe: dict | None = None
    if snapshot is not None and is_poll:
        serve_cache, probe = await routes._poll_can_serve_cache(key, "issue", state, snapshot)
        if not serve_cache:
            snapshot = None
    if snapshot is not None:
        return web.json_response(
            {
                **routes._identity(key),
                "state": state,
                "issues": snapshot["rows"],
                "from_cache": True,
            }
        )

    fetch = client.list_open_issues if state == "open" else client.list_closed_issues
    try:
        # Fetch and store under ONE lock: a label applied between the two would
        # otherwise be overwritten by this pre-fetch snapshot, so a change the user
        # just made would vanish from the list (see store.refresh_issues_cache).
        # The poll fingerprint rides along so rows and probe land in one write.
        issues = await routes._st(
            key,
            store.refresh_issues_cache,
            owner,
            repo,
            lambda: fetch(owner, repo, **pkw),
            state=state,
            probe=probe,
        )
    except routes.GhCliError as exc:
        return web.json_response({"error": str(exc)}, status=502)
    return web.json_response(
        {**routes._identity(key), "state": state, "issues": issues, "from_cache": False}
    )


async def _handle_issues_first_page(
    key: provider.RepoKey, client: provider.ProviderClient, pkw: dict
) -> web.Response:
    """The progressive first-paint branch of ``/issues`` (open state only).

    A warm cache means the full list is already one instant read away, so serve it
    whole and mark it complete — there is nothing to gain from a partial. Only a
    COLD cache pays a fetch, and then just the newest single page (one request,
    ``partial: true``) so the app paints without waiting on the full pagination the
    authoritative fetch runs next.

    Deliberately does NOT write the cache: the durable cache is owned by the full
    fetch, which stores the complete set plus the poll ``probe`` under one lock.
    Persisting a partial here would let a subsequent poll serve an INCOMPLETE list
    as if it were whole (and with no probe), so this path stays read-only — its
    result lives only in the client's transient first-paint query.
    """
    from .. import routes  # circular import: backend.routes imports this module

    owner, repo = key.owner, key.repo
    snapshot = await routes._st(key, store.read_issues_snapshot, owner, repo, state="open")
    if snapshot is not None:
        return web.json_response(
            {
                **routes._identity(key),
                "state": "open",
                "issues": snapshot["rows"],
                "from_cache": True,
                "partial": False,
            }
        )
    try:
        issues = await asyncio.to_thread(
            partial(client.list_open_issues_first_page, owner, repo, **pkw)
        )
    except routes.GhCliError as exc:
        logger.warning("issue-radar list_open_issues provider error: %s", exc)
        return web.json_response(
            {"error": "upstream provider error", "code": "provider_error"}, status=502
        )
    return web.json_response(
        {
            **routes._identity(key),
            "state": "open",
            "issues": issues,
            "from_cache": False,
            "partial": True,
        }
    )


async def _handle_issue_detail(request: web.Request) -> web.Response:
    """GET /issue?owner=<o>&repo=<r>&number=<n>[&refresh=1] — one issue's full
    detail + normalized timeline (comments, label/assignee/close events, and
    cross-references), cache-first (mirrors /issues and /labels).

    ``number`` is parsed as an int before it reaches ``gh``, so it can't inject
    path segments; access is gated on the repo already being connected (same
    guard as /issues)."""
    from .. import routes  # circular import: backend.routes imports this module

    key = routes._key_from_request(request)
    owner, repo = key.owner, key.repo
    client = provider.client_for(key)
    pkw = provider.call_kwargs(key)
    number_raw = (request.query.get("number") or "").strip()
    if not owner or not repo or not number_raw:
        return web.json_response({"error": "missing ?owner=, ?repo= and ?number="}, status=400)

    number, number_error = routes._parse_item_number(number_raw)
    if number_error is not None:
        return number_error

    if not await asyncio.to_thread(routes._connected, key):
        return web.json_response(
            {"error": f"{owner}/{repo} is not connected — call /connect first"},
            status=404,
        )

    force_refresh = request.query.get("refresh") == "1"
    cached = (
        None
        if force_refresh
        else await routes._st(key, store.read_issue_detail_cache, owner, repo, number)
    )
    if cached is not None and cached.get("detail") is not None:
        return web.json_response(
            {
                "owner": owner,
                "repo": repo,
                "number": number,
                "detail": cached["detail"],
                "timeline": cached.get("timeline", []),
                "from_cache": True,
            }
        )

    try:
        detail = await asyncio.to_thread(
            partial(client.get_issue_detail, owner, repo, number, **pkw)
        )
        timeline = await asyncio.to_thread(
            partial(client.list_issue_timeline, owner, repo, number, **pkw)
        )
    except routes.GhCliError as exc:
        return web.json_response({"error": str(exc)}, status=502)

    await routes._st(key, store.write_issue_detail_cache, owner, repo, number, detail, timeline)
    return web.json_response(
        {
            "owner": owner,
            "repo": repo,
            "number": number,
            "detail": detail,
            "timeline": timeline,
            "from_cache": False,
        }
    )


async def _load_open_issues_for_reco(key: provider.RepoKey, *, refresh: bool = False) -> list[dict]:
    """Return the repo's open issues, cache-first (fetch + cache on miss).

    ``refresh`` bypasses the cache — the Tagging queue needs it because labels are
    routinely added on GitHub itself, and a cache-first read keeps showing those
    issues as untagged no matter how many times the user reloads."""
    from .. import routes  # circular import: backend.routes imports this module

    owner, repo = key.owner, key.repo
    client = provider.client_for(key)
    pkw = provider.call_kwargs(key)
    if not refresh:
        cached = await routes._st(key, store.read_issues_cache, owner, repo, state="open")
        if cached is not None:
            return cached
    # Fetch and store under ONE lock. Locking only the write would let an apply
    # patch the cache between the two and then be overwritten by this pre-write
    # snapshot, so a label the user just applied would vanish from the dashboard.
    return await routes._st(
        key,
        store.refresh_issues_cache,
        owner,
        repo,
        lambda: client.list_open_issues(owner, repo, **pkw),
        state="open",
    )


# ── pull requests (read-only list + detail) ─────────────────────────────────


async def _handle_pulls(request: web.Request) -> web.Response:
    """GET /pulls?owner=<o>&repo=<r>[&state=open|closed][&refresh=1][&poll=1][&first_page=1] — list PRs.

    Cache-first (mirrors /issues). ``state`` defaults to open; closed is bounded
    to the 100 most-recently-updated (includes both merged and closed-unmerged —
    the frontend splits them on ``merged_at``). Pass refresh=1 to force a fresh
    ``gh`` fetch, or poll=1 for the probe-gated client-poll path.

    ``first_page=1`` is the PROGRESSIVE-PAINT intent, open state only, and the PR
    counterpart of ``/issues?first_page=1``: a cold ``/pulls`` open is the app's
    slowest — it paginates every open PR AND runs the GraphQL enrichment before a
    byte renders — so this branch fetches only the newest single page (one
    request, un-enriched, ``partial: true``) WITHOUT writing the cache, and the
    client swaps the full enriched set in behind it. See ``_handle_pulls_first_page``.
    """
    from .. import routes  # circular import: backend.routes imports this module

    key = routes._key_from_request(request)
    owner, repo = key.owner, key.repo
    client = provider.client_for(key)
    pkw = provider.call_kwargs(key)
    if not owner or not repo:
        return web.json_response({"error": "missing ?owner= and ?repo="}, status=400)

    state = (request.query.get("state") or "open").strip().lower()
    if state not in ("open", "closed"):
        return web.json_response({"error": "state must be 'open' or 'closed'"}, status=400)

    if not await asyncio.to_thread(routes._connected, key):
        return web.json_response(
            {"error": f"{owner}/{repo} is not connected — call /connect first"},
            status=404,
        )

    # Progressive first paint takes its own branch BEFORE the poll/refresh logic:
    # a read-only fast path (never writes the cache, never probes, never enriches)
    # whose only job is to paint the newest page while the authoritative fetch runs.
    if request.query.get("first_page") == "1" and state == "open":
        return await routes._handle_pulls_first_page(key, client, pkw)

    force_refresh = request.query.get("refresh") == "1"
    is_poll = request.query.get("poll") == "1"
    snapshot = (
        None
        if force_refresh
        else await routes._st(key, store.read_pulls_snapshot, owner, repo, state=state)
    )
    probe: dict | None = None
    if snapshot is not None and is_poll:
        serve_cache, probe = await routes._poll_can_serve_cache(key, "pr", state, snapshot)
        if not serve_cache:
            snapshot = None
    if snapshot is not None:
        return web.json_response(
            {
                **routes._identity(key),
                "state": state,
                "pulls": snapshot["rows"],
                "from_cache": True,
                "bulk_max": _BULK_PR_MAX,
            }
        )

    fetch = client.list_open_pulls if state == "open" else client.list_closed_pulls
    try:
        pulls = await asyncio.to_thread(partial(fetch, owner, repo, **pkw))
    except routes.GhCliError as exc:
        return web.json_response({"error": str(exc)}, status=502)

    # One extra GraphQL call adds each row's diff size + aggregate check state
    # (the REST list carries neither). Best effort — a failure leaves the rows
    # un-enriched rather than failing the list.
    pulls = await asyncio.to_thread(partial(client.enrich_pulls, owner, repo, pulls, state, **pkw))

    # Only PERSIST fully-enriched rows. The list cache has no TTL, so caching a
    # row whose enrichment failed would keep serving "diff/check state unknown"
    # (rendered as absent) until the user manually refreshes. Skipping the write is
    # not enough on a forced refresh — the PREVIOUS cache would still be there and
    # the next plain request would serve those older rows — so the stale entry is
    # dropped too. The response itself still goes out: the list is useful without
    # the card decoration.
    if client.enrichment_complete(pulls):
        await routes._st(key, store.write_pulls_cache, owner, repo, pulls, state=state, probe=probe)
    else:
        await routes._st(key, store.drop_pulls_cache, owner, repo, state)
    return web.json_response(
        {
            **routes._identity(key),
            "state": state,
            "pulls": pulls,
            "from_cache": False,
            "bulk_max": _BULK_PR_MAX,
        }
    )


async def _handle_pulls_first_page(
    key: provider.RepoKey, client: provider.ProviderClient, pkw: dict
) -> web.Response:
    """The progressive first-paint branch of ``/pulls`` (open state only).

    The PR counterpart of ``_handle_issues_first_page``, and the bigger win: a
    cold ``/pulls`` blocks on BOTH the full pagination and the GraphQL enrichment
    before rendering, so a busy repo can sit on a skeleton for many seconds. A
    warm cache is served whole and complete; a COLD cache pays only the newest
    single page in one request and returns it ``partial: true``.

    The first page is returned UN-ENRICHED — no diff size, no check tally. That
    is deliberate: enrichment is the other slow leg, so paying it here would
    defeat the fast path, and a row's missing enrichment renders as absent (the
    card's bottom row is simply omitted) rather than as a wrong "no diff, no
    checks". The authoritative fetch the client runs next enriches and caches.

    Deliberately does NOT write the cache, for the same reason as the issues fast
    path: the durable cache is owned by the full fetch (which stores fully
    enriched rows plus the poll ``probe`` under one lock, and refuses to cache
    incomplete rows). Persisting an un-enriched partial here would let a later
    poll serve it as if it were whole, so this path stays read-only — its result
    lives only in the client's transient first-paint query.
    """
    from .. import routes  # circular import: backend.routes imports this module

    owner, repo = key.owner, key.repo
    snapshot = await routes._st(key, store.read_pulls_snapshot, owner, repo, state="open")
    if snapshot is not None:
        return web.json_response(
            {
                **routes._identity(key),
                "state": "open",
                "pulls": snapshot["rows"],
                "from_cache": True,
                "partial": False,
                "bulk_max": _BULK_PR_MAX,
            }
        )
    try:
        pulls = await asyncio.to_thread(
            partial(client.list_open_pulls_first_page, owner, repo, **pkw)
        )
    except routes.GhCliError as exc:
        logger.warning("issue-radar list_open_pulls provider error: %s", exc)
        return web.json_response(
            {"error": "upstream provider error", "code": "provider_error"}, status=502
        )
    return web.json_response(
        {
            **routes._identity(key),
            "state": "open",
            "pulls": pulls,
            "from_cache": False,
            "partial": True,
            "bulk_max": _BULK_PR_MAX,
        }
    )


async def _handle_pulls_search(request: web.Request) -> web.Response:
    """GET /pulls/search?owner=<o>&repo=<r>[&state=][&author=][&assignee=][&review_requested=]
    — PRs matching a per-person filter, resolved SERVER-side by GitHub search.

    The bounded /pulls list caps closed PRs at one page, which makes a
    client-side "authored by me" filter miss older PRs on a busy repo. This route
    answers those filters with a search query instead, so the result set is
    complete for that person regardless of repo size. ``state`` is open | merged |
    closed (closed = closed WITHOUT merge). At least one person parameter is
    required. Live call (not cached) — mirrors /recent-repos: the result is only
    read while a person filter is on, and a stale answer is worse than the wait.
    """
    from .. import routes  # circular import: backend.routes imports this module

    key = routes._key_from_request(request)
    owner, repo = key.owner, key.repo
    client = provider.client_for(key)
    pkw = provider.call_kwargs(key)
    if not owner or not repo:
        return web.json_response({"error": "missing ?owner= and ?repo="}, status=400)

    state = (request.query.get("state") or "open").strip().lower()
    author = (request.query.get("author") or "").strip() or None
    assignee = (request.query.get("assignee") or "").strip() or None
    review_requested = (request.query.get("review_requested") or "").strip() or None

    if not await asyncio.to_thread(routes._connected, key):
        return web.json_response(
            {"error": f"{owner}/{repo} is not connected — call /connect first"},
            status=404,
        )

    # The search cap is the DISPATCHED client's own (it is that CLI's paging
    # ceiling), not GitHub's — see _pr_merge_method_field for the same reasoning.
    # It is read once and reused, so the ceiling requested, the truncation test
    # and the number reported to the UI cannot disagree.
    search_max = client.PR_SEARCH_MAX  # type: ignore[attr-defined]
    try:
        pulls = await asyncio.to_thread(
            partial(client.search_pulls, owner, repo, **pkw),
            state=state,
            author=author,
            assignee=assignee,
            review_requested=review_requested,
            # One MORE than we will return, so "was anything left out?" is answered
            # by fact rather than by `len(rows) == cap` — a person with exactly the
            # cap's worth of matches omits nothing and must not be labelled capped.
            limit=search_max + 1,
        )
    except routes.PrSearchError as exc:
        # Bad state / invalid login / no person qualifier — a client input error.
        return web.json_response({"error": str(exc)}, status=400)
    except routes.GhCliError as exc:
        return web.json_response({"error": str(exc)}, status=502)

    truncated = len(pulls) > search_max
    pulls = pulls[:search_max]

    # Search rows carry no diff size or check state, so the cards would lose their
    # bottom row the moment a person filter is on. Enrich BY NUMBER (not by state)
    # because a search hit can rank outside the recently-updated window.
    pulls = await asyncio.to_thread(
        partial(client.enrich_pulls_by_number, owner, repo, pulls, **pkw)
    )

    return web.json_response(
        {
            "owner": owner,
            "repo": repo,
            "state": state,
            "pulls": pulls,
            "from_cache": False,
            "bulk_max": _BULK_PR_MAX,
            # The search is capped (PR_SEARCH_MAX). Saying so lets the UI stop
            # implying "this is every PR of yours in the repo" when it is the newest N —
            # the whole point of this route is escaping the list's page cap, so
            # silently imposing another one would undo that claim.
            "truncated": truncated,
            "limit": search_max,
        }
    )


async def _handle_pull_detail(request: web.Request) -> web.Response:
    """GET /pull?owner=<o>&repo=<r>&number=<n>[&refresh=1] — one PR's full detail
    + normalized timeline (comments, reviews, commits, label/close events) +
    the automated checks on its head commit, cache-first (mirrors /issue).

    The cache is served only while it is younger than
    ``store.PR_DETAIL_CACHE_TTL_SEC``; past that a plain GET refetches on its own.
    Freshness is therefore the route's property, not something each caller has to
    know to ask for with ``refresh=1`` (which remains available to force a read).

    ``number`` is parsed as an int before it reaches ``gh``, so it can't inject
    path segments; access is gated on the repo already being connected."""
    from .. import routes  # circular import: backend.routes imports this module

    key = routes._key_from_request(request)
    owner, repo = key.owner, key.repo
    client = provider.client_for(key)
    pkw = provider.call_kwargs(key)
    number_raw = (request.query.get("number") or "").strip()
    if not owner or not repo or not number_raw:
        return web.json_response({"error": "missing ?owner=, ?repo= and ?number="}, status=400)

    number, number_error = routes._parse_item_number(number_raw)
    if number_error is not None:
        return number_error

    if not await asyncio.to_thread(routes._connected, key):
        return web.json_response(
            {"error": f"{owner}/{repo} is not connected — call /connect first"},
            status=404,
        )

    force_refresh = request.query.get("refresh") == "1"
    cached = (
        None
        if force_refresh
        else await routes._st(
            key,
            store.read_pr_detail_cache,
            owner,
            repo,
            number,
            max_age_sec=store.PR_DETAIL_CACHE_TTL_SEC,
        )
    )
    if cached is not None and cached.get("detail") is not None:
        return web.json_response(
            {
                "owner": owner,
                "repo": repo,
                "number": number,
                "detail": cached["detail"],
                "timeline": cached.get("timeline", []),
                "checks": cached.get("checks", []),
                "checks_summary": client.summarize_checks(cached.get("checks") or []),
                "from_cache": True,
            }
        )

    try:
        # The detail fetch usually pays a deliberate retry for mergeability (GitHub
        # computes it lazily, see get_pr_detail), so it is the slow leg. Run the
        # timeline — which needs nothing from it — CONCURRENTLY rather than after,
        # so that wait overlaps real work instead of adding to it. The
        # PR-flavoured timeline is issue events PLUS inline code-anchored review
        # comments, which the issues timeline endpoint does not carry.
        detail, timeline = await asyncio.gather(
            asyncio.to_thread(partial(client.get_pr_detail, owner, repo, number, **pkw)),
            asyncio.to_thread(partial(client.list_pr_timeline, owner, repo, number, **pkw)),
        )
        # Automated checks hang off the PR's head commit, whose sha the detail
        # call already returned — so no extra PR round-trip. A PR with no head
        # sha (deleted fork branch) simply has no checks.
        head_sha = detail.get("head_sha")
        checks = (
            await asyncio.to_thread(partial(client.list_pr_checks, owner, repo, head_sha, **pkw))
            if head_sha
            else []
        )
    except routes.GhCliError as exc:
        return web.json_response({"error": str(exc)}, status=502)

    await routes._st(
        key, store.write_pr_detail_cache, owner, repo, number, detail, timeline, checks
    )
    # Write the fresh check state back onto the PR's LIST row too, so the card
    # and the sidebar cannot disagree: the detail pane re-reads checks every
    # couple of minutes, and without this the card kept whatever the last list
    # refresh computed.
    checks_summary = client.summarize_checks(checks)
    await routes._st(key, store.apply_pr_checks_to_list_cache, owner, repo, number, checks_summary)
    return web.json_response(
        {
            "owner": owner,
            "repo": repo,
            "number": number,
            "detail": detail,
            "timeline": timeline,
            "checks": checks,
            # Echoed so the client can patch its cached list row without refetching
            # the whole list (the card's tally + dot come from exactly these rows).
            "checks_summary": checks_summary,
            "from_cache": False,
        }
    )


async def _handle_ref_summary(request: web.Request) -> web.Response:
    """GET /ref?owner=<o>&repo=<r>&number=<n>[&refresh=1] — compact summary of one
    referenced issue OR pull request.

    Backs the in-app cross-reference UI: the hover preview (number, title, author,
    when, lifecycle) and the issue-vs-PR resolution a bare ``#123`` needs, since
    GitHub's ``/issues/{n}`` silently redirects to ``/pull/{n}``. Deliberately
    NOT ``/issue``: that route also pages the whole timeline, which is far too
    expensive to pay on hover.

    Cache-first with a short TTL (``store.REF_SUMMARY_CACHE_TTL_SEC``), so
    freshness is the route's property. Same guards as every other read: the
    number is parsed as an int before it reaches the provider CLI, and access is
    gated on the repo already being connected.
    """
    from .. import routes  # circular import: backend.routes imports this module

    key = routes._key_from_request(request)
    client = provider.client_for(key)
    pkw = provider.call_kwargs(key)
    owner = (request.query.get("owner") or "").strip()
    repo = (request.query.get("repo") or "").strip()
    number_raw = (request.query.get("number") or "").strip()
    if not owner or not repo or not number_raw:
        return web.json_response({"error": "missing ?owner=, ?repo= and ?number="}, status=400)

    number, number_error = routes._parse_item_number(number_raw)
    if number_error is not None:
        return number_error

    if not await asyncio.to_thread(routes._connected, key):
        return web.json_response(
            {"error": f"{owner}/{repo} is not connected — call /connect first"},
            status=404,
        )

    force_refresh = request.query.get("refresh") == "1"
    cached = (
        None
        if force_refresh
        else await routes._st(
            key,
            store.read_ref_summary_cache,
            owner,
            repo,
            number,
            max_age_sec=store.REF_SUMMARY_CACHE_TTL_SEC,
        )
    )
    if cached is not None:
        return web.json_response(
            {
                "owner": owner,
                "repo": repo,
                "provider": key.provider,
                "host": key.host,
                "number": number,
                "summary": cached,
                "from_cache": True,
            }
        )

    try:
        summary = await asyncio.to_thread(
            partial(client.get_ref_summary, owner, repo, number, **pkw)
        )
    except routes.GhCliError as exc:
        return web.json_response({"error": str(exc)}, status=502)

    await routes._st(key, store.write_ref_summary_cache, owner, repo, number, summary)
    return web.json_response(
        {
            "owner": owner,
            "repo": repo,
            "provider": key.provider,
            "host": key.host,
            "number": number,
            "summary": summary,
            "from_cache": False,
        }
    )

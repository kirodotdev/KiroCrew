"""Connected repositories, their triage settings, and the account-level reads.

The switcher (``/repos`` with its bounded, concurrent permission self-heal), the
connect dialog's picker (``/me``, ``/recent-repos``), per-repo settings (the
revision-checked PUT and the append-only ``/settings/role``), the repo's label set
and member roster, and disconnect. ``/connect`` itself stays in ``backend.routes``,
where a source guard pins its off-loop URL parse.
"""

from __future__ import annotations

import asyncio
from functools import partial

from aiohttp import web

from kiro_crew.apps.builtins.issue_radar.backend import provider, store

# Max concurrent live permission-verify calls when /repos self-heals rows connected
# before permissions were tracked. Bounded so a switcher with many un-healed repos
# fans out a few `gh` calls at once rather than an unbounded burst against the
# provider, while still beating the old one-at-a-time loop that gated app open.
_REPO_HEAL_CONCURRENCY = 6


def _load_members(key: provider.RepoKey) -> tuple[list[dict], str]:
    """Load the repo's member roster and its source.

    Primary: the authoritative COLLABORATORS roster (needs push access) —
    ``[{login, role}]`` with role ∈ admin/maintain/write/triage/read. Fallback
    (on 403, i.e. a read-only repo): the members inferred from issue authors'
    ``author_association``, using whatever issues are already cached. Persists
    the result with its ``source`` and returns ``(members, source)``.

    Synchronous (subprocess + disk) — call via ``asyncio.to_thread``. A
    non-permission ``GhCliError`` (network/timeout) propagates so the route can
    surface it rather than silently degrading.

    On GitLab the primary path is readable by any project member (and includes
    members inherited from ancestor groups), so the fallback is effectively
    GitHub-only -- and ``gitlab_client.derive_members`` deliberately returns an
    empty roster rather than inventing one from issue authors, who on a public
    GitLab project may be strangers.
    """
    from .. import routes  # circular import: backend.routes imports this module

    owner, repo = key.owner, key.repo
    client = provider.client_for(key)
    pkw = provider.call_kwargs(key)
    scope = routes._scope(key)
    try:
        collaborators = client.list_repo_collaborators(owner, repo, **pkw)
        members = [
            {"login": c["login"], "role": c.get("role_name") or "member"}
            for c in collaborators
            if c.get("login")
        ]
        members.sort(key=lambda m: m["login"].lower())
        source = "collaborators"
    except routes.GhPermissionError:
        # Read-only repo: fall back to the issue-derived set (best effort).
        open_issues = store.read_issues_cache(owner, repo, scope, state="open") or []
        closed_issues = store.read_issues_cache(owner, repo, scope, state="closed") or []
        members = [
            {"login": m["login"], "role": m["association"]}
            for m in client.derive_members(open_issues + closed_issues)
        ]
        source = "derived"
    store.write_members_cache(owner, repo, members, root=scope, source=source)
    return members, source


async def _handle_labels(request: web.Request) -> web.Response:
    """GET /labels?owner=<o>&repo=<r>[&refresh=1] — list the repo's labels.

    Cache-first (mirrors /issues); pass refresh=1 to force a fresh `gh` fetch.
    Each label carries its GitHub-configured colour so the frontend can render
    the left-rail filter column and issue chips in the repo's real colours.
    """
    from .. import routes  # circular import: backend.routes imports this module

    key = routes._key_from_request(request)
    owner, repo = key.owner, key.repo
    client = provider.client_for(key)
    pkw = provider.call_kwargs(key)
    if not owner or not repo:
        return web.json_response({"error": "missing ?owner= and ?repo="}, status=400)

    if not await asyncio.to_thread(routes._connected, key):
        return web.json_response(
            {"error": f"{owner}/{repo} is not connected — call /connect first"},
            status=404,
        )

    force_refresh = request.query.get("refresh") == "1"
    cached = None if force_refresh else await routes._st(key, store.read_labels_cache, owner, repo)
    if cached is not None:
        return web.json_response(
            {"owner": owner, "repo": repo, "labels": cached, "from_cache": True}
        )

    try:
        # Fetch and store under ONE lock, so a label created between the two cannot
        # be overwritten by this pre-fetch snapshot and left invisible in every
        # picker (see store.refresh_labels_cache).
        labels = await routes._st(
            key,
            store.refresh_labels_cache,
            owner,
            repo,
            lambda: client.list_repo_labels(owner, repo, **pkw),
        )
    except routes.GhCliError as exc:
        return web.json_response({"error": str(exc)}, status=502)

    return web.json_response({"owner": owner, "repo": repo, "labels": labels, "from_cache": False})


async def _load_labels_for_ai(key: provider.RepoKey) -> list[dict]:
    """Return the repo's labels, cache-first, fetching + caching on miss."""
    from .. import routes  # circular import: backend.routes imports this module

    owner, repo = key.owner, key.repo
    client = provider.client_for(key)
    pkw = provider.call_kwargs(key)
    cached = await routes._st(key, store.read_labels_cache, owner, repo)
    if cached is not None:
        return cached
    # Fetch and store under ONE lock, so a label created between the two cannot be
    # overwritten by this pre-fetch snapshot and left invisible in every picker.
    labels = await routes._st(
        key,
        store.refresh_labels_cache,
        owner,
        repo,
        lambda: client.list_repo_labels(owner, repo, **pkw),
    )
    return labels


async def _handle_members(request: web.Request) -> web.Response:
    """GET /members?owner=<o>&repo=<r>[&refresh=1] — the repo's member roster.

    Cache-first (mirrors /labels). The roster is the authoritative COLLABORATORS
    list (everyone with access, each with a role) when the caller has push
    access; on a read-only repo GitHub 403s and we fall back to the members
    inferred from issue authors. The response carries a ``source`` marker
    (``collaborators`` | ``derived``) so the UI can note when it's the fallback.
    """
    from .. import routes  # circular import: backend.routes imports this module

    key = routes._key_from_request(request)
    owner, repo = key.owner, key.repo
    if not owner or not repo:
        return web.json_response({"error": "missing ?owner= and ?repo="}, status=400)

    if not await asyncio.to_thread(routes._connected, key):
        return web.json_response(
            {"error": f"{owner}/{repo} is not connected — call /connect first"},
            status=404,
        )

    force_refresh = request.query.get("refresh") == "1"
    cached = None if force_refresh else await routes._st(key, store.read_members_cache, owner, repo)
    if cached is not None:
        return web.json_response(
            {
                "owner": owner,
                "repo": repo,
                "members": cached["members"],
                "source": cached.get("source"),
                "from_cache": True,
            }
        )

    try:
        members, source = await asyncio.to_thread(routes._load_members, key)
    except routes.GhCliError as exc:
        return web.json_response({"error": str(exc)}, status=502)
    return web.json_response(
        {
            "owner": owner,
            "repo": repo,
            "members": members,
            "source": source,
            "from_cache": False,
        }
    )


async def _handle_repos(request: web.Request) -> web.Response:
    """GET /repos — list the repos connected in config.json (for the switcher).

    Self-heals: any repo missing a cached ``permissions`` object (connected
    before permissions were tracked) gets one fetched live and written back,
    so the UI can badge Read/Write access.
    """
    from .. import routes  # circular import: backend.routes imports this module

    repos = await asyncio.to_thread(store.list_connected_repos)
    # Self-heal the rows missing a cached permissions object CONCURRENTLY. This
    # runs on the app-open path and gates the switcher, and a serial loop paid one
    # live round-trip PLUS one config write per un-healed repo, back to back — so a
    # handful of legacy repos added seconds to first paint. A bounded gather fans
    # the reads out; the semaphore keeps it from spawning an unbounded burst of `gh`
    # when many repos need healing at once.
    missing = [r for r in repos if not r.get("permissions")]
    if missing:
        sem = asyncio.Semaphore(_REPO_HEAL_CONCURRENCY)

        async def _heal(r: dict) -> None:
            # Each entry carries its own provider+host, so a mixed GitHub/GitLab
            # switcher self-heals every row against the right server rather than
            # asking GitHub about a GitLab project.
            entry_key = provider.key_from_parts(
                str(r.get("owner") or ""),
                str(r.get("repo") or ""),
                r.get("provider"),
                r.get("host"),
            )
            entry_client = provider.client_for(entry_key)
            async with sem:
                try:
                    summary = await asyncio.to_thread(
                        partial(
                            entry_client.verify_repo_access,
                            entry_key.owner,
                            entry_key.repo,
                            **provider.call_kwargs(entry_key),
                        )
                    )
                except routes.GhCliError:
                    # A single unreadable repo must not fail the batch — the same
                    # per-row skip the serial loop had. It stays un-badged and
                    # re-heals on the next /repos.
                    return
            perms = summary.get("permissions")
            r["permissions"] = perms
            await asyncio.to_thread(
                partial(
                    store.set_repo_permissions,
                    entry_key.owner,
                    entry_key.repo,
                    perms,
                    provider=entry_key.provider,
                    host=entry_key.host,
                )
            )

        # return_exceptions so an unexpected error in one heal cannot abort the rest;
        # _heal already swallows the expected GhCliError as a per-row skip.
        await asyncio.gather(*(_heal(r) for r in missing), return_exceptions=True)
    return web.json_response({"repos": repos})


async def _handle_me(request: web.Request) -> web.Response:
    """GET /me[?provider=&host=] — the authenticated user's login on that
    provider (for the "requested/assigned to me" filters).

    Provider-scoped, because the login is NOT portable: the same person is
    ``alice`` on GitHub and possibly ``alice.smith`` on a company GitLab. Serving
    the GitHub login while a GitLab project is active would silently make those
    filters match nobody — a wrong answer with no error, which is why the
    provider rides on the request instead of being assumed.

    Returns ``{"login": null}`` rather than erroring if the CLI cannot resolve a
    login, so the UI just hides those filters.
    """
    from .. import routes  # circular import: backend.routes imports this module

    key = routes._account_key(request)
    try:
        login = await asyncio.to_thread(
            partial(provider.client_for(key).get_current_login, **provider.call_kwargs(key))
        )
    except routes.GhCliError:
        return web.json_response({"login": None})
    return web.json_response({"login": login, "provider": key.provider, "host": key.host})


async def _handle_recent_repos(request: web.Request) -> web.Response:
    """GET /recent-repos[?days=<d>&provider=&host=] — repos the current user
    personally CONTRIBUTED to within the last ``days`` (default 30), newest
    contribution first, for the connect dialog's picker.

    On GitLab "contributed to" is answered by project MEMBERSHIP ordered by last
    activity, which is both cheaper and more accurate than GitHub's public event
    feed — see ``gitlab_client.list_contributed_repos``.

    Each row carries ``last_contributed_at`` (that user's own latest
    contribution to the repo) and is flagged ``connected`` so the picker can
    show — and disable — repos already wired up. Live `gh` call, not cached:
    the list is only read while the connect dialog is open, and a stale picker
    is worse than a one-second wait. A `gh` failure is a 502 (upstream/auth),
    matching /issues.
    """
    from .. import routes  # circular import: backend.routes imports this module

    key = routes._account_key(request)
    client = provider.client_for(key)
    pkw = provider.call_kwargs(key)
    # Both windows come off the DISPATCHED client, not github_client: they are that
    # provider's own bounds (its feed horizon, its accepted range), and reading
    # GitHub's would apply one provider's limits to another's request. Same
    # reasoning as _pr_merge_method_field.
    contrib_window_days = client.CONTRIB_WINDOW_DAYS  # type: ignore[attr-defined]
    max_window_days = client.MAX_WINDOW_DAYS  # type: ignore[attr-defined]
    raw_days = (request.query.get("days") or "").strip()
    try:
        days = int(raw_days) if raw_days else contrib_window_days
    except ValueError:
        return web.json_response({"error": "days must be an integer"}, status=400)
    # Bounded before it reaches timedelta(days=...): an arbitrarily large value
    # raises OverflowError there, which would surface as a 500. 0 stays legal
    # (it disables the window); MAX_WINDOW_DAYS is far beyond the event feed's
    # own ~90-day horizon, so the cap costs nothing in practice.
    if not 0 <= days <= max_window_days:
        return web.json_response(
            {"error": f"days must be between 0 and {max_window_days}"},
            status=400,
        )

    try:
        login = await asyncio.to_thread(partial(client.get_current_login, **pkw))
    except routes.GhSetupError as exc:
        # Host isn't set up (no gh, or no session). Not an error the user can
        # retry away — answer 200 with a reason so the dialog can render install
        # / `gh auth login` instructions and keep the manual URL field usable.
        return web.json_response({"repos": [], "setup_required": exc.reason, "error": str(exc)})
    except routes.GhCliError as exc:
        return web.json_response({"error": str(exc)}, status=502)
    if not login:
        # No resolvable login means no event feed to read. An empty list (not a
        # 502) keeps the dialog usable — the manual URL field still works.
        return web.json_response({"repos": []})

    try:
        repos, truncated = await asyncio.to_thread(
            partial(client.list_contributed_repos, login, within_days=days, **pkw)
        )
    except routes.GhSetupError as exc:
        return web.json_response({"repos": [], "setup_required": exc.reason, "error": str(exc)})
    except routes.GhCliError as exc:
        return web.json_response({"error": str(exc)}, status=502)

    # Name identity follows the PROVIDER's case semantics, via the same helper the
    # authorization gate uses (store._name_matches is defined in terms of it). On
    # GitHub the names are case-preserving but not case-sensitive, and the event
    # feed can spell a repo differently from the stored config (`Owner/Repo` vs
    # `owner/repo`); a case-sensitive compare there would mark an
    # already-connected repo as connectable and let the user create a duplicate
    # config + cache entry for the same repo. Casefolding UNCONDITIONALLY has the
    # opposite failure on a case-sensitive provider: two genuinely distinct
    # projects collapse, and one is shown as already connected when it is not.
    def _key(owner: object, repo: object) -> tuple[str, str]:
        return (
            store.name_compare_key(str(owner or ""), key.provider),
            store.name_compare_key(str(repo or ""), key.provider),
        )

    connected = {
        _key(r.get("owner"), r.get("repo"))
        for r in await asyncio.to_thread(store.list_connected_repos)
        if str(r.get("provider") or "github") == key.provider
        and str(r.get("host") or "github.com") == key.host
    }
    for r in repos:
        r["connected"] = _key(r.get("owner"), r.get("repo")) in connected
        # Echoed so the picker can build a connect URL and a repo ref without
        # re-deriving which provider the list came from.
        r["provider"] = key.provider
        r["host"] = key.host

    # `truncated` tells the UI not to present the list as exhaustive — see
    # list_contributed_repos.
    return web.json_response(
        {
            "repos": repos,
            "truncated": truncated,
            "provider": key.provider,
            "host": key.host,
        }
    )


async def _handle_get_settings(request: web.Request) -> web.Response:
    """GET /settings?owner=<o>&repo=<r> — the repo's local triage settings
    (triage labels, unlabeled-is-untriaged toggle, good-first-issue labels).
    Returns defaults for a connected repo that has never been configured."""
    from .. import routes  # circular import: backend.routes imports this module

    key = routes._key_from_request(request)
    owner, repo = key.owner, key.repo
    if not owner or not repo:
        return web.json_response({"error": "missing ?owner= and ?repo="}, status=400)

    if not await asyncio.to_thread(routes._connected, key):
        return web.json_response(
            {"error": f"{owner}/{repo} is not connected — call /connect first"},
            status=404,
        )

    settings = await asyncio.to_thread(
        partial(store.read_repo_settings, owner, repo, provider=key.provider, host=key.host)
    )
    return web.json_response({"owner": owner, "repo": repo, "settings": settings})


async def _handle_put_settings(request: web.Request) -> web.Response:
    """PUT /settings {"owner","repo","settings":{...}} — persist a repo's triage
    settings. The body is normalized server-side (unknown keys dropped, label
    lists coerced to de-duplicated strings), so the stored object is always the
    known schema regardless of client input."""
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    from .. import routes  # circular import: backend.routes imports this module

    # Repo settings steer every crew and triage view: owner only.
    owner_denied = await require_owner_dashboard_request(request, "issue_radar.settings_put")
    if owner_denied is not None:
        return owner_denied

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "request body must be JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "request body must be a JSON object"}, status=400)

    key = routes._key_from_body(body)
    owner, repo = key.owner, key.repo
    if not owner or not repo:
        return web.json_response({"error": "missing 'owner'/'repo'"}, status=400)

    settings = body.get("settings")
    if not isinstance(settings, dict):
        return web.json_response({"error": "'settings' must be an object"}, status=400)

    # Optimistic concurrency, MANDATORY. This PUT replaces the WHOLE document, so a
    # client that read revision N must say so: if the stored revision has moved on
    # (typically because /settings/role appended a label from another tab) the
    # write is refused instead of silently discarding that change.
    #
    # A missing revision is rejected rather than treated as "don't check" — an
    # opt-out is indistinguishable from a stale client that simply never sent one,
    # and that path could still erase newer settings.
    expected = settings.get("revision")
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
        return web.json_response(
            {
                "error": "'settings.revision' is required (send the revision you read, "
                "so a write built on stale settings can be refused)"
            },
            status=400,
        )

    try:
        saved = await asyncio.to_thread(
            partial(
                store.write_repo_settings,
                owner,
                repo,
                settings,
                expected_revision=expected,
                provider=key.provider,
                host=key.host,
            )
        )
    except store.SettingsConflict as conflict:
        return web.json_response(
            {
                "error": "These settings changed in another tab while you were editing. "
                "Reload to pick up the newer version, then re-apply your change.",
                "settings": conflict.current,
            },
            status=409,
        )
    except KeyError:
        return web.json_response(
            {"error": f"{owner}/{repo} is not connected — call /connect first"},
            status=404,
        )
    return web.json_response({"owner": owner, "repo": repo, "settings": saved})


async def _handle_add_settings_label(request: web.Request) -> web.Response:
    """POST /settings/role {"owner","repo","role","label"} — APPEND one label to a
    repo's triage-label role.

    Exists because the settings PUT replaces the whole document, so a client that
    reads-then-writes can only serialize ITSELF. Two dashboard tabs, or a tab and
    an API client, each read the same settings and issue competing replacements,
    and the later write permanently drops the other's label. Appending here puts
    the read and the write in one critical section for every caller.

    Local-only (nothing is written to GitHub), so no forge permission gate; it is
    still owner only, like every other settings write. Idempotent.
    """
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    from .. import routes  # circular import: backend.routes imports this module

    # Same document as PUT /settings: owner only.
    owner_denied = await require_owner_dashboard_request(request, "issue_radar.settings_role")
    if owner_denied is not None:
        return owner_denied

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "request body must be JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "request body must be a JSON object"}, status=400)

    key = routes._key_from_body(body)
    owner, repo = key.owner, key.repo
    role = routes._str_field(body, "role")
    label = routes._str_field(body, "label")
    if not owner or not repo:
        return web.json_response({"error": "missing 'owner'/'repo'"}, status=400)
    if not role or not label:
        return web.json_response({"error": "missing 'role'/'label'"}, status=400)

    try:
        settings = await asyncio.to_thread(
            partial(
                store.add_setting_label,
                owner,
                repo,
                role,
                label,
                provider=key.provider,
                host=key.host,
            )
        )
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except KeyError:
        return web.json_response(
            {"error": f"{owner}/{repo} is not connected — call /connect first"},
            status=404,
        )
    return web.json_response({"owner": owner, "repo": repo, "settings": settings})


async def _handle_disconnect(request: web.Request) -> web.Response:
    """DELETE /repos?owner=<o>&repo=<r> — disconnect a repo. Drops it from
    config.json and deletes its local issue/label cache. Local-only: nothing on
    GitHub is changed and the user's `gh` auth is untouched."""
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    from .. import routes  # circular import: backend.routes imports this module

    # Removes a repo from the owner's connected set: owner only.
    owner_denied = await require_owner_dashboard_request(request, "issue_radar.disconnect")
    if owner_denied is not None:
        return owner_denied

    key = routes._key_from_request(request)
    owner, repo = key.owner, key.repo
    if not owner or not repo:
        return web.json_response({"error": "missing ?owner= and ?repo="}, status=400)

    removed = await asyncio.to_thread(
        partial(
            store.remove_connected_repo,
            owner,
            repo,
            provider=key.provider,
            host=key.host,
        )
    )
    if not removed:
        return web.json_response({"error": f"{owner}/{repo} is not connected"}, status=404)
    return web.json_response({"ok": True, "owner": owner, "repo": repo})

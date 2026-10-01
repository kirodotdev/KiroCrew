"""Issue Radar — backend routes.

Registered at gateway startup by ``apps/routes.py:register_app_routes``
(loaded via the app's ``backend.routes`` manifest field:
``"backend.routes:register_routes"``).

Routes (browser-facing, same-origin authed — same pattern as every other
builtin app's ``/api/apps/{name}/*`` surface):

  POST /api/apps/issue-radar/connect   {"url": "<full github repo URL>"}
                                        -> {"owner", "repo", "full_name",
                                            "private", "open_issues_count"}
  GET  /api/apps/issue-radar/issues?owner=<o>&repo=<r>[&state=open|closed][&refresh=1]
                                        -> {"owner", "repo", "state",
                                            "issues": [...], "from_cache": bool}
  GET  /api/apps/issue-radar/issue?owner=<o>&repo=<r>&number=<n>[&refresh=1]
                                        -> {"owner", "repo", "number",
                                            "detail": {...}, "timeline": [...],
                                            "from_cache": bool}
  GET  /api/apps/issue-radar/labels?owner=<o>&repo=<r>[&refresh=1]
                                        -> {"owner", "repo", "labels": [...],
                                            "from_cache": bool}
  GET  /api/apps/issue-radar/members?owner=<o>&repo=<r>[&refresh=1]
                                        -> {"owner", "repo", "members": [...],
                                            "from_cache": bool}
  GET  /api/apps/issue-radar/repos      -> {"repos": [{"owner","repo","enabled"}]}
  GET  /api/apps/issue-radar/recent-repos[?days=<d>]
                                        -> {"repos": [{"owner","repo","full_name",
                                            "last_contributed_at",
                                            "contribution_count","connected"}]}

  GET  /api/apps/issue-radar/issue-ai?owner=<o>&repo=<r>&number=<n>[&refresh=1]
                                        -> {"owner","repo","number","summary",
                                            "suggested_labels":[{"name","reason"}],
                                            "from_cache": bool}
  GET  /api/apps/issue-radar/pull-ai?owner=<o>&repo=<r>&number=<n>[&refresh=1]
                                        -> {"owner","repo","number","summary",
                                            "from_cache": bool}
  POST /api/apps/issue-radar/labels/apply  {"owner","repo","number","add":[],"remove":[]}
                                        -> {"owner","repo","number","labels":[...]}
  POST /api/apps/issue-radar/issue/state   {"owner","repo","number","state","state_reason"?}
                                        -> {"owner","repo","number","state","state_reason"}
  POST /api/apps/issue-radar/issue/assignees {"owner","repo","number","assignees":[...],"expected":[...]}
                                        -> {"owner","repo","number","assignees":[...]}
                                           409 + current set when "expected" is stale

Connect / list / detail / labels stay a pure ``gh`` CLI + local-cache path (the
same "deterministic backbone" principle as code_review_sage's repo-scan routes).
The single LLM-backed route is ``/issue-ai``: it computes an issue's triage
summary + suggested labels via one model call, cache-first (paid once per issue,
served instantly on re-open). ``/pull-ai`` does the same for a pull request,
summarizing its description + whole conversation + check state; its cache is keyed
by a fingerprint of those inputs, so a new comment or a flipped check earns a
fresh summary while an unchanged PR is never re-summarized. The write routes (``/labels/apply``,
``/issue/state``, ``/issue/assignees``) are the confirm half of the suggest->confirm loop and are gated
on the user's ``triage``/``push`` access; a read-only repo degrades to
suggest-only (writes 403).

This module is the route layer's facade. It owns what every handler runs first --
request identity, the enabled / connected / write-permission gates, per-repo store
scoping (``_st``), the SEL audit and the provider-neutral error aliases -- plus
``/connect``, the probe-gated list-poll decision and ``register_routes``. The
handlers live in the private ``http_routes`` package, one module per
responsibility, and reach every gate and monkeypatch seam through this module at
call time, so a patch on ``routes.<name>`` still intercepts them. The config and
UI-language helpers it imports (``KiroCrewConfig``, ``ui_language_tag``,
``normalize_ui_language_tag``) are re-exported for callers but are not seams:
``http_routes.ai`` reads its own bindings of them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from functools import partial, wraps

from aiohttp import web

from kiro_crew.apps.builtins.issue_radar.backend import (
    github_client,
    pipeline_routes,
    provider,
    store,
    watch,
)
from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.config.loader import KiroCrewConfig  # noqa: F401 -- re-exported, not a seam
from kiro_crew.context import (  # noqa: F401 -- re-exported, not a seam
    normalize_ui_language_tag,
    ui_language_tag,
)
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.sel import sel

# The handlers and their helpers live in ``http_routes`` (one module per
# responsibility, see its package docstring). Every name is re-exported here: the
# route layer's tests patch and read them as ``routes.<name>``, ``crew_routes``
# reaches its shared gates this way, and other modules' docs cite them so.
from .http_routes.ai import (  # noqa: F401
    _AI_BODY_MAX_CHARS,
    _AI_MAX_SUGGESTIONS,
    _LANG_HINT_FIELD,
    _PR_AI_BODY_MAX_CHARS,
    _PR_AI_COMMENT_MAX_CHARS,
    _PR_AI_MAX_COMMENTS,
    _PR_AI_MAX_VERDICTS,
    _build_ai_prompt,
    _build_pr_ai_prompt,
    _compute_issue_ai,
    _compute_pr_ai,
    _handle_issue_ai,
    _handle_pull_ai,
    _hint_language,
    _language_directive,
    _load_detail_for_ai,
    _pr_ai_comment_rows,
    _pr_ai_fingerprint,
    _pr_lifecycle,
    _resolve_ui_language,
    _run_oneshot_model,
    _ui_language,
)
from .http_routes.deps import (  # noqa: F401
    _DEPS_REBUILD_LOCKS_APP_KEY,
    _DEPS_REFRESH_TASKS_APP_KEY,
    _deps_node_hints,
    _deps_rebuild_lock,
    _deps_refresh_registry,
    _deps_reg_key,
    _DepsRebuildLocks,
    _DepsRefreshTasks,
    _DepsScopeUnavailable,
    _handle_deps,
    _rebuild_deps,
    _schedule_deps_refresh,
    _stop_deps_refreshes,
)
from .http_routes.investigation import (  # noqa: F401
    _handle_get_investigation,
    _handle_put_investigation,
    _item_kind,
)
from .http_routes.issue_writes import (  # noqa: F401
    MAX_ASSIGNEES,
    _apply_label_change,
    _handle_issue_assignees,
    _handle_issue_state,
    _handle_labels_apply,
    _replace_assignees_checked,
    _reread_labels_and_patch,
)
from .http_routes.items import (  # noqa: F401
    _handle_issue_detail,
    _handle_issues,
    _handle_issues_first_page,
    _handle_pull_detail,
    _handle_pulls,
    _handle_pulls_first_page,
    _handle_pulls_search,
    _handle_ref_summary,
    _load_open_issues_for_reco,
)
from .http_routes.pr_actions import (  # noqa: F401
    _BULK_PR_ACTIONS,
    _BULK_PR_MAX,
    _HEAD_SHA_RE,
    _MERGE_ALLOWED_STATES,
    _PINNED_BULK_PR_ACTIONS,
    _PR_BODY_MAX_CHARS,
    MAX_RUN_ID,
    _handle_pull_auto_merge,
    _handle_pull_comment,
    _handle_pull_merge,
    _handle_pull_review,
    _handle_pull_run_action,
    _handle_pull_runs,
    _handle_pull_state,
    _handle_pulls_bulk,
    _pr_action_error,
    _pr_action_preamble,
    _pr_body_field,
    _pr_head_sha_field,
    _pr_head_shas_field,
    _pr_merge_method_field,
    _pr_number_field,
    _pr_numbers_field,
    _refuse_if_head_moved,
    _run_pr_action,
)
from .http_routes.recommendations import (  # noqa: F401
    _DEFAULT_CATEGORY_COLOR,
    _ISSUE_CITATION_RE,
    _ISSUE_REF_RE,
    _RATIONALE_MAX_CHARS,
    _RECO_BODY_MAX_CHARS,
    _RECO_CATEGORIES,
    _RECO_ISSUE_SAMPLE,
    _RECO_MAX,
    _RECO_MAX_EXAMPLES,
    _build_reco_prompt,
    _compute_label_recommendations,
    _handle_create_label,
    _handle_generate_recommendations,
    _handle_get_recommendations,
    _short_rationale,
    _valid_hex6,
)
from .http_routes.repositories import (  # noqa: F401
    _REPO_HEAL_CONCURRENCY,
    _handle_add_settings_label,
    _handle_disconnect,
    _handle_get_settings,
    _handle_labels,
    _handle_me,
    _handle_members,
    _handle_put_settings,
    _handle_recent_repos,
    _handle_repos,
    _load_labels_for_ai,
    _load_members,
)
from .http_routes.tagging import (  # noqa: F401
    _TAG_BATCH_MAX,
    _TAG_BODY_MAX_CHARS,
    _TAG_BULK_MAX,
    _TAG_MAX_PER_ISSUE,
    _build_tagging_prompt,
    _compute_tagging_suggestions,
    _handle_generate_tagging,
    _handle_get_tagging,
    _handle_labels_apply_bulk,
    _untagged,
)

logger = logging.getLogger("kirocrew.app.issue-radar")

# Re-exported so the route layer's `except GhCliError` clauses read
# as provider-neutral, which they now are: both clients raise these exact classes
# (see backend/errors.py — they are aliases, not parallel hierarchies).
#
# PrSearchError is listed for the same reason, and ONLY for that reason: it was
# already caught as `github_client.PrSearchError`, which is not a bug — it is
# literally the same class object every client raises (`errors.PrSearchError`,
# re-exported by name), so the clause already caught GitLab's and will catch
# Azure's. Reading it off one provider's module only made a provider-neutral catch
# look provider-specific.
GhCliError = github_client.GhCliError
GhPermissionError = github_client.GhPermissionError
GhSetupError = github_client.GhSetupError
PrSearchError = github_client.PrSearchError
# The provider rejected a VALUE in the request (an unassignable login). A subclass
# of GhCliError, so it must be caught BEFORE the generic clause or it lands in the
# 502 branch it exists to avoid.
GhInvalidInputError = github_client.GhInvalidInputError


def _account_key(request: web.Request) -> provider.RepoKey:
    """A provider+host key with NO repo, for account-scoped endpoints.

    ``/me`` and ``/recent-repos`` ask the provider CLI about the CURRENT USER
    rather than about a connected repo, so they cannot go through
    ``store.is_repo_connected``. The host is still never trusted from the
    client: it is normalized here and re-authorized against the operator's
    allowlist at the spawn boundary, which is what stops a crafted host reaching
    an arbitrary GitLab instance on an endpoint that has no connected-repo gate.
    """
    return provider.key_from_parts("", "", request.query.get("provider"), request.query.get("host"))


def _key_from_request(request: web.Request) -> provider.RepoKey:
    """Build a :class:`provider.RepoKey` from a request's query string.

    ``provider``/``host`` are OPTIONAL and default to public GitHub, so a client
    that predates GitLab support -- including a cached older frontend bundle --
    keeps working unchanged.

    Neither value is trusted here. ``normalize_provider`` collapses anything
    unknown to ``github``, ``normalize_host`` pins a GitHub key's host so a
    crafted host cannot become part of a cache path, and the GitLab host is
    re-authorized against the operator's allowlist at the spawn boundary on every
    single call. The gate that actually decides whether this request may touch a
    repo is ``store.is_repo_connected``, which now matches on provider+host too.
    """
    return provider.key_from_parts(
        (request.query.get("owner") or "").strip(),
        (request.query.get("repo") or "").strip(),
        request.query.get("provider"),
        request.query.get("host"),
    )


def _str_field(body: dict, key: str) -> str:
    """A trimmed string body field, or ``""`` for anything that is not a string.

    ``(body.get(key) or "").strip()`` raises AttributeError on a truthy non-string
    (``{"owner": 1}``, ``{"owner": []}``), which surfaces as a 500 for what is
    plainly a malformed request. Callers treat ``""`` as missing and return 400."""
    value = body.get(key)
    return value.strip() if isinstance(value, str) else ""


def _key_from_body(body: dict) -> provider.RepoKey:
    """Body counterpart to :func:`_key_from_request` (for POST/PUT/DELETE).

    Non-string ``owner``/``repo`` become ``""`` rather than being coerced, exactly
    as :func:`_str_field` does. ``str(value)`` would stringify a Mock, a list or an
    int into something that passes the "missing" check and then fails the
    connected-repo gate instead — turning a plainly malformed request from a 400
    into a 404 (or a 500 when the value reaches json.dumps). Callers treat ``""``
    as missing and answer 400.
    """
    return provider.key_from_parts(
        _str_field(body, "owner"),
        _str_field(body, "repo"),
        body.get("provider"),
        body.get("host"),
    )


def _scope(key: provider.RepoKey):
    """The store root that scopes ``key``'s on-disk data.

    EVERY per-repo store call in the route layer passes this as ``root=``. Omitting it
    would silently read or write the GitHub tree for a GitLab project, so the
    helper exists to make the correct call the short one.
    """
    return store.provider_root(root=None, provider=key.provider, host=key.host)


async def _st(key: provider.RepoKey, fn, *args, **kwargs):
    """Run a per-repo ``store`` function off-loop, scoped to ``key``'s data root.

    Every per-repo store call in the route layer goes through here. The point is not
    brevity -- it is that forgetting the scope is otherwise invisible: a plain
    ``store.read_issues_cache(owner, repo)`` for a GitLab project silently reads
    the GitHub tree and returns another repo's cached issues. Funnelling the calls
    means the omission is impossible to write by accident, and a test asserts no
    ``asyncio.to_thread(store.…)`` call survives outside this helper.

    Config-level functions (connected-repo records, per-repo settings) are NOT
    routed through here: they are keyed by provider+host inside ``config.json``
    rather than by data root, and are called directly with those arguments.
    """
    return await asyncio.to_thread(partial(fn, *args, root=_scope(key), **kwargs))


def _identity(key: provider.RepoKey) -> dict[str, str]:
    """The identity fields every response echoes back.

    The frontend round-trips these on the next request, so a repo the user is
    viewing cannot drift to a different provider mid-session.
    """
    return {"owner": key.owner, "repo": key.repo, "provider": key.provider, "host": key.host}


def _connected(key: provider.RepoKey) -> bool:
    """Whether ``key`` is a connected repo (the authorization gate)."""
    return store.is_repo_connected(key.owner, key.repo, provider=key.provider, host=key.host)


def _require_enabled(handler):
    """Deny requests when Issue Radar is disabled (deny-by-default). Routes are
    registered once at gateway startup, so a default-disabled / opt-in app would
    otherwise stay callable. ``is_app_enabled`` is a synchronous installed.json
    read, so it runs off the event loop (same as watch.py / the dashboard
    notifications_push handler)."""

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.Response:
        if not await asyncio.to_thread(is_app_enabled, store.APP_NAME):
            return web.json_response({"error": "issue-radar is disabled"}, status=403)
        return await handler(request)

    return _wrapped


def _audit(op: str, target: str, outcome: str, *, error: str = "") -> None:
    """Emit a Security Event Log entry for a GitHub-mutating action — on denial,
    success, or failure — mirroring deploy/handlers.py's ``_audit``. Fire-and-
    forget (non-critical); the caller keeps its own HTTP response."""
    sel().log_api_access(
        caller="core:issue-radar",
        operation=f"issue_radar.{op}",
        outcome=outcome,
        source="builtin-app",
        resources=target,
        error=error[:200] if error else "",
    )


# Upper bound on an issue/PR number this app will accept. GitHub numbers are
# per-repo sequences in the thousands (the largest public repos are in the
# hundreds of thousands), so this is generous by orders of magnitude. It exists
# because an unbounded int reaches the FILESYSTEM: the per-item caches are named
# ``issue-{n}.json`` / ``pull-{n}.json`` / ``ref-{n}.json``, and a several-hundred
# digit number makes ``Path.is_file()`` raise ENAMETOOLONG — a 500 on input that
# should simply be a 400.
MAX_ITEM_NUMBER = 1_000_000_000


def _parse_item_number(raw: str) -> tuple[int, web.Response | None]:
    """Parse an issue/PR ``?number=`` into a bounded positive int.

    Returns ``(number, None)`` on success, or ``(0, error_response)`` — so every
    item route validates identically instead of each re-deriving the rules.
    """
    try:
        number = int(raw)
    except ValueError:
        return 0, web.json_response({"error": "number must be an integer"}, status=400)
    if number <= 0:
        return 0, web.json_response({"error": "number must be a positive integer"}, status=400)
    if number > MAX_ITEM_NUMBER:
        return 0, web.json_response(
            {"error": f"number must be at most {MAX_ITEM_NUMBER}"}, status=400
        )
    return number, None


async def _handle_connect(request: web.Request) -> web.Response:
    """POST /connect — validate a repo URL against the user's provider CLI
    session, then persist it to config.json. Does not fetch issues (see /issues).

    The URL alone determines the provider and host: ``provider.parse_repo_url``
    dispatches on the URL's host and rejects any GitLab instance that is not
    gitlab.com or in the operator's ``dashboard.gitlab_hosts`` allowlist. The
    client cannot nominate a provider here -- that is what keeps a connected-repo
    record, and therefore every later request authorized against it, honest.
    """
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    # Connecting probes the repo with the OWNER's provider CLI, and the read routes
    # then serve whatever it can see, private repos included: owner only.
    owner_denied = await require_owner_dashboard_request(request, "issue_radar.connect")
    if owner_denied is not None:
        return owner_denied

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "request body must be JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "request body must be a JSON object"}, status=400)

    url = (body.get("url") or "").strip()
    if not url:
        return web.json_response({"error": "missing 'url'"}, status=400)

    try:
        # Off-loop: on a non-github.com URL this reads the operator's
        # ``dashboard.gitlab_hosts`` allowlist, and ``KiroCrewConfig.load()`` is
        # synchronous file I/O + validation. Cheap per call, but it is the
        # gateway's single event loop, and every other blocking call in this
        # module is already threaded for the same reason.
        key = await asyncio.to_thread(provider.parse_repo_url, url)
    except github_client.RepoUrlError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    owner, repo = key.owner, key.repo
    client = provider.client_for(key)
    pkw = provider.call_kwargs(key)

    try:
        summary = await asyncio.to_thread(partial(client.verify_repo_access, owner, repo, **pkw))
    except GhCliError as exc:
        # Upstream/auth problem (CLI not installed/authed, repo not found or
        # private-without-access, network/timeout) — not a client input error.
        return web.json_response({"error": str(exc)}, status=502)

    await asyncio.to_thread(
        partial(
            store.add_connected_repo,
            owner,
            repo,
            permissions=summary.get("permissions"),
            provider=key.provider,
            host=key.host,
        )
    )

    return web.json_response(
        {
            **_identity(key),
            "full_name": summary.get("full_name", f"{owner}/{repo}"),
            "private": summary.get("private", False),
            "open_issues_count": summary.get("open_issues_count", 0),
        }
    )


# Hard ceiling on how long a poll may keep answering from the cache without a
# real fetch, however confident the probe is. This bounds every way the probe can
# be WRONG rather than merely unavailable — a reading that is consistently wrong
# matches its own prior recording forever, so error handling alone cannot catch
# it. Two live examples:
#   * GitHub is retiring PR results from `search/issues` (the `advanced_search`
#     transition). When that lands the `is:pr` probe degenerates to a stable
#     {0, None}, which compares equal to itself — the PR list would freeze while
#     looking healthy.
#   * A PR's check run turning red changes NEITHER `updated_at` nor the open
#     count, so no probe of the issue/PR metadata can see CI move. (The PR you
#     actually have open stays current regardless: its detail poll writes fresh
#     check state back into the list cache — see apply_pr_checks_to_list_cache.)
# 10 minutes = every 10th poll at LIST_POLL_MS, so the worst case is ~6 full
# fetches an hour instead of 60 — still an order of magnitude below the unprobed
# cost this replaced.
LIST_POLL_MAX_STALENESS_SEC = 600.0


# How long one probe reading may be reused across CALLERS. Without this, every
# visible tab probes on its own 60s cadence and the search quota (30/min, shared
# with the user's own searches) scales with the number of open tabs. The lock
# makes concurrent polls join one in-flight probe instead of each issuing their
# own.
_PROBE_COALESCE_SEC = 15.0
# (provider, host, owner, repo, kind) — the provider and host are part of the key
# because the same owner/repo path exists on GitHub, on gitlab.com, and on every
# self-managed instance.
_ProbeKey = tuple[str, str, str, str, str]
_probe_memo: dict[_ProbeKey, tuple[float, dict]] = {}
_probe_inflight: dict[_ProbeKey, "asyncio.Future[dict]"] = {}
# Guards the two maps ONLY. It is deliberately never held across the probe call
# itself: a global lock around a 20s-timeout `gh` invocation would make one slow
# repo's probe stall every other repo's and kind's poll response.
_probe_lock = LoopBoundLock()


def _remember_probe(key: _ProbeKey, task: "asyncio.Future[dict]") -> None:
    """Done-callback: publish a finished probe and retire its in-flight entry.

    Runs on the event loop with no awaits, so it cannot interleave with the
    critical section in :func:`_coalesced_probe` (which also has no awaits).
    Recording here rather than in the awaiting caller means a request that is
    cancelled mid-probe (a closed tab) still contributes its reading to the
    window instead of wasting the call.
    """
    if _probe_inflight.get(key) is task:
        del _probe_inflight[key]
    if not task.cancelled() and task.exception() is None:
        _probe_memo[key] = (time.time(), task.result())


async def _coalesced_probe(repo_key: provider.RepoKey, kind: str) -> dict:
    """The provider's ``probe_open_list`` with a short shared-result window.

    Concurrent callers for the SAME key join one in-flight probe; callers for
    different keys never wait on each other.

    The memo key includes the provider and host, not just owner/repo: the same
    ``group/project`` path exists on GitHub, on gitlab.com, and on every
    self-managed instance, so keying on the slug alone would let one repo's probe
    be served as another's and a list be declared unchanged on the strength of a
    different server's answer.

    The name segments are folded to the PROVIDER's own case semantics
    (``store.name_compare_key``) rather than lowercased outright. Lowercasing is
    the same confusion one level down: on a case-sensitive provider
    ``group/Project`` and ``group/project`` are different projects, and a shared
    memo key would answer one of them with the other's probe -- declaring a list
    unchanged on a reading that was never taken of it.

    Raises :class:`GhCliError` like the underlying call.
    """
    key = (
        repo_key.provider,
        repo_key.host,
        store.name_compare_key(repo_key.owner, repo_key.provider),
        store.name_compare_key(repo_key.repo, repo_key.provider),
        kind,
    )
    async with _probe_lock:
        now = time.time()
        for stale_key, (taken_at, _) in list(_probe_memo.items()):
            if now - taken_at > _PROBE_COALESCE_SEC:
                del _probe_memo[stale_key]
        hit = _probe_memo.get(key)
        if hit is not None:
            return hit[1]
        task = _probe_inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(
                asyncio.to_thread(
                    partial(
                        provider.client_for(repo_key).probe_open_list,
                        repo_key.owner,
                        repo_key.repo,
                        kind,
                        **provider.call_kwargs(repo_key),
                    )
                )
            )
            _probe_inflight[key] = task
            task.add_done_callback(partial(_remember_probe, key))
    # Shielded so one cancelled request does not cancel the probe that the other
    # joined callers are still waiting on.
    return await asyncio.shield(task)


async def _poll_can_serve_cache(
    repo_key: provider.RepoKey, kind: str, state: str, snapshot: dict
) -> tuple[bool, dict | None]:
    """Decide whether a ``poll=1`` request can be answered from the cache.

    Returns ``(serve_cache, probe_to_record)``. ``probe_to_record`` is the probe
    value taken BEFORE any refetch, and is what the caller stores alongside the
    freshly fetched rows — deliberately the earlier reading, so a change that
    lands *during* the fetch leaves the recorded probe behind the real state and
    the next poll refetches. Recording a probe taken after the fetch would hide
    that change until something else moved.

    Only the OPEN lists are probed. The closed lists are bounded to a single
    ``per_page=100`` page, so refetching one is already one request — a probe
    would just add a second.
    """
    if state != "open":
        return False, None
    if snapshot["age_sec"] > LIST_POLL_MAX_STALENESS_SEC:
        # Past the ceiling: refetch WITHOUT probing. Probing here would only add
        # a request to a decision that is already made.
        return False, None
    try:
        probe = await _coalesced_probe(repo_key, kind)
    except GhCliError:
        # Probe unavailable → keep serving the cache. Refetching on every failed
        # probe would turn a sustained probe outage (an exhausted search quota,
        # say) into exactly the paginated-fetch-per-minute drain this path exists
        # to avoid. Staleness is already bounded by the ceiling above, which is
        # the honest backstop; freshness here is not worth that cost.
        return True, None
    if snapshot["probe"] is not None and snapshot["probe"] == probe:
        return True, probe
    return False, probe


# ── write-permission gate (label + state edits) ─────────────────────────────


def _has_write_access(perms: dict | None) -> bool:
    """True if a GitHub permissions object grants a write Issue Radar supports.

    Any of triage/push/maintain/admin can label and open/close issues; ``triage``
    is the minimal role that can, so it is the floor for the edit features."""
    if not isinstance(perms, dict):
        return False
    return bool(
        perms.get("triage") or perms.get("push") or perms.get("maintain") or perms.get("admin")
    )


def _repo_can_write(key: provider.RepoKey) -> bool | None:
    """Best-effort "can the current provider user edit issues on this repo?".

    Prefers the permissions stored at connect time (fast, no network); if the
    repo entry has none, fetches once and self-heals the store. Returns ``None``
    when it genuinely cannot tell (gh error) — callers treat ``None`` as DENIED
    (``is not True`` → 403), so a transient permissions-read failure shows the
    repo as read-only until the next successful refresh rather than allowing an
    unauthenticated write. This is deliberately fail-closed: a brief period of
    degraded write access is preferable to a single unauthorized mutation.

    On GitLab the permission object is derived from the caller's effective access
    level (project access or inherited group access, whichever is higher), with
    Reporter mapping to ``triage`` and Developer to ``push`` -- so the same
    ``_has_write_access`` gate applies unchanged."""
    owner, repo = key.owner, key.repo
    entry = store.find_connected_repo(owner, repo, provider=key.provider, host=key.host)
    if entry is not None:
        perms = entry.get("permissions")
        if isinstance(perms, dict):
            return _has_write_access(perms)
    try:
        perms = provider.client_for(key).get_repo_permissions(
            owner, repo, **provider.call_kwargs(key)
        )
    except GhCliError:
        return None
    store.set_repo_permissions(owner, repo, perms, provider=key.provider, host=key.host)
    return _has_write_access(perms)


def register_routes(app: web.Application) -> None:
    """Register this app's routes on the gateway's aiohttp Application.

    Signature/hardcoded-path convention matches every other builtin app
    (see code_review_sage/backend/routes.py:register_routes) — confirmed
    against the real call site in dashboard/server.py
    (``_mod.register_routes(app)``, single argument, no base_path passed in).
    """
    app.router.add_post("/api/apps/issue-radar/connect", _require_enabled(_handle_connect))
    app.router.add_get("/api/apps/issue-radar/issues", _require_enabled(_handle_issues))
    app.router.add_get("/api/apps/issue-radar/issue", _require_enabled(_handle_issue_detail))
    app.router.add_get("/api/apps/issue-radar/pulls", _require_enabled(_handle_pulls))
    app.router.add_get("/api/apps/issue-radar/pulls/search", _require_enabled(_handle_pulls_search))
    app.router.add_get("/api/apps/issue-radar/pull", _require_enabled(_handle_pull_detail))
    app.router.add_get("/api/apps/issue-radar/ref", _require_enabled(_handle_ref_summary))
    app.router.add_get("/api/apps/issue-radar/deps", _require_enabled(_handle_deps))
    app.router.add_get("/api/apps/issue-radar/labels", _require_enabled(_handle_labels))
    app.router.add_get("/api/apps/issue-radar/members", _require_enabled(_handle_members))
    app.router.add_get("/api/apps/issue-radar/repos", _require_enabled(_handle_repos))
    app.router.add_get("/api/apps/issue-radar/recent-repos", _require_enabled(_handle_recent_repos))
    app.router.add_delete("/api/apps/issue-radar/repos", _require_enabled(_handle_disconnect))
    app.router.add_get("/api/apps/issue-radar/me", _require_enabled(_handle_me))
    app.router.add_get("/api/apps/issue-radar/settings", _require_enabled(_handle_get_settings))
    app.router.add_put("/api/apps/issue-radar/settings", _require_enabled(_handle_put_settings))
    app.router.add_post(
        "/api/apps/issue-radar/settings/role", _require_enabled(_handle_add_settings_label)
    )
    app.router.add_get("/api/apps/issue-radar/issue-ai", _require_enabled(_handle_issue_ai))
    app.router.add_get("/api/apps/issue-radar/pull-ai", _require_enabled(_handle_pull_ai))
    app.router.add_post(
        "/api/apps/issue-radar/labels/apply", _require_enabled(_handle_labels_apply)
    )
    app.router.add_post("/api/apps/issue-radar/issue/state", _require_enabled(_handle_issue_state))
    app.router.add_post(
        "/api/apps/issue-radar/issue/assignees", _require_enabled(_handle_issue_assignees)
    )
    # Pull-request actions (see the section note in ``http_routes/pr_actions.py``).
    app.router.add_post("/api/apps/issue-radar/pull/state", _require_enabled(_handle_pull_state))
    app.router.add_post("/api/apps/issue-radar/pull/review", _require_enabled(_handle_pull_review))
    app.router.add_post(
        "/api/apps/issue-radar/pull/comment", _require_enabled(_handle_pull_comment)
    )
    app.router.add_post("/api/apps/issue-radar/pull/merge", _require_enabled(_handle_pull_merge))
    app.router.add_post(
        "/api/apps/issue-radar/pull/auto-merge", _require_enabled(_handle_pull_auto_merge)
    )
    app.router.add_get("/api/apps/issue-radar/pull/runs", _require_enabled(_handle_pull_runs))
    app.router.add_post("/api/apps/issue-radar/pull/run", _require_enabled(_handle_pull_run_action))
    app.router.add_post("/api/apps/issue-radar/pulls/bulk", _require_enabled(_handle_pulls_bulk))
    app.router.add_get(
        "/api/apps/issue-radar/investigation", _require_enabled(_handle_get_investigation)
    )
    app.router.add_put(
        "/api/apps/issue-radar/investigation", _require_enabled(_handle_put_investigation)
    )
    app.router.add_get(
        "/api/apps/issue-radar/recommendations", _require_enabled(_handle_get_recommendations)
    )
    app.router.add_post(
        "/api/apps/issue-radar/recommendations", _require_enabled(_handle_generate_recommendations)
    )
    app.router.add_post(
        "/api/apps/issue-radar/labels/create", _require_enabled(_handle_create_label)
    )
    app.router.add_get("/api/apps/issue-radar/tagging", _require_enabled(_handle_get_tagging))
    app.router.add_post("/api/apps/issue-radar/tagging", _require_enabled(_handle_generate_tagging))
    app.router.add_post(
        "/api/apps/issue-radar/labels/apply-bulk", _require_enabled(_handle_labels_apply_bulk)
    )

    # Crews (the worker-agent surface) live in their own module but register HERE,
    # so this function stays the single place that lists this app's routes. The
    # import is function-local because it is CIRCULAR: crew_routes imports this
    # module for the shared gates (_require_enabled, _pr_action_preamble, _st), so
    # a module-scope import here would reach a half-initialized routes module.
    from . import crew_routes

    crew_routes.register_crew_routes(app)

    # The pipeline dashboard's routes, same arrangement and for the same reason:
    # its own module, registered HERE so this function stays the one place that
    # lists this app's routes. Unlike crew_routes this import is NOT circular --
    # pipeline_routes depends only on its fold and on `store` for the app name --
    # so it is imported at module scope with the rest.
    pipeline_routes.register_routes(app)

    # Background new-issue watcher: a single in-process asyncio loop (NOT a cron
    # job) that polls opted-in repos every ~60s and pushes a KiroCrew
    # notification when a new issue is opened. register_app_routes runs before
    # runner.setup() freezes the signal lists, so these appends fire (same
    # pattern as code_review_sage's on_cleanup hook); guarded so a hook-append
    # failure can never break gateway startup.
    try:
        app.on_startup.append(watch.start_watcher)
        app.on_cleanup.append(watch.stop_watcher)
        # Cancel any in-flight background /deps revalidations on shutdown so a
        # serve-stale rebuild never outlives the app as an unawaited task.
        app.on_cleanup.append(_stop_deps_refreshes)
    except Exception:  # pragma: no cover - defensive
        logger.warning("issue-radar: could not register watcher lifecycle hooks", exc_info=True)

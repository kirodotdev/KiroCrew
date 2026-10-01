"""The privileged pull-request actions: state, review, comment, merge, auto-merge,
CI runs, and bulk.

Every mutation runs ``_pr_action_preamble`` first and reaches the provider through
``_run_pr_action``, the one place an action name becomes a provider call. The
rules every handler here keeps are in the section note below.
"""

from __future__ import annotations

import asyncio
import logging
import re
from functools import partial

from aiohttp import web

from kiro_crew.apps.builtins.issue_radar.backend import provider, store

logger = logging.getLogger("kirocrew.app.issue-radar")


# ── pull-request actions ─────────────────────────────────────────────────────
#
# The write half of the PR pane: the actions a maintainer otherwise leaves the app
# to perform on the provider's own web UI — close/reopen, approve, comment, merge or
# arm auto-merge, cancel or retry CI. Each is available per-PR and, where the action
# is safe to repeat across many PRs, in BULK from the list.
#
# Four rules hold for every handler here:
#
# 1. **Merging is offered, and it cannot bypass a gate — because the APP checks too,
#    not because the provider is a sufficient backstop.** Branch protection —
#    required reviews, required checks, required conversation resolution — is
#    enforced by the PROVIDER on its own merge endpoint, and an unsatisfied PR comes
#    back 405. But that is true only for an ORDINARY user: a repository admin holding
#    bypass-branch-protection gets the merge honoured, so "the provider adjudicates"
#    stops being true exactly for the account that can do the most damage. That is why
#    ``_handle_pull_merge`` re-reads the PR and refuses anything outside
#    ``_MERGE_ALLOWED_STATES`` itself, and why the merge is pinned to the reviewed
#    ``head_sha``. See the note on that constant.
#    So the app offers both halves of the real
#    workflow: ``merge`` for a PR that is mergeable now, and ``auto_merge`` for one
#    that should land by itself once its checks pass. An earlier revision shipped
#    only the second, reasoning that a direct merge could land unreviewed code —
#    which, with the app-side gate above, it cannot; what that omission actually did
#    was leave a repo with NO branch rule (where auto-merge is unavailable) with no
#    merge path at all.
#    There is still deliberately no "override and merge": an override is a
#    governance decision recorded ON the provider (this repo does it with a reviewed
#    `/ai-review override` comment), and a button
#    that silently sheds a required check is the one thing the provider would NOT
#    adjudicate for us.
# 2. **Bulk is opt-in per action, not a generic loop.** ``_handle_pulls_bulk``
#    dispatches only the verbs in :data:`_BULK_PR_ACTIONS`. Merging is deliberately
#    NOT among them: a merge is irreversible, and 50 of them from one click is a
#    blast radius no confirmation makes reasonable. Arming auto-merge IS, because it
#    is reversible from the same bar and the provider still decides each one.
# 3. **Partial failure is reported, never swallowed** — same contract as
#    ``/labels/apply-bulk``: per-PR results, so one locked PR does not fail a batch
#    that otherwise applied, and the user is never told about a write that did not
#    happen.
# 4. **Every mutation is permission-gated and SEL-audited**, and drops the caches
#    the action invalidated so the pane cannot keep showing the pre-action state.

# Upper bound on a CI run id. Provider run ids are global monotonic sequences (much
# larger than a per-repo item number, so this is its own constant), and like every
# other number here it reaches a path segment in the provider argv — an unbounded int
# makes that segment arbitrarily long.
MAX_RUN_ID = 1_000_000_000_000

# The verbs the bulk endpoint accepts. Deliberately a fixed allowlist rather than
# "any action name": a generic fan-out would silently gain every future action,
# including ones whose blast radius makes a 50-PR batch a bad idea — which is
# exactly why ``merge`` is absent (see rule 2 above).
_BULK_PR_ACTIONS = (
    "close",
    "reopen",
    "approve",
    "comment",
    "auto_merge",
    "cancel_auto_merge",
)

# Upper bound on PRs in one bulk request. Each one is at least one provider call,
# and the whole batch runs inside a single HTTP request, so this bounds both the
# request's wall-clock and how much a mis-click can touch. Matches the existing
# label bulk cap's reasoning.
_BULK_PR_MAX = 50

# Upper bound on a comment / review body. Generous for prose, bounded so a
# multi-megabyte paste is a 400 rather than a provider-side rejection after N
# successful writes in a batch.
_PR_BODY_MAX_CHARS = 65_536

# A commit sha, in the shortest-to-longest form either provider accepts. Shared by
# every gate that pins a write to a revision (merge, review, bulk review) so they
# cannot drift apart in what they consider a commit.
_HEAD_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")

# The bulk verbs that are a statement about a REVISION and therefore need a per-PR
# head commit. A review is one; closing, commenting and arming auto-merge are not —
# they act on the pull request itself, and would still mean the same thing after a
# push.
_PINNED_BULK_PR_ACTIONS = frozenset({"approve"})


def _pr_numbers_field(body: dict) -> tuple[list[int], web.Response | None]:
    """Parse and validate a bulk request's ``numbers`` array.

    De-duplicated while PRESERVING order (a repeated number would otherwise be
    acted on twice — harmless for approve, but a second close is a wasted call and
    a confusing duplicate row in the response). Bounds each value with the same
    ``_parse_item_number`` the single-item routes use, so a bulk request cannot
    smuggle in a number the per-PR path would have refused.
    """
    from .. import routes  # circular import: backend.routes imports this module

    raw = body.get("numbers")
    if not isinstance(raw, list) or not raw:
        return [], web.json_response(
            {
                "error": "'numbers' must be a non-empty array",
                "code": "numbers_required",
            },
            status=400,
        )
    if len(raw) > _BULK_PR_MAX:
        return [], web.json_response(
            {
                "error": f"too many pull requests in one request (max {_BULK_PR_MAX})",
                "code": "too_many_pulls",
            },
            status=400,
        )
    out: list[int] = []
    seen: set[int] = set()
    for value in raw:
        # bool is a subclass of int: JSON `true` would otherwise validate as #1.
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return [], web.json_response(
                {
                    "error": "each entry in 'numbers' must be a positive integer",
                    "code": "invalid_number",
                },
                status=400,
            )
        if value > routes.MAX_ITEM_NUMBER:
            return [], web.json_response(
                {
                    "error": f"pull-request number out of range (max {routes.MAX_ITEM_NUMBER})",
                    "code": "number_out_of_range",
                },
                status=400,
            )
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out, None


def _pr_head_shas_field(
    body: dict, numbers: list[int]
) -> tuple[dict[int, str], web.Response | None]:
    """The per-PR head commits a bulk PINNED action was formed against.

    ``head_shas`` is a ``{"<number>": "<sha>"}`` map rather than a parallel array, so
    a client that reorders or filters its selection cannot silently pair a sha with
    the wrong PR — the association is by number, not by index.

    Required for EVERY number when the action is a pinned one (today: ``approve``).
    A bulk approve is N verdicts, and each has to name the revision its row was
    rendered at; accepting a partial map would approve the remainder at whatever the
    head happened to be, which is exactly the hole the per-PR pin closes.
    """
    raw = body.get("head_shas")
    if not isinstance(raw, dict):
        return {}, web.json_response(
            {
                "error": "'head_shas' must be an object mapping each pull-request "
                "number to the head commit you reviewed",
                "code": "head_shas_required",
            },
            status=400,
        )
    out: dict[int, str] = {}
    for number in numbers:
        value = raw.get(str(number))
        sha = value.strip() if isinstance(value, str) else ""
        if not _HEAD_SHA_RE.match(sha):
            return {}, web.json_response(
                {
                    "error": f"'head_shas' is missing or invalid for #{number} — each "
                    "pull request is pinned to the commit you reviewed",
                    "code": "head_shas_required",
                },
                status=400,
            )
        out[number] = sha
    return out, None


def _pr_body_field(body: dict, key: str = "body") -> tuple[str, web.Response | None]:
    """A bounded comment/review body from the request."""
    from .. import routes  # circular import: backend.routes imports this module

    text = routes._str_field(body, key)
    if len(text) > _PR_BODY_MAX_CHARS:
        return "", web.json_response(
            {
                "error": f"'{key}' is too long (max {_PR_BODY_MAX_CHARS} characters)",
                "code": "body_too_long",
            },
            status=400,
        )
    return text, None


async def _pr_action_preamble(
    request: web.Request,
    op: str,
) -> tuple[dict, provider.RepoKey, web.Response | None]:
    """The checks EVERY pull-request action shares: the dashboard-owner gate,
    JSON body, owner/repo, connected-repo gate, and the triage/push permission gate.

    Factored out because it is the security-relevant part and it is identical for
    all of them — a per-handler copy is how one of them eventually ships without
    the permission check. Returns the parsed body and key, or a response to
    return immediately.
    """
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    from .. import routes  # circular import: backend.routes imports this module

    # Every action here writes to the forge as the OWNER's gh/glab login, so
    # only the dashboard owner may call it: a non-owner subject and every app
    # token, this app's own included, are refused before the body is read.
    owner_denied = await require_owner_dashboard_request(request, f"issue_radar.{op}")
    if owner_denied is not None:
        return {}, provider.RepoKey(), owner_denied

    try:
        raw = await request.json()
    except Exception:
        return (
            {},
            provider.RepoKey(),
            web.json_response(
                {"error": "request body must be JSON", "code": "invalid_json"},
                status=400,
            ),
        )
    if not isinstance(raw, dict):
        return (
            {},
            provider.RepoKey(),
            web.json_response(
                {"error": "request body must be a JSON object", "code": "invalid_json"},
                status=400,
            ),
        )

    key = routes._key_from_body(raw)
    if not key.owner or not key.repo:
        return (
            raw,
            key,
            web.json_response(
                {"error": "missing 'owner'/'repo'", "code": "missing_repo"}, status=400
            ),
        )

    if not await asyncio.to_thread(routes._connected, key):
        return (
            raw,
            key,
            web.json_response(
                {
                    "error": f"{key.slug} is not connected — call /connect first",
                    "code": "repo_not_connected",
                },
                status=404,
            ),
        )

    if (await asyncio.to_thread(routes._repo_can_write, key)) is not True:
        routes._audit(op, key.slug, "denied", error="no confirmed write access")
        return (
            raw,
            key,
            web.json_response(
                {
                    "error": "This repo is connected read-only — you need triage or push "
                    "access to act on pull requests.",
                    "code": "repo_read_only",
                },
                status=403,
            ),
        )
    return raw, key, None


def _pr_action_error(op: str, target: str, exc: Exception) -> web.Response:
    """Map a provider failure from a PR action onto its HTTP status.

    A permission error is 403 (the user's session cannot do this), a bad request
    the client could fix is 400, and anything else upstream is 502 — the same
    taxonomy the label/state routes use, so one action behaving differently is not
    something a caller has to discover.

    The 502 relays ``str(exc)`` rather than a fixed string, unlike the read routes.
    A PR action fails for a reason the caller can usually FIX — "Allow auto-merge is
    off for this repository", "the base branch is protected" — and that reason lives
    only in the provider's own text. Withholding it here would leave the operator
    retrying a button that can never work. The read routes carry no such actionable
    text, so they sanitize (see ``_handle_pull_runs``).
    """
    from .. import routes  # circular import: backend.routes imports this module

    if isinstance(exc, routes.GhPermissionError):
        routes._audit(op, target, "denied", error=str(exc))
        return web.json_response({"error": str(exc), "code": "provider_forbidden"}, status=403)
    routes._audit(op, target, "failure", error=str(exc))
    return web.json_response({"error": str(exc), "code": "provider_error"}, status=502)


async def _run_pr_action(
    key: provider.RepoKey,
    action: str,
    number: int,
    *,
    body: str = "",
    method: str = "SQUASH",
    failed_only: bool = False,
    run_id: int = 0,
    head_sha: str = "",
) -> dict:
    """Perform ONE pull-request action against the provider, off the event loop.

    The single place an action name becomes a provider call, so the per-PR route
    and the bulk route cannot drift into doing different things for the same verb.
    Raises the provider's own errors; the callers map them.
    """
    from .. import routes  # circular import: backend.routes imports this module

    client = provider.client_for(key)
    pkw = provider.call_kwargs(key)
    owner, repo = key.owner, key.repo

    if action in ("close", "reopen"):
        state = "closed" if action == "close" else "open"
        result = await asyncio.to_thread(
            partial(client.set_pr_state, owner, repo, number, state, **pkw)
        )
        await routes._st(
            key,
            store.apply_pr_state_change_to_caches,
            owner,
            repo,
            number,
            result.get("state", state),
        )
        return result

    if action in ("approve", "request_changes", "comment_review"):
        event = {
            "approve": "APPROVE",
            "request_changes": "REQUEST_CHANGES",
            "comment_review": "COMMENT",
        }[action]
        # PINNED to the head the caller read, exactly like the merge below: a review
        # is a verdict on a REVISION, and without the pin a force-push between the
        # render and the click records an approval of code nobody looked at. Both
        # clients refuse an empty sha and send it as the provider's own precondition,
        # so a moved head is a provider refusal rather than a stale verdict.
        result = await asyncio.to_thread(
            partial(
                client.submit_pr_review,
                owner,
                repo,
                number,
                event,
                body,
                head_sha,
                **pkw,
            )
        )
        await routes._st(key, store.drop_pr_detail_cache, owner, repo, number)
        return result

    if action == "comment":
        # The PR-specific function, not add_issue_comment: on GitLab those are
        # different collections with independent numbering, so the generic one
        # would comment on an unrelated issue that happens to share the number.
        result = await asyncio.to_thread(
            partial(client.add_pr_comment, owner, repo, number, body, **pkw)
        )
        await routes._st(key, store.drop_pr_detail_cache, owner, repo, number)
        return result

    if action == "merge":
        result = await asyncio.to_thread(
            partial(client.merge_pull_request, owner, repo, number, method, head_sha, **pkw)
        )
        # A REFUSAL is not an exception on every provider: GitLab answers 200 with a
        # non-merged state and a `merge_error` (its approval rules said no), so
        # trusting the call's return would evict a still-open PR from the open list
        # and report success. The state change is applied only on a merge that
        # actually happened.
        if not result.get("merged"):
            raise routes.GhCliError(
                result.get("message")
                or "the provider did not merge this pull request (its rules were not satisfied)"
            )
        # A merge closes the PR, so it leaves the open list exactly as a close does.
        await routes._st(key, store.apply_pr_state_change_to_caches, owner, repo, number, "closed")
        return result

    if action == "auto_merge":
        result = await asyncio.to_thread(
            partial(client.enable_auto_merge, owner, repo, number, method, **pkw)
        )
        await routes._st(key, store.drop_pr_detail_cache, owner, repo, number)
        return result

    if action == "cancel_auto_merge":
        result = await asyncio.to_thread(
            partial(client.disable_auto_merge, owner, repo, number, **pkw)
        )
        await routes._st(key, store.drop_pr_detail_cache, owner, repo, number)
        return result

    if action == "cancel_run":
        result = await asyncio.to_thread(
            partial(client.cancel_workflow_run, owner, repo, run_id, **pkw)
        )
        await routes._st(key, store.drop_pr_detail_cache, owner, repo, number)
        return result

    if action == "rerun_run":
        result = await asyncio.to_thread(
            partial(
                client.rerun_workflow_run,
                owner,
                repo,
                run_id,
                failed_only=failed_only,
                **pkw,
            )
        )
        await routes._st(key, store.drop_pr_detail_cache, owner, repo, number)
        return result

    raise ValueError(f"unknown pull-request action: {action!r}")


def _pr_number_field(body: dict) -> tuple[int, web.Response | None]:
    """A bounded positive PR number from a request BODY.

    The body counterpart to ``_parse_item_number`` (query strings) and
    ``_pr_numbers_field`` (arrays). Factored for the same reason
    ``_pr_action_preamble`` is: five verbatim copies of a validation block is how
    one of them eventually ships without the bound.
    """
    from .. import routes  # circular import: backend.routes imports this module

    number = body.get("number")
    # bool is a subclass of int: JSON `true` would otherwise validate as #1.
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        return 0, web.json_response(
            {"error": "'number' must be a positive integer", "code": "invalid_number"},
            status=400,
        )
    if number > routes.MAX_ITEM_NUMBER:
        return 0, web.json_response(
            {
                "error": f"pull-request number out of range (max {routes.MAX_ITEM_NUMBER})",
                "code": "number_out_of_range",
            },
            status=400,
        )
    return number, None


def _pr_head_sha_field(body: dict) -> tuple[str, web.Response | None]:
    """The REQUIRED head commit a pinned action was formed against.

    Required for both the merge and the review verbs, and for the same reason: each
    is a statement about a REVISION. Without the pin, a force-push between the render
    and the click makes an approval apply to code the reviewer never saw and a merge
    land a commit nobody read. Validated here as well as in the clients so the caller
    gets a 400 rather than a 502 relayed from the provider.
    """
    from .. import routes  # circular import: backend.routes imports this module

    head_sha = routes._str_field(body, "head_sha")
    if not _HEAD_SHA_RE.match(head_sha):
        return "", web.json_response(
            {
                "error": "'head_sha' is required — the action is pinned to the commit "
                "you reviewed",
                "code": "head_sha_required",
            },
            status=400,
        )
    return head_sha, None


def _pr_merge_method_field(body: dict, key: provider.RepoKey) -> tuple[str, web.Response | None]:
    """A validated merge method, from the KEY's OWN provider client.

    Read off ``provider.client_for(key)`` rather than ``github_client`` directly:
    the two providers' tuples happen to be identical today, so reaching for the
    GitHub one worked by coincidence and would silently mis-validate the moment a
    provider accepted a different set.
    """
    from .. import routes  # circular import: backend.routes imports this module

    methods = provider.client_for(key).PR_MERGE_METHODS  # type: ignore[attr-defined]
    method = (routes._str_field(body, "method") or "SQUASH").upper()
    if method not in methods:
        return "", web.json_response(
            {
                "error": "method must be one of " f"{', '.join(m.lower() for m in methods)}",
                "code": "invalid_merge_method",
            },
            status=400,
        )
    return method, None


async def _handle_pull_state(request: web.Request) -> web.Response:
    """POST /pull/state {"owner","repo","number","state"} — close or reopen a PR.

    Routed through the provider's PULL endpoint rather than its issue endpoint, so
    a merged PR's un-reopenability is enforced by the provider instead of silently
    succeeding against the issue shadow (see github_client.set_pr_state)."""
    from .. import routes  # circular import: backend.routes imports this module

    body, key, early = await _pr_action_preamble(request, "pull_state")
    if early is not None:
        return early

    number, number_error = _pr_number_field(body)
    if number_error is not None:
        return number_error
    state = routes._str_field(body, "state").lower()
    if state not in ("open", "closed"):
        return web.json_response(
            {"error": "state must be 'open' or 'closed'", "code": "invalid_state"},
            status=400,
        )

    target = f"{key.slug}#{number}"
    action = "close" if state == "closed" else "reopen"
    try:
        result = await routes._run_pr_action(key, action, number)
    except routes.GhCliError as exc:
        return _pr_action_error("pull_state", target, exc)

    routes._audit("pull_state", f"{target}->{result.get('state', state)}", "ok")
    return web.json_response({**routes._identity(key), "number": number, **result})


async def _refuse_if_head_moved(
    key: provider.RepoKey,
    number: int,
    head_sha: str,
    op: str,
) -> web.Response | None:
    """409 when the PR's LIVE head is not the commit the caller reviewed.

    The check neither provider will do for us on a review. GitLab's ``/approve`` takes a
    real ``sha`` precondition, but GitHub's ``commit_id`` only ATTRIBUTES the review to a
    commit — it accepts one that is not the head, and whether the resulting stale
    approval still counts toward branch protection is a per-repo setting ("dismiss stale
    pull request approvals"). Where that is off, an unchecked approval satisfies
    protection on code nobody read. So the app reads the head itself, exactly as
    ``_handle_pull_merge`` does for the merge state.

    Returns ``None`` when the head still matches (or the provider did not report one —
    "unknown" must not become a refusal that blocks every review on that provider), and
    a ready-to-return 409 otherwise.

    A provider read FAILING is not treated as a conflict: it is relayed with its own
    taxonomy by the caller's ``except``, because "we could not check" and "the head
    moved" are different answers and the second is the one the user must act on.
    """
    from .. import routes  # circular import: backend.routes imports this module

    try:
        # ``resolve_mergeable=False``: this check reads ONLY ``head_sha``, which GitHub
        # returns eagerly on the first request. The default path would sleep 1.5s and
        # issue a SECOND call to resolve the lazy merge state (unknown on a cold read
        # for essentially every PR), and this runs once per verdict AND per row of a
        # bulk approve — so on a 50-PR approve it was ~75s of pure sleep plus ~50
        # redundant round-trips. Dropping the retry does not weaken the pin: the read
        # is still a LIVE read of the current head, which is all the 409 needs.
        detail = await asyncio.to_thread(
            partial(
                provider.client_for(key).get_pr_detail,
                key.owner,
                key.repo,
                number,
                resolve_mergeable=False,
                **provider.call_kwargs(key),
            )
        )
    except routes.GhCliError as exc:
        return _pr_action_error(op, f"{key.slug}#{number}", exc)
    live_sha = str(detail.get("head_sha") or "")
    if not live_sha or live_sha.lower() == head_sha.lower():
        return None
    routes._audit(
        op,
        f"{key.slug}#{number}",
        "denied",
        error=f"head moved: reviewed={head_sha} live={live_sha}",
    )
    return web.json_response(
        {
            "error": "The head branch moved since this page last read it — refresh and "
            "review the new commit.",
            "code": "review_conflict",
        },
        status=409,
    )


async def _handle_pull_review(request: web.Request) -> web.Response:
    """POST /pull/review {"owner","repo","number","event","body"?} — submit a review.

    ``event`` is ``approve`` / ``request_changes`` / ``comment``. Which of those a
    provider can honour differs — GitLab has no "request changes" verb — and the
    client refuses rather than approximating, so the error names the real
    limitation instead of recording a verdict the platform never stored.

    ``head_sha`` is REQUIRED, like ``/pull/merge``'s: a review is a verdict on a
    REVISION, and it rides to the provider as GitHub's ``commit_id`` / GitLab's
    ``/approve`` ``sha``.

    For a VERDICT (approve / request changes) this route also re-reads the PR's live
    head and refuses a moved one with **409 ``review_conflict``**, which is what
    actually closes the stale-approval hole. The provider parameters alone do NOT:
    GitLab's ``sha`` is a real precondition, but GitHub's ``commit_id`` is only
    ATTRIBUTION — GitHub accepts a review naming a non-head commit and records it
    there, and whether that stale approval still counts toward branch protection
    depends on the repo's "dismiss stale approvals" setting. So an unchecked approval
    could satisfy protection on code nobody read wherever dismissal is off. Same shape
    as ``_handle_pull_merge``: the app does the check it cannot delegate.

    A plain ``comment`` review skips the check — it records no verdict, so it stays
    valid prose about the PR no matter what the head does, and refusing it would only
    cost the user their typing."""
    from .. import routes  # circular import: backend.routes imports this module

    body, key, early = await _pr_action_preamble(request, "pull_review")
    if early is not None:
        return early

    number, number_error = _pr_number_field(body)
    if number_error is not None:
        return number_error
    event = routes._str_field(body, "event").lower()
    if event not in ("approve", "request_changes", "comment"):
        return web.json_response(
            {
                "error": "event must be 'approve', 'request_changes' or 'comment'",
                "code": "invalid_event",
            },
            status=400,
        )
    text, too_long = _pr_body_field(body)
    if too_long is not None:
        return too_long
    head_sha, sha_error = _pr_head_sha_field(body)
    if sha_error is not None:
        return sha_error

    target = f"{key.slug}#{number}"
    action = "comment_review" if event == "comment" else event
    # A VERDICT is refused when the head has moved. See the docstring: GitHub's
    # ``commit_id`` attributes the review to that commit but does not reject a stale
    # one, so without this an approval could satisfy branch protection on code nobody
    # read (wherever "dismiss stale approvals" is off). A `comment` carries no verdict
    # and is left alone.
    if event != "comment":
        conflict = await routes._refuse_if_head_moved(key, number, head_sha, "pull_review")
        if conflict is not None:
            return conflict
    try:
        result = await routes._run_pr_action(key, action, number, body=text, head_sha=head_sha)
    except routes.GhCliError as exc:
        return _pr_action_error("pull_review", target, exc)

    routes._audit("pull_review", f"{target}:{event}", "ok")
    return web.json_response({**routes._identity(key), "number": number, **result})


async def _handle_pull_comment(request: web.Request) -> web.Response:
    """POST /pull/comment {"owner","repo","number","body"} — post a conversation
    comment on a PR (or an issue: the same endpoint serves both on GitHub, and the
    GitLab client is told which collection to use)."""
    from .. import routes  # circular import: backend.routes imports this module

    body, key, early = await _pr_action_preamble(request, "pull_comment")
    if early is not None:
        return early

    number, number_error = _pr_number_field(body)
    if number_error is not None:
        return number_error
    text, too_long = _pr_body_field(body)
    if too_long is not None:
        return too_long
    if not text:
        return web.json_response(
            {"error": "'body' is required", "code": "body_required"}, status=400
        )

    target = f"{key.slug}#{number}"
    try:
        result = await routes._run_pr_action(key, "comment", number, body=text)
    except routes.GhCliError as exc:
        return _pr_action_error("pull_comment", target, exc)

    routes._audit("pull_comment", target, "ok")
    return web.json_response({**routes._identity(key), "number": number, **result})


async def _handle_pull_auto_merge(request: web.Request) -> web.Response:
    """POST /pull/auto-merge {"owner","repo","number","enabled","method"?} — arm or
    disarm the PROVIDER's own auto-merge. **GitHub only.**

    For the PR that is not mergeable YET: the provider lands it once ITS required
    reviews and checks pass. ``/pull/merge`` is the complement, for one that is
    mergeable now. A repo with no branch rule (or with 'Allow auto-merge' off) cannot
    arm auto-merge at all; the provider's own refusal text names which, and it is
    relayed verbatim through ``_pr_action_error`` rather than replaced with a guess.

    On GITLAB both verbs are REFUSED by the client, so this route only ever answers
    an error there: ``merge_when_pipeline_succeeds`` is a deferral modifier on the
    merge endpoint rather than an independent arm verb, and with no pipeline in
    flight GitLab merges the MR immediately — see
    ``gitlab_client.enable_auto_merge``. The UI hides both controls on GitLab, so
    this path is reached only by a direct API caller."""
    from .. import routes  # circular import: backend.routes imports this module

    body, key, early = await _pr_action_preamble(request, "pull_auto_merge")
    if early is not None:
        return early

    number, number_error = _pr_number_field(body)
    if number_error is not None:
        return number_error
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return web.json_response(
            {"error": "'enabled' must be a boolean", "code": "invalid_enabled"},
            status=400,
        )
    method, method_error = _pr_merge_method_field(body, key)
    if method_error is not None:
        return method_error

    target = f"{key.slug}#{number}"
    action = "auto_merge" if enabled else "cancel_auto_merge"
    try:
        result = await routes._run_pr_action(key, action, number, method=method)
    except routes.GhCliError as exc:
        return _pr_action_error("pull_auto_merge", target, exc)

    routes._audit("pull_auto_merge", f"{target}:{'on' if enabled else 'off'}", "ok")
    return web.json_response({**routes._identity(key), "number": number, **result})


# Provider merge-state values that mean "this PR's protections are SATISFIED".
#
# The distinction this encodes is the whole safety story of the direct merge, and it is
# easy to get wrong: GitHub's ``mergeable`` means only "no merge CONFLICTS". A PR whose
# required reviews or required checks have not passed is ``mergeable: true`` with
# ``mergeable_state: "blocked"``. For an ordinary user the merge endpoint then answers
# 405, so the provider is the backstop — but a repository ADMIN may hold
# bypass-branch-protection, and for them the provider would honour the merge. Gating on
# ``mergeable`` alone therefore offered a privileged user a one-click way to land a PR
# its own rules had rejected.
#
#   clean      — GitHub: mergeable, protections satisfied
#   has_hooks  — GitHub: clean, with pre-receive hooks configured
#   mergeable  — GitLab `detailed_merge_status`: no conflicts AND approval rules,
#                blocking discussions and required pipelines all satisfied
#
# `unstable` is deliberately EXCLUDED. It is usually described as "only non-required
# checks are failing", but the state does not actually distinguish a failing REQUIRED
# check from a failing optional one, so it cannot prove the protections are satisfied.
# For an ordinary
# user the provider would refuse anyway; for the admin this gate exists to protect
# against, allowing it would land code over a red required check. A gate that cannot
# tell must refuse: the PR is still one click from `auto_merge`, which lets the provider
# decide once the checks finish.
#
# GitLab's LEGACY `can_be_merged` is excluded for exactly that reason, and it is the
# subtler case. `_norm_pull` falls back to the old `merge_status` field when
# `detailed_merge_status` is absent (a pre-16.x server, or a payload that omits it), and
# `merge_status` reports ONLY whether the branches conflict — it is GitLab's exact
# analogue of GitHub's `mergeable`, and knows nothing about unmet approvals, unresolved
# blocking discussions or a red required pipeline. Accepting it therefore reproduced the
# very hole this set exists to close, on the older servers least likely to be watched. A
# server that cannot say more than "no conflicts" gets the refusal and the auto-merge
# path, not the benefit of the doubt.
#
# Everything else is refused HERE rather than left to the provider: `blocked`
# (protections unsatisfied), `behind`, `dirty`, `draft`, `unknown`, and GitLab's
# `not_approved` / `discussions_not_resolved` / `ci_still_running` family.
_MERGE_ALLOWED_STATES = frozenset({"clean", "has_hooks", "mergeable"})


async def _handle_pull_merge(request: web.Request) -> web.Response:
    """POST /pull/merge {"owner","repo","number","method"?} — merge a PR now.

    Per-PR only; there is deliberately no bulk merge (see rule 2 in the section
    note). This does NOT bypass a gate: branch protection, required reviews and
    required checks are enforced by the provider on its own merge endpoint, so a PR
    that has not satisfied them comes back 405 and nothing is merged. The 405 is
    mapped to a message that says so, because "Method Not Allowed" on a merge button
    reads like a bug rather than like the repo's own policy answering.
    """
    from .. import routes  # circular import: backend.routes imports this module

    body, key, early = await _pr_action_preamble(request, "pull_merge")
    if early is not None:
        return early

    number, number_error = _pr_number_field(body)
    if number_error is not None:
        return number_error
    method, method_error = _pr_merge_method_field(body, key)
    if method_error is not None:
        return method_error
    # REQUIRED: the merge is pinned to the commit the client actually rendered, so a
    # push landing between the read and the click answers 409 instead of merging code
    # nobody reviewed.
    head_sha, sha_error = _pr_head_sha_field(body)
    if sha_error is not None:
        return sha_error

    # Read the PR's own merge state and refuse anything but a satisfied one. This is
    # the check that the provider CANNOT be relied on for: it 405s an ordinary user but
    # honours an admin with bypass-branch-protection, so "the provider adjudicates"
    # stops being true exactly for the account that can do the most damage.
    try:
        detail = await asyncio.to_thread(
            partial(
                provider.client_for(key).get_pr_detail,
                key.owner,
                key.repo,
                number,
                **provider.call_kwargs(key),
            )
        )
    except routes.GhCliError as exc:
        return _pr_action_error("pull_merge", f"{key.slug}#{number}", exc)
    state = str(detail.get("mergeable_state") or "").lower()
    if state not in _MERGE_ALLOWED_STATES:
        routes._audit(
            "pull_merge",
            f"{key.slug}#{number}",
            "denied",
            error=f"mergeable_state={state}",
        )
        return web.json_response(
            {
                "error": "This pull request is not ready to merge "
                f"(the provider reports it as '{state or 'unknown'}'). Arm "
                "auto-merge to land it once its required reviews and checks pass.",
                "code": "merge_not_ready",
            },
            status=409,
        )
    # Pin to the head the CALLER reviewed, and also refuse if it has moved since this
    # read — the state above describes that commit, not a newer one.
    live_sha = str(detail.get("head_sha") or "")
    if live_sha and live_sha.lower() != head_sha.lower():
        # Audited as `denied` like the readiness refusal above, NOT left silent: this
        # is the app refusing a merge, and it is the one branch in this handler that
        # is neither a provider error nor a validation 400. Without the record, a
        # query over the merge surface for refusals misses exactly the stale-head
        # case — the one worth noticing, because a repeated hit means someone is
        # racing a live branch.
        routes._audit(
            "pull_merge",
            f"{key.slug}#{number}",
            "denied",
            error=f"head moved: reviewed={head_sha} live={live_sha}",
        )
        return web.json_response(
            {
                "error": "The head branch moved since this page last read it — "
                "refresh and try again.",
                "code": "merge_conflict",
            },
            status=409,
        )

    target = f"{key.slug}#{number}"
    try:
        result = await routes._run_pr_action(key, "merge", number, method=method, head_sha=head_sha)
    except routes.GhPermissionError as exc:
        routes._audit("pull_merge", target, "denied", error=str(exc))
        return web.json_response({"error": str(exc), "code": "provider_forbidden"}, status=403)
    except routes.GhCliError as exc:
        message = str(exc)
        # 405 here is the repository's RULES speaking, not a broken request: the
        # provider refuses to merge a PR whose required reviews/checks are not
        # satisfied (or whose merge method the repo disallows). Saying that plainly
        # is the difference between "the app is broken" and "the PR is not ready".
        if "HTTP 405" in message or "405" in message.split()[:3]:
            routes._audit("pull_merge", target, "denied", error=message)
            return web.json_response(
                {
                    "error": "The provider refused to merge this — its required "
                    "reviews or checks are not satisfied, or the repository "
                    "does not allow this merge method. Arm auto-merge to "
                    "land it once they pass.",
                    "code": "merge_not_allowed",
                },
                status=409,
            )
        if "HTTP 409" in message:
            routes._audit("pull_merge", target, "failure", error=message)
            return web.json_response(
                {
                    "error": "The head branch moved since this page last read it — "
                    "refresh and try again.",
                    "code": "merge_conflict",
                },
                status=409,
            )
        return _pr_action_error("pull_merge", target, exc)

    routes._audit("pull_merge", target, "ok")
    return web.json_response({**routes._identity(key), "number": number, **result})


async def _handle_pull_runs(request: web.Request) -> web.Response:
    """GET /pull/runs?owner=&repo=&number=&sha= — the CI runs on a PR's head commit.

    A read, but it lives with the actions because it exists to make them safe: a
    run carries the id that cancel/re-run addresses, plus ``cancellable`` /
    ``rerunnable``, so the UI never offers an action the provider will refuse.
    Deliberately separate from ``/pull``'s ``checks``: a check is a per-job RESULT
    (and may come from a service with no runs at all), while cancelling acts on the
    parent RUN."""
    from .. import routes  # circular import: backend.routes imports this module

    key = routes._key_from_request(request)
    owner, repo = key.owner, key.repo
    sha = (request.query.get("sha") or "").strip()
    number_raw = (request.query.get("number") or "").strip()
    if not owner or not repo or not sha:
        return web.json_response(
            {"error": "missing ?owner=, ?repo= and ?sha=", "code": "missing_params"},
            status=400,
        )
    # The number is only echoed back (the runs are addressed by sha), but it is
    # still validated so a caller cannot get a response keyed to a bogus item.
    number, number_error = routes._parse_item_number(number_raw) if number_raw else (0, None)
    if number_error is not None:
        return number_error

    if not await asyncio.to_thread(routes._connected, key):
        return web.json_response(
            {
                "error": f"{owner}/{repo} is not connected — call /connect first",
                "code": "repo_not_connected",
            },
            status=404,
        )

    try:
        runs = await asyncio.to_thread(
            partial(
                provider.client_for(key).list_pr_workflow_runs,
                owner,
                repo,
                sha,
                **provider.call_kwargs(key),
            )
        )
    except routes.GhCliError as exc:
        # Fixed string, not `str(exc)`: the message is gh's stderr tail (command,
        # exit code, upstream body). Logged for us, withheld from the caller.
        logger.warning("issue-radar list_pr_workflow_runs provider error: %s", exc)
        return web.json_response(
            {"error": "upstream provider error", "code": "provider_error"}, status=502
        )
    return web.json_response({**routes._identity(key), "number": number, "runs": runs})


async def _handle_pull_run_action(request: web.Request) -> web.Response:
    """POST /pull/run {"owner","repo","number","run_id","action","failed_only"?} —
    cancel or re-run one CI run on a PR.

    ``action`` is ``cancel`` or ``rerun``. Gated on the same triage/push access as
    every other mutation: cancelling another contributor's CI is a write."""
    from .. import routes  # circular import: backend.routes imports this module

    body, key, early = await _pr_action_preamble(request, "pull_run")
    if early is not None:
        return early

    number, number_error = _pr_number_field(body)
    if number_error is not None:
        return number_error
    run_id = body.get("run_id")
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
        return web.json_response(
            {"error": "'run_id' must be a positive integer", "code": "invalid_run_id"},
            status=400,
        )
    # Bounded for the same reason every other number here is: it reaches a PATH
    # segment in the provider argv, and an unbounded int makes that segment
    # arbitrarily long. Run ids get their OWN, larger ceiling (MAX_RUN_ID) because
    # they are a global provider sequence rather than a per-repo one — see the
    # constant. Both are orders of magnitude below their bound.
    if run_id > MAX_RUN_ID:
        return web.json_response(
            {
                "error": f"run id out of range (max {MAX_RUN_ID})",
                "code": "run_id_out_of_range",
            },
            status=400,
        )
    action = routes._str_field(body, "action").lower()
    if action not in ("cancel", "rerun"):
        return web.json_response(
            {"error": "action must be 'cancel' or 'rerun'", "code": "invalid_action"},
            status=400,
        )
    failed_only = body.get("failed_only", False)
    if not isinstance(failed_only, bool):
        return web.json_response(
            {"error": "'failed_only' must be a boolean", "code": "invalid_failed_only"},
            status=400,
        )

    target = f"{key.slug}#{number}/run/{run_id}"
    try:
        result = await routes._run_pr_action(
            key,
            "cancel_run" if action == "cancel" else "rerun_run",
            number,
            run_id=run_id,
            failed_only=failed_only,
        )
    except routes.GhCliError as exc:
        return _pr_action_error("pull_run", target, exc)

    routes._audit("pull_run", f"{target}:{action}", "ok")
    return web.json_response({**routes._identity(key), "number": number, **result})


async def _handle_pulls_bulk(request: web.Request) -> web.Response:
    """POST /pulls/bulk {"owner","repo","numbers":[...],"action","body"?,"method"?} —
    apply ONE action to many pull requests.

    The list view's mass-action endpoint. ``action`` must be in
    :data:`_BULK_PR_ACTIONS` — a fixed allowlist, not a generic fan-out, so a
    future action does not silently become mass-appliable.

    Partial failure is EXPECTED and reported per PR (a locked, transferred, or
    already-merged PR fails on its own), so a batch is never rolled back over one
    row and the caller is never told about a write that did not happen. The PRs are
    processed SEQUENTIALLY: they share one provider rate limit, and a 50-wide
    parallel fan-out is how a bulk click turns into a secondary-rate-limit block
    that fails rows for no reason of their own."""
    from .. import routes  # circular import: backend.routes imports this module

    body, key, early = await _pr_action_preamble(request, "pulls_bulk")
    if early is not None:
        return early

    action = routes._str_field(body, "action").lower()
    if action not in _BULK_PR_ACTIONS:
        return web.json_response(
            {
                "error": f"action must be one of {', '.join(_BULK_PR_ACTIONS)}",
                "code": "invalid_action",
            },
            status=400,
        )
    numbers, numbers_error = _pr_numbers_field(body)
    if numbers_error is not None:
        return numbers_error
    text, too_long = _pr_body_field(body)
    if too_long is not None:
        return too_long
    if action == "comment" and not text:
        return web.json_response(
            {"error": "'body' is required for a bulk comment", "code": "body_required"},
            status=400,
        )
    method, method_error = _pr_merge_method_field(body, key)
    if method_error is not None:
        return method_error
    # A bulk REVIEW is N verdicts, so each one names the commit its row was rendered
    # at — the same pin the per-PR review carries, by number rather than by index so a
    # reordered selection cannot pair a sha with the wrong pull request. The other
    # bulk verbs act on the PR rather than on a revision and take no sha.
    head_shas: dict[int, str] = {}
    if action in _PINNED_BULK_PR_ACTIONS:
        head_shas, shas_error = _pr_head_shas_field(body, numbers)
        if shas_error is not None:
            return shas_error

    applied: list[dict] = []
    failed: list[dict] = []
    for number in numbers:
        target = f"{key.slug}#{number}"
        # Same stale-verdict refusal the per-PR review route makes, applied per ROW: a
        # bulk approve is N verdicts, and one of them landing on a force-pushed head is
        # exactly as wrong as one from the detail pane. Reported as that row's failure so
        # the rest of the batch still applies (and the row stays ticked for a retry after
        # a refresh), rather than aborting the whole request.
        if action in _PINNED_BULK_PR_ACTIONS:
            conflict = await routes._refuse_if_head_moved(
                key, number, head_shas.get(number, ""), "pulls_bulk"
            )
            if conflict is not None:
                failed.append(
                    {
                        "number": number,
                        "error": "the head branch moved since this list was read — refresh "
                        "and review the new commit",
                    }
                )
                continue
        try:
            result = await routes._run_pr_action(
                key,
                action,
                number,
                body=text,
                method=method,
                head_sha=head_shas.get(number, ""),
            )
        except routes.GhPermissionError as exc:
            # A single PR the session cannot touch is a per-row failure, not a reason
            # to abandon the rows that succeeded — but it is a permission DECISION and
            # must be audited as one. Collapsing it into "failure" (which an earlier
            # revision did, since GhPermissionError subclasses GhCliError) made a
            # refused mutation indistinguishable from a network timeout, so a query
            # for outcome=denied returned nothing for the whole bulk surface.
            routes._audit("pulls_bulk", f"{target}:{action}", "denied", error=str(exc))
            failed.append({"number": number, "error": str(exc)})
            continue
        except routes.GhCliError as exc:
            routes._audit("pulls_bulk", f"{target}:{action}", "failure", error=str(exc))
            failed.append({"number": number, "error": str(exc)})
            continue
        routes._audit("pulls_bulk", f"{target}:{action}", "ok")
        applied.append({"number": number, **result})

    return web.json_response(
        {
            **routes._identity(key),
            "action": action,
            "applied": applied,
            "failed": failed,
        }
    )

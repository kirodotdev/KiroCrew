"""Every write Issue Radar makes on GitHub.

The JSON-stdin write runner, the triage writes (labels, state, assignees, label
creation) and the permission read that gates them, and the pull-request actions:
create, close or reopen, review, comment, merge and auto-merge, with the workflow
runs a PR's CI actions read, cancel and re-run. Callers reach every name here
through :mod:`kiro_crew.apps.builtins.issue_radar.backend.github_client`.
"""

from __future__ import annotations

import re
import time
from urllib.parse import quote

from . import github_normalization, github_transport
from .errors import sanitize_cli_stderr
from .github_client_cli import (
    GH_TIMEOUT_SEC,
    GhCliError,
    GhInvalidInputError,
    GhMergeRefusedError,
    GhPermissionError,
    _gh_run,
    _run_gh_api,
)
from .github_client_repo_reads import verify_repo_access

# ── write primitives (triage actions: label + state) ────────────────────────
#
# These are the ONLY mutating calls Issue Radar makes. Per the feature design,
# they are the write half of the "suggest → confirm" loop (accept an AI-suggested
# label, hand-pick a label, close/reopen a triaged issue) — deliberately NOT a
# GitHub client clone (no title/body edit, no label CRUD). Every one:
#   • is a list-argv subprocess (never ``shell=True``);
#   • coerces ``number`` to ``int`` before it reaches the path;
#   • sends its request body as JSON on stdin (``--input -``) so label names /
#     state reasons are DATA, never argv the shell could reinterpret;
#   • URL-encodes any value that must sit in the path (the label name on DELETE).
# The route layer additionally gates them on triage/push access and validates
# label names against the repo's real label set before calling in here.


def _run_gh_write(
    method: str, path: str, payload: dict | None = None, *, timeout: float = GH_TIMEOUT_SEC
) -> object:
    """Run ``gh api --method <METHOD> <path>`` (optionally with a JSON stdin body)
    and return the parsed JSON response (dict/list), or ``None`` on empty output.

    A non-zero exit whose stderr carries ``HTTP 403`` (or 401/404 on a write —
    GitHub returns 404 rather than 403 when the token cannot even see the write
    surface) is raised as :class:`GhPermissionError` so the route maps it to an
    HTTP 403 instead of a generic 502. ``payload`` is serialized to JSON and fed
    on stdin, never interpolated into argv.
    """
    return github_transport.run_write(
        method,
        path,
        payload,
        timeout=timeout,
        gh_run=_gh_run,
        sanitize=sanitize_cli_stderr,
    )


def _shape_labels(raw: object) -> list[dict]:
    """Normalize a GitHub label array (as returned by the labels endpoints) to
    the ``[{name, color, description}]`` shape the detail pane + caches use.
    Tolerates a non-list (returns ``[]``)."""
    return github_normalization.shape_labels(raw)


def get_repo_permissions(owner: str, repo: str, *, timeout: float = GH_TIMEOUT_SEC) -> dict:
    """Return the authenticated `gh` user's permission object for the repo
    (``{admin, maintain, push, triage, pull}``), or ``{}`` if GitHub omits it.

    Thin wrapper over :func:`verify_repo_access` (which already selects
    ``.permissions``); the write routes use it to gate on ``triage``/``push``."""
    perms = verify_repo_access(owner, repo, timeout=timeout).get("permissions")
    return perms if isinstance(perms, dict) else {}


def add_issue_labels(
    owner: str, repo: str, number: int, labels: list[str], *, timeout: float = GH_TIMEOUT_SEC
) -> list[dict]:
    """Add ``labels`` to an issue (``POST .../issues/{n}/labels``).

    GitHub is additive + idempotent here (already-present labels are no-ops) and
    returns the issue's FULL label set after the add, which is returned shaped.
    ``labels`` is sent as a JSON body on stdin, so names with spaces/specials
    (e.g. ``good first issue``) are safe."""
    data = _run_gh_write(
        "POST",
        f"repos/{owner}/{repo}/issues/{int(number)}/labels",
        {"labels": list(labels)},
        timeout=timeout,
    )
    return _shape_labels(data)


def remove_issue_label(
    owner: str, repo: str, number: int, label: str, *, timeout: float = GH_TIMEOUT_SEC
) -> list[dict] | None:
    """Remove ONE label from an issue (``DELETE .../issues/{n}/labels/{label}``).

    The label name is URL-encoded into the path (GitHub has no bulk-remove; it
    is one call per label). Returns the issue's remaining labels, shaped. A
    ``404`` (the label was not on the issue) is idempotent success but the
    remaining set is unknown, so it returns ``None`` — the caller then re-reads
    the authoritative set rather than assuming the labels were cleared."""
    enc = quote(label, safe="")
    try:
        data = _run_gh_write(
            "DELETE",
            f"repos/{owner}/{repo}/issues/{int(number)}/labels/{enc}",
            None,
            timeout=timeout,
        )
    except GhCliError as exc:
        if "HTTP 404" in str(exc) or "Label does not exist" in str(exc):
            return None
        raise
    return _shape_labels(data)


def set_issue_state(
    owner: str,
    repo: str,
    number: int,
    state: str,
    state_reason: str | None = None,
    *,
    timeout: float = GH_TIMEOUT_SEC,
) -> dict:
    """Close or reopen an issue (``PATCH .../issues/{n}``).

    ``state`` is ``"open"`` or ``"closed"``. On close, ``state_reason`` may be
    ``"completed"`` (default) or ``"not_planned"``; on reopen the reason is
    cleared. Returns ``{"state", "state_reason"}`` from the updated issue."""
    payload: dict[str, object] = {"state": state}
    if state == "closed":
        payload["state_reason"] = state_reason or "completed"
    else:
        # Reopen: clear any prior close reason (GitHub accepts null here).
        payload["state_reason"] = None
    data = _run_gh_write(
        "PATCH", f"repos/{owner}/{repo}/issues/{int(number)}", payload, timeout=timeout
    )
    if isinstance(data, dict):
        return {"state": data.get("state", state), "state_reason": data.get("state_reason")}
    return {"state": state, "state_reason": payload.get("state_reason")}


def set_issue_assignees(
    owner: str, repo: str, number: int, assignees: list[str], *, timeout: float = GH_TIMEOUT_SEC
) -> list[str]:
    """REPLACE an issue's assignees with ``assignees`` (``PATCH .../issues/{n}``).

    The set is REPLACED, not merged: GitHub's ``PATCH`` with an ``assignees``
    array makes the given list authoritative (an empty list clears every
    assignee), which is exactly what a "pick the final set" editor wants and
    avoids the add/remove ordering races the labels path has to guard against.

    An unassignable login is REJECTED, not ignored. Verified against the live API:
    a login with no access to the repo -- whether it names no GitHub account at all
    or a real account that simply is not a collaborator -- answers HTTP 422 and
    applies NONE of the request, including the logins that were valid. That is
    raised as :class:`GhInvalidInputError` carrying the refused logins so the route
    can answer 400 and name them; reporting it as a 502 would blame the forge for
    the user's choice, and reporting success would show an assignee the issue does
    not carry.

    The resulting set is still read back from the RESPONSE rather than echoed from
    the request, because a successful write is not required to be an exact echo
    (GitLab Free silently keeps only the first assignee -- see
    gitlab_client.set_issue_assignees) and the response is the only authority on
    what the issue now carries.

    ``assignees`` ride in a JSON stdin body (never argv). Returns the issue's
    authoritative assignee logins after the change."""
    try:
        data = _run_gh_write(
            "PATCH",
            f"repos/{owner}/{repo}/issues/{int(number)}",
            {"assignees": list(assignees)},
            timeout=timeout,
        )
    except GhPermissionError:
        raise  # 403 -> the route's permission branch, not an input problem
    except GhCliError as exc:
        # A 422 from THIS call can only be about the assignees, because the request
        # body carries exactly one field. That matters because the field name is not
        # reliably available to match on: gh's stderr tail often keeps only its own
        # summary line ("gh: Validation Failed (HTTP 422)") and drops the API body
        # that named ``"field":"assignees"``. Keying on the status alone is therefore
        # both sufficient and necessary here. Verified live: an unassignable login
        # produces exactly this message.
        if "422" in str(exc):
            raise GhInvalidInputError(
                "GitHub will not assign: "
                + ", ".join(assignees)
                + " -- an assignee must have access to the repository.",
                values=list(assignees),
            ) from exc
        raise
    if isinstance(data, dict):
        return [
            a["login"] for a in data.get("assignees", []) if isinstance(a, dict) and a.get("login")
        ]
    return []


def create_label(
    owner: str,
    repo: str,
    name: str,
    color: str = "888888",
    description: str = "",
    *,
    timeout: float = GH_TIMEOUT_SEC,
) -> dict:
    """Create a new label on the repo (``POST repos/{o}/{r}/labels``).

    ``color`` is a 6-hex string WITHOUT a leading ``#`` (GitHub's labels API
    rejects the ``#``); a leading ``#`` is stripped defensively. ``name`` /
    ``description`` ride in a JSON stdin body (never argv), so names with
    spaces/specials (e.g. ``good first issue``) are safe. The caller gates this
    on triage/push access; a 403/401 surfaces as :class:`GhPermissionError`.

    Idempotent: if the label already exists (GitHub 422) the existing label is
    re-read and returned rather than raising. Returns ``{name, color,
    description}`` (via :func:`_shape_labels`)."""
    hexcolor = (color or "").lstrip("#").strip() or "888888"
    payload = {"name": name, "color": hexcolor, "description": description or ""}
    try:
        data = _run_gh_write("POST", f"repos/{owner}/{repo}/labels", payload, timeout=timeout)
    except GhPermissionError:
        raise  # no write access — route maps to HTTP 403
    except GhCliError as exc:
        # 422 == label already exists: idempotent success. Re-read the existing
        # label so the returned color/description are truthful, not our request.
        if "HTTP 422" in str(exc) or "already_exists" in str(exc):
            existing = _run_gh_write(
                "GET", f"repos/{owner}/{repo}/labels/{quote(name, safe='')}", None, timeout=timeout
            )
            shaped = _shape_labels([existing] if isinstance(existing, dict) else [])
            return shaped[0] if shaped else dict(payload)
        raise
    shaped = _shape_labels([data] if isinstance(data, dict) else [])
    return shaped[0] if shaped else dict(payload)


# ── pull-request actions (the write surface the PR pane's buttons drive) ──────
#
# Everything below MUTATES a pull request or its CI. They are the actions a
# maintainer performs from GitHub's own PR page — close/reopen, approve, comment,
# enable auto-merge, cancel a workflow run — done in-app so a triage pass does not
# require a browser round-trip.
#
# Three properties are load-bearing for all of them:
#
#   * They go through :func:`_run_gh_write`, so a 403/401 becomes
#     :class:`GhPermissionError` and the route answers 403 rather than a generic
#     502, and every body rides on stdin as JSON (never argv).
#   * Every one is addressed by the PR/run NUMBER coerced with ``int()``, so no
#     caller-supplied text reaches a path segment.
#   * Merging is offered in TWO forms, and neither can bypass a gate. A direct
#     :func:`merge_pull_request` is a request GitHub itself adjudicates — branch
#     protection, required reviews and required checks are enforced SERVER-side and
#     an unsatisfied PR comes back 405, so the button cannot land unreviewed code.
#     :func:`enable_auto_merge` covers the other case: the PR is not mergeable YET
#     and should land by itself once its checks pass. Shipping only the second left
#     a repo with no branch rule (auto-merge is unavailable there) with no merge
#     path at all — see the note on :func:`merge_pull_request`.


def create_pull_request(
    owner: str,
    repo: str,
    head: str,
    base: str,
    title: str,
    body: str = "",
    *,
    draft: bool = False,
    timeout: float = GH_TIMEOUT_SEC,
) -> dict:
    """Open a pull request (``POST repos/{o}/{r}/pulls``).

    REST, not ``gh pr create``, and that is the point. The CLI takes the title as
    ``--title <text>`` and needs the body in a file — both of which put
    model-authored prose on an argv (or on disk) at the moment a crew opens its PR.
    Going through :func:`_run_gh_write` sends title AND body as JSON on stdin, so
    neither can be reinterpreted as a flag or an option value; it also inherits the
    403/401 → :class:`GhPermissionError` mapping, so a crew without push access gets
    a permission error instead of an opaque exit code.
    (``auto_improvement``'s ``pr_recipe.draft()`` is the CLI-based ancestor of this
    call; it is deliberately NOT reused — it also pushes the branch, writes a durable
    queue copy, and degrades to ``QUEUED:<fp>`` instead of raising.)

    ``head`` is a branch name on this repo, or ``owner:branch`` for a cross-fork PR.
    Neither it nor ``base`` is charset-validated, because unlike ``owner``/``repo``
    they never reach a path or an argv — they are values inside the JSON body.

    ``draft=True`` opens the PR as a draft. GitHub itself refuses a draft on a repo
    that does not allow them (422), so that policy is not second-guessed here.

    Returns ``{number, url, html_url, draft, state}``. ``url`` is the module's own
    spelling for the web link (every row here — issues, PRs, checks, comments — uses
    it), and ``html_url`` carries the same value under GitHub's REST name so a caller
    written against the API field does not silently read ``None``.
    """
    subject = (title or "").strip()
    if not subject:
        # GitHub 422s on an empty title; failing here makes it a clear error instead
        # of an API rejection the caller has to decode.
        raise GhCliError("a pull request needs a title")
    if not (head or "").strip() or not (base or "").strip():
        raise GhCliError(f"a pull request needs both head and base refs (got {head!r} → {base!r})")
    payload: dict[str, object] = {
        "title": subject,
        "head": head.strip(),
        "base": base.strip(),
        "body": body or "",
        "draft": bool(draft),
    }
    data = _run_gh_write("POST", f"repos/{owner}/{repo}/pulls", payload, timeout=timeout)
    if isinstance(data, dict):
        link = data.get("html_url")
        return {
            "number": data.get("number"),
            "url": link,
            "html_url": link,
            "draft": bool(data.get("draft", draft)),
            "state": data.get("state") or "open",
        }
    # No parseable response body: the POST did not fail (that would have raised), but
    # the PR cannot be identified. Reported as unknown rather than guessed — a caller
    # that recorded a fabricated number would address the wrong PR from then on.
    raise GhCliError(f"gh returned no pull request for {owner}/{repo} ({head} → {base})")


def set_pr_state(
    owner: str, repo: str, number: int, state: str, *, timeout: float = GH_TIMEOUT_SEC
) -> dict:
    """Close or reopen a PULL REQUEST (``PATCH .../pulls/{n}``).

    Deliberately NOT :func:`set_issue_state`, even though GitHub's issues
    endpoint accepts a PR number: closing through ``issues/{n}`` works but
    reopening does not report the PR-native fields the pane reads back
    (``draft``/``merged``), and a MERGED PR must not be reopenable at all —
    GitHub rejects that, and routing through the PR endpoint is what surfaces the
    rejection instead of silently succeeding against the issue shadow.

    Returns ``{state, merged, draft}`` from the updated PR.
    """
    if state not in ("open", "closed"):
        raise GhCliError(f"invalid PR state: {state!r}")
    data = _run_gh_write(
        "PATCH", f"repos/{owner}/{repo}/pulls/{int(number)}", {"state": state}, timeout=timeout
    )
    if isinstance(data, dict):
        return {
            "state": data.get("state", state),
            "merged": bool(data.get("merged", False)),
            "draft": bool(data.get("draft", False)),
        }
    return {"state": state, "merged": False, "draft": False}


# GitHub's review verbs. ``APPROVE`` and ``REQUEST_CHANGES`` are the two that
# carry a verdict; ``COMMENT`` leaves review prose with no verdict. Anything else
# is refused rather than passed through, so a typo cannot become an unintended
# approval.
PR_REVIEW_EVENTS = ("APPROVE", "REQUEST_CHANGES", "COMMENT")


def submit_pr_review(
    owner: str,
    repo: str,
    number: int,
    event: str,
    body: str = "",
    head_sha: str = "",
    *,
    timeout: float = GH_TIMEOUT_SEC,
) -> dict:
    """Submit a REVIEW on a PR (``POST .../pulls/{n}/reviews``).

    ``event`` is one of :data:`PR_REVIEW_EVENTS`. ``body`` is optional for
    ``APPROVE`` but REQUIRED by GitHub for ``REQUEST_CHANGES`` and ``COMMENT``,
    which is validated here so the failure is a clear 400 rather than a 422 from
    the API.

    ``head_sha`` is REQUIRED and rides as GitHub's ``commit_id``. Without it the
    review attaches to whatever the head is at the moment the request lands, so a
    force-push between the render and the click records an APPROVAL of code the
    reviewer never saw.

    **``commit_id`` is ATTRIBUTION, not a rejecting precondition** — unlike the
    ``sha`` parameter on :func:`merge_pull_request`, which GitHub really does check
    and 409s. GitHub accepts a review naming a commit that is not the head; it
    just records the review against that commit, and whether the stale approval still
    counts toward branch protection depends on the repo's
    "dismiss stale pull request approvals" setting. So the pin makes the verdict
    HONEST (it says which revision was reviewed, and a repo with dismissal on
    discards it) but it cannot by itself make a stale approval fail. The refusal is
    the ROUTE's job: ``routes._handle_pull_review`` re-reads the PR's live head and
    answers 409 before calling this, exactly as ``_handle_pull_merge`` does. Do not
    move that check in here — it needs a provider read the caller has already paid
    for, and both routes share the taxonomy.

    Note that GitHub refuses to let an author approve their OWN PR (422). That is
    not pre-checked here: the caller would have to fetch the PR's author to know,
    and the API's refusal is authoritative and already surfaces as an error.

    Returns ``{id, state, submitted_at}`` of the created review.
    """
    verb = (event or "").strip().upper()
    if verb not in PR_REVIEW_EVENTS:
        raise GhCliError(f"invalid review event: {event!r}")
    text = (body or "").strip()
    if verb in ("REQUEST_CHANGES", "COMMENT") and not text:
        raise GhCliError(f"a {verb} review requires a comment body")
    sha = (head_sha or "").strip()
    if not re.match(r"^[0-9a-fA-F]{7,64}$", sha):
        raise GhCliError(
            "refusing to review without the head commit it was read at " f"(got {head_sha!r})"
        )
    payload: dict[str, object] = {"event": verb, "commit_id": sha}
    if text:
        payload["body"] = text
    data = _run_gh_write(
        "POST", f"repos/{owner}/{repo}/pulls/{int(number)}/reviews", payload, timeout=timeout
    )
    if isinstance(data, dict):
        return {
            "id": data.get("id"),
            "state": data.get("state"),
            "submitted_at": data.get("submitted_at"),
        }
    return {"id": None, "state": verb, "submitted_at": None}


def add_issue_comment(
    owner: str, repo: str, number: int, body: str, *, timeout: float = GH_TIMEOUT_SEC
) -> dict:
    """Post a plain comment on a PR or issue (``POST .../issues/{n}/comments``).

    The issues endpoint is correct for both: a PR's conversation comments ARE
    issue comments on GitHub (only inline review comments live elsewhere), which
    is the same reason the timeline reader uses ``issues/{n}/timeline``.

    Returns ``{id, url, created_at}``.
    """
    text = (body or "").strip()
    if not text:
        raise GhCliError("a comment needs a body")
    data = _run_gh_write(
        "POST",
        f"repos/{owner}/{repo}/issues/{int(number)}/comments",
        {"body": text},
        timeout=timeout,
    )
    if isinstance(data, dict):
        return {
            "id": data.get("id"),
            "url": data.get("html_url"),
            "created_at": data.get("created_at"),
        }
    return {"id": None, "url": None, "created_at": None}


def add_pr_comment(
    owner: str, repo: str, number: int, body: str, *, timeout: float = GH_TIMEOUT_SEC
) -> dict:
    """Post a conversation comment on a PULL REQUEST.

    On GitHub this is literally :func:`add_issue_comment` — a PR's conversation
    comments ARE issue comments, drawn from one number sequence per repo. It exists
    as its own name because GitLab's issues and merge requests are separate
    collections with INDEPENDENT numbering, so a single shared entry point would be
    a silent way to comment on the wrong item there. Routes call the PR-specific
    function for a PR on both providers; here the two coincide.
    """
    return add_issue_comment(owner, repo, number, body, timeout=timeout)


def update_issue_comment(
    owner: str, repo: str, comment_id: int, body: str, *, timeout: float = GH_TIMEOUT_SEC
) -> dict:
    """EDIT an existing issue/PR comment (``PATCH .../issues/comments/{id}``).

    Addressed by COMMENT id, not by issue number — GitHub's comment endpoints are
    repo-scoped and flat (``issues/comments/{id}``, no ``/issues/{n}/`` segment),
    which is also why this one call serves a comment on an issue and on a PR alike.

    This exists for the crew claim ledger: a crew keeps ONE comment as its public
    record and rewrites it as work progresses, rather than appending a comment per
    heartbeat. Editing is what makes a 20-minute heartbeat acceptable — GitHub
    sends no notification for an edit, so a live claim does not spam every
    subscriber, whereas a fresh comment each cycle would.

    ``body`` is model-authored prose, so it rides through :func:`_run_gh_write` as
    JSON on stdin and never touches argv; ``comment_id`` is ``int()``-coerced
    before it reaches the path, so it cannot inject path segments.

    Returns ``{id, url, updated_at}``. ``updated_at`` rather than ``created_at``
    deliberately: on an edited comment ``created_at`` still reports the ORIGINAL
    post time, so it is the one field that cannot confirm the edit landed — and a
    reader using it would see a freshly-heartbeated claim as days stale.
    """
    text = (body or "").strip()
    if not text:
        # An empty edit is not a no-op — it would BLANK the claim ledger, leaving
        # the comment in place with nothing in it for either a human or the next
        # crew to read.
        raise GhCliError("a comment edit needs a body")
    data = _run_gh_write(
        "PATCH",
        f"repos/{owner}/{repo}/issues/comments/{int(comment_id)}",
        {"body": text},
        timeout=timeout,
    )
    if isinstance(data, dict):
        return {
            "id": data.get("id"),
            "url": data.get("html_url"),
            "updated_at": data.get("updated_at"),
        }
    return {"id": int(comment_id), "url": None, "updated_at": None}


# GitHub's merge methods, as accepted by the auto-merge mutation.
PR_MERGE_METHODS = ("MERGE", "SQUASH", "REBASE")


def _pr_node_id(owner: str, repo: str, number: int, *, timeout: float) -> str:
    """The GraphQL node id for a PR — required by the auto-merge mutations, which
    have no REST equivalent."""
    data = _run_gh_write("GET", f"repos/{owner}/{repo}/pulls/{int(number)}", None, timeout=timeout)
    node_id = data.get("node_id") if isinstance(data, dict) else None
    if not isinstance(node_id, str) or not node_id:
        raise GhCliError(f"could not resolve a node id for {owner}/{repo}#{int(number)}")
    return node_id


def _run_gh_graphql_mutation(mutation: str, variables: dict[str, str], *, timeout: float) -> dict:
    """Run a GraphQL mutation via ``gh api graphql`` and return ``.data``.

    Variables are passed with ``-F`` (never interpolated into the query text), and
    a permission failure is mapped to :class:`GhPermissionError` exactly as
    :func:`_run_gh_write` does — GraphQL reports authorization in the errors array
    with a 200 status, so the string check is on stdout as well as stderr.
    """
    return github_transport.run_graphql_mutation(
        mutation,
        variables,
        timeout=timeout,
        gh_run=_gh_run,
        sanitize=sanitize_cli_stderr,
    )


_ENABLE_AUTO_MERGE = (
    "mutation($pr:ID!,$method:PullRequestMergeMethod!){"
    " enablePullRequestAutoMerge(input:{pullRequestId:$pr, mergeMethod:$method}){"
    "  pullRequest{ number autoMergeRequest{ enabledAt mergeMethod } } } }"
)

_DISABLE_AUTO_MERGE = (
    "mutation($pr:ID!){"
    " disablePullRequestAutoMerge(input:{pullRequestId:$pr}){"
    "  pullRequest{ number autoMergeRequest{ enabledAt } } } }"
)


def merge_pull_request(
    owner: str,
    repo: str,
    number: int,
    method: str = "SQUASH",
    head_sha: str = "",
    *,
    timeout: float = GH_TIMEOUT_SEC,
) -> dict:
    """Merge a pull request now, through GitHub's asynchronous merge API.

    ``PUT .../pulls/{n}/merge-async`` accepts the request and merges in the
    background; this function then polls ``GET .../merge-async/{uuid}`` until the
    request settles or :data:`MERGE_ASYNC_WAIT_SEC` runs out. GitHub recommends it
    over the synchronous ``PUT .../merge``: the background job retries transient
    errors on a busy repository and is not bound by the synchronous endpoint's
    request timeout. It returns only once the merge has actually landed, so a
    caller merging several PRs in turn still sees each one on the base before the
    next starts.

    ``merge_action`` is ``direct_merge``: the button means "merge now", and a
    branch that requires a merge queue is reached through :func:`enable_auto_merge`
    instead, which hands the PR to the queue once it is ready. ``bypass_rules`` is
    sent as ``false`` explicitly, so even an account permitted to bypass the
    repository's rules does not do so from here.

    **This cannot bypass a gate, and that is why it is safe to offer.** Branch
    protection — required reviews, required status checks, required conversation
    resolution — is enforced by GitHub in the background merge, not by the caller:
    a PR whose rules are not satisfied settles as ``failed`` and nothing is merged.
    That, and the immediate 400 for a closed or draft PR, raise
    :class:`GhMergeRefusedError`, carrying GitHub's message for a ``failed``
    result (an immediate 400 carries only the status). A request GitHub is
    still working on when the wait runs out, or one that was already pending (409),
    comes back as ``{"merged": False, "pending": True}`` rather than as a merge. A
    pinned sha the head has moved past is a 400, so it is a refusal too.

    So the honest division of labour is:

    * this function — the PR is mergeable *now* and the operator says land it;
    * :func:`enable_auto_merge` — the PR is *not* mergeable yet and should land by
      itself once its checks pass.

    Shipping only the second is what an earlier revision did, on the theory that a
    direct merge could bypass review. It cannot; GitHub adjudicates. What that
    omission actually did was leave a repository with no branch rule — where
    auto-merge is simply unavailable — with **no merge path at all**, which is a
    worse outcome than the one it was guarding against.

    ``method`` is one of :data:`PR_MERGE_METHODS`; a repo that disallows the chosen
    method makes the merge settle as ``failed`` too, so the error is the repo's own
    policy speaking.

    ``head_sha`` is REQUIRED and is sent as GitHub's ``sha`` precondition, so the
    merge is pinned to the commit the caller actually looked at. Without it, a push
    landing between the read and the click merges code nobody reviewed — and on a repo
    with no branch protection there is nothing else to catch that, which is exactly
    the case this function exists to serve. A head that moves before the background
    merge runs makes GitHub cancel it rather than merge. It is a positional parameter
    with an empty default only so the two clients keep identical signatures; an empty
    value is refused here, not defaulted.

    Returns ``{merged, sha, message, pending}``.
    """
    verb = (method or "").strip().upper()
    if verb not in PR_MERGE_METHODS:
        raise GhCliError(f"invalid merge method: {method!r}")
    sha = (head_sha or "").strip()
    if not re.match(r"^[0-9a-fA-F]{7,64}$", sha):
        raise GhCliError(
            "refusing to merge without the head commit it was reviewed at " f"(got {head_sha!r})"
        )
    path = f"repos/{owner}/{repo}/pulls/{int(number)}/merge-async"
    try:
        data = _run_gh_write(
            "PUT",
            path,
            {
                "merge_method": verb.lower(),
                "sha": sha,
                "merge_action": "direct_merge",
                "bypass_rules": False,
            },
            timeout=timeout,
        )
    except GhPermissionError:
        raise
    except GhCliError as exc:
        message = str(exc)
        if "HTTP 400" in message:
            raise GhMergeRefusedError(message) from exc
        if "HTTP 409" in message:
            # Verified live: 409 means a merge request for this PR is already in
            # flight. A stale pinned sha answers 400 ("head branch was modified").
            return _merge_pending("GitHub is already merging this pull request.")
        raise

    deadline = time.monotonic() + MERGE_ASYNC_WAIT_SEC
    for delay in _MERGE_ASYNC_POLL_DELAYS:
        outcome = _merge_async_outcome(data)
        if outcome is not None:
            return outcome
        uuid = _merge_async_uuid(data)
        if not uuid:
            raise GhCliError(
                f"GitHub accepted the merge of {owner}/{repo}#{int(number)} "
                "but returned no request id to follow"
            )
        if time.monotonic() + delay > deadline:
            break
        time.sleep(delay)
        data = _run_gh_write("GET", f"{path}/{quote(uuid, safe='')}", None, timeout=timeout)
    outcome = _merge_async_outcome(data)
    if outcome is not None:
        return outcome
    return _merge_pending("GitHub is still merging this pull request.")


# How long merge_pull_request waits for GitHub's background merge to settle, and the
# gaps between polls. A merge that has not settled by then is reported as pending,
# never as merged.
MERGE_ASYNC_WAIT_SEC = 60.0
_MERGE_ASYNC_POLL_DELAYS = (1.0, 1.0, 2.0, 2.0, 3.0, 3.0) + (5.0,) * 10


def _merge_pending(message: str) -> dict:
    return {
        "merged": False,
        "pending": True,
        "sha": None,
        "message": f"{message} Refresh in a moment to see whether it landed.",
    }


def _merge_async_uuid(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    details = data.get("details")
    uuid = (details.get("uuid") if isinstance(details, dict) else None) or data.get("uuid")
    return uuid if isinstance(uuid, str) else ""


def _merge_async_outcome(data: object) -> dict | None:
    """The final result of an async merge response, or ``None`` while it is pending.

    ``merged`` returns the merge. ``failed`` raises :class:`GhMergeRefusedError`
    with GitHub's message. Any other status (``enqueued`` included, which a
    ``direct_merge`` request does not produce) raises an error naming it.
    """
    if not isinstance(data, dict):
        return None
    status = str(data.get("status") or "").lower()
    raw_details = data.get("details")
    details: dict = raw_details if isinstance(raw_details, dict) else {}
    message = str(details.get("message") or "")
    if status == "merged":
        sha = details.get("sha")
        return {"merged": True, "pending": False, "sha": sha, "message": message}
    if status == "failed":
        raise GhMergeRefusedError(
            sanitize_cli_stderr(message) or "GitHub did not merge this pull request."
        )
    if status and status != "pending":
        raise GhCliError(f"GitHub returned an unexpected merge status: {status!r}")
    return None


def enable_auto_merge(
    owner: str,
    repo: str,
    number: int,
    method: str = "SQUASH",
    *,
    timeout: float = GH_TIMEOUT_SEC,
) -> dict:
    """Arm GitHub's OWN auto-merge on a PR (GraphQL ``enablePullRequestAutoMerge``).

    For the PR that is not mergeable YET: GitHub merges it once the repository's
    required reviews and status checks pass. The complement of
    :func:`merge_pull_request`, which is for the PR that is mergeable now.

    Requires the repo to have 'Allow auto-merge' enabled and a branch rule, and
    GitHub also refuses to arm a PR that is already clean (there is nothing to wait
    for). Either way it answers with an error, which surfaces to the caller rather
    than being swallowed into a false success: GitHub's own text names the reason, and
    the route relays it verbatim rather than substituting a guess at which of the two
    causes applied.

    Returns ``{auto_merge: bool, method, enabled_at}``.
    """
    verb = (method or "").strip().upper()
    if verb not in PR_MERGE_METHODS:
        raise GhCliError(f"invalid merge method: {method!r}")
    node_id = _pr_node_id(owner, repo, number, timeout=timeout)
    data = _run_gh_graphql_mutation(
        _ENABLE_AUTO_MERGE, {"pr": node_id, "method": verb}, timeout=timeout
    )
    request = ((data.get("enablePullRequestAutoMerge") or {}).get("pullRequest") or {}).get(
        "autoMergeRequest"
    ) or {}
    # Derived from what came BACK, not asserted. A hardcoded True made the response a
    # claim rather than an observation: the only thing between a failed mutation and a
    # reported success was the errors-array check, and the equivalent shortcut on the
    # GitLab path is what let an immediate merge report itself as "armed".
    return {
        "auto_merge": bool(request),
        "method": request.get("mergeMethod") or (verb if request else None),
        "enabled_at": request.get("enabledAt"),
    }


def disable_auto_merge(
    owner: str, repo: str, number: int, *, timeout: float = GH_TIMEOUT_SEC
) -> dict:
    """Disarm GitHub's auto-merge on a PR (``disablePullRequestAutoMerge``).

    The inverse of :func:`enable_auto_merge`, so an accidental arm is reversible
    from the same place it was set. Returns ``{auto_merge: False}``."""
    node_id = _pr_node_id(owner, repo, number, timeout=timeout)
    _run_gh_graphql_mutation(_DISABLE_AUTO_MERGE, {"pr": node_id}, timeout=timeout)
    return {"auto_merge": False, "method": None, "enabled_at": None}


# One workflow run as the actions surface needs it: enough to name it, say
# whether it is still cancellable, and link out.
_WORKFLOW_RUN_JQ = (
    '.workflow_runs[] | {id: .id, name: (.name // .display_title // "workflow"), '
    "status: .status, conclusion: .conclusion, url: .html_url, "
    "event: (.event // null), created_at: .created_at, attempt: (.run_attempt // 1)}"
)

# A run in one of these states has not finished, so cancelling it is meaningful.
# Anything else (completed) can only be RE-RUN, never cancelled.
_RUN_CANCELLABLE_STATES = frozenset(
    {
        "queued",
        "in_progress",
        "waiting",
        "requested",
        "pending",
    }
)


def list_pr_workflow_runs(
    owner: str, repo: str, sha: str, *, timeout: float = GH_TIMEOUT_SEC
) -> list[dict]:
    """The GitHub Actions runs for a PR's head commit.

    Separate from :func:`list_pr_checks` because a CHECK is not a RUN: the checks
    surface reports per-job results (and merges in commit statuses from services
    that have no runs at all), while cancelling or re-running is an operation on
    the parent workflow RUN and needs its id. Asking for the runs by head sha is
    what ties them to the PR without a second PR round-trip.

    ``sha`` is charset-validated before it reaches the path, as in
    :func:`list_pr_checks`. Each row carries ``cancellable``/``rerunnable`` so the
    UI never offers an action GitHub will refuse.
    """
    if not re.match(r"^[0-9a-fA-F]{7,64}$", sha or ""):
        raise GhCliError(f"invalid commit sha: {sha!r}")
    rows = _run_gh_api(
        f"repos/{owner}/{repo}/actions/runs?head_sha={sha}&per_page=100",
        _WORKFLOW_RUN_JQ,
        timeout=timeout,
        paginate=False,
    )
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        status = str(row.get("status") or "")
        out.append(
            {
                **row,
                "cancellable": status in _RUN_CANCELLABLE_STATES,
                # A finished run can be re-run; an in-flight one cannot.
                "rerunnable": status == "completed",
            }
        )
    return out


def cancel_workflow_run(
    owner: str, repo: str, run_id: int, *, timeout: float = GH_TIMEOUT_SEC
) -> dict:
    """Cancel one in-flight Actions run (``POST .../actions/runs/{id}/cancel``).

    GitHub answers 202 with an empty body, so success is the ABSENCE of an error
    rather than anything in the response. A run that already finished answers 409;
    that is reported as an error rather than a no-op success, because "cancel"
    silently doing nothing is indistinguishable from it having worked.

    Returns ``{run_id, cancelled: True}``.
    """
    _run_gh_write(
        "POST", f"repos/{owner}/{repo}/actions/runs/{int(run_id)}/cancel", None, timeout=timeout
    )
    return {"run_id": int(run_id), "cancelled": True}


def rerun_workflow_run(
    owner: str,
    repo: str,
    run_id: int,
    *,
    failed_only: bool = False,
    timeout: float = GH_TIMEOUT_SEC,
) -> dict:
    """Re-run a completed Actions run, or only its failed jobs.

    ``failed_only`` selects ``/rerun-failed-jobs``, which is the cheaper and more
    common intent after a flake. Returns ``{run_id, rerun: True, failed_only}``.
    """
    verb = "rerun-failed-jobs" if failed_only else "rerun"
    _run_gh_write(
        "POST", f"repos/{owner}/{repo}/actions/runs/{int(run_id)}/{verb}", None, timeout=timeout
    )
    return {"run_id": int(run_id), "rerun": True, "failed_only": bool(failed_only)}

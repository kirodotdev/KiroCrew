"""Owner-authenticated pull-request writes: review threads, comments, auto-merge, ready.

Each write refuses an issue ref, dispatches a registered provider to its own hook,
proves a browser-supplied thread id belongs to the pull request in the URL, reads
the provider first so an inapplicable request is refused before anything is sent,
and invalidates both caches BEFORE dispatch -- once a provider call starts its
remote outcome is unknown under cancellation.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from kiro_crew.dashboard.source_providers import cache, github, hosts, links, plugins, runner
from kiro_crew.dashboard.source_providers.contract import (
    ConfirmationRequired,
    SourceProviderError,
    SourceRef,
)

_GITHUB_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_=+-]{1,128}$")
_GITLAB_THREAD_ID_RE = re.compile(r"^[A-Fa-f0-9]{1,128}$")

_GITHUB_RESOLVE_MUTATION = (
    "mutation($threadId:ID!){resolveReviewThread(input:{threadId:$threadId})"
    "{thread{isResolved}}}"
)

_GITHUB_UNRESOLVE_MUTATION = (
    "mutation($threadId:ID!){unresolveReviewThread(input:{threadId:$threadId})"
    "{thread{isResolved}}}"
)

_GITHUB_THREAD_REPLY_MUTATION = (
    "mutation($threadId:ID!,$body:String!)"
    "{addPullRequestReviewThreadReply"
    "(input:{pullRequestReviewThreadId:$threadId,body:$body})"
    "{comment{id}}}"
)

# Comment bodies are user text passed as a single CLI argument (argv, never a
# shell string), so the only real risk is size. GitHub rejects bodies past 65536
# characters anyway, so refusing here turns a provider error into a clear local
# one and bounds the argument.
_MAX_COMMENT_CHARS = 65536


def _validated_comment_body(body: str) -> str:
    """Return a comment body that is safe and worth sending.

    Empty bodies are refused rather than posted: an accidental empty comment is
    visible to everyone on the pull request and cannot be removed from here.
    """
    text = (body or "").strip()
    if not text:
        raise ValueError("A comment body is required.")
    if len(text) > _MAX_COMMENT_CHARS:
        raise ValueError(f"A comment body must be at most {_MAX_COMMENT_CHARS} characters.")
    # The body is passed to the provider CLI as an argument, which cannot carry
    # a NUL byte; refuse it here so the request fails before anything runs.
    if "\x00" in text:
        raise ValueError("A comment body cannot contain a NUL character.")
    return text


# Node ids are provider-issued, but they are interpolated into a CLI argument,
# so they get the same shape check as review-thread ids before dispatch.
_GITHUB_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_=+-]{1,128}$")

_GITHUB_PULL_REQUEST_NODE_QUERY = (
    "query($owner:String!,$repo:String!,$number:Int!){repository(owner:$owner,name:$repo)"
    "{squashMergeAllowed mergeCommitAllowed rebaseMergeAllowed"
    " pullRequest(number:$number){id isDraft state autoMergeRequest{enabledAt}}}}"
)

_GITHUB_AUTO_MERGE_MUTATION = (
    "mutation($pullRequestId:ID!,$mergeMethod:PullRequestMergeMethod!)"
    "{enablePullRequestAutoMerge(input:{pullRequestId:$pullRequestId,mergeMethod:$mergeMethod})"
    "{pullRequest{autoMergeRequest{enabledAt}}}}"
)

_GITHUB_READY_MUTATION = (
    "mutation($pullRequestId:ID!)"
    "{markPullRequestReadyForReview(input:{pullRequestId:$pullRequestId})"
    "{pullRequest{isDraft}}}"
)

# GitHub merge methods in the order this dashboard prefers them, gated on what
# the repository actually allows: enabling auto-merge with a disallowed method
# fails, and the repository's allow-list is the only machine-readable signal
# GitHub exposes (there is no "default method" field in the API).
_GITHUB_MERGE_METHODS: tuple[tuple[str, str], ...] = (
    ("squashMergeAllowed", "SQUASH"),
    ("mergeCommitAllowed", "MERGE"),
    ("rebaseMergeAllowed", "REBASE"),
)

# GitLab stores draft state as a title prefix, but exposes a dedicated
# mutation that performs the transition itself. Using it keeps the prefix
# grammar (Draft:/[WIP]/...) the provider's problem and avoids a read-modify-
# write of the title, which would clobber a concurrent retitle and could
# mangle titles that merely start with a draft-like word ("Drafting widgets").
_GITLAB_SET_DRAFT_MUTATION = (
    "mutation($projectPath:ID!,$iid:String!,$draft:Boolean!)"
    "{mergeRequestSetDraft(input:{projectPath:$projectPath,iid:$iid,draft:$draft})"
    "{errors mergeRequest{draft}}}"
)


def _raise_on_graphql_errors(payload: Any, message: str) -> None:
    """Raise when a GraphQL response carries errors instead of a mutation result.

    GraphQL reports refusals in the body with HTTP 200, so a provider CLI that
    only fails on transport errors would let a rejected mutation look like a
    success. Both the transport-level ``errors`` array and the per-mutation
    ``errors`` field are checked, since GitLab uses the latter.
    """
    if not isinstance(payload, dict):
        raise SourceProviderError(message)
    if payload.get("errors"):
        raise SourceProviderError(message)
    data = payload.get("data")
    if not isinstance(data, dict):
        return
    for result in data.values():
        if isinstance(result, dict) and result.get("errors"):
            raise SourceProviderError(message)


def _github_repository_node(payload: Any) -> dict[str, Any]:
    """Extract the repository node from a GraphQL pull-request node response."""
    data = payload.get("data") if isinstance(payload, dict) else None
    repository = data.get("repository") if isinstance(data, dict) else None
    return repository if isinstance(repository, dict) else {}


async def _github_pull_request_node(ref: SourceRef) -> tuple[str, dict[str, Any]]:
    """Return the pull request's validated node id plus its repository node."""
    payload = await runner._run_json(
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={_GITHUB_PULL_REQUEST_NODE_QUERY}",
        "-f",
        f"owner={ref.owner}",
        "-f",
        f"repo={ref.repo}",
        "-F",
        f"number={ref.number}",
    )
    repository = _github_repository_node(payload)
    pull_request = repository.get("pullRequest")
    pull_request = pull_request if isinstance(pull_request, dict) else {}
    node_id = str(pull_request.get("id") or "")
    if not _GITHUB_NODE_ID_RE.fullmatch(node_id):
        raise SourceProviderError("GitHub did not return a usable pull-request id")
    return node_id, repository


async def _github_thread_ref(raw_url: str, thread_id: str) -> SourceRef:
    """Validate a thread id AND prove it belongs to the pull request in the url.

    The ownership check is the security control: the thread id arrives from the
    browser, and without it an owner-authenticated mutation could be steered at a
    thread on an unrelated pull request. Shared by the GitHub branch of
    reply/unresolve so no future GitHub call site can skip it. GitLab does not
    come through here: its writes go to the merge request's own scoped
    `discussions/<id>` path, which refuses a foreign id. Registered providers
    never reach it either -- both callers dispatch a plugin ref to its own hook
    first -- so the plugin branch below is a fail-closed backstop for a future
    call site that forgets that dispatch, not a live path.
    """
    await hosts.ensure_gitlab_hosts_loaded()
    # A future call site that forgets the per-provider dispatch must still fail
    # closed, so the kind check belongs here too — not only in the callers.
    ref = links._require_change_ref(links.parse_source_url(raw_url))
    if plugins._plugin_for_change(ref) is not None:
        raise ValueError(
            f"Review-thread operations are not supported by the '{ref.provider}' source provider."
        )
    if ref.provider != "github":
        raise ValueError("Replying to review threads is only supported on GitHub so far.")
    if not _GITHUB_THREAD_ID_RE.fullmatch(thread_id or ""):
        raise ValueError("A valid thread id is required.")
    threads = await runner._run_json(
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={github._GITHUB_REVIEW_THREADS_QUERY}",
        "-f",
        f"owner={ref.owner}",
        "-f",
        f"repo={ref.repo}",
        "-F",
        f"number={ref.number}",
    )
    if thread_id not in github._github_thread_ids(threads):
        raise ValueError("Review thread does not belong to this pull request.")
    return ref


async def reply_to_review_thread(raw_url: str, thread_id: str, body: str) -> None:
    """Post a reply into an existing review thread."""
    text = _validated_comment_body(body)
    # Refresh the self-managed GitLab allowlist off the event loop before any
    # URL validation reads the cached snapshot.
    await hosts.ensure_gitlab_hosts_loaded()
    ref = links._require_change_ref(links.parse_source_url(raw_url))
    hook = plugins._require_plugin_hook(ref, "reply_to_thread", "Replying to review threads")
    if hook is not None:
        # The thread id is the plugin's own vocabulary: ownership and shape
        # validation are the hook's job, exactly as on the resolve path.
        await cache._invalidate_pull_request_cache(ref.url)
        with plugins._plugin_errors(ref.provider):
            await hook(ref, thread_id, text)
        return
    if ref.provider == "gitlab":
        # Shape check only: the id reaches an argv. Ownership needs no read-back
        # here, because the write itself is the merge request's own scoped
        # `discussions/<id>/notes` path, which refuses a foreign id.
        if not _GITLAB_THREAD_ID_RE.fullmatch(thread_id or ""):
            raise ValueError("A valid thread id is required.")
        project = quote(ref.project, safe="")
        await cache._invalidate_pull_request_cache(ref.url)
        # `-f` is glab's raw string field: a body starting with `@` is sent as
        # text, never read as a file path the way `-F` would.
        await runner._run_json(
            "glab",
            "api",
            "-X",
            "POST",
            f"projects/{project}/merge_requests/{ref.number}/discussions/{thread_id}/notes",
            "-f",
            f"body={text}",
            host=ref.host,
        )
        return
    ref = await _github_thread_ref(raw_url, thread_id)
    # Invalidate before dispatch, matching resolve: once the provider call
    # starts its remote result is uncertain under cancellation, so a stale
    # generation must already be unable to satisfy the post-write refresh.
    await cache._invalidate_pull_request_cache(ref.url)
    payload = await runner._run_json(
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={_GITHUB_THREAD_REPLY_MUTATION}",
        "-f",
        f"threadId={thread_id}",
        "-f",
        f"body={text}",
    )
    _raise_on_graphql_errors(payload, "could not post the reply")


async def unresolve_pull_request_thread(raw_url: str, thread_id: str) -> None:
    """Reopen a resolved review thread."""
    # Refresh the self-managed GitLab allowlist off the event loop before any
    # URL validation reads the cached snapshot.
    await hosts.ensure_gitlab_hosts_loaded()
    ref = links._require_change_ref(links.parse_source_url(raw_url))
    # Reopen is the same provider capability as resolve, so it dispatches to the
    # same hook with `resolved=False` -- one hook, one capability flag
    # (`resolveThreads`), no way for a plugin to support one direction only.
    hook = plugins._require_plugin_hook(ref, "resolve_thread", "Reopening review threads")
    if hook is not None:
        await cache._invalidate_pull_request_cache(ref.url)
        with plugins._plugin_errors(ref.provider):
            await hook(ref, thread_id, resolved=False)
        return
    if ref.provider == "gitlab":
        # Mirrors resolve: shape check only, because the id reaches an argv. The
        # merge request's own scoped path refuses a foreign or unresolvable id.
        if not _GITLAB_THREAD_ID_RE.fullmatch(thread_id or ""):
            raise ValueError("A valid thread id is required.")
        project = quote(ref.project, safe="")
        await cache._invalidate_pull_request_cache(ref.url)
        # Same endpoint and field as resolve, with the opposite value.
        await runner._run_json(
            "glab",
            "api",
            "-X",
            "PUT",
            f"projects/{project}/merge_requests/{ref.number}/discussions/{thread_id}",
            "-f",
            "resolved=false",
            host=ref.host,
        )
        return
    ref = await _github_thread_ref(raw_url, thread_id)
    await cache._invalidate_pull_request_cache(ref.url)
    payload = await runner._run_json(
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={_GITHUB_UNRESOLVE_MUTATION}",
        "-f",
        f"threadId={thread_id}",
    )
    _raise_on_graphql_errors(payload, "could not reopen the thread")


async def comment_on_pull_request(raw_url: str, body: str) -> None:
    """Post a top-level comment on the pull request itself (not a thread)."""
    text = _validated_comment_body(body)
    await hosts.ensure_gitlab_hosts_loaded()
    # Issue refs are refused here for the same reason the thread mutations refuse
    # them: this posts to /issues/{number}/comments, and on GitHub issues and pull
    # requests share one number counter, so an issue URL would publish a comment
    # on an unrelated issue that happens to carry the PR's number.
    ref = links._require_change_ref(links.parse_source_url(raw_url))
    hook = plugins._require_plugin_hook(ref, "comment", "Commenting")
    if hook is not None:
        await cache._invalidate_pull_request_cache(ref.url)
        with plugins._plugin_errors(ref.provider):
            await hook(ref, text)
        return
    if ref.provider == "gitlab":
        # A merge request's own notes endpoint: the iid namespace is the merge
        # request's, so unlike GitHub there is no issue with the same number to
        # land on by mistake. The issue-ref refusal above still applies.
        project = quote(ref.project, safe="")
        await cache._invalidate_pull_request_cache(ref.url)
        await runner._run_json(
            "glab",
            "api",
            "-X",
            "POST",
            f"projects/{project}/merge_requests/{ref.number}/notes",
            "-f",
            f"body={text}",
            host=ref.host,
        )
        return
    if ref.provider != "github":
        raise ValueError("Commenting is only supported on GitHub and GitLab.")
    await cache._invalidate_pull_request_cache(ref.url)
    # Issue comments, because a pull request's conversation timeline IS its issue
    # timeline; the review-comment endpoints require a diff position.
    await runner._run_json(
        "gh",
        "api",
        "-X",
        "POST",
        f"repos/{ref.owner}/{ref.repo}/issues/{ref.number}/comments",
        "-f",
        f"body={text}",
    )


async def resolve_pull_request_thread(raw_url: str, thread_id: str) -> None:
    """Resolve a review thread after conservatively invalidating cached data."""
    # Refresh the self-managed GitLab allowlist off the event loop before any
    # URL validation reads the cached snapshot.
    await hosts.ensure_gitlab_hosts_loaded()
    ref = links._require_change_ref(links.parse_source_url(raw_url))
    hook = plugins._require_plugin_hook(ref, "resolve_thread", "Resolving review threads")
    if hook is not None:
        await cache._invalidate_pull_request_cache(ref.url)
        with plugins._plugin_errors(ref.provider):
            await hook(ref, thread_id, resolved=True)
        return
    thread_pattern = _GITHUB_THREAD_ID_RE if ref.provider == "github" else _GITLAB_THREAD_ID_RE
    if not thread_pattern.fullmatch(thread_id or ""):
        raise ValueError("A valid thread id is required.")
    if ref.provider == "github":
        threads = await runner._run_json(
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={github._GITHUB_REVIEW_THREADS_QUERY}",
            "-f",
            f"owner={ref.owner}",
            "-f",
            f"repo={ref.repo}",
            "-F",
            f"number={ref.number}",
        )
        if thread_id not in github._github_thread_ids(threads):
            raise ValueError("Review thread does not belong to this pull request.")
        # Invalidate before dispatch. Once the provider call starts its remote
        # result is uncertain under cancellation, so stale generations must
        # already be unable to refill or satisfy a post-mutation refresh.
        await cache._invalidate_pull_request_cache(ref.url)
        await runner._run_json(
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={_GITHUB_RESOLVE_MUTATION}",
            "-f",
            f"threadId={thread_id}",
        )
    else:
        project = quote(ref.project, safe="")
        await cache._invalidate_pull_request_cache(ref.url)
        await runner._run_json(
            "glab",
            "api",
            "-X",
            "PUT",
            f"projects/{project}/merge_requests/{ref.number}/discussions/{thread_id}",
            "-f",
            "resolved=true",
            host=ref.host,
        )


async def _gitlab_merge_request(ref: SourceRef) -> dict[str, Any]:
    """Read a merge request so a mutation can refuse inapplicable requests."""
    project = quote(ref.project, safe="")
    details = await runner._run_json(
        "glab", "api", f"projects/{project}/merge_requests/{ref.number}", host=ref.host
    )
    if not isinstance(details, dict):
        raise SourceProviderError("GitLab returned an invalid merge-request payload")
    return details


def _gitlab_is_draft(details: dict[str, Any]) -> bool:
    """Report draft state, tolerating the legacy ``work_in_progress`` field."""
    return bool(details.get("draft") or details.get("work_in_progress"))


# GitLab pipeline statuses that still have to finish. While one of these is the
# head pipeline's status, merge_when_pipeline_succeeds genuinely defers the
# merge; outside them there is nothing left to wait for and the same call
# merges right away.
_GITLAB_PENDING_PIPELINE_STATUSES = frozenset(
    {"created", "waiting_for_resource", "preparing", "pending", "running", "scheduled", "manual"}
)


def _gitlab_has_pending_pipeline(details: dict[str, Any]) -> bool:
    """Report whether a pipeline would actually gate the merge."""
    pipeline = details.get("head_pipeline") or details.get("pipeline")
    if not isinstance(pipeline, dict):
        return False
    return str(pipeline.get("status") or "").lower() in _GITLAB_PENDING_PIPELINE_STATUSES


async def enable_pull_request_auto_merge(
    raw_url: str, *, confirm_immediate_merge: bool = False
) -> str:
    """Enable auto-merge (merge once requirements pass) and return the method.

    Both providers are read first so an inapplicable request is refused before
    anything is dispatched. GitHub has a real auto-merge switch and refuses a
    draft or already-armed pull request. GitLab has none: its equivalent is a
    merge call flagged ``merge_when_pipeline_succeeds``, which merges
    **immediately** when no pipeline is pending. That makes the GitLab path a
    merge authorization, so when nothing would gate the merge the caller must
    pass ``confirm_immediate_merge`` to acknowledge it. The refusal is raised as
    ``ConfirmationRequired`` so a client can discover the hazard from the server
    rather than pre-emptively asserting consent: the acknowledgement is only
    ever sent in answer to this specific refusal, which keeps the guard live for
    the dashboard instead of degrading it into a constant.
    """
    # Warm the allowlist BEFORE parsing: a self-managed URL is validated against
    # the cached snapshot, so a cold mutation would otherwise be rejected as an
    # unsupported host (400) even though the operator authorized it.
    await hosts.ensure_gitlab_hosts_loaded()
    ref = links._require_change_ref(links.parse_source_url(raw_url))
    hook = plugins._require_plugin_hook(ref, "enable_auto_merge", "Auto-merge")
    if hook is not None:
        await cache._invalidate_pull_request_cache(ref.url)
        with plugins._plugin_errors(ref.provider):
            method = await hook(ref, confirm_immediate_merge=confirm_immediate_merge)
        # The contract is the merge METHOD as a string; a plugin returning
        # anything else degrades to "" rather than leaking a foreign shape into
        # the response the dashboard renders.
        return method if isinstance(method, str) else ""
    if ref.provider == "github":
        node_id, repository = await _github_pull_request_node(ref)
        pull_request = repository.get("pullRequest")
        pull_request = pull_request if isinstance(pull_request, dict) else {}
        if pull_request.get("isDraft"):
            raise ValueError(
                "GitHub cannot enable auto-merge on a draft pull request. "
                "Mark it ready for review first."
            )
        if pull_request.get("autoMergeRequest"):
            raise ValueError("Auto-merge is already enabled for this pull request.")
        method = next(
            (
                graphql_method
                for field, graphql_method in _GITHUB_MERGE_METHODS
                if repository.get(field)
            ),
            "",
        )
        if not method:
            raise ValueError("This repository does not allow any merge method.")
        # Invalidate before dispatch: once the provider call starts its remote
        # result is uncertain under cancellation, so stale generations must
        # already be unable to refill or satisfy a post-mutation refresh.
        await cache._invalidate_pull_request_cache(ref.url)
        payload = await runner._run_json(
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={_GITHUB_AUTO_MERGE_MUTATION}",
            "-f",
            f"pullRequestId={node_id}",
            "-f",
            f"mergeMethod={method}",
        )
        _raise_on_graphql_errors(payload, "GitHub refused to enable auto-merge")
        return method.lower()
    details = await _gitlab_merge_request(ref)
    if _gitlab_is_draft(details):
        raise ValueError("GitLab cannot arm a draft merge request. Mark it ready for review first.")
    if details.get("merge_when_pipeline_succeeds"):
        raise ValueError("Auto-merge is already enabled for this merge request.")
    if not _gitlab_has_pending_pipeline(details) and not confirm_immediate_merge:
        raise ConfirmationRequired(
            "No pipeline is pending, so GitLab would merge this merge request "
            "immediately. Confirm the merge to proceed."
        )
    project = quote(ref.project, safe="")
    await cache._invalidate_pull_request_cache(ref.url)
    await runner._run_json(
        "glab",
        "api",
        "-X",
        "PUT",
        f"projects/{project}/merge_requests/{ref.number}/merge",
        "-f",
        "merge_when_pipeline_succeeds=true",
        host=ref.host,
    )
    return "pipeline"


async def mark_pull_request_ready(raw_url: str) -> None:
    """Take a draft pull/merge request out of draft state.

    Both providers expose a dedicated transition, so neither path rewrites the
    title: GitLab's draft prefix grammar stays the provider's concern and a
    concurrent retitle cannot be clobbered by this call.
    """
    # Warm the allowlist BEFORE parsing: a self-managed URL is validated against
    # the cached snapshot, so a cold mutation would otherwise be rejected as an
    # unsupported host (400) even though the operator authorized it.
    await hosts.ensure_gitlab_hosts_loaded()
    ref = links._require_change_ref(links.parse_source_url(raw_url))
    hook = plugins._require_plugin_hook(ref, "mark_ready", "Marking a change ready for review")
    if hook is not None:
        await cache._invalidate_pull_request_cache(ref.url)
        with plugins._plugin_errors(ref.provider):
            await hook(ref)
        return
    if ref.provider == "github":
        node_id, repository = await _github_pull_request_node(ref)
        pull_request = repository.get("pullRequest")
        pull_request = pull_request if isinstance(pull_request, dict) else {}
        if not pull_request.get("isDraft"):
            raise ValueError("This pull request is already ready for review.")
        await cache._invalidate_pull_request_cache(ref.url)
        payload = await runner._run_json(
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={_GITHUB_READY_MUTATION}",
            "-f",
            f"pullRequestId={node_id}",
        )
        _raise_on_graphql_errors(payload, "GitHub refused to mark the pull request ready")
        return
    details = await _gitlab_merge_request(ref)
    if not _gitlab_is_draft(details):
        raise ValueError("This merge request is already ready for review.")
    await cache._invalidate_pull_request_cache(ref.url)
    payload = await runner._run_json(
        "glab",
        "api",
        "graphql",
        "-f",
        f"query={_GITLAB_SET_DRAFT_MUTATION}",
        "-f",
        f"projectPath={ref.project}",
        "-f",
        f"iid={ref.number}",
        "-F",
        "draft=false",
        host=ref.host,
    )
    _raise_on_graphql_errors(payload, "GitLab refused to mark the merge request ready")

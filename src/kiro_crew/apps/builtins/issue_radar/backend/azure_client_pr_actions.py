"""The pull-request actions Issue Radar takes on Azure DevOps.

Close or reopen, review, merge, auto-complete on and off, and the builds a PR's CI
actions read, cancel and re-run. Callers reach every name here through
:mod:`kiro_crew.apps.builtins.issue_radar.backend.azure_client`.
"""

from __future__ import annotations

from .azure_client_cli import (
    _API_BUILD,
    _API_GIT,
    _GUID_RE,
    _SHA_RE,
    AZ_TIMEOUT_SEC,
    _az_invoke,
    _check_repo,
    _identity_id,
    _obj,
    _split_owner,
)
from .azure_client_pulls import (
    _BUILD_FINISHED_STATUSES,
    _MERGE_STRATEGIES,
    _MERGE_STRATEGY_NAMES,
    PR_MERGE_METHODS,
    _builds_for_sha,
)
from .azure_client_work_items import _current_identity
from .azure_client_writes import add_pr_comment
from .errors import ProviderCliError

# ── pull-request actions (parity with github_client's PR action surface) ──────
#
# Azure's vocabulary differs from GitHub's at every one of these, so each function
# maps the app's provider-neutral request onto Azure's own concept:
#
#   close/reopen        -> the PR's ``status`` (abandoned / active)
#   review verdict      -> a reviewer VOTE on a numeric scale
#   comment             -> a thread with no threadContext
#   merge               -> "complete" the PR, with a merge strategy
#   auto-merge          -> ``autoCompleteSetBy``, a real reversible armed state
#   cancel / re-run CI  -> a pipeline BUILD, not a workflow run
#
# Azure supports all three merge strategies and reversible auto-complete, but its
# reviewer vote is PR-scoped rather than revision-scoped. Verdicts are therefore
# refused below; only a comment can be bound safely to the displayed discussion.


def _pr_patch(
    org: str,
    project: str,
    repo: str,
    number: int,
    payload: dict,
    *,
    host: str,
    timeout: float,
) -> dict:
    """PATCH one pull request.

    ``repositoryId`` is passed even though a pull request id is unique across the
    whole collection: every mutating PR route still requires it in the path, so
    omitting it would 404 rather than resolve.
    """
    return _obj(
        _az_invoke(
            org=org,
            area="git",
            resource="pullRequests",
            host=host,
            timeout=timeout,
            route={"project": project, "repositoryId": repo, "pullRequestId": int(number)},
            method="PATCH",
            body=payload,
            api_version=_API_GIT,
        )
    )


def set_pr_state(
    owner: str,
    repo: str,
    number: int,
    state: str,
    *,
    host: str = "",
    timeout: float = AZ_TIMEOUT_SEC,
) -> dict:
    """Close or reopen a pull request. ``state`` is ``"open"`` or ``"closed"``.

    Closing sets ``abandoned``, which is Azure's reversible "closed without
    merging" -- not ``completed``, which would MERGE the pull request. Reopening an
    abandoned PR sets it back to ``active``; a COMPLETED pull request cannot be
    reopened at all, and Azure answers with an error rather than this function
    pretending otherwise.
    """
    if state not in ("open", "closed"):
        raise ProviderCliError(f"invalid pull request state: {state!r}")
    org, project = _split_owner(owner)
    name = _check_repo(repo)
    data = _pr_patch(
        org,
        project,
        name,
        number,
        {"status": "abandoned" if state == "closed" else "active"},
        host=host,
        timeout=timeout,
    )
    status = str(data.get("status") or "").lower()
    if status == "active":
        resolved = "open"
    elif status:
        resolved = "closed"
    else:
        # No usable status came back -- absent, null and "" all normalize to "" --
        # so report what was asked for rather than reading it as "closed".
        resolved = state
    return {
        "state": resolved,
        "merged": status == "completed",
        "draft": bool(data.get("isDraft")),
    }


# COMMENT only. Azure records a review as a numeric reviewer VOTE rather than as a
# review object, and that vote carries no revision -- so the two VERDICT verbs are
# refused rather than approximated, for the ordering reason spelled out in
# :func:`submit_pr_review`. This is the same shape of honest refusal gitlab_client
# makes for the verdict IT cannot express.
PR_REVIEW_EVENTS = ("COMMENT",)

# The two verdict verbs Azure can express but cannot bind to a revision. Named so
# the refusal can explain WHICH verb was asked for, and so a reader sees they were
# considered rather than overlooked.
_UNBINDABLE_REVIEW_VERBS = ("APPROVE", "REQUEST_CHANGES")


def submit_pr_review(
    owner: str,
    repo: str,
    number: int,
    event: str,
    body: str = "",
    head_sha: str = "",
    *,
    host: str = "",
    timeout: float = AZ_TIMEOUT_SEC,
) -> dict:
    """Post a review on a pull request. ``COMMENT`` is the only verb Azure accepts.

    ``COMMENT`` records no vote and posts prose only, because a comment is not a
    verdict -- so it is expressible here. ``APPROVE`` and ``REQUEST_CHANGES`` are
    REFUSED, and the refusal is the substance of this function rather than a gap in
    it: Azure's reviewer vote is a numeric value attached to the PULL REQUEST, with
    no revision parameter to bind it to the commit the reviewer actually read. The
    full ordering argument is in the refusal branch below.

    ``head_sha`` is REQUIRED, matching both other clients, even though nothing can
    be done with it on the wire here. It keeps the verbs from diverging into "one
    of them names the revision and the other does not", and it gives the route's
    own head-moved check (which re-reads the live head and answers 409) a value to
    compare against.
    """
    verb = (event or "").strip().upper()
    if verb in _UNBINDABLE_REVIEW_VERBS:
        # Azure's reviewer vote takes no revision parameter: it attaches to the PULL
        # REQUEST, not to a commit. So between the route's head-moved check and this
        # call, a push can land and the verdict then records against code nobody
        # read. Azure resetting votes on push does NOT close that ordering -- the
        # reset fires with the push, before our vote arrives. There is no
        # compensating fix either: withdrawing a vote after re-reading the head is
        # itself a write that can fail, and its failure leaves an approval standing
        # on unreviewed code, which is the wrong direction to fail in.
        #
        # So the verb is REFUSED rather than approximated, the same choice
        # gitlab_client makes for the verdict it cannot express. Commenting still
        # works, and a human can vote in Azure's own UI where they can see the head
        # they are voting on.
        raise ProviderCliError(
            f"Azure DevOps cannot record a {verb} review that is bound to the "
            "commit it was formed on -- its reviewer vote attaches to the pull "
            "request, not to a revision, so a push landing first would apply the "
            "verdict to unreviewed code. Post a comment instead, or vote in Azure "
            "DevOps directly."
        )
    if verb not in PR_REVIEW_EVENTS:
        raise ProviderCliError(f"invalid review event: {event!r}")
    text = (body or "").strip()
    if not text:
        raise ProviderCliError(f"a {verb} review requires a comment body")
    sha = (head_sha or "").strip()
    if not _SHA_RE.match(sha):
        raise ProviderCliError(
            "refusing to review without the head commit it was read at " f"(got {head_sha!r})"
        )
    # Called for their validation, not their values: the owner must still split into
    # an org/project pair and the repository name must still pass the segment
    # charset, so a malformed identity is refused here rather than inside the
    # comment call.
    _split_owner(owner)
    _check_repo(repo)
    if verb == "COMMENT":
        add_pr_comment(owner, repo, number, text, host=host, timeout=timeout)
        return {"id": None, "state": "COMMENTED", "submitted_at": None}

    # Unreachable: PR_REVIEW_EVENTS holds only COMMENT and every other verb was
    # refused above. Kept as a loud failure rather than a silent fall-through, so a
    # future verb added to the tuple cannot quietly return None.
    raise ProviderCliError(f"unhandled review event: {verb!r}")


def merge_pull_request(
    owner: str,
    repo: str,
    number: int,
    method: str = "SQUASH",
    head_sha: str = "",
    *,
    host: str = "",
    timeout: float = AZ_TIMEOUT_SEC,
) -> dict:
    """Complete (merge) a pull request now.

    Azure merges a PR by PATCHing it to ``completed`` with the commit it is pinned
    to. Like the other two providers this cannot bypass a gate: Azure enforces the
    branch's policies on completion and refuses a PR that has not satisfied them,
    which surfaces as an error.

    ``head_sha`` is REQUIRED and rides as ``lastMergeSourceCommit``, which Azure
    treats as a real precondition: it refuses the completion when that is not the
    PR's last source commit, so a push landing between the review and the click
    cannot merge unreviewed code. It is a positional parameter with an empty default
    only so the three module signatures stay identical; an empty value is refused
    here, never defaulted.

    ``bypassPolicy`` is deliberately never sent. It is Azure's override switch, and
    a button that silently sheds a required policy is the one thing the provider
    would not adjudicate for us.

    Returns ``{merged, sha, message}`` in the GitHub-shaped vocabulary the route and
    the UI speak.
    """
    verb = (method or "").strip().upper()
    if verb not in PR_MERGE_METHODS:
        raise ProviderCliError(f"invalid merge method: {method!r}")
    sha = (head_sha or "").strip()
    if not _SHA_RE.match(sha):
        raise ProviderCliError(
            "refusing to merge without the head commit it was reviewed at " f"(got {head_sha!r})"
        )
    org, project = _split_owner(owner)
    name = _check_repo(repo)
    data = _pr_patch(
        org,
        project,
        name,
        number,
        {
            "status": "completed",
            "lastMergeSourceCommit": {"commitId": sha},
            "completionOptions": {
                "mergeStrategy": _MERGE_STRATEGIES[verb],
                # Delete the source branch: matches what the other two providers'
                # repository settings do by default and what the app's users expect
                # after a merge. Explicit either way, because Azure reads an omitted
                # option as "leave as armed", which would inherit whatever the PR
                # was last set to rather than what this call asked for.
                "deleteSourceBranch": False,
                "bypassPolicy": False,
            },
        },
        host=host,
        timeout=timeout,
    )
    status = str(data.get("status") or "").lower()
    return {
        "merged": status == "completed",
        "sha": _obj(data.get("lastMergeCommit")).get("commitId"),
        "message": str(data.get("mergeFailureMessage") or ""),
    }


def enable_auto_merge(
    owner: str,
    repo: str,
    number: int,
    method: str = "SQUASH",
    *,
    host: str = "",
    timeout: float = AZ_TIMEOUT_SEC,
) -> dict:
    """Arm Azure's auto-complete on a pull request.

    Azure has a REAL deferred-merge verb, unlike GitLab: setting
    ``autoCompleteSetBy`` to an identity arms the PR, and Azure completes it by
    itself once every policy passes. Nothing merges at call time -- an unarmed PR
    with no outstanding policy is completed by Azure a moment later, which is the
    same behaviour GitHub's auto-merge has -- so this is genuinely the reversible
    arming action the bulk affordance advertises, and :func:`disable_auto_merge`
    really does cancel it.

    The merge strategy is armed alongside, so the deferred merge produces the
    history the caller chose rather than whatever the PR was last set to.

    ``auto_merge`` in the result is DERIVED from what came back, not asserted: a
    hardcoded ``True`` would make the response a claim rather than an observation.

    Returns ``{auto_merge, method, enabled_at}``.
    """
    verb = (method or "").strip().upper()
    if verb not in PR_MERGE_METHODS:
        raise ProviderCliError(f"invalid merge method: {method!r}")
    org, project = _split_owner(owner)
    name = _check_repo(repo)
    identity = _current_identity(org, host=host, timeout=timeout)
    identity_id = str(identity.get("id") or "")
    if not _GUID_RE.match(identity_id):
        raise ProviderCliError(
            "cannot arm auto-complete: the authenticated Azure DevOps identity could not be resolved"
        )
    data = _pr_patch(
        org,
        project,
        name,
        number,
        {
            "autoCompleteSetBy": {"id": identity_id},
            "completionOptions": {
                "mergeStrategy": _MERGE_STRATEGIES[verb],
                "deleteSourceBranch": False,
                "bypassPolicy": False,
            },
        },
        host=host,
        timeout=timeout,
    )
    armed = _identity_id(data.get("autoCompleteSetBy"))
    strategy = str(_obj(data.get("completionOptions")).get("mergeStrategy") or "").lower()
    return {
        "auto_merge": bool(armed),
        "method": _MERGE_STRATEGY_NAMES.get(strategy) or (verb if armed else None),
        # Azure records no timestamp for the arming, and substituting "now" would
        # report a time nothing stored.
        "enabled_at": None,
    }


# The identity that CLEARS an armed auto-complete. Azure has no "unset" verb for
# ``autoCompleteSetBy``: patching it to the empty GUID is the documented way to
# cancel, and an omitted field would leave the PR armed.
_EMPTY_GUID = "00000000-0000-0000-0000-000000000000"


def disable_auto_merge(
    owner: str, repo: str, number: int, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> dict:
    """Cancel Azure's auto-complete on a pull request.

    The genuine inverse of :func:`enable_auto_merge` -- so an accidental arm is
    reversible from the same place it was set, which is what makes offering the arm
    in bulk defensible. Returns ``{auto_merge: False, method: None, enabled_at: None}``.
    """
    org, project = _split_owner(owner)
    name = _check_repo(repo)
    data = _pr_patch(
        org,
        project,
        name,
        number,
        {"autoCompleteSetBy": {"id": _EMPTY_GUID}},
        host=host,
        timeout=timeout,
    )
    still_armed = _identity_id(data.get("autoCompleteSetBy"))
    if still_armed and still_armed != _EMPTY_GUID:
        raise ProviderCliError(
            "Azure DevOps still reports auto-complete as armed after the cancel request"
        )
    return {"auto_merge": False, "method": None, "enabled_at": None}


# Azure build status -> the GitHub CONCLUSION vocabulary the shared UI compares
# against. Only the spellings that differ need an entry; note "canceled" (one l) is
# Azure's spelling and the UI keys on GitHub's "cancelled", so passing it through
# would leave a consumer unable to see a cancelled build.
_BUILD_CONCLUSIONS = {
    "succeeded": "success",
    "failed": "failure",
    "partiallysucceeded": "neutral",
    "canceled": "cancelled",
    "cancelled": "cancelled",
}


def list_pr_workflow_runs(
    owner: str, repo: str, sha: str, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> list[dict]:
    """The pipeline builds for a PR's head commit -- Azure's analogue of a
    workflow run (see github_client for why checks and runs are separate surfaces).

    ``sha`` is charset-validated before it reaches a query, as on the other two
    paths. Rows carry ``cancellable``/``rerunnable`` so the UI never offers an
    action Azure will refuse: a finished build can only be re-queued, and an
    in-flight one can only be cancelled.
    """
    if not _SHA_RE.match(sha or ""):
        raise ProviderCliError(f"invalid commit sha: {sha!r}")
    org, project = _split_owner(owner)
    name = _check_repo(repo)
    out: list[dict] = []
    for build in _builds_for_sha(org, project, name, sha, host=host, timeout=timeout):
        build_id = build.get("id")
        if not isinstance(build_id, int):
            continue
        status = str(build.get("status") or "").lower()
        result = str(build.get("result") or "").lower()
        finished = status in _BUILD_FINISHED_STATUSES
        definition = _obj(build.get("definition"))
        out.append(
            {
                "id": build_id,
                "name": str(definition.get("name") or build.get("buildNumber") or "build"),
                # Normalized to the same two fields the GitHub rows use, so the UI reads
                # one shape: Azure reports status and result separately, like GitHub's
                # status and conclusion, but spells both differently.
                "status": "completed" if finished else status,
                "conclusion": _BUILD_CONCLUSIONS.get(result, result or None) if finished else None,
                "url": _obj(_obj(build.get("_links")).get("web")).get("href"),
                "event": build.get("reason"),
                "created_at": build.get("queueTime") or build.get("startTime"),
                "cancellable": not finished,
                "rerunnable": finished,
            }
        )
    return out


def _read_build(org: str, project: str, run_id: int, *, host: str, timeout: float) -> dict:
    """One pipeline build, by id."""
    return _obj(
        _az_invoke(
            org=org,
            area="build",
            resource="builds",
            host=host,
            timeout=timeout,
            route={"project": project, "buildId": int(run_id)},
            api_version=_API_BUILD,
        )
    )


def _assert_build_belongs_to_repo(
    org: str, project: str, run_id: int, name: str, *, host: str, timeout: float
) -> dict:
    """Refuse a build id that is not this repository's, returning it when it is.

    A build id is PROJECT-scoped on Azure, not repository-scoped, so a caller
    holding a connected repo A can name a build belonging to repo B in the same
    project and the mutation would land on B: the route's connected-repo gate
    authorizes A and never sees that the id points elsewhere. Every run mutation
    therefore reads the build first and compares its repository, which is the only
    place that binding can be checked.

    Compared EXACTLY, matching the rest of this client: Azure repository names are
    not documented as case-insensitive, so folding could accept a different
    repository that differs only by case.
    """
    build = _read_build(org, project, run_id, host=host, timeout=timeout)
    owner_name = str(_obj(build.get("repository")).get("name") or "")
    if not owner_name:
        raise ProviderCliError(
            f"could not determine which repository build {int(run_id)} belongs to, "
            "so the request is refused"
        )
    if owner_name != name:
        raise ProviderCliError(
            f"build {int(run_id)} belongs to repository {owner_name!r}, not {name!r} -- "
            "a build id is project-scoped, so it is refused rather than acted on"
        )
    return build


def cancel_workflow_run(
    owner: str, repo: str, run_id: int, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> dict:
    """Cancel one in-flight pipeline build.

    Azure cancels by PATCHing the build to ``cancelling`` -- the participle is the
    real value: the request records the intent and the agent stops the build
    shortly after, so a successful call means "cancellation accepted", not
    "already stopped". A build that has already finished is refused by Azure and
    surfaces as an error rather than as a no-op success, because a cancel that
    silently does nothing is indistinguishable from one that worked.

    Returns ``{run_id, cancelled: True}``.
    """
    org, project = _split_owner(owner)
    name = _check_repo(repo)
    _assert_build_belongs_to_repo(org, project, run_id, name, host=host, timeout=timeout)
    _az_invoke(
        org=org,
        area="build",
        resource="builds",
        host=host,
        timeout=timeout,
        route={"project": project, "buildId": int(run_id)},
        method="PATCH",
        body={"status": "cancelling"},
        api_version=_API_BUILD,
    )
    return {"run_id": int(run_id), "cancelled": True}


def rerun_workflow_run(
    owner: str,
    repo: str,
    run_id: int,
    *,
    failed_only: bool = False,
    host: str = "",
    timeout: float = AZ_TIMEOUT_SEC,
) -> dict:
    """Re-queue a finished pipeline build.

    Azure has no "re-run" verb: a retry is a NEW build queued from the same
    definition, branch and commit, which is why the original is read first. The
    returned ``run_id`` is the NEW build's id, so a caller can follow what it
    actually started rather than polling a run that will never change again.

    ``failed_only`` is accepted for signature parity and cannot be honoured -- Azure
    re-runs whole stages, not the failed jobs of a completed build -- so the
    returned ``failed_only`` reports what Azure actually did (``False``), never what
    was asked. A caller is therefore not told a cheap partial retry happened when a
    full build was queued.
    """
    del failed_only
    org, project = _split_owner(owner)
    _check_repo(repo)
    original = _assert_build_belongs_to_repo(
        org, project, run_id, _check_repo(repo), host=host, timeout=timeout
    )
    definition_id = _obj(original.get("definition")).get("id")
    if not isinstance(definition_id, int):
        raise ProviderCliError(f"could not read the pipeline definition of build {int(run_id)}")
    payload: dict[str, object] = {"definition": {"id": definition_id}}
    # Pinned to the ORIGINAL branch and commit. Queuing without them would build
    # the branch's current tip, which is a different revision than the one the user
    # asked to retry.
    if original.get("sourceBranch"):
        payload["sourceBranch"] = original["sourceBranch"]
    if original.get("sourceVersion"):
        payload["sourceVersion"] = original["sourceVersion"]
    queued = _obj(
        _az_invoke(
            org=org,
            area="build",
            resource="builds",
            host=host,
            timeout=timeout,
            route={"project": project},
            method="POST",
            body=payload,
            api_version=_API_BUILD,
        )
    )
    new_id = queued.get("id")
    return {
        "run_id": new_id if isinstance(new_id, int) else int(run_id),
        "rerun": True,
        "failed_only": False,
    }

"""Reading and publishing the caller's own pending GitHub review.

Publishing is bound to what the caller was shown: the pending review's id, a
digest of its body and every inline comment, a head that has not moved, content
that redaction did not alter, and -- for an approval -- a base branch that
dismisses stale approvals and no armed auto-merge. A head that moves during the
submit is answered by dismissing the verdict again.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import re
from typing import Any
from urllib.parse import quote

from kiro_crew.dashboard.source_providers import LOGGER_NAME, cache, hosts, links, runner, sanitize
from kiro_crew.dashboard.source_providers.contract import SourceProviderError, SourceRef

logger = logging.getLogger(LOGGER_NAME)


# The three verdicts GitHub accepts when submitting a pending review. DISMISS and
# the other review endpoints are deliberately absent: this path exists to publish a
# draft the caller has read, not to act on reviews someone else submitted.
_REVIEW_SUBMIT_EVENTS = frozenset({"APPROVE", "REQUEST_CHANGES", "COMMENT"})
# The verdicts that GATE a merge, and therefore the ones whose attachment to a
# specific commit is load-bearing. A COMMENT review carries no verdict, so a head
# that moves under it costs nothing but misplaced line anchors.
_REVIEW_GATING_EVENTS = frozenset({"APPROVE", "REQUEST_CHANGES"})
# GitHub review ids are positive integers. Bounded and anchored because the value
# is interpolated into the REST path.
_GITHUB_REVIEW_ID_RE = re.compile(r"[1-9][0-9]{0,19}")


def _flatten_paginated(payload: Any) -> list[dict[str, Any]]:
    """Flatten a ``gh api --paginate --slurp`` result into one list of objects.

    ``--slurp`` returns an array of per-page arrays, but a single-page result from
    a caller without the flag is already flat, so both shapes are accepted rather
    than assuming one. Non-dict members are dropped: a malformed page must not
    smuggle a value past the scans that read these lists.
    """
    out: list[dict[str, Any]] = []
    if not isinstance(payload, list):
        return out
    for item in payload:
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, list):
            out.extend(p for p in item if isinstance(p, dict))
    return out


async def _github_pending_review(ref: SourceRef) -> dict[str, Any]:
    """Return the caller's own unsubmitted review on ``ref``, or empty fields.

    GitHub scopes a PENDING review to its author and permits only one per user
    per pull request, so the single PENDING entry this returns is necessarily the
    authenticated user's own draft -- there is no way to observe, or therefore to
    publish, someone else's.

    Beyond the body, two facts decide whether the draft may be published at all,
    so they are read here rather than re-derived at submit time:

    ``stale`` -- the draft's ``commit_id`` is not the pull request's live head.
    Publishing a verdict then attributes a review to code that was never read, and
    on a repository without stale-approval dismissal a stale ``APPROVE`` counts as
    a live approval of unreviewed code.

    ``contentRedacted`` -- redaction ALTERS the draft's own text (body or any of
    its inline comments). Submitting publishes GitHub's stored draft, not the
    redacted copy this returns, so a draft quoting a credential would be shown
    redacted here and published verbatim there. The mismatch is reported so the
    publish path can refuse rather than silently leak.
    """
    raw = await runner._run_json(
        "gh",
        "api",
        "--paginate",
        "--slurp",
        f"repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/reviews",
    )
    if not isinstance(raw, list):
        raise SourceProviderError("GitHub returned an invalid reviews payload")
    # Paginated: a pull request with more reviews than one page can carry would
    # otherwise hide the PENDING entry past the first 30 and read back as "no
    # draft" -- the same page-one blindness that made the comment scan unsafe.
    reviews = _flatten_paginated(raw)
    for entry in reviews:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("state") or "").upper() != "PENDING":
            continue
        review_id = str(entry.get("id") or "")
        raw_body = str(entry.get("body") or "")
        state = await _github_pull_request_state(ref)
        head_sha = str(state["headSha"])
        draft_sha = str(entry.get("commit_id") or "")
        texts = [raw_body]
        comments: list[dict[str, Any]] = []
        if review_id:
            comments = await _github_pending_review_comments(ref, review_id)
            texts.extend(str(c.get("body") or "") for c in comments)
        # The body is provider-controlled text on its way to the dashboard, so it
        # goes through the same redaction chokepoint as every other provider read
        # (fetch_pull_request, fetch_pull_request_checks). A hand-written draft can
        # quote a credential as easily as a comment can.
        return {
            "reviewId": review_id,
            "body": str(sanitize._redact_provider_data(raw_body)),
            # The inline comments are part of what a verdict publishes, and
            # `contentDigest` binds them, so they have to be RETURNED as well or the
            # digest would certify text the reader never saw -- consent to a body
            # standing in for consent to comments hidden behind it. Redacted the same
            # way and on the same chokepoint as the body.
            "comments": [
                {
                    "path": str(sanitize._redact_provider_data(str(c.get("path") or ""))),
                    "line": c.get("line"),
                    "body": str(sanitize._redact_provider_data(str(c.get("body") or ""))),
                }
                for c in comments
            ],
            "commitId": draft_sha,
            "headSha": head_sha,
            # Unknown draft or head sha counts as stale: fail closed rather than
            # treat an unanswerable question as "current".
            "stale": not (draft_sha and head_sha and draft_sha == head_sha),
            "contentRedacted": any(sanitize._redact_provider_data(t) != t for t in texts),
            "autoMergeArmed": bool(state["autoMergeArmed"]),
            "contentDigest": _review_content_digest(raw_body, comments),
            "staleDismissalEnabled": await _github_stale_dismissal_enabled(
                ref, str(state["baseRef"])
            ),
        }
    return {
        "reviewId": "",
        "body": "",
        "comments": [],
        "commitId": "",
        "headSha": "",
        "stale": False,
        "contentRedacted": False,
        "autoMergeArmed": False,
        "contentDigest": "",
        "staleDismissalEnabled": False,
    }


async def _github_pull_request_state(ref: SourceRef) -> dict[str, Any]:
    """The live facts a publish decision needs: head sha, and whether auto-merge is armed.

    One fetch for both, because they are read together on every publish path and the
    object carries them both.

    Fetches the object and reads the fields in Python rather than passing
    ``--jq .head.sha``: ``gh``'s jq output for a string is a BARE token, and
    ``_run_json`` feeds every response to ``json.loads``, which rejects it — so the
    jq form turned every read of this value into a 503.
    """
    payload = await runner._run_json(
        "gh",
        "api",
        f"repos/{ref.owner}/{ref.repo}/pulls/{ref.number}",
    )
    if not isinstance(payload, dict):
        raise SourceProviderError("GitHub returned an invalid pull-request payload")
    head = payload.get("head")
    sha = str((head or {}).get("sha") or "").strip() if isinstance(head, dict) else ""
    # `auto_merge` is null when disarmed and an object when armed. Anything else
    # counts as armed: an unrecognised shape must not read as "safe".
    auto = payload.get("auto_merge")
    base = payload.get("base")
    base_ref = str((base or {}).get("ref") or "") if isinstance(base, dict) else ""
    return {
        "headSha": sha,
        "autoMergeArmed": auto is not None,
        "baseRef": base_ref,
    }


async def _github_stale_dismissal_enabled(ref: SourceRef, base_ref: str) -> bool:
    """Whether the base branch dismisses approvals when new commits land.

    This is the setting that makes a stale approval HARMLESS: with it on, GitHub
    itself dismisses the approval the moment the head moves, so an approval can
    never authorize a merge of code nobody reviewed -- no matter when auto-merge is
    armed or when the force-push lands relative to our own checks.

    Fail CLOSED: when NEITHER read can confirm the setting -- no protection at all,
    no permission on either surface, any error -- this returns False, which withholds
    APPROVE rather than assuming the branch is safe.

    Two reads, because they need different privileges. `GET /branches/{ref}/protection`
    is admin-only, so a non-admin reviewer on a properly protected repository would be
    refused APPROVE forever and sent back to github.com -- the context switch this
    feature removes. GraphQL's `branchProtectionRules` answers the same question at a
    lower privilege, so it is tried when REST cannot answer. The safety property is
    unchanged: only an explicit `true` from one of them opens the verdict.
    """
    if not base_ref:
        return False
    if await _github_rest_dismisses_stale(ref, base_ref):
        return True
    return await _github_graphql_dismisses_stale(ref, base_ref)


async def _github_rest_dismisses_stale(ref: SourceRef, base_ref: str) -> bool:
    """The admin-only REST read of the base branch's protection block."""
    try:
        payload = await runner._run_json(
            "gh",
            "api",
            f"repos/{ref.owner}/{ref.repo}/branches/{quote(base_ref, safe='')}/protection",
        )
    except Exception:
        logger.debug("branch protection unreadable via REST for %s", ref.url, exc_info=True)
        return False
    if not isinstance(payload, dict):
        return False
    reviews = payload.get("required_pull_request_reviews")
    if not isinstance(reviews, dict):
        return False
    return reviews.get("dismiss_stale_reviews") is True


async def _github_graphql_dismisses_stale(ref: SourceRef, base_ref: str) -> bool:
    """The lower-privilege GraphQL read, used when REST cannot answer.

    A rule's `pattern` may be a glob (`releases/*`), so the branch is matched with
    fnmatch rather than by equality. Only a rule that BOTH matches this branch and has
    `dismissesStaleReviews` true counts -- a non-matching rule says nothing about the
    branch being merged into.
    """
    query = (
        "query($owner:String!,$name:String!){"
        " repository(owner:$owner,name:$name){"
        " branchProtectionRules(first:100){"
        " nodes{ pattern dismissesStaleReviews } } } }"
    )
    try:
        payload = await runner._run_json(
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={ref.owner}",
            "-F",
            f"name={ref.repo}",
        )
    except Exception:
        logger.debug("branch protection unreadable via GraphQL for %s", ref.url, exc_info=True)
        return False
    nodes = (((payload or {}).get("data") or {}).get("repository") or {}).get(
        "branchProtectionRules"
    ) or {}
    for rule in nodes.get("nodes") or []:
        if not isinstance(rule, dict):
            continue
        pattern = rule.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            continue
        if _branch_pattern_matches(pattern, base_ref) and rule.get("dismissesStaleReviews") is True:
            return True
    return False


def _branch_pattern_matches(pattern: str, ref: str) -> bool:
    """Does a GitHub branch-protection ``pattern`` cover branch ``ref``?

    GitHub matches these patterns **per path segment**: a single ``*`` never
    crosses a ``/`` and ``**`` is the only wildcard that spans segments. Python's
    :func:`fnmatch.fnmatch` has no pathname mode -- its ``*`` swallows separators
    -- so ``releases/*`` would match ``releases/1/2`` here while GitHub's rule
    does not cover that branch at all.

    That difference is not cosmetic: this predicate is what opens the APPROVE
    verdict, so a pattern that appears to match a branch it does not actually
    protect is a fail-OPEN -- a stale approval could survive a head change on a
    branch carrying no stale-dismissal rule. Compare segment by segment.

    Case-sensitive by design (``fnmatchcase``): branch names are, and plain
    ``fnmatch`` would fold case on macOS and Windows.
    """
    if not pattern or not ref:
        return False
    return _segments_match(pattern.split("/"), ref.split("/"))


def _segments_match(pat: list[str], seg: list[str]) -> bool:
    """Segment-wise glob match, where ``**`` consumes zero or more segments."""
    if not pat:
        return not seg
    if pat[0] == "**":
        return any(_segments_match(pat[1:], seg[i:]) for i in range(len(seg) + 1))
    if not seg:
        return False
    if not fnmatch.fnmatchcase(seg[0], pat[0]):
        return False
    return _segments_match(pat[1:], seg[1:])


async def _github_pull_request_head_sha(ref: SourceRef) -> str:
    """The pull request's live head sha, read to detect a stale draft review."""
    return str((await _github_pull_request_state(ref))["headSha"])


async def _github_pending_review_comments(ref: SourceRef, review_id: str) -> list[dict[str, Any]]:
    """A pending review's inline comments, ACROSS ALL PAGES.

    Needed because submission publishes every comment GitHub holds for the draft,
    not just the body this app can see -- so a credential hiding in an inline
    comment of a hand-written draft has to be detectable too, and the content
    digest has to cover them. Pagination is not optional here: the endpoint returns
    30 per page, so an unpaginated scan clears a draft whose 31st comment carries
    the credential and then publishes it.
    """
    raw = await runner._run_json(
        "gh",
        "api",
        "--paginate",
        "--slurp",
        f"repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/reviews/{review_id}/comments",
    )
    return _flatten_paginated(raw)


def _review_content_digest(body: str, comments: list[dict[str, Any]]) -> str:
    """Stable digest of everything submitting this draft would publish.

    The review id identifies the review OBJECT, not its contents: GitHub lets a
    pending review's body be edited and its inline comments be added or removed
    under the same id. So an id match alone cannot prove the caller read what is
    about to go out -- this digest can, and the submit path requires the one the
    caller was shown.

    Sorted by comment id (stable and unique) so a re-ordered read of identical
    content yields the same digest, while an edited body, a moved line, or an
    added/removed comment all change it. Hashes the RAW text, because raw text is
    what GitHub publishes.
    """
    payload: list[dict[str, Any]] = [
        {
            "id": str(c.get("id") or ""),
            "path": str(c.get("path") or ""),
            "line": c.get("line"),
            "body": str(c.get("body") or ""),
        }
        for c in comments
    ]
    payload.sort(key=lambda c: str(c["id"]))
    blob = json.dumps({"body": body, "comments": payload}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


async def pull_request_pending_review(raw_url: str) -> dict[str, Any]:
    """Read the caller's unsubmitted draft review, so a client can show it.

    Separate from the submit call because publishing is only safe when the caller
    has been shown the exact draft it is about to publish: the id returned here is
    what :func:`submit_pull_request_review` requires back.
    """
    await hosts.ensure_gitlab_hosts_loaded()
    ref = links._require_change_ref(links.parse_source_url(raw_url))
    if ref.provider != "github":
        raise ValueError("Draft reviews can only be read on GitHub pull requests.")
    return await _github_pending_review(ref)


async def submit_pull_request_review(
    raw_url: str, review_id: str, event: str, content_digest: str
) -> dict[str, Any]:
    """Publish an existing pending review with a verdict.

    ``review_id`` is required rather than resolved implicitly: a pending review may
    equally be one the human started by hand in the provider UI, so submitting
    whatever draft happens to exist could publish an unfinished review the caller
    never saw. Requiring the id the caller was shown -- and re-checking that it is
    still the pending one -- makes that impossible and turns a concurrent
    submit-or-replace into a rejection instead of a surprise.
    """
    await hosts.ensure_gitlab_hosts_loaded()
    ref = links._require_change_ref(links.parse_source_url(raw_url))
    if ref.provider != "github":
        raise ValueError("Draft reviews can only be published on GitHub pull requests.")
    normalized = (event or "").strip().upper()
    if normalized not in _REVIEW_SUBMIT_EVENTS:
        raise ValueError("event must be APPROVE, REQUEST_CHANGES, or COMMENT.")
    if not _GITHUB_REVIEW_ID_RE.fullmatch(review_id or ""):
        raise ValueError("A valid review id is required.")
    pending = await _github_pending_review(ref)
    if pending.get("reviewId") != review_id:
        raise ValueError(
            "This draft review is no longer pending -- it was already submitted or "
            "replaced. Reload the pull request and try again."
        )
    # The id identifies the review OBJECT; GitHub lets its body be edited and its
    # inline comments change under the same id. So the id alone cannot prove the
    # caller read what is about to be published -- the digest of the current
    # content must match the one the caller was shown. REQUIRED, never optional:
    # an omitted digest that skipped the comparison would be a one-parameter
    # bypass of this whole guard.
    # What this endpoint does NOT check: whether the pending draft belongs to the
    # caller's own review RUN. It enforces identity of the DRAFT (`reviewId` must be
    # the pending one) and identity of its CONTENT (the digest), which is everything
    # visible from here -- run provenance lives in Sage's run records, which this
    # provider-level handler has no access to. Sage's publish bar checks it before
    # offering a verdict; a different caller publishing "the pending draft" is
    # publishing what it read, bound to what it read, and gets no run-coherence
    # guarantee from this endpoint.
    if not content_digest:
        raise ValueError(
            "A contentDigest is required: publishing must be bound to the exact "
            "draft contents that were displayed."
        )
    if content_digest != pending.get("contentDigest"):
        raise ValueError(
            "This draft changed after it was displayed -- its body or inline comments "
            "are no longer what you read. Reload the pull request and review it again."
        )
    # Submission publishes the draft GitHub stored, NOT the redacted copy the read
    # returned. So when redaction alters the draft's own text the two disagree: the
    # dashboard shows `[REDACTED]` while the published review would carry the
    # secret verbatim. Refuse -- a leak the user was shown as redacted is worse
    # than no publish button.
    if pending.get("contentRedacted"):
        raise ValueError(
            "This draft contains content that must be redacted (a credential or an "
            "unsafe URL), and publishing would post the original text. Edit or "
            "discard the draft on GitHub instead."
        )
    # A draft written against an older head reviewed code the current head lacks.
    # For APPROVE that is the dangerous case -- a repository without stale-approval
    # dismissal counts it as a live approval of unreviewed code -- but a verdict of
    # any kind, and inline comments anchored to vanished lines, are all wrong on a
    # moved head, so every event is refused rather than carving out an exception.
    if pending.get("stale"):
        raise ValueError(
            "This draft was written against an earlier commit and the pull request "
            "has moved since. Re-review the current head before publishing."
        )
    # The stale check above and the submit below cannot be atomic — GitHub's
    # submit-review API takes no expected-head parameter — so a force-push in that
    # window leaves a verdict on an unreviewed head. The post-submit dismissal
    # further down repairs that, EXCEPT when auto-merge is armed: then the approval
    # satisfies branch protection and GitHub can merge before the dismissal lands,
    # and nothing repairs a merge. Refuse APPROVE for exactly that combination
    # rather than removing the verdict outright.
    # Every remaining variant of the stale-approval race -- auto-merge armed before
    # OR after this check, a force-push landing inside the submit round trip, a
    # manual merge in that same window -- needs ONE precondition to do harm: the base
    # branch must NOT dismiss approvals when new commits land. With
    # `dismiss_stale_reviews` on, GitHub retracts the approval itself the moment the
    # head moves, so a stale approval can never authorize unreviewed code and the
    # timing of our own checks stops mattering. Requiring it closes the chain instead
    # of narrowing it, and it fails closed: unreadable protection (no admin rights,
    # no protection at all) withholds APPROVE.
    if normalized == "APPROVE" and not pending.get("staleDismissalEnabled"):
        raise ValueError(
            "Approve is unavailable because this pull request's base branch does not "
            "dismiss approvals when new commits are pushed (or its protection is not "
            "readable from here). Without that, an approval published now could "
            'outlive the commit it reviewed. Enable "Dismiss stale pull request '
            'approvals when new commits are pushed" on the base branch, or approve '
            "on GitHub."
        )
    if normalized == "APPROVE" and pending.get("autoMergeArmed"):
        raise ValueError(
            "Auto-merge is armed on this pull request, so an approval could merge it "
            "before a stale-head check could take the approval back. Disarm "
            "auto-merge to approve from here, or approve on GitHub where you can see "
            "the head you are approving."
        )
    # Invalidate before dispatch: once the provider call starts its remote result
    # is uncertain under cancellation, so a stale generation must already be unable
    # to refill or satisfy a post-mutation refresh.
    await cache._invalidate_pull_request_cache(ref.url)
    validated_head = str(pending.get("headSha") or "")
    await runner._run_json(
        "gh",
        "api",
        "-X",
        "POST",
        f"repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/reviews/{review_id}/events",
        "-f",
        f"event={normalized}",
    )
    # GitHub's submit-review API takes no expected-head parameter, so the check
    # above and this call cannot be one atomic operation: a force-push landing in
    # between would leave a verdict attached to a head nobody reviewed. Re-read the
    # head and, for a GATING verdict, dismiss what we just published rather than
    # leave a silent stale approval. This is a compensating action, not atomicity --
    # it turns an invisible window into a visible, self-reverting one.
    if normalized in _REVIEW_GATING_EVENTS:
        landed_head = await _github_pull_request_head_sha(ref)
        if landed_head and validated_head and landed_head != validated_head:
            dismissed = await _github_dismiss_review(ref, review_id)
            await cache._invalidate_pull_request_cache(ref.url)
            if dismissed:
                raise SourceProviderError(
                    "The pull request head moved while this review was being "
                    f"published, so the {normalized} was dismissed again. Re-review "
                    "the new head."
                )
            raise SourceProviderError(
                "The pull request head moved while this review was being published, "
                f"and the resulting {normalized} could NOT be dismissed "
                "automatically. Dismiss it on GitHub: it applies to a commit that "
                "was not reviewed."
            )
    return {"submitted": True, "event": normalized}


async def _github_dismiss_review(ref: SourceRef, review_id: str) -> bool:
    """Dismiss a just-published review whose head moved under it. Never raises.

    Best-effort by design: the caller reports a different, louder error when this
    fails, because an undismissable stale approval is exactly the state a human has
    to know about.
    """
    try:
        await runner._run_json(
            "gh",
            "api",
            "-X",
            "PUT",
            f"repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/reviews/{review_id}/dismissals",
            "-f",
            "message=Dismissed automatically: the pull request head changed while "
            "this review was being published, so it applied to unreviewed code.",
            "-f",
            "event=DISMISS",
        )
        return True
    except Exception:
        logger.warning("could not dismiss a stale review on %s", ref.url, exc_info=True)
        return False

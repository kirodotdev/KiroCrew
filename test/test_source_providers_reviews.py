"""Writing to a pull request under the owner's provider identity: every
pull-request-only entry point refuses an issue URL, review-thread replies and
top-level comments, and reading and submitting a pending review (head, redaction,
content digest and stale-approval checks)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from source_providers_helpers import _clear_issue_cache  # noqa: F401
from source_providers_helpers import _mock_source_sel_fixture  # noqa: F401
from source_providers_helpers import _no_visibility_refresh_side_task  # noqa: F401
from source_providers_helpers import _ISSUE_URL, _app

from kiro_crew.dashboard.handlers import source_providers as source


# Every pull-request-only entry point. An issue URL parses successfully now, so
# each of these must refuse it explicitly or it would address the PR namespace.
@pytest.mark.asyncio
async def test_fetch_pull_request_refuses_an_issue_url(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="points at an issue"):
        await source.fetch_pull_request(_ISSUE_URL)


# --- Review-thread replies, top-level comments, unresolve -------------------
# Writes to someone else's pull request under the owner's provider identity, so
# each one repeats resolve's contract: validated url, thread-ownership proof,
# cache invalidated BEFORE dispatch.

_THREAD_MEMBERSHIP = {
    "data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [{"id": "PRRT_1"}]}}}}
}


@pytest.mark.asyncio
async def test_reply_posts_into_the_thread(monkeypatch) -> None:
    calls: list[tuple] = []

    async def run(*argv, **kwargs):
        calls.append(argv)
        if any("reviewThreads" in a for a in argv):
            return _THREAD_MEMBERSHIP
        return {"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "1"}}}}

    monkeypatch.setattr(source, "_run_json", run)
    await source.reply_to_review_thread("https://github.com/acme/repo/pull/12", "PRRT_1", "Agreed")

    mutation = calls[-1]
    assert any("addPullRequestReviewThreadReply" in a for a in mutation)
    assert "threadId=PRRT_1" in mutation
    assert "body=Agreed" in mutation


@pytest.mark.asyncio
async def test_reply_rejects_a_thread_from_another_pull_request(monkeypatch) -> None:
    # The thread id comes from the browser: without this an owner-authenticated
    # reply could be steered at an unrelated pull request.
    run = AsyncMock(
        return_value={
            "data": {
                "repository": {"pullRequest": {"reviewThreads": {"nodes": [{"id": "PRRT_x"}]}}}
            }
        }
    )
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="does not belong"):
        await source.reply_to_review_thread(
            "https://github.com/acme/repo/pull/12", "PRRT_1", "Agreed"
        )
    run.assert_awaited_once()


@pytest.mark.asyncio
async def test_reply_rejects_a_path_shaped_thread_id(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="valid thread id"):
        await source.reply_to_review_thread(
            "https://github.com/acme/repo/pull/12", "../../etc/passwd", "hi"
        )
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_pull_request_checks_refuses_an_issue_url(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="points at an issue"):
        await source.fetch_pull_request_checks(_ISSUE_URL)


@pytest.mark.asyncio
async def test_reply_refuses_an_empty_body(monkeypatch) -> None:
    # An accidental empty comment is visible to everyone and is not removable
    # from this surface, so it never reaches the provider.
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="comment body is required"):
        await source.reply_to_review_thread(
            "https://github.com/acme/repo/pull/12", "PRRT_1", "   \n "
        )
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_pull_request_thread_refuses_an_issue_url(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="points at an issue"):
        await source.resolve_pull_request_thread(_ISSUE_URL, "PRRT_thread1")


@pytest.mark.asyncio
async def test_reply_refuses_an_oversized_body(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="at most"):
        await source.reply_to_review_thread(
            "https://github.com/acme/repo/pull/12", "PRRT_1", "x" * (source._MAX_COMMENT_CHARS + 1)
        )
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_enable_auto_merge_refuses_an_issue_url(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="points at an issue"):
        await source.enable_pull_request_auto_merge(_ISSUE_URL)


@pytest.mark.asyncio
async def test_reply_raises_on_a_graphql_refusal(monkeypatch) -> None:
    # GraphQL reports refusals with HTTP 200, so a transport-only check would
    # report a rejected reply as posted.
    async def run(*argv, **kwargs):
        if any("reviewThreads" in a for a in argv):
            return _THREAD_MEMBERSHIP
        return {"errors": [{"message": "not authorized"}]}

    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(source.SourceProviderError, match="could not post the reply"):
        await source.reply_to_review_thread(
            "https://github.com/acme/repo/pull/12", "PRRT_1", "Agreed"
        )


@pytest.mark.asyncio
async def test_mark_ready_refuses_an_issue_url(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="points at an issue"):
        await source.mark_pull_request_ready(_ISSUE_URL)


@pytest.mark.asyncio
async def test_reply_invalidates_the_cache_before_dispatch(monkeypatch) -> None:
    order: list[str] = []

    async def invalidate(url):
        order.append("invalidate")

    async def run(*argv, **kwargs):
        if any("reviewThreads" in a for a in argv):
            return _THREAD_MEMBERSHIP
        order.append("dispatch")
        return {"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "1"}}}}

    monkeypatch.setattr(source, "_invalidate_pull_request_cache", invalidate)
    monkeypatch.setattr(source, "_run_json", run)
    await source.reply_to_review_thread("https://github.com/acme/repo/pull/12", "PRRT_1", "Agreed")
    assert order == ["invalidate", "dispatch"]


@pytest.mark.asyncio
async def test_unresolve_reopens_the_thread(monkeypatch) -> None:
    calls: list[tuple] = []

    async def run(*argv, **kwargs):
        calls.append(argv)
        if any("reviewThreads" in a for a in argv):
            return _THREAD_MEMBERSHIP
        return {"data": {"unresolveReviewThread": {"thread": {"isResolved": False}}}}

    monkeypatch.setattr(source, "_run_json", run)
    await source.unresolve_pull_request_thread("https://github.com/acme/repo/pull/12", "PRRT_1")
    assert any("unresolveReviewThread" in a for a in calls[-1])


@pytest.mark.asyncio
async def test_comment_posts_to_the_issue_timeline(monkeypatch) -> None:
    calls: list[tuple] = []

    async def run(*argv, **kwargs):
        calls.append(argv)
        return {"id": 1}

    monkeypatch.setattr(source, "_run_json", run)
    await source.comment_on_pull_request("https://github.com/acme/repo/pull/12", "Looks good")
    argv = calls[-1]
    assert "repos/acme/repo/issues/12/comments" in argv
    assert "body=Looks good" in argv
    assert "-X" in argv and "POST" in argv


@pytest.mark.asyncio
async def test_comment_refuses_an_empty_body(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="comment body is required"):
        await source.comment_on_pull_request("https://github.com/acme/repo/pull/12", "")
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_check_status_refuses_an_issue_url(monkeypatch) -> None:
    """The chip refresh reaches `gh pr view`, so it must refuse an issue too."""
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="points at an issue"):
        await source._fetch_check_status(_ISSUE_URL)
    run.assert_not_awaited()


_SUBMIT_PR_URL = "https://github.com/acme/repo/pull/7"


def _pending_reviews_payload() -> list[dict]:
    return [
        {"id": 11, "state": "APPROVED", "body": "someone else already reviewed"},
        {
            "id": 4242,
            "state": "PENDING",
            "body": "[code-review-sage] draft",
            "commit_id": _HEAD_SHA,
        },
    ]


_HEAD_SHA = "9f1c2ab7de40aa11bb22cc33dd44ee55ff667788"


def _stub_run_json(
    monkeypatch,
    reviews,
    *,
    head=_HEAD_SHA,
    comments=(),
    submit=None,
    heads=None,
    dismiss_fails=False,
    auto_merge=None,
    stale_dismissal=True,
    protection_fails=False,
):
    """Route the reads submit_pull_request_review makes by their argv.

    Keyed on the request path rather than call order, because the guards changed
    how many reads happen and an order-keyed side_effect list silently mis-pairs
    responses when that count moves.

    List endpoints are returned in the ``--paginate --slurp`` shape (an array of
    per-page arrays) so the tests exercise the flattening the real calls need.
    ``heads`` supplies successive head reads, which is how the post-submit
    head-moved path is driven.
    """
    calls: list[tuple] = []
    head_queue = list(heads or [])

    async def fake(*argv, **kwargs):
        calls.append(argv)
        path = argv[-1] if argv[-1].startswith("repos/") else ""
        for a in argv:
            if a.startswith("repos/"):
                path = a
                break
        if "-X" in argv and "PUT" in argv:
            if dismiss_fails:
                raise source.SourceProviderError("dismissal refused")
            return {}
        if "-X" in argv and "POST" in argv:
            return submit if submit is not None else {}
        if path.endswith("/reviews"):
            return [list(reviews)]  # one page
        if path.endswith("/comments"):
            return [list(comments)]  # one page
        # The head read fetches the whole pull-request object (no `--jq`), so the
        # double must return that SHAPE — returning a bare string is what let a
        # json.loads crash hide behind green tests for two rounds.
        if path.endswith("/protection"):
            if protection_fails:
                raise source.SourceProviderError("protection unreadable")
            return {"required_pull_request_reviews": {"dismiss_stale_reviews": stale_dismissal}}
        sha = head_queue.pop(0) if head_queue else head
        return {"head": {"sha": sha}, "auto_merge": auto_merge, "base": {"ref": "main"}}

    monkeypatch.setattr(source, "_run_json", fake)
    return calls


@pytest.mark.asyncio
async def test_pending_review_returns_the_single_pending_draft(monkeypatch) -> None:
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    _stub_run_json(monkeypatch, _pending_reviews_payload())
    result = await source.pull_request_pending_review(_SUBMIT_PR_URL)
    digest = result.pop("contentDigest")
    assert len(digest) == 64, "digest should be a sha256 hex string"
    assert result == {
        "reviewId": "4242",
        "body": "[code-review-sage] draft",
        # The inline comments come back too: `contentDigest` binds them, so returning
        # only the body would have the digest certify text the reader never saw.
        "comments": [],
        "commitId": _HEAD_SHA,
        "headSha": _HEAD_SHA,
        "stale": False,
        "contentRedacted": False,
        "autoMergeArmed": False,
        "staleDismissalEnabled": True,
    }


@pytest.mark.asyncio
async def test_pending_review_returns_the_inline_comments(monkeypatch) -> None:
    """The digest binds them, so the reader has to be able to see them."""
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    _stub_run_json(monkeypatch, _pending_reviews_payload())
    monkeypatch.setattr(
        source,
        "_github_pending_review_comments",
        AsyncMock(
            return_value=[
                {"path": "src/auth.py", "line": 42, "body": "widens the token scope"},
                {"path": "src/x.py", "line": None, "body": "no anchor"},
            ]
        ),
    )
    result = await source.pull_request_pending_review(_SUBMIT_PR_URL)
    assert result["comments"] == [
        {"path": "src/auth.py", "line": 42, "body": "widens the token scope"},
        {"path": "src/x.py", "line": None, "body": "no anchor"},
    ]


@pytest.mark.asyncio
async def test_pending_review_reports_no_draft_when_none_is_pending(monkeypatch) -> None:
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    _stub_run_json(monkeypatch, [{"id": 11, "state": "APPROVED"}])
    assert await source.pull_request_pending_review(_SUBMIT_PR_URL) == {
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


@pytest.mark.asyncio
async def test_pending_review_redacts_a_credential_in_the_draft_body(monkeypatch) -> None:
    """The body is provider-controlled text; a hand-written draft can quote a secret."""
    secret = "ghp_0123456789abcdefghijklmnopqrstuvwx"
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    _stub_run_json(
        monkeypatch,
        [
            {
                "id": 4242,
                "state": "PENDING",
                "body": f"use {secret} to deploy",
                "commit_id": _HEAD_SHA,
            },
        ],
    )
    result = await source.pull_request_pending_review(_SUBMIT_PR_URL)
    assert result["reviewId"] == "4242"
    assert secret not in result["body"]
    # Redaction altered the draft, so the publish path must be able to refuse.
    assert result["contentRedacted"] is True


@pytest.mark.asyncio
async def test_pending_review_reports_a_draft_written_against_an_older_head(
    monkeypatch,
) -> None:
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    _stub_run_json(
        monkeypatch,
        [
            {"id": 4242, "state": "PENDING", "body": "ok", "commit_id": "a" * 40},
        ],
    )
    result = await source.pull_request_pending_review(_SUBMIT_PR_URL)
    assert result["stale"] is True
    assert result["commitId"] == "a" * 40
    assert result["headSha"] == _HEAD_SHA


@pytest.mark.asyncio
async def test_pending_review_treats_an_unknown_head_as_stale(monkeypatch) -> None:
    """Fail closed: an unanswerable freshness question is not 'current'."""
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    _stub_run_json(
        monkeypatch,
        [
            {"id": 4242, "state": "PENDING", "body": "ok", "commit_id": _HEAD_SHA},
        ],
        head="",
    )
    assert (await source.pull_request_pending_review(_SUBMIT_PR_URL))["stale"] is True


@pytest.mark.asyncio
async def test_pending_review_detects_a_credential_in_an_inline_comment(
    monkeypatch,
) -> None:
    """Submission publishes every stored comment, not just the body this app reads."""
    secret = "ghp_0123456789abcdefghijklmnopqrstuvwx"
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "clean body", "commit_id": _HEAD_SHA}],
        comments=[{"body": f"token is {secret}"}],
    )
    result = await source.pull_request_pending_review(_SUBMIT_PR_URL)
    assert result["body"] == "clean body"
    assert result["contentRedacted"] is True


@pytest.mark.asyncio
async def test_pending_review_refuses_an_issue_url(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="points at an issue"):
        await source.pull_request_pending_review(_ISSUE_URL)
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_review_posts_the_event_for_the_pending_review(monkeypatch) -> None:
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    invalidate = AsyncMock()
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", invalidate)
    calls = _stub_run_json(monkeypatch, _pending_reviews_payload())
    result = await source.submit_pull_request_review(
        _SUBMIT_PR_URL, "4242", "approve", _digest("[code-review-sage] draft")
    )
    assert result == {"submitted": True, "event": "APPROVE"}
    # The cache is dropped BEFORE the mutation, so a cancelled request can never
    # leave a stale generation able to satisfy a post-mutation refresh.
    invalidate.assert_awaited_once()
    submit_calls = [c for c in calls if "POST" in c]
    assert len(submit_calls) == 1
    assert submit_calls[0] == (
        "gh",
        "api",
        "-X",
        "POST",
        "repos/acme/repo/pulls/7/reviews/4242/events",
        "-f",
        "event=APPROVE",
    )
    # A gating verdict re-reads the head AFTER submitting, so the last call is that
    # check rather than the submit itself.
    assert calls[-1] == ("gh", "api", "repos/acme/repo/pulls/7")


@pytest.mark.parametrize("event", ["APPROVE", "REQUEST_CHANGES", "COMMENT"])
@pytest.mark.asyncio
async def test_submit_review_refuses_a_draft_written_against_an_older_head(
    monkeypatch, event
) -> None:
    """A stale APPROVE is the dangerous case, but no verdict is right on a moved head.

    Repositories without stale-approval dismissal count a stale APPROVE as a live
    approval of code nobody read, and inline comments anchor to lines that may be
    gone -- so every event is refused, not just the verdicts.
    """
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    invalidate = AsyncMock()
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", invalidate)
    calls = _stub_run_json(
        monkeypatch,
        [
            {"id": 4242, "state": "PENDING", "body": "ok", "commit_id": "a" * 40},
        ],
    )
    with pytest.raises(ValueError, match="written against an earlier commit"):
        await source.submit_pull_request_review(_SUBMIT_PR_URL, "4242", event, _digest("ok"))
    assert not any("POST" in c for c in calls)
    invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_review_refuses_a_draft_whose_text_needs_redaction(
    monkeypatch,
) -> None:
    """Submission publishes GitHub's stored draft, not the redacted copy we showed.

    So a draft the dashboard rendered as `[REDACTED]` would go out verbatim. Refuse:
    a leak the user was shown as redacted is worse than no publish button.
    """
    secret = "ghp_0123456789abcdefghijklmnopqrstuvwx"
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    invalidate = AsyncMock()
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", invalidate)
    calls = _stub_run_json(
        monkeypatch,
        [
            {"id": 4242, "state": "PENDING", "body": f"use {secret}", "commit_id": _HEAD_SHA},
        ],
    )
    with pytest.raises(ValueError, match="must be redacted"):
        await source.submit_pull_request_review(
            _SUBMIT_PR_URL, "4242", "COMMENT", _digest(f"use {secret}")
        )
    assert not any("POST" in c for c in calls)
    invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_review_refuses_when_only_an_inline_comment_needs_redaction(
    monkeypatch,
) -> None:
    secret = "ghp_0123456789abcdefghijklmnopqrstuvwx"
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", AsyncMock())
    calls = _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "clean", "commit_id": _HEAD_SHA}],
        comments=[{"body": f"token {secret}"}],
    )
    with pytest.raises(ValueError, match="must be redacted"):
        await source.submit_pull_request_review(
            _SUBMIT_PR_URL, "4242", "COMMENT", _digest("clean", [{"body": f"token {secret}"}])
        )
    assert not any("POST" in c for c in calls)


@pytest.mark.asyncio
async def test_pending_review_scans_every_page_of_inline_comments(monkeypatch) -> None:
    """The comments endpoint returns 30 per page; a page-one-only scan leaks.

    Drives the multi-page `--paginate --slurp` shape directly: the credential sits
    on the SECOND page, which an unpaginated read would clear for publishing.
    """
    secret = "ghp_0123456789abcdefghijklmnopqrstuvwx"
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())

    async def fake(*argv, **kwargs):
        path = next((a for a in argv if a.startswith("repos/")), "")
        if path.endswith("/reviews"):
            return [[{"id": 4242, "state": "PENDING", "body": "clean", "commit_id": _HEAD_SHA}]]
        if path.endswith("/comments"):
            assert "--paginate" in argv and "--slurp" in argv, "comment scan not paginated"
            return [
                [{"body": f"nit {i}"} for i in range(30)],  # page 1: clean
                [{"body": f"token {secret}"}],  # page 2: the leak
            ]
        if path.endswith("/protection"):
            return {"required_pull_request_reviews": {"dismiss_stale_reviews": True}}
        return {"head": {"sha": _HEAD_SHA}, "auto_merge": None, "base": {"ref": "main"}}

    monkeypatch.setattr(source, "_run_json", fake)
    result = await source.pull_request_pending_review(_SUBMIT_PR_URL)
    assert result["contentRedacted"] is True


@pytest.mark.asyncio
async def test_pending_review_finds_a_draft_past_the_first_page_of_reviews(
    monkeypatch,
) -> None:
    """The reviews list paginates too -- a draft on page two must not read as absent."""
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())

    async def fake(*argv, **kwargs):
        path = next((a for a in argv if a.startswith("repos/")), "")
        if path.endswith("/reviews"):
            assert "--paginate" in argv and "--slurp" in argv, "reviews list not paginated"
            return [
                [{"id": i, "state": "APPROVED", "body": ""} for i in range(30)],
                [{"id": 4242, "state": "PENDING", "body": "late draft", "commit_id": _HEAD_SHA}],
            ]
        if path.endswith("/comments"):
            return [[]]
        if path.endswith("/protection"):
            return {"required_pull_request_reviews": {"dismiss_stale_reviews": True}}
        return {"head": {"sha": _HEAD_SHA}, "auto_merge": None, "base": {"ref": "main"}}

    monkeypatch.setattr(source, "_run_json", fake)
    assert (await source.pull_request_pending_review(_SUBMIT_PR_URL))["reviewId"] == "4242"


@pytest.mark.parametrize("event", ["APPROVE", "REQUEST_CHANGES"])
@pytest.mark.asyncio
async def test_submit_review_dismisses_a_verdict_whose_head_moved_mid_publish(
    monkeypatch, event
) -> None:
    """GitHub's submit API takes no expected-head, so validate-then-submit is not atomic.

    A force-push landing in that window would otherwise leave a verdict attached to
    a head nobody reviewed. The verdict is dismissed again and the caller is told.
    """
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", AsyncMock())
    calls = _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "ok", "commit_id": _HEAD_SHA}],
        heads=[_HEAD_SHA, "b" * 40],  # validation sees the old head, re-read sees new
    )
    with pytest.raises(source.SourceProviderError, match="was dismissed again"):
        await source.submit_pull_request_review(_SUBMIT_PR_URL, "4242", event, _digest("ok"))
    assert any("PUT" in c for c in calls), "the stale verdict was not dismissed"


@pytest.mark.asyncio
async def test_submit_review_reports_loudly_when_a_stale_verdict_cannot_be_dismissed(
    monkeypatch,
) -> None:
    """An undismissable stale approval is precisely what a human must be told about."""
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", AsyncMock())
    _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "ok", "commit_id": _HEAD_SHA}],
        heads=[_HEAD_SHA, "b" * 40],
        dismiss_fails=True,
    )
    with pytest.raises(source.SourceProviderError, match="could NOT be dismissed"):
        await source.submit_pull_request_review(_SUBMIT_PR_URL, "4242", "APPROVE", _digest("ok"))


@pytest.mark.asyncio
async def test_submit_review_does_not_head_check_a_comment_only_review(
    monkeypatch,
) -> None:
    """A COMMENT carries no verdict, so a moved head costs nothing to gate on."""
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", AsyncMock())
    calls = _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "ok", "commit_id": _HEAD_SHA}],
        heads=[_HEAD_SHA, "b" * 40],
    )
    result = await source.submit_pull_request_review(
        _SUBMIT_PR_URL, "4242", "COMMENT", _digest("ok")
    )
    assert result == {"submitted": True, "event": "COMMENT"}
    assert not any("PUT" in c for c in calls)


@pytest.mark.asyncio
async def test_head_sha_is_read_from_the_object_not_via_jq(monkeypatch) -> None:
    """`gh api --jq .head.sha` prints a BARE token that `_run_json`'s json.loads rejects.

    That turned every pending-review read into a 503. The regression is invisible to
    a double that replaces `_run_json`, so this test pins BOTH halves: the argv must
    carry no `--jq`, and the value must be decoded out of the nested object.
    """
    seen: list[tuple] = []

    async def fake(*argv, **kwargs):
        seen.append(argv)
        return {
            "head": {"sha": _HEAD_SHA},
            "auto_merge": None,
            "number": 7,
            "base": {"ref": "main"},
        }

    monkeypatch.setattr(source, "_run_json", fake)
    ref = source._require_change_ref(source.parse_source_url(_SUBMIT_PR_URL))
    assert await source._github_pull_request_head_sha(ref) == _HEAD_SHA
    assert seen == [("gh", "api", "repos/acme/repo/pulls/7")]
    assert not any("--jq" in a for c in seen for a in c)


@pytest.mark.asyncio
async def test_head_sha_survives_a_payload_without_a_head_object(monkeypatch) -> None:
    """A missing/odd head must read as unknown -- which the caller treats as stale."""
    monkeypatch.setattr(source, "_run_json", AsyncMock(return_value={"number": 7}))
    ref = source._require_change_ref(source.parse_source_url(_SUBMIT_PR_URL))
    assert await source._github_pull_request_head_sha(ref) == ""


@pytest.mark.asyncio
async def test_submit_review_refuses_approve_while_auto_merge_is_armed(
    monkeypatch,
) -> None:
    """The one combination the post-submit dismissal cannot repair.

    Validate-then-submit is not atomic (GitHub offers no expected-head parameter),
    and with auto-merge armed the approval satisfies branch protection and GitHub can
    merge the unreviewed head BEFORE the compensating dismissal lands. Nothing
    repairs a merge, so APPROVE is refused for exactly this case.
    """
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    invalidate = AsyncMock()
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", invalidate)
    calls = _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "ok", "commit_id": _HEAD_SHA}],
        auto_merge={"enabled_by": {"login": "someone"}, "merge_method": "squash"},
    )
    with pytest.raises(ValueError, match="Auto-merge is armed"):
        await source.submit_pull_request_review(_SUBMIT_PR_URL, "4242", "APPROVE", _digest("ok"))
    assert not any("POST" in c for c in calls)
    invalidate.assert_not_awaited()


@pytest.mark.parametrize("event", ["COMMENT", "REQUEST_CHANGES"])
@pytest.mark.asyncio
async def test_submit_review_allows_non_approving_verdicts_under_auto_merge(
    monkeypatch, event
) -> None:
    """Only APPROVE can satisfy protection and let a merge through; the others cannot."""
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", AsyncMock())
    calls = _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "ok", "commit_id": _HEAD_SHA}],
        auto_merge={"merge_method": "squash"},
    )
    result = await source.submit_pull_request_review(_SUBMIT_PR_URL, "4242", event, _digest("ok"))
    assert result == {"submitted": True, "event": event}
    assert any("POST" in c for c in calls)


@pytest.mark.asyncio
async def test_pending_review_reports_auto_merge_so_the_ui_can_withhold_approve(
    monkeypatch,
) -> None:
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "ok", "commit_id": _HEAD_SHA}],
        auto_merge={"merge_method": "squash"},
    )
    assert (await source.pull_request_pending_review(_SUBMIT_PR_URL))["autoMergeArmed"] is True


@pytest.mark.asyncio
async def test_pull_request_state_treats_an_odd_auto_merge_shape_as_armed(
    monkeypatch,
) -> None:
    """Fail closed: an unrecognised `auto_merge` value must not read as safe."""
    monkeypatch.setattr(
        source,
        "_run_json",
        AsyncMock(return_value={"head": {"sha": _HEAD_SHA}, "auto_merge": "yes"}),
    )
    ref = source._require_change_ref(source.parse_source_url(_SUBMIT_PR_URL))
    assert (await source._github_pull_request_state(ref))["autoMergeArmed"] is True


@pytest.mark.asyncio
async def test_submit_review_rejects_an_unknown_event(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="APPROVE, REQUEST_CHANGES, or COMMENT"):
        await source.submit_pull_request_review(_SUBMIT_PR_URL, "4242", "DISMISS", "d")
    run.assert_not_awaited()


@pytest.mark.parametrize("review_id", ["", "0", "abc", "42; rm -rf /", "../99", "4242 "])
@pytest.mark.asyncio
async def test_submit_review_rejects_a_malformed_review_id(monkeypatch, review_id) -> None:
    """The id is interpolated into the REST path, so only a bare positive int passes."""
    run = AsyncMock()
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="valid review id"):
        await source.submit_pull_request_review(_SUBMIT_PR_URL, review_id, "COMMENT", "d")
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_review_refuses_a_draft_the_caller_did_not_read(monkeypatch) -> None:
    """A stale id must be rejected, never resolved to whatever draft exists now.

    Otherwise a review the human started by hand after the page loaded would be
    published in place of the one the caller was shown.
    """
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    invalidate = AsyncMock()
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", invalidate)
    calls = _stub_run_json(monkeypatch, _pending_reviews_payload())
    with pytest.raises(ValueError, match="no longer pending"):
        await source.submit_pull_request_review(_SUBMIT_PR_URL, "999", "APPROVE", "d")
    assert not any("POST" in c for c in calls)  # reads only, never submit
    invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_review_refuses_an_issue_url(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="points at an issue"):
        await source.submit_pull_request_review(_ISSUE_URL, "4242", "COMMENT", "d")
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_review_refuses_a_gitlab_merge_request(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="only be published on GitHub"):
        await source.submit_pull_request_review(
            "https://gitlab.com/acme/repo/-/merge_requests/7", "4242", "COMMENT", "d"
        )
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_and_comment_endpoints_require_the_owner(monkeypatch) -> None:
    # These write to a third-party pull request under the owner's provider
    # identity, so they inherit resolve's owner gate rather than defining their
    # own. An app-scoped caller must not reach them.
    reply = AsyncMock()
    comment = AsyncMock()
    unresolve = AsyncMock()
    monkeypatch.setattr(source, "reply_to_review_thread", reply)
    monkeypatch.setattr(source, "comment_on_pull_request", comment)
    monkeypatch.setattr(source, "unresolve_pull_request_thread", unresolve)

    url = "https://github.com/acme/repo/pull/12"
    app = _app(app_name="some-app")
    async with TestClient(TestServer(app)) as client:
        replied = await client.post(
            "/api/source/pull-request/reply",
            json={"url": url, "threadId": "PRRT_1", "body": "hi"},
        )
        commented = await client.post(
            "/api/source/pull-request/comment", json={"url": url, "body": "hi"}
        )
        unresolved = await client.post(
            "/api/source/pull-request/unresolve",
            json={"url": url, "threadId": "PRRT_1"},
        )

    assert replied.status == 403
    assert commented.status == 403
    assert unresolved.status == 403
    reply.assert_not_awaited()
    comment.assert_not_awaited()
    unresolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_endpoint_passes_the_body_through(monkeypatch) -> None:
    reply = AsyncMock()
    monkeypatch.setattr(source, "reply_to_review_thread", reply)
    url = "https://github.com/acme/repo/pull/12"
    app = _app()
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/source/pull-request/reply",
            json={"url": url, "threadId": "PRRT_1", "body": "Agreed"},
        )
        assert response.status == 200
        assert await response.json() == {"posted": True}
    reply.assert_awaited_once_with(url, "PRRT_1", "Agreed")


def _digest(body, comments=()):
    return source._review_content_digest(body, list(comments))


def test_content_digest_changes_with_every_publishable_field() -> None:
    """The digest must move when anything GitHub would publish moves."""
    base = _digest("body", [{"id": 1, "path": "a.py", "line": 3, "body": "nit"}])
    assert base != _digest("edited", [{"id": 1, "path": "a.py", "line": 3, "body": "nit"}])
    assert base != _digest("body", [{"id": 1, "path": "a.py", "line": 3, "body": "changed"}])
    assert base != _digest("body", [{"id": 1, "path": "b.py", "line": 3, "body": "nit"}])
    assert base != _digest("body", [{"id": 1, "path": "a.py", "line": 9, "body": "nit"}])
    assert base != _digest("body", [])  # comment removed
    assert base != _digest(
        "body",
        [
            {"id": 1, "path": "a.py", "line": 3, "body": "nit"},
            {"id": 2, "path": "a.py", "line": 4, "body": "more"},
        ],
    )  # comment added


def test_content_digest_is_stable_under_reordering() -> None:
    """A re-ordered read of identical content must not read as an edit."""
    a = {"id": 1, "path": "a.py", "line": 3, "body": "one"}
    b = {"id": 2, "path": "a.py", "line": 4, "body": "two"}
    assert _digest("body", [a, b]) == _digest("body", [b, a])


@pytest.mark.asyncio
async def test_submit_review_refuses_a_draft_edited_since_it_was_displayed(
    monkeypatch,
) -> None:
    """The review id identifies the OBJECT; GitHub lets its content change under it.

    A draft edited after the UI rendered it would otherwise publish text the caller
    never read, with the id guard none the wiser.
    """
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    invalidate = AsyncMock()
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", invalidate)
    calls = _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "edited since display", "commit_id": _HEAD_SHA}],
    )
    with pytest.raises(ValueError, match="changed after it was displayed"):
        await source.submit_pull_request_review(
            _SUBMIT_PR_URL,
            "4242",
            "COMMENT",
            _digest("what the caller read"),
        )
    assert not any("POST" in c for c in calls)
    invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_review_accepts_the_digest_it_was_shown(monkeypatch) -> None:
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", AsyncMock())
    reviews = [{"id": 4242, "state": "PENDING", "body": "unchanged", "commit_id": _HEAD_SHA}]
    calls = _stub_run_json(monkeypatch, reviews)
    result = await source.submit_pull_request_review(
        _SUBMIT_PR_URL,
        "4242",
        "COMMENT",
        _digest("unchanged"),
    )
    assert result == {"submitted": True, "event": "COMMENT"}
    assert any("POST" in c for c in calls)


@pytest.mark.parametrize("digest", ["", None])
@pytest.mark.asyncio
async def test_submit_review_refuses_a_missing_content_digest(monkeypatch, digest) -> None:
    """An omitted digest must FAIL, never skip the comparison.

    A digest that is only checked when present is a one-parameter bypass of the
    content binding: any caller omitting it publishes an unseen draft.
    """
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    invalidate = AsyncMock()
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", invalidate)
    calls = _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "ok", "commit_id": _HEAD_SHA}],
    )
    with pytest.raises(ValueError, match="contentDigest is required"):
        await source.submit_pull_request_review(_SUBMIT_PR_URL, "4242", "COMMENT", digest or "")
    assert not any("POST" in c for c in calls)
    invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_review_refuses_approve_when_the_branch_keeps_stale_approvals(
    monkeypatch,
) -> None:
    """`dismiss_stale_reviews` is what makes a stale approval harmless.

    Without it, an approval can outlive the commit it reviewed regardless of how our
    own checks are ordered -- so APPROVE is withheld rather than raced.
    """
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    invalidate = AsyncMock()
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", invalidate)
    calls = _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "ok", "commit_id": _HEAD_SHA}],
        stale_dismissal=False,
    )
    with pytest.raises(ValueError, match="does not dismiss approvals"):
        await source.submit_pull_request_review(_SUBMIT_PR_URL, "4242", "APPROVE", _digest("ok"))
    assert not any("POST" in c for c in calls)
    invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_review_refuses_approve_when_protection_is_unreadable(
    monkeypatch,
) -> None:
    """Fail closed: no admin rights (or no protection) must not read as safe."""
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", AsyncMock())
    _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "ok", "commit_id": _HEAD_SHA}],
        protection_fails=True,
    )
    with pytest.raises(ValueError, match="does not dismiss approvals"):
        await source.submit_pull_request_review(_SUBMIT_PR_URL, "4242", "APPROVE", _digest("ok"))


@pytest.mark.parametrize("event", ["COMMENT", "REQUEST_CHANGES"])
@pytest.mark.asyncio
async def test_submit_review_allows_non_approving_verdicts_without_stale_dismissal(
    monkeypatch, event
) -> None:
    """Only an APPROVE can authorize a merge, so only APPROVE needs the setting."""
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", AsyncMock())
    calls = _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "ok", "commit_id": _HEAD_SHA}],
        stale_dismissal=False,
    )
    result = await source.submit_pull_request_review(_SUBMIT_PR_URL, "4242", event, _digest("ok"))
    assert result == {"submitted": True, "event": event}
    assert any("POST" in c for c in calls)


@pytest.mark.asyncio
async def test_pending_review_reports_stale_dismissal_for_the_ui(monkeypatch) -> None:
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock())
    _stub_run_json(
        monkeypatch,
        [{"id": 4242, "state": "PENDING", "body": "ok", "commit_id": _HEAD_SHA}],
        stale_dismissal=False,
    )
    got = await source.pull_request_pending_review(_SUBMIT_PR_URL)
    assert got["staleDismissalEnabled"] is False


class TestStaleDismissalTwoSurfaces:
    """APPROVE needs a confirmed `dismiss stale reviews`, from whichever read can see it."""

    @staticmethod
    def _ref():
        return source.SourceRef(
            provider="github",
            url="https://github.com/o/r/pull/7",
            host="github.com",
            owner="o",
            repo="r",
            number=7,
        )

    @pytest.mark.asyncio
    async def test_graphql_answers_when_rest_is_forbidden(self, monkeypatch):
        # The non-admin case: REST protection is admin-only, so it raises; GraphQL sees the
        # rule. Withholding here would send a contributor back to github.com to approve.
        async def fake_run_json(*args, **kwargs):
            if "graphql" in args:
                return {
                    "data": {
                        "repository": {
                            "branchProtectionRules": {
                                "nodes": [
                                    {"pattern": "main", "dismissesStaleReviews": True},
                                ]
                            }
                        }
                    }
                }
            raise RuntimeError("HTTP 403: admin rights required")

        monkeypatch.setattr(source, "_run_json", fake_run_json)
        assert await source._github_stale_dismissal_enabled(self._ref(), "main") is True

    @pytest.mark.asyncio
    async def test_withheld_when_neither_surface_confirms(self, monkeypatch):
        async def fake_run_json(*args, **kwargs):
            if "graphql" in args:
                return {"data": {"repository": {"branchProtectionRules": {"nodes": []}}}}
            raise RuntimeError("HTTP 404: branch not protected")

        monkeypatch.setattr(source, "_run_json", fake_run_json)
        assert await source._github_stale_dismissal_enabled(self._ref(), "main") is False

    @pytest.mark.asyncio
    async def test_a_rule_for_another_branch_says_nothing(self, monkeypatch):
        # A rule on `releases/*` tells us nothing about `main`; treating any rule as
        # covering any branch would approve against an unprotected base.
        async def fake_run_json(*args, **kwargs):
            if "graphql" in args:
                return {
                    "data": {
                        "repository": {
                            "branchProtectionRules": {
                                "nodes": [
                                    {"pattern": "releases/*", "dismissesStaleReviews": True},
                                ]
                            }
                        }
                    }
                }
            raise RuntimeError("HTTP 403")

        monkeypatch.setattr(source, "_run_json", fake_run_json)
        assert await source._github_stale_dismissal_enabled(self._ref(), "main") is False

    @pytest.mark.asyncio
    async def test_a_glob_that_covers_the_branch_counts(self, monkeypatch):
        async def fake_run_json(*args, **kwargs):
            if "graphql" in args:
                return {
                    "data": {
                        "repository": {
                            "branchProtectionRules": {
                                "nodes": [
                                    {"pattern": "releases/*", "dismissesStaleReviews": True},
                                ]
                            }
                        }
                    }
                }
            raise RuntimeError("HTTP 403")

        monkeypatch.setattr(source, "_run_json", fake_run_json)
        assert await source._github_stale_dismissal_enabled(self._ref(), "releases/1.2") is True

    @pytest.mark.asyncio
    async def test_a_glob_does_not_reach_a_deeper_branch(self, monkeypatch):
        # GitHub matches protection patterns per segment: `releases/*` covers
        # `releases/1.2` but NOT `releases/1/2`. Python's fnmatch would match both,
        # so this asserts the SITE uses the slash-aware matcher -- a fail-open here
        # lets a stale approval survive on a branch with no dismissal rule at all.
        async def fake_run_json(*args, **kwargs):
            if "graphql" in args:
                return {
                    "data": {
                        "repository": {
                            "branchProtectionRules": {
                                "nodes": [
                                    {"pattern": "releases/*", "dismissesStaleReviews": True},
                                ]
                            }
                        }
                    }
                }
            raise RuntimeError("HTTP 403")

        monkeypatch.setattr(source, "_run_json", fake_run_json)
        assert await source._github_stale_dismissal_enabled(self._ref(), "releases/1/2") is False


class TestBranchPatternSlashSemantics:
    """GitHub matches protection patterns per path segment; Python's fnmatch does
    not. Getting this wrong treats an unprotected branch as protected, which is a
    fail-OPEN on the APPROVE verdict."""

    def test_single_star_does_not_cross_a_slash(self):
        assert source._branch_pattern_matches("releases/*", "releases/1.0")
        assert not source._branch_pattern_matches("releases/*", "releases/1/0")

    def test_double_star_spans_segments(self):
        assert source._branch_pattern_matches("releases/**", "releases/1/0")
        assert source._branch_pattern_matches("releases/**", "releases/1.0")

    def test_exact_pattern_still_matches_and_is_case_sensitive(self):
        assert source._branch_pattern_matches("main", "main")
        assert not source._branch_pattern_matches("Main", "main")

    def test_deeper_branch_is_not_covered_by_a_shallow_pattern(self):
        # The reported fail-open: a `releases/*` rule must not open APPROVE for
        # `releases/x/y`, which GitHub does not protect.
        assert not source._branch_pattern_matches("*", "releases/x")

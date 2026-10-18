"""Pull and merge request mutations: resolving a review thread, arming auto-merge
and marking a draft ready, each validated before dispatch and each invalidating the
cached payload."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from source_providers_helpers import _clear_issue_cache  # noqa: F401
from source_providers_helpers import _mock_source_sel_fixture  # noqa: F401
from source_providers_helpers import _no_visibility_refresh_side_task  # noqa: F401
from source_providers_helpers import _app

from kiro_crew.dashboard.handlers import source_providers as source


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_id", ["", "bad id with spaces", "x" * 129, "semi;colon"])
async def test_resolve_rejects_invalid_thread_ids(thread_id: str) -> None:
    with pytest.raises(ValueError):
        await source.resolve_pull_request_thread("https://github.com/acme/repo/pull/12", thread_id)


@pytest.mark.asyncio
async def test_resolve_github_dispatches_graphql_mutation_and_busts_cache(monkeypatch) -> None:
    membership = {
        "data": {
            "repository": {"pullRequest": {"reviewThreads": {"nodes": [{"id": "PRRT_thread1"}]}}}
        }
    }
    run = AsyncMock(side_effect=[membership, {}])
    monkeypatch.setattr(source, "_run_json", run)
    url = "https://github.com/acme/repo/pull/12"
    source._CACHE[url] = (0.0, 21, {"provider": "github"})

    await source.resolve_pull_request_thread(url, "PRRT_thread1")

    assert run.await_count == 2
    membership_argv = run.await_args_list[0].args
    assert "owner=acme" in membership_argv
    assert "repo=repo" in membership_argv
    assert "number=12" in membership_argv
    mutation_argv = run.await_args_list[1].args
    assert any("resolveReviewThread" in part for part in mutation_argv)
    assert "threadId=PRRT_thread1" in mutation_argv
    assert url not in source._CACHE


@pytest.mark.asyncio
async def test_resolve_cancellation_after_dispatch_keeps_cache_invalidated(monkeypatch) -> None:
    membership = {
        "data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [{"id": "PRRT_1"}]}}}}
    }
    run = AsyncMock(side_effect=[membership, source.asyncio.CancelledError()])
    monkeypatch.setattr(source, "_run_json", run)
    url = "https://github.com/acme/repo/pull/12"
    source._CACHE[url] = (0.0, 21, {"provider": "github", "stale": True})
    release = source.asyncio.Event()

    async def stale_fetch():
        # flake-ok: pre-existing unbounded hold in the stub until the test releases it, moved verbatim; tracked for a de-flake pass
        await release.wait()
        return {"provider": "github", "stale": True}

    stale_task = source.asyncio.create_task(stale_fetch())
    source._FULL_FETCH_INFLIGHT[url] = stale_task
    source._FULL_FETCH_TASKS[url] = {stale_task}

    try:
        with pytest.raises(source.asyncio.CancelledError):
            await source.resolve_pull_request_thread(url, "PRRT_1")

        assert url not in source._CACHE
        assert url not in source._FULL_FETCH_INFLIGHT
        assert source._FULL_FETCH_GENERATIONS[url] == 1
        assert stale_task in source._FULL_FETCH_TASKS[url]
    finally:
        release.set()
        # flake-ok: pre-existing unbounded await of a task the test has already released, moved verbatim; tracked for a de-flake pass
        await stale_task
        source._CACHE.clear()
        source._FULL_FETCH_INFLIGHT.clear()
        source._FULL_FETCH_TASKS.clear()
        source._FULL_FETCH_GENERATIONS.clear()


@pytest.mark.asyncio
async def test_resolve_github_rejects_thread_from_another_pull_request(monkeypatch) -> None:
    membership = {
        "data": {
            "repository": {"pullRequest": {"reviewThreads": {"nodes": [{"id": "PRRT_other"}]}}}
        }
    }
    run = AsyncMock(return_value=membership)
    monkeypatch.setattr(source, "_run_json", run)

    with pytest.raises(ValueError, match="does not belong"):
        await source.resolve_pull_request_thread(
            "https://github.com/acme/repo/pull/12", "PRRT_thread1"
        )

    run.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_gitlab_rejects_path_shaped_thread_id() -> None:
    with pytest.raises(ValueError, match="valid thread id"):
        await source.resolve_pull_request_thread(
            "https://gitlab.com/acme/repo/-/merge_requests/42", "../other"
        )


@pytest.mark.asyncio
async def test_resolve_gitlab_dispatches_discussion_put(monkeypatch) -> None:
    run = AsyncMock(return_value={})
    monkeypatch.setattr(source, "_run_json", run)

    await source.resolve_pull_request_thread(
        "https://gitlab.com/acme/platform/service/-/merge_requests/42", "a1b2c3"
    )

    argv = run.call_args.args
    assert argv[0] == "glab"
    assert "PUT" in argv
    assert "projects/acme%2Fplatform%2Fservice/merge_requests/42/discussions/a1b2c3" in argv
    assert "resolved=true" in argv
    # host must be forwarded or _run_json's guard refuses the mutation.
    assert run.call_args.kwargs.get("host") == "gitlab.com"


@pytest.mark.asyncio
async def test_auto_merge_github_picks_allowed_method_and_busts_cache(monkeypatch) -> None:
    node = {
        "data": {
            "repository": {
                "squashMergeAllowed": False,
                "mergeCommitAllowed": True,
                "rebaseMergeAllowed": True,
                "pullRequest": {"id": "PR_node1", "isDraft": False, "state": "OPEN"},
            }
        }
    }
    run = AsyncMock(side_effect=[node, {}])
    monkeypatch.setattr(source, "_run_json", run)
    url = "https://github.com/acme/repo/pull/12"
    source._CACHE[url] = (0.0, 21, {"provider": "github"})

    try:
        assert await source.enable_pull_request_auto_merge(url) == "merge"
    finally:
        source._CACHE.clear()

    mutation_argv = run.await_args_list[1].args
    assert any("enablePullRequestAutoMerge" in part for part in mutation_argv)
    assert "pullRequestId=PR_node1" in mutation_argv
    assert "mergeMethod=MERGE" in mutation_argv
    assert url not in source._CACHE


@pytest.mark.asyncio
async def test_auto_merge_github_refuses_draft_before_dispatch(monkeypatch) -> None:
    node = {
        "data": {
            "repository": {
                "squashMergeAllowed": True,
                "pullRequest": {"id": "PR_node1", "isDraft": True, "state": "OPEN"},
            }
        }
    }
    run = AsyncMock(return_value=node)
    monkeypatch.setattr(source, "_run_json", run)

    with pytest.raises(ValueError, match="draft"):
        await source.enable_pull_request_auto_merge("https://github.com/acme/repo/pull/12")

    run.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_merge_github_refuses_when_already_armed(monkeypatch) -> None:
    node = {
        "data": {
            "repository": {
                "squashMergeAllowed": True,
                "pullRequest": {
                    "id": "PR_node1",
                    "isDraft": False,
                    "autoMergeRequest": {"enabledAt": "2026-07-13T10:00:00Z"},
                },
            }
        }
    }
    monkeypatch.setattr(source, "_run_json", AsyncMock(return_value=node))

    with pytest.raises(ValueError, match="already enabled"):
        await source.enable_pull_request_auto_merge("https://github.com/acme/repo/pull/12")


@pytest.mark.asyncio
async def test_auto_merge_github_rejects_unusable_node_id(monkeypatch) -> None:
    node = {"data": {"repository": {"squashMergeAllowed": True, "pullRequest": {"id": "bad id"}}}}
    monkeypatch.setattr(source, "_run_json", AsyncMock(return_value=node))

    with pytest.raises(source.SourceProviderError, match="usable pull-request id"):
        await source.enable_pull_request_auto_merge("https://github.com/acme/repo/pull/12")


@pytest.mark.asyncio
async def test_auto_merge_github_refuses_when_no_method_allowed(monkeypatch) -> None:
    node = {
        "data": {
            "repository": {
                "squashMergeAllowed": False,
                "mergeCommitAllowed": False,
                "rebaseMergeAllowed": False,
                "pullRequest": {"id": "PR_node1", "isDraft": False},
            }
        }
    }
    run = AsyncMock(return_value=node)
    monkeypatch.setattr(source, "_run_json", run)

    with pytest.raises(ValueError, match="merge method"):
        await source.enable_pull_request_auto_merge("https://github.com/acme/repo/pull/12")

    run.assert_awaited_once()


def _github_already_mergeable_node(merge_state: str) -> dict:
    """A GitHub node response for an open PR GitHub would refuse to arm.

    ``CLEAN``/``UNSTABLE`` mean every required check is settled, so nothing is
    left for ``enablePullRequestAutoMerge`` to wait on and GitHub refuses it with
    ``Pull request is in clean/unstable status``.
    """
    return {
        "data": {
            "repository": {
                "squashMergeAllowed": True,
                "mergeCommitAllowed": True,
                "rebaseMergeAllowed": True,
                "pullRequest": {
                    "id": "PR_node1",
                    "headRefOid": "deadbeef00",
                    "isDraft": False,
                    "state": "OPEN",
                    "mergeStateStatus": merge_state,
                },
            }
        }
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("merge_state", ["CLEAN", "UNSTABLE", "HAS_HOOKS"])
async def test_auto_merge_github_already_mergeable_refusal_is_answerable(
    monkeypatch, merge_state: str
) -> None:
    """An already-mergeable GitHub PR raises an ANSWERABLE refusal, not a 503.

    GitHub refuses ``enablePullRequestAutoMerge`` once a PR is already mergeable
    (``CLEAN``/``UNSTABLE``). Dispatching the arming mutation blind would turn that
    normal, user-actionable state into ``gh: Pull request ... is in unstable
    status`` surfaced as an HTTP 503 ``provider_error``. Instead the GitHub branch
    mirrors the GitLab immediate-merge handshake: it raises ``ConfirmationRequired``
    (HTTP 400, answerable), and merges NOTHING until the caller confirms.
    """
    run = AsyncMock(return_value=_github_already_mergeable_node(merge_state))
    monkeypatch.setattr(source, "_run_json", run)

    with pytest.raises(source.ConfirmationRequired, match="merged now"):
        await source.enable_pull_request_auto_merge("https://github.com/acme/repo/pull/12")

    # Only the precondition read happened: the blind arming mutation that 503s
    # must never be dispatched for this state.
    run.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("merge_state", ["CLEAN", "UNSTABLE", "HAS_HOOKS"])
async def test_auto_merge_github_merges_already_mergeable_only_when_confirmed(
    monkeypatch, merge_state: str
) -> None:
    """With consent, an already-mergeable GitHub PR is merged NOW, not armed.

    The confirm answer dispatches ``mergePullRequest`` (immediate merge), never
    ``enablePullRequestAutoMerge`` -- which GitHub would reject for this state.
    """
    node = _github_already_mergeable_node(merge_state)
    run = AsyncMock(side_effect=[node, {}])
    monkeypatch.setattr(source, "_run_json", run)
    url = "https://github.com/acme/repo/pull/12"
    source._CACHE[url] = (0.0, 21, {"provider": "github"})

    try:
        result = await source.enable_pull_request_auto_merge(url, confirm_immediate_merge=True)
        assert result == "squash"
    finally:
        source._CACHE.clear()

    mutation_argv = run.await_args_list[1].args
    assert any("mergePullRequest" in part for part in mutation_argv)
    assert not any("enablePullRequestAutoMerge" in part for part in mutation_argv)
    assert "pullRequestId=PR_node1" in mutation_argv
    assert "mergeMethod=SQUASH" in mutation_argv
    # The merge is pinned to the exact head the read returned, so a push landing
    # between the read and this dispatch makes GitHub reject it rather than
    # merging a head the operator never saw.
    assert any("expectedHeadOid" in part for part in mutation_argv)
    assert "head=deadbeef00" in mutation_argv
    assert url not in source._CACHE


@pytest.mark.asyncio
async def test_auto_merge_github_refuses_immediate_merge_without_a_head_oid(
    monkeypatch,
) -> None:
    """A confirmed immediate merge needs the read head to pin to; absent it, refuse.

    ``expectedHeadOid`` is required for the pin, so a node response that carries no
    ``headRefOid`` must raise rather than dispatch an unpinned merge.
    """
    node = _github_already_mergeable_node("CLEAN")
    node["data"]["repository"]["pullRequest"].pop("headRefOid")
    run = AsyncMock(side_effect=[node, {}])
    monkeypatch.setattr(source, "_run_json", run)

    with pytest.raises(source.SourceProviderError, match="head commit"):
        await source.enable_pull_request_auto_merge(
            "https://github.com/acme/repo/pull/12", confirm_immediate_merge=True
        )

    # Only the precondition read happened; no unpinned merge was dispatched.
    run.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("merge_state", ["BLOCKED", "BEHIND", "UNKNOWN", ""])
async def test_auto_merge_github_still_arms_when_something_is_pending(
    monkeypatch, merge_state: str
) -> None:
    """A PR with work left to wait for still ARMS auto-merge, unchanged.

    The already-mergeable handshake must not swallow the ordinary case: a state
    other than ``CLEAN``/``UNSTABLE`` dispatches ``enablePullRequestAutoMerge``
    with no confirmation required.
    """
    node = _github_already_mergeable_node(merge_state)
    run = AsyncMock(side_effect=[node, {}])
    monkeypatch.setattr(source, "_run_json", run)

    result = await source.enable_pull_request_auto_merge("https://github.com/acme/repo/pull/12")
    assert result == "squash"

    mutation_argv = run.await_args_list[1].args
    assert any("enablePullRequestAutoMerge" in part for part in mutation_argv)


@pytest.mark.asyncio
async def test_github_pull_request_node_query_requests_merge_state_status() -> None:
    """The node query must fetch ``mergeStateStatus`` or the handshake is blind.

    Without this field the GitHub branch cannot tell "already mergeable" from
    "still pending" and falls back to dispatching the arming mutation blind.
    """
    assert "mergeStateStatus" in source._GITHUB_PULL_REQUEST_NODE_QUERY


@pytest.mark.asyncio
async def test_auto_merge_gitlab_dispatches_merge_when_pipeline_succeeds(monkeypatch) -> None:
    run = AsyncMock(side_effect=[{"head_pipeline": {"status": "running"}}, {}])
    monkeypatch.setattr(source, "_run_json", run)

    method = await source.enable_pull_request_auto_merge(
        "https://gitlab.com/acme/platform/service/-/merge_requests/42"
    )

    assert method == "pipeline"
    argv = run.await_args_list[1].args
    assert argv[0] == "glab"
    assert "PUT" in argv
    assert "projects/acme%2Fplatform%2Fservice/merge_requests/42/merge" in argv
    assert "merge_when_pipeline_succeeds=true" in argv


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("details", "expected"),
    [
        ({"draft": True, "head_pipeline": {"status": "running"}}, "draft"),
        ({"work_in_progress": True, "head_pipeline": {"status": "running"}}, "draft"),
        (
            {"merge_when_pipeline_succeeds": True, "head_pipeline": {"status": "running"}},
            "already enabled",
        ),
        ({"head_pipeline": {"status": "success"}}, "immediately"),
        ({}, "immediately"),
    ],
)
async def test_auto_merge_gitlab_refuses_inapplicable_requests_before_dispatch(
    monkeypatch, details: dict, expected: str
) -> None:
    run = AsyncMock(return_value=details)
    monkeypatch.setattr(source, "_run_json", run)

    with pytest.raises(ValueError, match=expected):
        await source.enable_pull_request_auto_merge(
            "https://gitlab.com/acme/platform/service/-/merge_requests/42"
        )

    # Only the precondition read happened: nothing was merged.
    run.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_merge_gitlab_merges_without_pipeline_only_when_confirmed(monkeypatch) -> None:
    run = AsyncMock(side_effect=[{"head_pipeline": {"status": "success"}}, {}])
    monkeypatch.setattr(source, "_run_json", run)

    method = await source.enable_pull_request_auto_merge(
        "https://gitlab.com/acme/platform/service/-/merge_requests/42",
        confirm_immediate_merge=True,
    )

    assert method == "pipeline"
    assert "merge_when_pipeline_succeeds=true" in run.await_args_list[1].args


@pytest.mark.asyncio
async def test_auto_merge_gitlab_immediate_refusal_is_answerable(monkeypatch) -> None:
    """The no-pipeline refusal is distinguishable from an ordinary rejection.

    A client can only offer a meaningful acknowledgement if it can tell this
    refusal apart from a permanent one, so it carries its own type.
    """
    monkeypatch.setattr(source, "_run_json", AsyncMock(return_value={}))

    with pytest.raises(source.ConfirmationRequired):
        await source.enable_pull_request_auto_merge(
            "https://gitlab.com/acme/platform/service/-/merge_requests/42"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("confirm", ["false", "true", 1, 0, {}, [], None, "yes"])
async def test_auto_merge_handler_rejects_non_boolean_confirmation(
    monkeypatch, confirm: object
) -> None:
    """Only a real JSON boolean counts as consent.

    ``bool()`` coercion would read the string ``"false"`` -- and every other
    truthy value -- as an acknowledgement, so a malformed client would satisfy
    the very guard standing between it and an immediate merge.
    """
    action = AsyncMock(return_value="squash")
    monkeypatch.setattr(source, "enable_pull_request_auto_merge", action)
    monkeypatch.setattr(source, "_sel", lambda: MagicMock())

    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request/auto-merge",
            json={"url": "https://github.com/acme/repo/pull/12", "confirmImmediateMerge": confirm},
        )
        assert response.status == 400
        assert (await response.json())["error"] == "confirmImmediateMerge must be true or false."

    action.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_merge_handler_marks_confirmation_required_refusals(monkeypatch) -> None:
    """The 400 carries a machine-readable marker, not just prose."""
    monkeypatch.setattr(
        source,
        "enable_pull_request_auto_merge",
        AsyncMock(side_effect=source.ConfirmationRequired("No pipeline is pending.")),
    )
    monkeypatch.setattr(source, "_sel", lambda: MagicMock())

    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request/auto-merge",
            json={"url": "https://gitlab.com/acme/platform/service/-/merge_requests/42"},
        )
        assert response.status == 400
        assert await response.json() == {
            "error": "No pipeline is pending.",
            "confirmationRequired": True,
        }


@pytest.mark.asyncio
async def test_ordinary_rejections_carry_no_confirmation_marker(monkeypatch) -> None:
    """Only an answerable refusal invites a retry with the acknowledgement."""
    monkeypatch.setattr(
        source,
        "enable_pull_request_auto_merge",
        AsyncMock(side_effect=ValueError("Auto-merge is already enabled.")),
    )
    monkeypatch.setattr(source, "_sel", lambda: MagicMock())

    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request/auto-merge",
            json={"url": "https://github.com/acme/repo/pull/12"},
        )
        assert response.status == 400
        assert await response.json() == {"error": "Auto-merge is already enabled."}


@pytest.mark.asyncio
async def test_ready_github_dispatches_mutation_and_busts_cache(monkeypatch) -> None:
    node = {"data": {"repository": {"pullRequest": {"id": "PR_node1", "isDraft": True}}}}
    run = AsyncMock(side_effect=[node, {}])
    monkeypatch.setattr(source, "_run_json", run)
    url = "https://github.com/acme/repo/pull/12"
    source._CACHE[url] = (0.0, 21, {"provider": "github"})

    try:
        await source.mark_pull_request_ready(url)
    finally:
        source._CACHE.clear()

    mutation_argv = run.await_args_list[1].args
    assert any("markPullRequestReadyForReview" in part for part in mutation_argv)
    assert "pullRequestId=PR_node1" in mutation_argv
    assert url not in source._CACHE


@pytest.mark.asyncio
async def test_ready_github_refuses_non_draft_before_dispatch(monkeypatch) -> None:
    node = {"data": {"repository": {"pullRequest": {"id": "PR_node1", "isDraft": False}}}}
    run = AsyncMock(return_value=node)
    monkeypatch.setattr(source, "_run_json", run)

    with pytest.raises(ValueError, match="already ready"):
        await source.mark_pull_request_ready("https://github.com/acme/repo/pull/12")

    run.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("details", [{"draft": True}, {"work_in_progress": True}])
async def test_ready_gitlab_uses_set_draft_mutation_not_a_title_rewrite(
    monkeypatch, details: dict
) -> None:
    run = AsyncMock(side_effect=[details, {"data": {"mergeRequestSetDraft": {"errors": []}}}])
    monkeypatch.setattr(source, "_run_json", run)

    await source.mark_pull_request_ready(
        "https://gitlab.com/acme/platform/service/-/merge_requests/42"
    )

    argv = run.await_args_list[1].args
    assert "graphql" in argv
    assert any("mergeRequestSetDraft" in part for part in argv)
    assert "projectPath=acme/platform/service" in argv
    assert "iid=42" in argv
    assert "draft=false" in argv
    # The title is never read back out or written, so a concurrent retitle and a
    # title that merely starts with a draft-like word are both left alone.
    assert not any(str(part).startswith("title=") for part in argv)


@pytest.mark.asyncio
async def test_ready_gitlab_refuses_when_not_draft(monkeypatch) -> None:
    run = AsyncMock(return_value={"title": "Drafting widgets", "draft": False})
    monkeypatch.setattr(source, "_run_json", run)

    with pytest.raises(ValueError, match="already ready"):
        await source.mark_pull_request_ready(
            "https://gitlab.com/acme/platform/service/-/merge_requests/42"
        )

    run.assert_awaited_once()


@pytest.mark.asyncio
async def test_ready_gitlab_raises_when_mutation_reports_errors(monkeypatch) -> None:
    run = AsyncMock(
        side_effect=[
            {"draft": True},
            {"data": {"mergeRequestSetDraft": {"errors": ["Not allowed"]}}},
        ]
    )
    monkeypatch.setattr(source, "_run_json", run)

    # GraphQL reports refusals in the body with HTTP 200, so a rejected mutation
    # must not read as success.
    with pytest.raises(source.SourceProviderError, match="refused"):
        await source.mark_pull_request_ready(
            "https://gitlab.com/acme/platform/service/-/merge_requests/42"
        )


@pytest.mark.asyncio
async def test_mutation_invalidates_chip_status_cache(monkeypatch) -> None:
    # The chips read a separate, shorter-lived cache from the full payload, so a
    # mutation must bust both or the chips keep showing pre-mutation state.
    url = "https://github.com/acme/repo/pull/12"
    source._check_cache[url] = (time.monotonic(), {"state": "draft"})
    try:
        await source._invalidate_pull_request_cache(url)
        assert url not in source._check_cache
    finally:
        source._check_cache.pop(url, None)
        source._check_generations.pop(url, None)


@pytest.mark.asyncio
async def test_inflight_status_refresh_cannot_restore_superseded_state(monkeypatch) -> None:
    url = "https://github.com/acme/repo/pull/12"
    started = asyncio.Event()
    release = asyncio.Event()

    async def fetch(_url: str) -> dict[str, str]:
        started.set()
        # flake-ok: pre-existing unbounded hold in the stub until the test releases it, moved verbatim; tracked for a de-flake pass
        await release.wait()
        return {"state": "draft"}

    monkeypatch.setattr(source, "_fetch_check_status", fetch)
    source._check_cache[url] = (time.monotonic(), {"state": "draft"})
    try:
        task = asyncio.create_task(source._refresh_check_status(url))
        # flake-ok: pre-existing unbounded wait for the stub to signal it started, moved verbatim; tracked for a de-flake pass
        await started.wait()
        # The mutation lands while the refresh is still in flight.
        await source._invalidate_pull_request_cache(url)
        release.set()
        # flake-ok: pre-existing unbounded await of a task the test has already released, moved verbatim; tracked for a de-flake pass
        await task

        assert url not in source._check_cache
    finally:
        source._check_cache.pop(url, None)
        source._check_generations.pop(url, None)
        source._check_inflight.discard(url)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    ["enable_pull_request_auto_merge", "mark_pull_request_ready"],
)
async def test_mutations_reject_unsupported_urls(action: str) -> None:
    with pytest.raises(ValueError):
        await getattr(source, action)("https://example.com/acme/repo/pull/12")

"""GitLab review-thread replies, thread reopen, and top-level merge-request comments.

These write to a merge request under the owner's glab identity, so each repeats
the resolve path's contract: a validated url, a shape-checked discussion id that
only ever reaches the merge request's own scoped path, the cache invalidated
BEFORE dispatch, and the host forwarded so ``_run_json``'s allowlist guard
applies. Like resolve, reopen sends one write and no read: the scoped path
itself refuses a foreign or unresolvable discussion.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from kiro_crew.dashboard.handlers import source_providers as source

MR_URL = "https://gitlab.com/acme/platform/service/-/merge_requests/42"
PROJECT = "projects/acme%2Fplatform%2Fservice/merge_requests/42"
THREAD = "a1b2c3d4"


def _discussion(thread_id: str = THREAD, *, resolvable: bool = True) -> dict:
    return {
        "id": thread_id,
        "individual_note": not resolvable,
        "notes": [{"id": 1, "body": "please fix", "resolvable": resolvable}],
    }


def _recorder(discussion: dict | None = None):
    """A ``_run_json`` stand-in: records every call, answers a discussion read if one is sent."""
    calls: list[tuple[tuple, dict]] = []

    async def run(*argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[:2] == ("glab", "api") and argv[2].endswith(f"/discussions/{THREAD}"):
            return discussion if discussion is not None else _discussion()
        return {"id": 99}

    return run, calls


@pytest.fixture(autouse=True)
def _gitlab_allowed(_floor_monkeypatch):
    _floor_monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock(return_value=None))


# --- reply ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reply_posts_a_note_into_the_discussion(monkeypatch) -> None:
    run, calls = _recorder()
    monkeypatch.setattr(source, "_run_json", run)
    await source.reply_to_review_thread(MR_URL, THREAD, "Agreed")

    # One call: the write itself is the merge request's own scoped path, so a
    # foreign id is refused by GitLab there, with no separate read-back.
    (post,) = calls
    argv, kwargs = post
    assert argv[:4] == ("glab", "api", "-X", "POST")
    assert f"{PROJECT}/discussions/{THREAD}/notes" in argv
    assert argv[argv.index("body=Agreed") - 1] == "-f"
    assert kwargs.get("host") == "gitlab.com"


@pytest.mark.asyncio
async def test_reply_propagates_a_provider_refusal_of_the_write(monkeypatch) -> None:
    # A discussion GitLab will not find under this merge request (404) fails the
    # scoped write, and the error reaches the caller.
    calls: list[tuple] = []

    async def run(*argv, **kwargs):
        calls.append(argv)
        raise source.SourceProviderError("404 Not found")

    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(source.SourceProviderError):
        await source.reply_to_review_thread(MR_URL, THREAD, "Agreed")
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["../other", "a1b2/notes", "", "zz" * 3, "a" * 129])
async def test_reply_rejects_a_malformed_discussion_id(monkeypatch, bad) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="valid thread id"):
        await source.reply_to_review_thread(MR_URL, bad, "hi")
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_refuses_an_empty_body_before_any_call(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="comment body is required"):
        await source.reply_to_review_thread(MR_URL, THREAD, "  \n")
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_nul_in_a_reply_or_comment_body_is_refused_before_any_call(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="NUL character"):
        await source.reply_to_review_thread(MR_URL, THREAD, "hi\x00there")
    with pytest.raises(ValueError, match="NUL character"):
        await source.comment_on_pull_request(MR_URL, "hi\x00there")
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_body_starting_with_at_is_sent_as_text(monkeypatch) -> None:
    run, calls = _recorder()
    monkeypatch.setattr(source, "_run_json", run)
    await source.reply_to_review_thread(MR_URL, THREAD, "@/etc/passwd")
    argv = calls[-1][0]
    assert argv[argv.index("body=@/etc/passwd") - 1] == "-f"
    assert "-F" not in argv


@pytest.mark.asyncio
async def test_reply_invalidates_the_cache_before_dispatch(
    monkeypatch,
) -> None:
    order: list[str] = []

    async def invalidate(url):
        order.append("invalidate")

    async def run(*argv, **kwargs):
        order.append("write" if "-X" in argv else "read")
        return _discussion() if "-X" not in argv else {"id": 1}

    monkeypatch.setattr(source, "_invalidate_pull_request_cache", invalidate)
    monkeypatch.setattr(source, "_run_json", run)
    await source.reply_to_review_thread(MR_URL, THREAD, "Agreed")
    assert order == ["invalidate", "write"]


# --- reopen -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_unresolve_puts_resolved_false_on_the_discussion(monkeypatch) -> None:
    run, calls = _recorder()
    monkeypatch.setattr(source, "_run_json", run)
    await source.unresolve_pull_request_thread(MR_URL, THREAD)

    argv, kwargs = calls[-1]
    assert argv[:4] == ("glab", "api", "-X", "PUT")
    assert f"{PROJECT}/discussions/{THREAD}" in argv
    assert argv[argv.index("resolved=false") - 1] == "-f"
    assert "resolved=true" not in argv
    assert kwargs.get("host") == "gitlab.com"


@pytest.mark.asyncio
async def test_unresolve_sends_one_write_and_no_read(monkeypatch) -> None:
    # The scoped PUT itself refuses a foreign or unresolvable discussion, so a
    # pre-flight read would only add a round trip (resolve sends none either).
    run, calls = _recorder()
    monkeypatch.setattr(source, "_run_json", run)
    await source.unresolve_pull_request_thread(MR_URL, THREAD)
    assert [argv[:4] for argv, _ in calls] == [("glab", "api", "-X", "PUT")]


@pytest.mark.asyncio
async def test_unresolve_rejects_a_malformed_discussion_id(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="valid thread id"):
        await source.unresolve_pull_request_thread(MR_URL, "../../x")
    run.assert_not_awaited()


# --- top-level comment ------------------------------------------------------


@pytest.mark.asyncio
async def test_comment_posts_to_the_merge_request_notes(monkeypatch) -> None:
    run, calls = _recorder()
    monkeypatch.setattr(source, "_run_json", run)
    await source.comment_on_pull_request(MR_URL, "Looks good")

    [(argv, kwargs)] = calls
    assert argv[:4] == ("glab", "api", "-X", "POST")
    assert f"{PROJECT}/notes" in argv
    assert argv[argv.index("body=Looks good") - 1] == "-f"
    assert kwargs.get("host") == "gitlab.com"


@pytest.mark.asyncio
async def test_comment_refuses_a_gitlab_issue_url(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    with pytest.raises(ValueError, match="points at an issue"):
        await source.comment_on_pull_request(
            "https://gitlab.com/acme/platform/service/-/issues/42", "hello"
        )
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_comment_refuses_an_unlisted_self_managed_host(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)
    monkeypatch.setattr(source, "_allowed_gitlab_hosts", lambda: frozenset())
    with pytest.raises(ValueError):
        await source.comment_on_pull_request(
            "https://gitlab.internal.example/a/b/-/merge_requests/1", "hello"
        )
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_comment_invalidates_the_cache_before_dispatch(monkeypatch) -> None:
    order: list[str] = []

    async def invalidate(url):
        order.append("invalidate")

    async def run(*argv, **kwargs):
        order.append("write")
        return {"id": 1}

    monkeypatch.setattr(source, "_invalidate_pull_request_cache", invalidate)
    monkeypatch.setattr(source, "_run_json", run)
    await source.comment_on_pull_request(MR_URL, "Looks good")
    assert order == ["invalidate", "write"]

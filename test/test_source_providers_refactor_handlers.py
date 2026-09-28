"""The HTTP contract of the source-provider handlers, pinned over their owners.

Each ``/api/source/*`` handler authorizes the dashboard owner, reads a JSON body,
calls one owner, audits the outcome, and maps its failure to a status and a
body. These cases pin that mapping -- status, body, error ``code`` and audit
reason -- for every handler, so the handler layer answers exactly as it did when
the provider code lived beside it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard.handlers import source_providers as sp

_PR = "https://github.com/acme/repo/pull/12"


class _Request:
    """An authenticated dashboard request carrying a JSON body."""

    def __init__(
        self,
        body: Any = None,
        *,
        user: str = "U_OWNER",
        app: object = "",
        owner_id: str = "U_OWNER",
        json_error: BaseException | None = None,
    ) -> None:
        state = MagicMock()
        state.owner_id = owner_id
        self.app = {"state": state}
        self._claims: dict[str, object] = {"user": user, "app": app}
        self._body = body
        self._json_error = json_error

    def get(self, key: str, default: object = None) -> object:
        return self._claims.get(key, default)

    def __contains__(self, key: object) -> bool:
        return key in self._claims

    def __getitem__(self, key: str) -> object:
        return self._claims[key]

    async def json(self) -> Any:
        if self._json_error is not None:
            raise self._json_error
        return self._body


@pytest.fixture
def audit(monkeypatch) -> MagicMock:
    recorder = MagicMock()
    monkeypatch.setattr(sp, "_sel", lambda: recorder)
    return recorder


def _audited(audit: MagicMock) -> list[tuple[str, str, str]]:
    return [
        (call.kwargs["operation"], call.kwargs["outcome"], call.kwargs["error"])
        for call in audit.log_api_access.call_args_list
    ]


def _body(response: Any) -> Any:
    return json.loads(response.body)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "status", "body", "reason"),
    [
        (
            ValueError("bad url"),
            400,
            {"error": "bad url", "code": "invalid_request"},
            "invalid_request",
        ),
        (
            ValueError("jira_no_credentials: set a token"),
            400,
            {"error": "jira_no_credentials: set a token", "code": "jira_no_credentials"},
            "jira_no_credentials",
        ),
        (
            ValueError("jira_config_error: unreadable"),
            400,
            {"error": "jira_config_error: unreadable", "code": "jira_config_error"},
            "jira_config_error",
        ),
        (
            sp.SourceProviderError("provider down"),
            503,
            {"error": "provider down", "code": "provider_error"},
            "provider_error",
        ),
        (
            sp.SourceCapacityError("busy"),
            503,
            {"error": "busy", "code": "source_busy"},
            "capacity_exhausted",
        ),
    ],
)
async def test_the_issue_read_maps_each_failure_to_its_code(
    monkeypatch, audit, exc, status, body, reason
) -> None:
    monkeypatch.setattr(sp, "fetch_issue", AsyncMock(side_effect=exc))

    response = await sp.api_issue_source(_Request({"url": "https://acme.atlassian.net/browse/P-1"}))

    assert (response.status, _body(response)) == (status, body)
    assert _audited(audit) == [("source.issue.read", "failed", reason)]


@pytest.mark.asyncio
async def test_the_issue_read_passes_url_and_refresh_through(monkeypatch, audit) -> None:
    fetch = AsyncMock(return_value={"title": "t"})
    monkeypatch.setattr(sp, "fetch_issue", fetch)

    response = await sp.api_issue_source(_Request({"url": "u", "refresh": 1}))

    assert (response.status, _body(response)) == (200, {"title": "t"})
    fetch.assert_awaited_once_with("u", refresh=True)
    assert _audited(audit) == [("source.issue.read", "completed", "")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "status", "body", "reason"),
    [
        (ValueError("nope"), 400, {"error": "nope", "code": "invalid_request"}, "invalid_request"),
        (
            sp.SourceProviderError("down"),
            503,
            {"error": "down", "code": "provider_error"},
            "provider_error",
        ),
    ],
)
async def test_the_contributors_read_maps_each_failure(
    monkeypatch, audit, exc, status, body, reason
) -> None:
    monkeypatch.setattr(sp, "fetch_app_contributors", AsyncMock(side_effect=exc))

    response = await sp.api_app_contributors(_Request({"url": "https://github.com/acme/repo"}))

    assert (response.status, _body(response)) == (status, body)
    assert _audited(audit) == [("source.contributors.read", "failed", reason)]


@pytest.mark.asyncio
async def test_the_contributors_read_wraps_the_list(monkeypatch, audit) -> None:
    fetch = AsyncMock(return_value=[{"login": "a"}])
    monkeypatch.setattr(sp, "fetch_app_contributors", fetch)

    response = await sp.api_app_contributors(_Request({"url": "r", "refresh": True}))

    assert _body(response) == {"contributors": [{"login": "a"}]}
    fetch.assert_awaited_once_with("r", refresh=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "status", "body", "reason"),
    [
        (
            ValueError("gh only"),
            400,
            {"error": "gh only", "code": "invalid_request"},
            "invalid_request",
        ),
        (
            sp.SourceProviderError("down"),
            503,
            {"error": "down", "code": "provider_error"},
            "provider_error",
        ),
    ],
)
async def test_the_pending_review_read_maps_each_failure(
    monkeypatch, audit, exc, status, body, reason
) -> None:
    monkeypatch.setattr(sp, "pull_request_pending_review", AsyncMock(side_effect=exc))

    response = await sp.api_pull_request_pending_review(_Request({"url": _PR}))

    assert (response.status, _body(response)) == (status, body)
    assert _audited(audit) == [("source.pull_request.pending_review", "failed", reason)]


@pytest.mark.asyncio
async def test_the_pending_review_read_returns_the_draft(monkeypatch, audit) -> None:
    read = AsyncMock(return_value={"reviewId": "7"})
    monkeypatch.setattr(sp, "pull_request_pending_review", read)

    response = await sp.api_pull_request_pending_review(_Request({"url": _PR}))

    assert (response.status, _body(response)) == (200, {"reviewId": "7"})
    read.assert_awaited_once_with(_PR)
    assert _audited(audit) == [("source.pull_request.pending_review", "completed", "")]


@pytest.mark.asyncio
async def test_the_pending_review_read_is_owner_only(monkeypatch, audit) -> None:
    read = AsyncMock()
    monkeypatch.setattr(sp, "pull_request_pending_review", read)

    response = await sp.api_pull_request_pending_review(_Request({"url": _PR}, user="U_OTHER"))

    assert (response.status, _body(response)) == (403, {"error": "forbidden"})
    read.assert_not_awaited()
    assert _audited(audit) == [("source.pull_request.pending_review", "denied", "non_owner")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "owner_call", "body", "expected_args", "payload"),
    [
        (
            "api_pull_request_unresolve",
            "unresolve_pull_request_thread",
            {"url": _PR, "threadId": "T1"},
            (_PR, "T1"),
            {"resolved": False},
        ),
        (
            "api_pull_request_comment",
            "comment_on_pull_request",
            {"url": _PR, "body": "hi"},
            (_PR, "hi"),
            {"posted": True},
        ),
        (
            "api_pull_request_submit_review",
            "submit_pull_request_review",
            {"url": _PR, "reviewId": "9", "event": "COMMENT", "contentDigest": "d"},
            (_PR, "9", "COMMENT", "d"),
            {"submitted": True},
        ),
    ],
)
async def test_each_mutation_forwards_its_body_fields(
    monkeypatch, audit, handler, owner_call, body, expected_args, payload
) -> None:
    call = AsyncMock(return_value=payload if handler.endswith("submit_review") else None)
    monkeypatch.setattr(sp, owner_call, call)

    response = await getattr(sp, handler)(_Request(body))

    assert (response.status, _body(response)) == (200, payload)
    call.assert_awaited_once_with(*expected_args)
    assert _audited(audit)[-1][1:] == ("completed", "")


@pytest.mark.asyncio
async def test_a_confirmation_refusal_is_marked_answerable(monkeypatch, audit) -> None:
    monkeypatch.setattr(
        sp,
        "enable_pull_request_auto_merge",
        AsyncMock(side_effect=sp.ConfirmationRequired("would merge now")),
    )

    response = await sp.api_pull_request_auto_merge(_Request({"url": _PR}))

    assert (response.status, _body(response)) == (
        400,
        {"error": "would merge now", "confirmationRequired": True},
    )
    assert _audited(audit) == [("source.pull_request.auto_merge", "failed", "invalid_request")]


@pytest.mark.asyncio
async def test_an_unexpected_mutation_error_is_audited_and_propagates(monkeypatch, audit) -> None:
    monkeypatch.setattr(sp, "mark_pull_request_ready", AsyncMock(side_effect=KeyError("x")))

    with pytest.raises(KeyError):
        await sp.api_pull_request_ready(_Request({"url": _PR}))

    assert _audited(audit) == [("source.pull_request.ready", "failed", "internal_error")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "operation"),
    [
        ("api_pull_request_source", "source.pull_request.read"),
        ("api_issue_source", "source.issue.read"),
        ("api_app_contributors", "source.contributors.read"),
        ("api_pull_request_checks", "source.pull_request.checks"),
        ("api_pull_request_status", "source.pull_request.status"),
        ("api_pull_request_pending_review", "source.pull_request.pending_review"),
        ("api_pull_request_resolve", "source.pull_request.resolve"),
        ("api_pull_request_ready", "source.pull_request.ready"),
    ],
)
async def test_a_cancelled_body_read_is_audited_then_reraised(audit, handler, operation) -> None:
    with pytest.raises(asyncio.CancelledError):
        await getattr(sp, handler)(_Request(json_error=asyncio.CancelledError()))

    assert _audited(audit) == [(operation, "failed", "request_cancelled")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "owner_call", "operation"),
    [
        ("api_issue_source", "fetch_issue", "source.issue.read"),
        ("api_app_contributors", "fetch_app_contributors", "source.contributors.read"),
        ("api_pull_request_checks", "fetch_pull_request_checks", "source.pull_request.checks"),
        (
            "api_pull_request_pending_review",
            "pull_request_pending_review",
            "source.pull_request.pending_review",
        ),
    ],
)
async def test_a_cancelled_provider_call_is_audited_then_reraised(
    monkeypatch, audit, handler, owner_call, operation
) -> None:
    monkeypatch.setattr(sp, owner_call, AsyncMock(side_effect=asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await getattr(sp, handler)(_Request({"url": _PR}))

    assert _audited(audit) == [(operation, "failed", "request_cancelled")]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [["not", "a", "dict"], "text"])
async def test_a_non_object_body_reads_as_empty(monkeypatch, audit, body) -> None:
    fetch = AsyncMock(return_value={})
    monkeypatch.setattr(sp, "fetch_pull_request", fetch)

    await sp.api_pull_request_source(_Request(body))

    fetch.assert_awaited_once_with("", refresh=False)


@pytest.mark.asyncio
async def test_an_unparseable_body_reads_as_empty(monkeypatch, audit) -> None:
    fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(sp, "fetch_pull_request_checks", fetch)

    response = await sp.api_pull_request_checks(_Request(json_error=ValueError("not json")))

    assert _body(response) == {"checks": []}
    fetch.assert_awaited_once_with("")


@pytest.mark.asyncio
async def test_the_status_read_audits_a_cancelled_allowlist_warmup(monkeypatch, audit) -> None:
    monkeypatch.setattr(
        sp, "ensure_gitlab_hosts_loaded", AsyncMock(side_effect=asyncio.CancelledError())
    )

    with pytest.raises(asyncio.CancelledError):
        await sp.api_pull_request_status(_Request({"urls": [_PR]}))

    assert _audited(audit) == [("source.pull_request.status", "failed", "request_cancelled")]


@pytest.mark.asyncio
async def test_the_status_read_skips_non_strings_and_duplicates(monkeypatch, audit) -> None:
    monkeypatch.setattr(sp, "ensure_gitlab_hosts_loaded", AsyncMock(return_value=frozenset()))
    scheduled: list[list[str]] = []
    monkeypatch.setattr(sp, "schedule_check_refresh", lambda urls: scheduled.append(urls) or [])
    monkeypatch.setattr(sp, "get_cached_check_status", lambda url: {"state": "open"})

    response = await sp.api_pull_request_status(
        _Request({"urls": [7, _PR, _PR + "/", "https://github.com/acme/repo/issues/3"]})
    )

    assert _body(response) == {
        "statuses": {_PR: {"state": "open"}},
        "refreshing": [],
        "ttlSecs": sp.CHECK_STATUS_TTL_SECS,
    }
    assert scheduled == [[_PR]]


def _owner_request(*, user: str, app: object = "", owner_id: str = "U_OWNER") -> _Request:
    return _Request(user=user, app=app, owner_id=owner_id)


@pytest.mark.parametrize(
    "request_",
    [
        _owner_request(user="local-app", app="some-app"),
        _owner_request(user="U_SOMEONE"),
        _owner_request(user="local-app", owner_id=""),
    ],
)
def test_only_a_signed_bootstrap_subject_under_an_owner_gets_the_stale_label(request_) -> None:
    assert sp.stale_owner_session_response(request_) is None


def test_a_signed_bootstrap_subject_under_an_owner_is_told_to_sign_in_again() -> None:
    response = sp.stale_owner_session_response(_owner_request(user="local-startup"))

    assert response is not None
    assert response.status == 401
    assert _body(response)["code"] == sp.STALE_OWNER_SESSION_CODE


def test_an_unevaluable_request_is_not_an_owner_view() -> None:
    assert sp.owner_view_for_request(MagicMock(app={})) is False
    assert sp.owner_view_for_request(_owner_request(user="U_OWNER")) is True


def test_an_app_token_is_never_the_owner() -> None:
    assert sp.is_owner_dashboard_request(_owner_request(user="U_OWNER", app="an-app")) is False

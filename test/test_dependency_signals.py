"""Dependency adapters: real error shapes -> one DependencySignal vocabulary."""

from __future__ import annotations

from email.utils import formatdate

import pytest

from kiro_crew.acp.client import AcpError
from kiro_crew.monitoring import github_pull_request, github_workflow_run
from kiro_crew.monitoring.models import ProviderErrorKind
from kiro_crew.taskq import dependency
from kiro_crew.taskq.adapters import acp_provider, github, http
from kiro_crew.taskq.dependency import (
    KIND_AUTH_FAILED,
    KIND_CONCURRENCY_EXCEEDED,
    KIND_DEPENDENCY_UNAVAILABLE,
    KIND_PERMANENT_PARAM_ERROR,
    KIND_QUOTA_EXHAUSTED,
    KIND_RATE_LIMITED,
    DependencySignal,
    classify_exception,
    register_adapter,
    registered_adapters,
    unregister_adapter,
)

NOW = 1_700_000_000.0


class HttpErr(Exception):
    """Duck-typed HTTP error: the fields urllib / aiohttp / httpx errors carry."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        headers: dict[str, str] | None = None,
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        if status is not None:
            self.status = status
        if headers is not None:
            self.headers = headers
        if url is not None:
            self.url = url
        self.observed_at = NOW


# ── DependencySignal ──────────────────────────────────────────────────────────


class TestSignal:
    def test_terminal_kinds_are_never_retryable(self) -> None:
        for kind in (KIND_AUTH_FAILED, KIND_PERMANENT_PARAM_ERROR):
            sig = DependencySignal(kind=kind, dependency_scope="x", source="t", retryable=True)
            assert sig.retryable is False and sig.terminal

    def test_quota_without_reset_is_terminal_with_reset_is_retryable(self) -> None:
        no_reset = DependencySignal(kind=KIND_QUOTA_EXHAUSTED, dependency_scope="x", source="t")
        assert no_reset.terminal
        reset = DependencySignal(
            kind=KIND_QUOTA_EXHAUSTED, dependency_scope="x", source="t", retry_at=NOW + 60
        )
        assert reset.retryable

    def test_unknown_kind_and_empty_scope_refused(self) -> None:
        with pytest.raises(ValueError):
            DependencySignal(kind="bogus", dependency_scope="x", source="t")
        with pytest.raises(ValueError):
            DependencySignal(kind=KIND_RATE_LIMITED, dependency_scope="", source="t")

    def test_round_trips_through_dict(self) -> None:
        sig = DependencySignal(
            kind=KIND_RATE_LIMITED,
            dependency_scope="github:api",
            source="github",
            retry_at=NOW + 30,
            detail="x" * 600,
        )
        back = DependencySignal.from_dict(sig.to_dict())
        assert back == sig
        assert len(sig.detail) == 500
        assert DependencySignal.from_dict({"kind": "nope"}) is None


# ── GitHub adapter ────────────────────────────────────────────────────────────


class TestGitHubAdapter:
    def test_primary_rate_limit_403_uses_x_ratelimit_reset(self) -> None:
        exc = HttpErr(
            "API rate limit exceeded for installation ID 1",
            status=403,
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(NOW + 900))},
            url="https://api.github.com/repos/o/r/pulls",
        )
        sig = classify_exception(exc)
        assert sig is not None
        assert sig.kind == KIND_RATE_LIMITED
        assert sig.source == "github"
        assert sig.dependency_scope == "github:api"
        assert sig.retry_at == NOW + 900
        assert sig.retryable

    def test_secondary_limit_429_honours_retry_after_and_own_scope(self) -> None:
        exc = HttpErr(
            "You have exceeded a secondary rate limit",
            status=429,
            headers={"Retry-After": "60", "X-RateLimit-Remaining": "4000"},
            url="https://api.github.com/graphql",
        )
        sig = classify_exception(exc)
        assert sig is not None
        assert sig.kind == KIND_RATE_LIMITED
        assert sig.dependency_scope == "github:secondary"
        assert sig.retry_at == NOW + 60

    def test_retry_after_http_date(self) -> None:
        exc = HttpErr(
            "too many requests",
            status=429,
            headers={"Retry-After": formatdate(NOW + 120, usegmt=True)},
            url="https://api.github.com/x",
        )
        sig = classify_exception(exc)
        assert sig is not None and sig.retry_at is not None
        assert abs(sig.retry_at - (NOW + 120)) < 1.0

    def test_plain_403_is_auth_and_terminal(self) -> None:
        exc = HttpErr(
            "Resource not accessible by integration",
            status=403,
            headers={"X-RateLimit-Remaining": "4999"},
            url="https://api.github.com/x",
        )
        sig = classify_exception(exc)
        assert sig is not None
        assert sig.kind == KIND_AUTH_FAILED and sig.terminal

    def test_401_and_404_are_terminal(self) -> None:
        bad = classify_exception(
            HttpErr("Bad credentials", status=401, url="https://api.github.com/x")
        )
        assert bad is not None and bad.kind == KIND_AUTH_FAILED and bad.terminal
        gone = classify_exception(HttpErr("Not Found", status=404, url="https://api.github.com/x"))
        assert gone is not None and gone.kind == KIND_PERMANENT_PARAM_ERROR and gone.terminal

    def test_5xx_is_unavailable_and_retryable(self) -> None:
        sig = classify_exception(HttpErr("bad gateway", status=502, url="https://api.github.com/x"))
        assert sig is not None
        assert sig.kind == KIND_DEPENDENCY_UNAVAILABLE and sig.retryable and sig.retry_at is None

    def test_graphql_rate_limited_envelope(self) -> None:
        exc = HttpErr("graphql", url="https://api.github.com/graphql")
        exc.graphql_errors = [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]
        sig = classify_exception(exc)
        assert sig is not None
        assert sig.kind == KIND_RATE_LIMITED and sig.dependency_scope == "github:graphql"
        exc.graphql_errors = [
            {"type": "NOT_FOUND", "message": "Could not resolve to a Repository with the name"}
        ]
        sig = classify_exception(exc)
        assert sig is not None and sig.kind == KIND_PERMANENT_PARAM_ERROR

    def test_gh_cli_stderr_shapes(self) -> None:
        rate = github.classify_stderr(
            "HTTP 403: API rate limit exceeded for installation ID 123 (https://api.github.com/x)",
            now=NOW,
        )
        assert rate is not None and rate.kind == KIND_RATE_LIMITED
        rate429 = github.classify_stderr("gh: HTTP 429 Too Many Requests", now=NOW)
        assert rate429 is not None and rate429.kind == KIND_RATE_LIMITED
        auth = github.classify_stderr(
            "gh: To get started with GitHub CLI, run gh auth login", now=NOW
        )
        assert auth is not None and auth.kind == KIND_AUTH_FAILED
        missing = github.classify_stderr(
            "GraphQL: Could not resolve to a PullRequest with the number of 9 (HTTP 200)", now=NOW
        )
        assert missing is not None and missing.kind == KIND_PERMANENT_PARAM_ERROR
        down = github.classify_stderr("HTTP 502: Server Error", now=NOW)
        assert down is not None and down.kind == KIND_DEPENDENCY_UNAVAILABLE
        assert github.classify_stderr("some unrelated wording", now=NOW) is None

    def test_gh_stderr_429_behind_an_earlier_status_survives(self) -> None:
        failure = github.parse_gh_stderr("HTTP 200 ok then HTTP 429", now=NOW)
        assert failure.category == github.CATEGORY_RATE_LIMITED

    def test_gh_stderr_retry_after_in_text(self) -> None:
        failure = github.parse_gh_stderr("HTTP 429 (Retry-After: 45)", now=NOW)
        assert failure.retry_at == NOW + 45

    def test_non_github_error_is_left_to_the_http_adapter(self) -> None:
        sig = classify_exception(HttpErr("x", status=429, url="https://example.org/api"))
        assert sig is not None and sig.source == "http"

    def test_monitors_answer_what_the_shared_parser_says(self) -> None:
        cases = {
            "HTTP 403: API rate limit exceeded": ProviderErrorKind.RATE_LIMITED,
            "abuse detection mechanism triggered": ProviderErrorKind.RATE_LIMITED,
            "HTTP 429": ProviderErrorKind.RATE_LIMITED,
            "HTTP 401: Bad credentials": ProviderErrorKind.AUTHENTICATION,
            "gh auth login": ProviderErrorKind.AUTHENTICATION,
            "HTTP 403: Forbidden": ProviderErrorKind.AUTHORIZATION,
            "Resource not accessible by integration": ProviderErrorKind.AUTHORIZATION,
            "HTTP 404: Not Found": ProviderErrorKind.NOT_FOUND,
            "Could not resolve to a Repository": ProviderErrorKind.NOT_FOUND,
            "HTTP 502": ProviderErrorKind.TRANSIENT,
            "could not resolve host: api.github.com": ProviderErrorKind.TRANSIENT,
            "": ProviderErrorKind.TRANSIENT,
        }
        for raw, kind in cases.items():
            assert github_pull_request._classify_cli_error(raw) is kind, raw
            assert github_workflow_run._classify_cli_error(raw) is kind, raw
        assert (
            github_workflow_run._classify_cli_error("no runs found") is ProviderErrorKind.NOT_FOUND
        )


# ── generic HTTP adapter ──────────────────────────────────────────────────────


class TestHttpAdapter:
    def test_429_with_retry_after_scopes_to_host(self) -> None:
        sig = classify_exception(
            HttpErr("x", status=429, headers={"retry-after": "7"}, url="https://EXAMPLE.org/v1")
        )
        assert sig is not None
        assert sig.kind == KIND_RATE_LIMITED
        assert sig.dependency_scope == "http:example.org"
        assert sig.retry_at == NOW + 7

    def test_503_with_and_without_retry_after(self) -> None:
        with_hdr = classify_exception(
            HttpErr("x", status=503, headers={"Retry-After": "30"}, url="https://h.example/x")
        )
        assert with_hdr is not None
        assert with_hdr.kind == KIND_DEPENDENCY_UNAVAILABLE and with_hdr.retry_at == NOW + 30
        bare = classify_exception(HttpErr("x", status=503, url="https://h.example/x"))
        assert bare is not None and bare.retry_at is None and bare.retryable

    def test_caller_scope_hint_wins(self) -> None:
        sig = classify_exception(HttpErr("x", status=429, url="https://h.example/x"), "db:primary")
        assert sig is not None and sig.dependency_scope == "db:primary"

    def test_status_read_from_text_when_no_attribute(self) -> None:
        sig = classify_exception(Exception("upstream answered HTTP 503 Service Unavailable"))
        assert sig is not None
        assert sig.kind == KIND_DEPENDENCY_UNAVAILABLE and sig.dependency_scope == "http"

    def test_terminal_statuses(self) -> None:
        for status, kind in (
            (401, KIND_AUTH_FAILED),
            (404, KIND_PERMANENT_PARAM_ERROR),
            (422, KIND_PERMANENT_PARAM_ERROR),
        ):
            sig = classify_exception(HttpErr("x", status=status, url="https://h.example/x"))
            assert sig is not None and sig.kind == kind and sig.terminal, status

    def test_ordinary_statuses_and_plain_exceptions_are_not_dependency_errors(self) -> None:
        assert classify_exception(HttpErr("x", status=200, url="https://h.example/x")) is None
        assert classify_exception(HttpErr("x", status=418, url="https://h.example/x")) is None
        assert classify_exception(ValueError("nothing to do with a dependency")) is None

    def test_header_helpers(self) -> None:
        assert http.header([("Retry-After", "5")], "retry-after") == "5"
        assert http.header({"X-RateLimit-Reset": "1"}, "x-ratelimit-reset") == "1"
        assert http.header(None, "Retry-After") is None
        assert http.parse_retry_after("", NOW) is None
        assert http.parse_retry_after("garbage", NOW) is None
        # Milliseconds are normalised to seconds.
        assert http.parse_reset_epoch(str(int((NOW + 5) * 1000))) == pytest.approx(NOW + 5)


# ── ACP / provider adapter ───────────────────────────────────────────────────


class TestAcpProviderAdapter:
    def _err(self, msg: str, *, transient: bool | None) -> AcpError:
        exc = AcpError(msg, transient=transient)
        exc.observed_at = NOW  # type: ignore[attr-defined]
        return exc

    def test_bedrock_throttle_is_rate_limited(self) -> None:
        sig = classify_exception(self._err("ThrottlingException: Rate exceeded", transient=True))
        assert sig is not None
        assert sig.kind == KIND_RATE_LIMITED and sig.source == "acp_provider"
        assert sig.dependency_scope == "provider:acp"
        generic = classify_exception(
            self._err("Bedrock is throttling requests. Try: wait", transient=True)
        )
        assert generic is not None and generic.kind == KIND_RATE_LIMITED

    def test_service_quota_exceeded_is_concurrency(self) -> None:
        sig = classify_exception(
            self._err("ServiceQuotaExceededException: too many", transient=True)
        )
        assert sig is not None and sig.kind == KIND_CONCURRENCY_EXCEEDED and sig.retryable

    def test_usage_limit_is_quota_exhausted_and_terminal(self) -> None:
        sig = classify_exception(
            self._err("Your monthly usage limit has been reached", transient=False)
        )
        assert sig is not None and sig.kind == KIND_QUOTA_EXHAUSTED and sig.terminal

    def test_auth_shapes_are_terminal(self) -> None:
        for msg in ("AccessDeniedException: no", "session has expired, please sign in", "HTTP 401"):
            sig = classify_exception(self._err(msg, transient=False))
            assert sig is not None and sig.kind == KIND_AUTH_FAILED and sig.terminal, msg
        flagged = self._err("nothing recognisable", transient=False)
        flagged.auth_required = True
        sig = classify_exception(flagged)
        assert sig is not None and sig.kind == KIND_AUTH_FAILED

    def test_model_unavailable_and_5xx_are_unavailable(self) -> None:
        cases = (
            "The model 'foo' is not available",
            "The model you've selected is temporarily unavailable. Please use '/model'",
            "InternalServerError: Bedrock hiccup",
            "ECONNRESET while streaming",
        )
        for msg in cases:
            sig = classify_exception(self._err(msg, transient=True))
            assert sig is not None and sig.kind == KIND_DEPENDENCY_UNAVAILABLE, msg

    def test_malformed_request_is_permanent(self) -> None:
        sig = classify_exception(self._err("Improperly formed request", transient=False))
        assert sig is not None and sig.kind == KIND_PERMANENT_PARAM_ERROR

    def test_rejected_model_names_the_scope(self) -> None:
        exc = self._err("ThrottlingException", transient=True)
        exc.rejected_model = "model-x"
        sig = classify_exception(exc)
        assert sig is not None and sig.dependency_scope == "provider:model-x"

    def test_transient_verdict_without_wording_is_unavailable_and_unknown_is_none(self) -> None:
        sig = classify_exception(self._err("opaque backend blip", transient=True))
        assert sig is not None and sig.kind == KIND_DEPENDENCY_UNAVAILABLE
        assert classify_exception(self._err("opaque terminal thing", transient=False)) is None

    def test_raw_error_frame(self) -> None:
        sig = acp_provider.classify_raw_error(
            {"code": -32000, "message": "error", "data": "ThrottlingException: slow down"}
        )
        assert sig is not None and sig.kind == KIND_RATE_LIMITED
        assert acp_provider.classify_raw_error("not a dict") is None

    def test_non_acp_exception_is_ignored(self) -> None:
        assert acp_provider.classify(ValueError("ThrottlingException"), "") is None


# ── registry ──────────────────────────────────────────────────────────────────


class TestRegistry:
    def test_builtin_adapters_in_order(self) -> None:
        names = registered_adapters()
        assert names[:3] == ["github", "http", "acp_provider"]

    def test_pre_attached_signal_wins(self) -> None:
        exc = ValueError("whatever")
        attached = DependencySignal(kind=KIND_RATE_LIMITED, dependency_scope="db:x", source="me")
        setattr(exc, dependency.SIGNAL_ATTR, attached)
        assert classify_exception(exc) is attached

    def test_custom_adapter_and_raising_adapter(self) -> None:
        def boom(exc: BaseException, scope: str) -> DependencySignal | None:
            raise RuntimeError("adapter bug")

        def mine(exc: BaseException, scope: str) -> DependencySignal | None:
            if isinstance(exc, KeyError):
                return DependencySignal(
                    kind=KIND_DEPENDENCY_UNAVAILABLE, dependency_scope="db:x", source="mine"
                )
            return None

        register_adapter("boom", boom, first=True)
        register_adapter("mine", mine)
        try:
            sig = classify_exception(KeyError("k"))
            assert sig is not None and sig.source == "mine"
        finally:
            assert unregister_adapter("boom")
            assert unregister_adapter("mine")
            assert not unregister_adapter("mine")

"""Negative-path mapping tests on vendor-documented GitHub failure shapes.

These are the campaign's required negative tests, each built from a shape the
vendor's own documentation describes, asserting the neutral RUN-01 class the
mapping must produce. Pure functions -- no I/O, no side effects.
"""

from __future__ import annotations

import pytest

from kiro_crew.connections.control_plane import ERROR_CLASSES
from kiro_crew.connections.vendors.github.errors import (
    GithubFailure,
    classify_github_failure,
)


def test_401_revoked_or_expired_credential_is_auth() -> None:
    # A revoked installation token / expired PAT: the server does not accept
    # the identity at all. Distinct from 403.
    assert classify_github_failure(GithubFailure(401, "Bad credentials")) == "auth"


def test_404_hidden_private_and_truly_missing_are_both_not_found() -> None:
    # GitHub returns 404 for BOTH a private repo hidden from an unauthorized
    # caller AND a genuinely missing resource -- indistinguishable on the wire
    # by design. The mapping must return not_found for both and must NOT
    # fabricate a forbidden/not_found distinction the wire does not carry.
    hidden_private = GithubFailure(404, "Not Found")
    truly_missing = GithubFailure(404, "Not Found")
    assert classify_github_failure(hidden_private) == "not_found"
    assert classify_github_failure(truly_missing) == "not_found"


def test_409_stale_sha_is_conflict() -> None:
    # A stale base SHA / head moved under a write.
    failure = GithubFailure(409, "is at ... but expected ...")
    assert classify_github_failure(failure) == "conflict"


def test_405_protected_branch_merge_refusal_is_conflict() -> None:
    # GitHub returns 405 when a protected-branch merge is refused (a required
    # check pending, an approval missing, the head moved). It is a resource-
    # state clash, not a malformed request -- so conflict, not input.
    failure = GithubFailure(
        405, "At least 1 approving review is required by reviewers with write access."
    )
    assert classify_github_failure(failure) == "conflict"


def test_422_bad_base_branch_is_input() -> None:
    # A bad base branch / malformed ref / duplicate name: GitHub's validation
    # status.
    failure = GithubFailure(422, "Validation Failed: base is invalid")
    assert classify_github_failure(failure) == "input"


def test_403_permission_denial_is_forbidden() -> None:
    # A bare 403 with no rate-limit signal is a genuine permission failure.
    failure = GithubFailure(403, "Resource not accessible by integration")
    assert classify_github_failure(failure) == "forbidden"


def test_403_secondary_rate_limit_via_retry_after_is_throttle() -> None:
    # A 403 carrying retry-after is a secondary-limit rejection, not a
    # permission failure.
    failure = GithubFailure(403, "You have exceeded a secondary rate limit", {"Retry-After": "60"})
    assert classify_github_failure(failure) == "throttle"


def test_403_secondary_rate_limit_via_body_phrase_is_throttle() -> None:
    # The body phrase alone (no retry-after header) is sufficient evidence.
    failure = GithubFailure(403, "You have exceeded a secondary rate limit. Please wait.")
    assert classify_github_failure(failure) == "throttle"


def test_403_abuse_detection_phrase_is_throttle() -> None:
    failure = GithubFailure(403, "You have triggered an abuse detection mechanism")
    assert classify_github_failure(failure) == "throttle"


def test_403_primary_rate_limit_exhausted_is_throttle() -> None:
    # x-ratelimit-remaining: 0 marks a primary-limit exhaustion.
    failure = GithubFailure(
        403,
        "API rate limit exceeded for user",
        {"x-ratelimit-remaining": "0", "x-ratelimit-limit": "5000"},
    )
    assert classify_github_failure(failure) == "throttle"


def test_403_permission_denial_is_not_confused_with_available_budget() -> None:
    # A 403 permission failure that happens to carry a healthy remaining budget
    # is still forbidden, not throttle.
    failure = GithubFailure(
        403,
        "Must have admin rights to Repository.",
        {"x-ratelimit-remaining": "4999"},
    )
    assert classify_github_failure(failure) == "forbidden"


def test_429_is_always_throttle() -> None:
    assert classify_github_failure(GithubFailure(429, "Too Many Requests")) == "throttle"


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_is_temporary(status: int) -> None:
    assert classify_github_failure(GithubFailure(status)) == "temporary"


def test_unhandled_4xx_is_input() -> None:
    assert classify_github_failure(GithubFailure(418, "I'm a teapot")) == "input"


def test_non_error_status_on_failure_path_is_ambiguous() -> None:
    # A 200 reaching a failure classifier means the caller mis-routed a
    # success into the failure path; that is ambiguous, not a real class.
    assert classify_github_failure(GithubFailure(200)) == "ambiguous"


def test_header_lookup_is_case_insensitive() -> None:
    lower = GithubFailure(403, "secondary rate limit", {"retry-after": "30"})
    upper = GithubFailure(403, "secondary rate limit", {"Retry-After": "30"})
    assert classify_github_failure(lower) == classify_github_failure(upper) == "throttle"


def test_every_status_maps_to_a_valid_control_plane_class() -> None:
    for status in (100, 200, 301, 400, 401, 403, 404, 409, 418, 422, 429, 500, 599, 600):
        cls = classify_github_failure(GithubFailure(status))
        assert cls in ERROR_CLASSES, (status, cls)

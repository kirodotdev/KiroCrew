"""GitHub rate-limit header/body reading tests. Pure -- no I/O, no waiting."""

from __future__ import annotations

from kiro_crew.connections.vendors.github.rate_limit import RateLimitSnapshot, read_rate_limit


def test_reads_full_primary_limit_header_set() -> None:
    headers = {
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": "4321",
        "x-ratelimit-reset": "1789494259",
        "x-ratelimit-used": "679",
        "x-ratelimit-resource": "core",
    }
    snap = read_rate_limit(headers)
    assert snap.limit == 5000
    assert snap.remaining == 4321
    assert snap.reset_epoch == 1789494259
    assert snap.used == 679
    assert snap.resource == "core"
    assert snap.secondary_limit_signaled is False
    assert snap.should_back_off is False


def test_remaining_zero_marks_primary_exhausted_and_backs_off() -> None:
    snap = read_rate_limit({"x-ratelimit-remaining": "0"})
    assert snap.primary_exhausted is True
    assert snap.should_back_off is True


def test_absent_remaining_is_not_exhausted() -> None:
    # An unknown budget is unknown, not empty.
    snap = read_rate_limit({})
    assert snap.remaining is None
    assert snap.primary_exhausted is False
    assert snap.should_back_off is False


def test_retry_after_header_signals_secondary_limit() -> None:
    snap = read_rate_limit({"Retry-After": "120"})
    assert snap.retry_after_seconds == 120
    assert snap.secondary_limit_signaled is True
    assert snap.should_back_off is True


def test_body_phrase_signals_secondary_limit_without_header() -> None:
    snap = read_rate_limit({}, "You have exceeded a secondary rate limit")
    assert snap.secondary_limit_signaled is True
    assert snap.should_back_off is True


def test_abuse_detection_phrase_signals_secondary_limit() -> None:
    snap = read_rate_limit({}, "You have triggered an abuse detection mechanism")
    assert snap.secondary_limit_signaled is True


def test_header_lookup_is_case_insensitive() -> None:
    snap = read_rate_limit({"X-RateLimit-Remaining": "10"})
    assert snap.remaining == 10


def test_malformed_numeric_header_is_treated_as_absent() -> None:
    # A stray non-numeric header must not raise on an otherwise-usable response.
    snap = read_rate_limit({"x-ratelimit-remaining": "not-a-number"})
    assert snap.remaining is None


def test_empty_snapshot_defaults() -> None:
    snap = RateLimitSnapshot()
    assert snap.limit is None
    assert snap.should_back_off is False

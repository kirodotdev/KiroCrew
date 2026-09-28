"""Malformed input is refused, not repaired, in two more places.

Both are the mistake `parse_route_prefix`'s own docstring names when it refuses a bare
word: a value that has to be guessed at is a value the operator did not mean, and the
guess surfaces later as a misrouted request or a suite that skipped, rather than now as
an error.
"""

from __future__ import annotations

import pytest
from container.common.config import ConfigError, parse_route_prefix, parse_task_ttl_seconds


@pytest.mark.parametrize("raw", ["/", "//", "///", " / ", " // "])
def test_a_prefix_that_normalises_to_nothing_is_refused(raw: str) -> None:
    """A value whose normalised form is empty must not read as "no prefix".

    That is the dangerous direction: the deployment believes it set a prefix, the
    container serves the routes bare, and nothing says so until a request arrives at a
    path nobody expected it to answer.
    """
    with pytest.raises(ConfigError):
        parse_route_prefix(raw)


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_an_absent_prefix_still_means_no_prefix(raw: str | None) -> None:
    """Non-vacuity for the refusal above: unset is a legitimate answer.

    A guard that refused everything would make the test above pass while breaking every
    deployment that does not use a prefix, which is all of them today.
    """
    assert parse_route_prefix(raw) == ""


@pytest.mark.parametrize(
    "raw,expected", [("/c/frontdesk", "/c/frontdesk"), ("/c/frontdesk/", "/c/frontdesk")]
)
def test_a_real_prefix_is_accepted_and_normalised(raw: str, expected: str) -> None:
    assert parse_route_prefix(raw) == expected


TTL_NAME = "SMC_TASK_TTL_SECONDS"


@pytest.mark.parametrize("raw", [None, "", "0"])
def test_an_absent_or_zero_lifetime_is_unbounded(raw: str | None, monkeypatch) -> None:
    """The case that keeps this setting from stopping anyone who has not asked for it.

    Absent, empty and an explicit zero all have to mean "no deadline", because the
    supervisor's wait treats a positive value as one. A default that read as a few
    seconds, or a refusal on absence, would stop tasks launched by every path that
    says nothing about lifetime.
    """
    if raw is None:
        monkeypatch.delenv(TTL_NAME, raising=False)
    else:
        monkeypatch.setenv(TTL_NAME, raw)
    assert parse_task_ttl_seconds(TTL_NAME) == 0


@pytest.mark.parametrize("raw,expected", [("1", 1), ("21600", 21600)])
def test_a_positive_lifetime_is_read_as_seconds(raw: str, expected: int, monkeypatch) -> None:
    """Non-vacuity for the refusals: a real bound has to survive the guard."""
    monkeypatch.setenv(TTL_NAME, raw)
    assert parse_task_ttl_seconds(TTL_NAME) == expected


@pytest.mark.parametrize("raw", ["-1", "-21600"])
def test_a_negative_lifetime_is_refused(raw: str, monkeypatch) -> None:
    """A deadline already past is refused, not repaired into unbounded or into now.

    Reading it as unbounded would silently drop a bound the launcher meant to set.
    Honouring it would stop the task in its first wait, and the supervisor reports a
    lifetime stop as ORDERLY, so a task that never worked would look like one that
    finished cleanly.
    """
    monkeypatch.setenv(TTL_NAME, raw)
    with pytest.raises(ConfigError):
        parse_task_ttl_seconds(TTL_NAME)


@pytest.mark.parametrize("raw", ["6h", "later", "6.5"])
def test_a_lifetime_that_is_not_an_integer_is_refused(raw: str, monkeypatch) -> None:
    """``6h`` is the shape the number was originally read from, and it is not seconds."""
    monkeypatch.setenv(TTL_NAME, raw)
    with pytest.raises(ConfigError):
        parse_task_ttl_seconds(TTL_NAME)

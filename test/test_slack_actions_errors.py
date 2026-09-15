"""Tests for the evidence-built sets of Slack's native error strings.

These assert the recorded strings ARE the documented Slack native error codes
(spelling and membership) and that the fault groups are disjoint. They do NOT
assert any string maps to a classification kind — this slice records the native
strings; the error-string -> classification mapping is delivered by consuming
W01's control plane (it_d610cbb5), not here.
"""

from __future__ import annotations

import pytest

from kiro_crew.connections.vendors.slack.errors import (
    _AUTH_CODES,
    _CONFLICT_CODES,
    _FORBIDDEN_CODES,
    _INPUT_CODES,
    _NOT_FOUND_CODES,
    _QUOTA_CODES,
    _SCOPE_CODES,
    _TEMPORARY_CODES,
    _THROTTLE_CODES,
    NATIVE_ERROR_CODES,
)

_ALL_GROUPS = (
    _AUTH_CODES,
    _SCOPE_CODES,
    _NOT_FOUND_CODES,
    _FORBIDDEN_CODES,
    _THROTTLE_CODES,
    _QUOTA_CODES,
    _CONFLICT_CODES,
    _INPUT_CODES,
    _TEMPORARY_CODES,
)


@pytest.mark.parametrize(
    "code",
    [
        # A representative documented string from each fault group; each is a
        # literal Slack native error code from api.slack.com/methods/*.
        "invalid_auth",
        "missing_scope",
        "channel_not_found",
        "not_in_channel",
        "ratelimited",
        "storage_limit_reached",
        "already_reacted",
        "invalid_arguments",
        "internal_error",
    ],
)
def test_documented_native_error_string_is_recorded(code):
    assert code in NATIVE_ERROR_CODES


def test_pagination_specific_error_string_is_recorded():
    # invalid_cursor is the only pagination-specific Slack error string.
    assert "invalid_cursor" in _INPUT_CODES


def test_throttle_group_carries_both_documented_spellings():
    # Slack surfaces both spellings across its method references.
    assert _THROTTLE_CODES == frozenset({"ratelimited", "rate_limited"})


def test_every_recorded_string_is_lowercase_snake_case():
    for code in NATIVE_ERROR_CODES:
        assert code == code.lower()
        assert " " not in code
        assert code.replace("_", "").isalnum()


def test_fault_groups_are_disjoint():
    # A native string names exactly one fault group; the union's size therefore
    # equals the sum of the group sizes.
    total = sum(len(group) for group in _ALL_GROUPS)
    assert len(NATIVE_ERROR_CODES) == total


def test_union_is_exactly_the_groups_combined():
    combined: set[str] = set()
    for group in _ALL_GROUPS:
        combined |= group
    assert NATIVE_ERROR_CODES == frozenset(combined)

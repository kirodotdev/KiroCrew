"""Tests for the Slack native-error -> RUN-01 mapping.

These assert the mapping CONSUMES W01's control-plane taxonomy: every class it
returns is a member of the control plane's closed set, every Slack native
string recorded in the vendor inventory is classified, and the mapping records
no string the inventory does not. They complement the sibling
``test_slack_actions_errors.py``, which asserts the inventory's spelling and
disjointness but deliberately asserts no classification.
"""

from __future__ import annotations

import pytest

from kiro_crew.connections.control_plane import (
    ERROR_CLASSES,
    MAX_ERROR_CHARS,
    operation_error,
)
from kiro_crew.connections.vendors.slack.error_mapping import (
    SLACK_ERROR_CLASSES,
    classify_slack_error,
    slack_operation_error,
)
from kiro_crew.connections.vendors.slack.errors import NATIVE_ERROR_CODES


def test_mapping_keys_are_exactly_the_recorded_native_strings():
    # The mapping classifies every recorded native string and no other: a
    # string added to the inventory without a class here, or a class here for a
    # string the inventory does not record, fails this equality. This is the
    # consume-not-fork contract with the vendor inventory made checkable.
    assert set(SLACK_ERROR_CLASSES) == set(NATIVE_ERROR_CODES)


def test_every_mapped_class_is_a_control_plane_error_class():
    # Consuming the taxonomy means every value is a member of the control
    # plane's closed set, never a locally invented string.
    for error_class in SLACK_ERROR_CLASSES.values():
        assert error_class in ERROR_CLASSES


@pytest.mark.parametrize(
    "code,expected",
    [
        ("invalid_auth", "auth"),
        ("token_revoked", "auth"),
        ("missing_scope", "scope"),
        ("not_allowed_token_type", "scope"),
        ("channel_not_found", "not_found"),
        ("unknown_method", "not_found"),
        ("not_in_channel", "forbidden"),
        ("access_denied", "forbidden"),
        ("ratelimited", "throttle"),
        ("rate_limited", "throttle"),
        ("storage_limit_reached", "quota"),
        ("msg_too_long", "quota"),
        ("already_reacted", "conflict"),
        ("message_not_modified", "conflict"),
        ("invalid_arguments", "input"),
        ("invalid_cursor", "input"),
        ("internal_error", "temporary"),
        ("service_unavailable", "temporary"),
    ],
)
def test_representative_native_string_maps_to_expected_class(code, expected):
    assert classify_slack_error(code) == expected


def test_classes_without_a_slack_native_string_are_never_assigned():
    # Slack's ok:false bodies surface no consent / failure-side partial /
    # ambiguous string, so the mapping assigns none of those three classes.
    assigned = set(SLACK_ERROR_CLASSES.values())
    assert "consent" not in assigned
    assert "partial" not in assigned
    assert "ambiguous" not in assigned


def test_unrecognized_string_degrades_to_ambiguous():
    # A string the mapping has no evidence for cannot be named a specific
    # class; it degrades to the neutral, non-actionable one.
    assert classify_slack_error("some_unknown_slack_code") == "ambiguous"
    assert classify_slack_error("") == "ambiguous"


def test_slack_operation_error_builds_a_classified_typed_error():
    err = slack_operation_error("channel_not_found", "channel C123 is gone")
    assert err["error_class"] == "not_found"
    assert err["detail"] == "channel C123 is gone"
    assert err["error_class"] in ERROR_CLASSES


def test_slack_operation_error_routes_detail_through_the_redaction_boundary():
    # slack_operation_error must build via the control plane's operation_error,
    # so a long detail is capped at the taxonomy's shared MAX_ERROR_CHARS the
    # same way a directly-built one is.
    long_detail = "x" * (MAX_ERROR_CHARS + 100)
    via_mapping = slack_operation_error("internal_error", long_detail)
    via_control_plane = operation_error("temporary", long_detail)
    assert via_mapping["detail"] == via_control_plane["detail"]
    assert len(via_mapping["detail"]) <= MAX_ERROR_CHARS

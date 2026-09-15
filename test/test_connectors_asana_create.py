"""Tests for Asana create semantics: no idempotency + batch partial failure.

Covers: the retry disposition never claims exactly-once (an ambiguous create
is READBACK_REQUIRED, a retry that could duplicate); readback best-effort match;
the 50-item batch cap; and batch partial failure preserved per-item rather than
collapsed. The "retry duplicates" negative path is exercised by asserting the
ambiguous-outcome disposition is NOT a safe blind retry.
"""

from __future__ import annotations

import pytest

from kiro_crew.connections.vendors.asana.create import (
    MCP_BATCH_MAX,
    AsanaCreateError,
    CreateIntent,
    RetryDisposition,
    parse_batch_result,
    plan_create_retry,
    readback_matches,
    validate_batch_size,
)
from kiro_crew.connections.vendors.asana.errors import AsanaErrorCategory

# ── retry disposition: no exactly-once, ever ────────────────────────────────


def test_retry_disposition_has_no_exactly_once_value():
    # The vocabulary itself must not offer an exactly-once claim.
    values = {d.value for d in RetryDisposition}
    assert "exactly_once" not in values


def test_failed_prior_is_safe_fresh_create():
    assert plan_create_retry(prior_outcome="failed") is RetryDisposition.SAFE_FRESH_CREATE


def test_unknown_prior_requires_readback_not_blind_retry():
    # The core rule: an ambiguous create is NEVER "safe to retry"; a blind retry
    # can duplicate because Asana has no idempotency key.
    disposition = plan_create_retry(prior_outcome="unknown")
    assert disposition is RetryDisposition.READBACK_REQUIRED
    assert disposition is not RetryDisposition.SAFE_FRESH_CREATE


def test_succeeded_prior_is_already_created():
    assert (
        plan_create_retry(prior_outcome="succeeded", created_gid="999")
        is RetryDisposition.ALREADY_CREATED
    )


def test_succeeded_without_gid_is_a_shape_error():
    with pytest.raises(AsanaCreateError):
        plan_create_retry(prior_outcome="succeeded")


def test_unknown_prior_outcome_string_rejected():
    with pytest.raises(AsanaCreateError):
        plan_create_retry(prior_outcome="maybe")


# ── readback: best-effort dedupe, not a guarantee ───────────────────────────


def test_readback_matches_returns_existing_gid():
    intent = CreateIntent(name="Deploy", context_gid="700")
    existing = [{"gid": "111", "name": "Other"}, {"gid": "222", "name": "Deploy"}]
    assert readback_matches(intent, existing) == "222"


def test_readback_no_match_returns_none_so_fresh_create_still_needed():
    intent = CreateIntent(name="Deploy", context_gid="700")
    assert readback_matches(intent, [{"gid": "111", "name": "Other"}]) is None


def test_readback_is_case_sensitive():
    intent = CreateIntent(name="Deploy", context_gid="700")
    # Asana names are case-sensitive; a differing case is not a match.
    assert readback_matches(intent, [{"gid": "111", "name": "deploy"}]) is None


def test_readback_rejects_non_mapping_row():
    intent = CreateIntent(name="Deploy", context_gid="700")
    with pytest.raises(AsanaCreateError):
        readback_matches(intent, ["not-a-mapping"])  # type: ignore[list-item]


# ── batch cap ───────────────────────────────────────────────────────────────


def test_batch_at_cap_ok():
    assert validate_batch_size([{}] * MCP_BATCH_MAX) == MCP_BATCH_MAX


def test_batch_over_cap_rejected():
    with pytest.raises(AsanaCreateError):
        validate_batch_size([{}] * (MCP_BATCH_MAX + 1))


def test_empty_batch_rejected():
    with pytest.raises(AsanaCreateError):
        validate_batch_size([])


# ── batch partial failure preserved per-item ────────────────────────────────


def test_batch_all_success():
    result = parse_batch_result([{"gid": "1"}, {"gid": "2"}])
    assert result.all_succeeded is True
    assert result.any_failed is False
    assert result.failed_indices == ()
    assert [o.gid for o in result.outcomes] == ["1", "2"]


def test_batch_partial_failure_is_not_collapsed():
    # Item 0 succeeds, item 1 fails, item 2 succeeds -> preserved as three rows,
    # only index 1 marked failed. NOT collapsed into one all-or-nothing verdict.
    result = parse_batch_result(
        [
            {"gid": "1"},
            {"status": 403, "errors": [{"message": "Forbidden"}]},
            {"gid": "3"},
        ]
    )
    assert result.all_succeeded is False
    assert result.any_failed is True
    assert result.failed_indices == (1,)
    assert result.outcomes[0].gid == "1"
    assert result.outcomes[1].succeeded is False
    assert result.outcomes[1].error is not None
    assert result.outcomes[1].error.category is AsanaErrorCategory.FORBIDDEN
    assert result.outcomes[2].gid == "3"


def test_batch_failure_row_without_status_still_typed():
    # An MCP error row may carry no HTTP status; it is still preserved as a
    # failure (classified via a synthetic 400) rather than raising.
    result = parse_batch_result([{"errors": [{"message": "bad"}]}])
    assert result.any_failed is True
    assert result.outcomes[0].error is not None


def test_batch_row_neither_success_nor_failure_is_shape_error():
    with pytest.raises(AsanaCreateError):
        parse_batch_result([{"name": "no gid, no error"}])


def test_batch_expected_count_mismatch_is_shape_error():
    with pytest.raises(AsanaCreateError):
        parse_batch_result([{"gid": "1"}], expected_count=2)


def test_batch_row_must_be_mapping():
    with pytest.raises(AsanaCreateError):
        parse_batch_result(["not-a-mapping"])  # type: ignore[list-item]

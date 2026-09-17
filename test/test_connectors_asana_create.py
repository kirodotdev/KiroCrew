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
    ReadbackOutcome,
    ReadbackResolution,
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


def test_readback_new_object_is_ambiguous_never_silently_adopted():
    # A newly-appeared same-name+context object (222, not in pre_attempt_gids)
    # is reported as AMBIGUOUS with its GID surfaced -- NOT returned as an
    # adopted match: it could be this attempt's result OR a concurrent create.
    intent = CreateIntent(name="Deploy", context_gid="700")
    existing = [
        {"gid": "111", "name": "Other", "workspace": {"gid": "700"}},
        {"gid": "222", "name": "Deploy", "workspace": {"gid": "700"}},
    ]
    outcome = readback_matches(intent, existing, frozenset())
    assert isinstance(outcome, ReadbackOutcome)
    assert outcome.resolution is ReadbackResolution.AMBIGUOUS
    assert outcome.candidate_gids == ("222",)


def test_readback_ambiguous_on_project_context():
    intent = CreateIntent(name="Deploy", context_gid="proj-9")
    # Context may be a PROJECT GID; a membership in projects[] counts.
    existing = [{"gid": "333", "name": "Deploy", "projects": [{"gid": "proj-9"}]}]
    outcome = readback_matches(intent, existing, frozenset())
    assert outcome.resolution is ReadbackResolution.AMBIGUOUS
    assert outcome.candidate_gids == ("333",)


def test_readback_pre_existing_same_name_context_object_is_no_match():
    # A genuinely-distinct object that ALREADY existed under the same
    # name+context (its GID is in the pre-attempt snapshot) is NOT a candidate:
    # it cannot be this attempt's result, so the resolution is NO_MATCH.
    intent = CreateIntent(name="Deploy", context_gid="700")
    existing = [{"gid": "555", "name": "Deploy", "workspace": {"gid": "700"}}]
    outcome = readback_matches(intent, existing, frozenset({"555"}))
    assert outcome.resolution is ReadbackResolution.NO_MATCH
    assert outcome.candidate_gids == ()


def test_readback_concurrent_twin_makes_it_ambiguous_not_adopted():
    # F2 core: a pre-existing twin (555, in snapshot) plus a new object (666,
    # NOT in snapshot) -- 666 could be this attempt's create OR a concurrent
    # unrelated one. Asana gives no discriminator, so 666 is surfaced as an
    # AMBIGUOUS candidate, never silently adopted as "mine".
    intent = CreateIntent(name="Deploy", context_gid="700")
    existing = [
        {"gid": "555", "name": "Deploy", "workspace": {"gid": "700"}},
        {"gid": "666", "name": "Deploy", "workspace": {"gid": "700"}},
    ]
    outcome = readback_matches(intent, existing, frozenset({"555"}))
    assert outcome.resolution is ReadbackResolution.AMBIGUOUS
    assert outcome.candidate_gids == ("666",)


def test_readback_multiple_new_objects_all_surfaced_as_candidates():
    # Two concurrent new same-name+context objects: BOTH are surfaced; the
    # connector picks neither.
    intent = CreateIntent(name="Deploy", context_gid="700")
    existing = [
        {"gid": "777", "name": "Deploy", "workspace": {"gid": "700"}},
        {"gid": "888", "name": "Deploy", "workspace": {"gid": "700"}},
    ]
    outcome = readback_matches(intent, existing, frozenset())
    assert outcome.resolution is ReadbackResolution.AMBIGUOUS
    assert set(outcome.candidate_gids) == {"777", "888"}


def test_readback_same_name_wrong_context_is_no_match():
    # An object in a DIFFERENT workspace/project is not a candidate at all.
    intent = CreateIntent(name="Deploy", context_gid="700")
    existing = [{"gid": "999", "name": "Deploy", "workspace": {"gid": "800"}}]
    assert readback_matches(intent, existing, frozenset()).resolution is ReadbackResolution.NO_MATCH


def test_readback_name_match_without_any_context_is_no_match():
    # A row that states no context cannot satisfy the natural key.
    intent = CreateIntent(name="Deploy", context_gid="700")
    outcome = readback_matches(intent, [{"gid": "999", "name": "Deploy"}], frozenset())
    assert outcome.resolution is ReadbackResolution.NO_MATCH


def test_readback_no_matching_row_is_no_match():
    intent = CreateIntent(name="Deploy", context_gid="700")
    outcome = readback_matches(
        intent, [{"gid": "111", "name": "Other", "workspace": {"gid": "700"}}], frozenset()
    )
    assert outcome.resolution is ReadbackResolution.NO_MATCH


def test_readback_is_case_sensitive():
    intent = CreateIntent(name="Deploy", context_gid="700")
    # Asana names are case-sensitive; a differing case is not a candidate.
    outcome = readback_matches(
        intent, [{"gid": "111", "name": "deploy", "workspace": {"gid": "700"}}], frozenset()
    )
    assert outcome.resolution is ReadbackResolution.NO_MATCH


def test_readback_rejects_non_mapping_row():
    intent = CreateIntent(name="Deploy", context_gid="700")
    with pytest.raises(AsanaCreateError):
        readback_matches(intent, ["not-a-mapping"], frozenset())  # type: ignore[list-item]


def test_readback_requires_pre_attempt_gids_argument():
    # pre_attempt_gids is a REQUIRED positional argument with no default: the
    # type system forbids calling readback without a pre-attempt snapshot.
    intent = CreateIntent(name="Deploy", context_gid="700")
    with pytest.raises(TypeError):
        readback_matches(intent, [])  # type: ignore[call-arg]


def test_readback_never_exposes_an_adopted_gid_field():
    # The outcome type has no "adopted"/"matched" single-GID field -- the
    # connector never resolves ambiguity to one GID on its own.
    outcome = ReadbackOutcome(resolution=ReadbackResolution.NO_MATCH)
    assert not hasattr(outcome, "gid")
    assert not hasattr(outcome, "matched_gid")


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
    result = parse_batch_result([{"gid": "1"}, {"gid": "2"}], expected_count=2)
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
        ],
        expected_count=3,
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
    result = parse_batch_result([{"errors": [{"message": "bad"}]}], expected_count=1)
    assert result.any_failed is True
    assert result.outcomes[0].error is not None


def test_batch_row_neither_success_nor_failure_is_shape_error():
    with pytest.raises(AsanaCreateError):
        parse_batch_result([{"name": "no gid, no error"}], expected_count=1)


def test_batch_expected_count_mismatch_is_shape_error():
    with pytest.raises(AsanaCreateError):
        parse_batch_result([{"gid": "1"}], expected_count=2)


def test_batch_empty_short_response_with_expected_count_is_shape_error():
    # F3 core: a non-empty submission (expected 2) with an EMPTY response must
    # NOT vacuously report success -- the exact-cardinality check always runs
    # and refuses it, so no items are silently lost.
    with pytest.raises(AsanaCreateError):
        parse_batch_result([], expected_count=2)


def test_batch_expected_count_is_required():
    # F3: expected_count is a REQUIRED keyword argument with no default -- the
    # silent-loss path (omit it -> skip cardinality -> vacuous all([])) is gone
    # at the type level.
    with pytest.raises(TypeError):
        parse_batch_result([{"gid": "1"}])  # type: ignore[call-arg]


def test_batch_expected_count_must_be_positive_int():
    with pytest.raises(AsanaCreateError):
        parse_batch_result([], expected_count=0)


def test_batch_row_must_be_mapping():
    with pytest.raises(AsanaCreateError):
        parse_batch_result(["not-a-mapping"], expected_count=1)  # type: ignore[list-item]

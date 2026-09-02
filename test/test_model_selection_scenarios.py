"""Dedicated, scenario-based tests for model selection.

One place that documents — as executable scenarios — how a model is chosen for
each real situation, across the four decision primitives in ``acp.client``:

  * ``resolve_usable_model(preferred, advertised)`` — the SUBSTITUTE path
    (background one-liners, tips, inherited/cold-start applies). Returns ``""``
    to mean "inherit the session's served backend default".
  * ``model_is_unusable(id, advertised)`` — the shared entitlement predicate.
  * ``AcpModelUnavailable`` — how an EXPLICIT user pick is refused (raise, not
    substitute).
  * ``_rejected_model_from_error(error)`` — classifies a mid-prompt wire
    rejection so ``run_bg_oneliner`` can react.

Situations covered: entitled account, free-tier (subset entitlement), a
partition that serves ``auto``, a partition that does NOT serve ``auto``, a
fresh session whose entitlement is not yet known, and an explicit unusable pick.

(The end-to-end wire/skip and reactive-retry behaviors live in
``test_run_bg_oneliner.py``; this file pins the decision logic those paths use.)
"""

from __future__ import annotations

from kiro_crew.acp.client import (
    AcpModelUnavailable,
    _rejected_model_from_error,
    advertised_model_ids,
    model_is_unusable,
    resolve_usable_model,
)

# Representative advertised sets for the scenarios below.
_ENTITLED = ["claude-opus-4.8", "claude-sonnet-4.6", "auto"]  # serves auto
_FREE_TIER = ["claude-sonnet-4.6"]                            # subset, no auto
_NO_AUTO_PARTITION = ["gpt-5.6-terra", "gpt-5.6-luna"]        # serves models, not auto
_UNKNOWN: list = []                                           # no session yet


class TestBackgroundResolution:
    """`resolve_usable_model`: the substitute path. `""` == inherit default."""

    def test_entitled_concrete_model_is_used_as_is(self):
        assert resolve_usable_model("claude-opus-4.8", _ENTITLED) == "claude-opus-4.8"

    def test_free_tier_unentitled_model_inherits_default(self):
        # Account is served a subset that excludes the requested model -> "".
        assert resolve_usable_model("claude-opus-4.8", _FREE_TIER) == ""

    def test_auto_is_sent_when_the_partition_serves_it(self):
        assert resolve_usable_model("auto", _ENTITLED) == "auto"

    def test_auto_inherits_default_when_the_partition_does_not_serve_it(self):
        # The literal "auto" must never reach a partition that rejects it.
        assert resolve_usable_model("auto", _NO_AUTO_PARTITION) == ""

    def test_unknown_entitlement_trusts_a_concrete_id(self):
        # Fresh session (nothing advertised yet): a concrete id can't be checked,
        # so it is trusted (the reactive retry is the backstop if it's wrong).
        assert resolve_usable_model("claude-opus-4.8", _UNKNOWN) == "claude-opus-4.8"

    def test_unknown_entitlement_still_inherits_default_for_auto(self):
        # But never send a literal "auto" we cannot verify.
        assert resolve_usable_model("auto", _UNKNOWN) == ""

    def test_empty_preference_inherits_default(self):
        assert resolve_usable_model("", _ENTITLED) == ""

    def test_membership_is_case_insensitive(self):
        assert resolve_usable_model("claude-opus-4.8", ["Claude-Opus-4.8"]) == "claude-opus-4.8"

    def test_blank_advertised_entries_are_ignored(self):
        assert resolve_usable_model("claude-sonnet-4.6", ["", "  ", "claude-sonnet-4.6"]) == (
            "claude-sonnet-4.6"
        )


class TestEntitlementPredicate:
    """`model_is_unusable` — the one shared membership check."""

    def test_unknown_advertised_allows(self):
        # Empty/None = entitlement unknowable -> allow (never withhold on no evidence).
        assert model_is_unusable("claude-opus-4.8", []) is False
        assert model_is_unusable("claude-opus-4.8", None) is False

    def test_served_model_is_usable(self):
        assert model_is_unusable("claude-sonnet-4.6", _ENTITLED) is False

    def test_unserved_model_is_unusable(self):
        assert model_is_unusable("claude-opus-4.8", _FREE_TIER) is True

    def test_case_insensitive(self):
        assert model_is_unusable("CLAUDE-SONNET-4.6", _ENTITLED) is False

    def test_advertised_model_ids_extracts_defensively(self):
        entries = [{"modelId": "a"}, {"value": "b"}, {"nope": "c"}, "junk", None]
        assert advertised_model_ids(entries) == ["a", "b"]
        assert advertised_model_ids("not-a-list") == []


class TestExplicitPickRefusal:
    """An EXPLICIT user pick that the account can't run RAISES — it is never
    silently substituted (a user who chose a model should see the error)."""

    def test_unavailable_pick_would_be_flagged_by_the_predicate(self):
        assert model_is_unusable("claude-opus-4.8", _FREE_TIER) is True

    def test_model_unavailable_error_is_terminal_and_names_alternatives(self):
        err = AcpModelUnavailable("claude-opus-4.8", _FREE_TIER)
        assert err.model_id == "claude-opus-4.8"
        assert err.advertised == _FREE_TIER
        assert err.transient is False  # no retry earns an entitlement
        assert "claude-opus-4.8" in str(err)
        assert "claude-sonnet-4.6" in str(err)  # advertised alternatives surfaced

    def test_model_unavailable_error_without_advertised_says_none(self):
        err = AcpModelUnavailable("claude-opus-4.8")
        assert err.advertised == []
        assert "none advertised" in str(err)


class TestRejectionClassifier:
    """`_rejected_model_from_error` — powers the reactive retry."""

    def test_matches_invalid_model_id_in_data(self):
        assert (
            _rejected_model_from_error({"data": "Invalid model ID: claude-haiku-4.5"})
            == "claude-haiku-4.5"
        )

    def test_matches_invalid_model_id_for_auto_in_message(self):
        assert _rejected_model_from_error({"message": "Invalid model ID: auto"}) == "auto"

    def test_matches_model_not_available_wording(self):
        assert (
            _rejected_model_from_error({"data": "The model 'sonnet-x' is not available"})
            == "sonnet-x"
        )

    def test_unrelated_error_returns_none(self):
        assert _rejected_model_from_error({"data": "ThrottlingException: slow down"}) is None

    def test_non_dict_returns_none(self):
        assert _rejected_model_from_error("nonsense") is None
        assert _rejected_model_from_error(None) is None

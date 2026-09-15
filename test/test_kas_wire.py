"""Unit tests for the consolidated KAS wire-shape helpers.

Pins :mod:`kiro_crew.acp.kas_wire` (the single home for KAS ``_meta.kiro``
parsing) and the shared ``AcpPromptStats`` context-meter helpers that both the
AcpClient and AcpSessionHandle paths now delegate to, so a future change to
either cannot silently drift the two backends apart.
"""

from __future__ import annotations

import math

from kiro_crew.acp import kas_wire
from kiro_crew.acp.types import AcpPromptStats


class TestKiroMeta:
    def test_extracts_the_kiro_blob(self):
        assert kas_wire.kiro_meta({"_meta": {"kiro": {"kind": "x"}}}) == {"kind": "x"}

    def test_missing_meta_is_none(self):
        assert kas_wire.kiro_meta({}) is None

    def test_meta_not_a_dict_is_none(self):
        assert kas_wire.kiro_meta({"_meta": "nope"}) is None

    def test_kiro_not_a_dict_is_none(self):
        assert kas_wire.kiro_meta({"_meta": {"kiro": []}}) is None

    def test_missing_kiro_is_none(self):
        assert kas_wire.kiro_meta({"_meta": {"other": {}}}) is None


class TestTurnCredits:
    def test_sums_only_credit_units(self):
        kiro = {
            "promptTurnSummaries": [
                {"usage": 3, "unit": "credit"},
                {"usage": 100, "unit": "token"},  # ignored — not a credit
                {"usage": 2, "unit": "credit"},
            ]
        }
        assert kas_wire.turn_credits(kiro) == 5.0

    def test_no_summaries_list_returns_none(self):
        # None signals "leave the prior value untouched", not "zero it".
        assert kas_wire.turn_credits({}) is None
        assert kas_wire.turn_credits({"promptTurnSummaries": "nope"}) is None

    def test_empty_list_is_zero_not_none(self):
        assert kas_wire.turn_credits({"promptTurnSummaries": []}) == 0.0

    def test_malformed_entries_are_skipped(self):
        kiro = {"promptTurnSummaries": ["x", {"unit": "credit"}, {"usage": 4, "unit": "credit"}]}
        assert kas_wire.turn_credits(kiro) == 4.0


class TestKindConstants:
    def test_summarization_set_membership(self):
        assert kas_wire.KIND_SUMMARIZATION_COMPLETED in kas_wire.SUMMARIZATION_KINDS
        assert kas_wire.KIND_CONTEXT_USAGE not in kas_wire.SUMMARIZATION_KINDS

    def test_steering_set_membership(self):
        assert kas_wire.KIND_STEERING_INJECTED in kas_wire.STEERING_KINDS
        assert kas_wire.KIND_AGENT_SUBTASK not in kas_wire.STEERING_KINDS

    def test_agent_subtask_kind_is_hyphenated(self):
        # The wire value is ``agent-subtask`` (hyphen), NOT ``agent_subtask``.
        assert kas_wire.KIND_AGENT_SUBTASK == "agent-subtask"


class TestSanitizePct:
    def test_none_is_none(self):
        assert AcpPromptStats.sanitize_pct(None) is None

    def test_unparseable_is_none(self):
        assert AcpPromptStats.sanitize_pct("abc") is None
        assert AcpPromptStats.sanitize_pct([]) is None

    def test_plain_value(self):
        assert AcpPromptStats.sanitize_pct("50") == 50.0
        assert AcpPromptStats.sanitize_pct(42) == 42.0

    def test_nan_becomes_zero(self):
        assert AcpPromptStats.sanitize_pct(math.nan) == 0.0

    def test_out_of_range_is_clamped(self):
        assert AcpPromptStats.sanitize_pct(150) == 100.0
        assert AcpPromptStats.sanitize_pct(-5) == 0.0
        assert AcpPromptStats.sanitize_pct(math.inf) == 100.0
        assert AcpPromptStats.sanitize_pct(1e308) == 100.0


class TestBackfillContextWindow:
    def test_noop_when_counts_from_usage(self):
        stats = AcpPromptStats()
        stats.context_tokens_from_usage = True
        stats.context_window_tokens = 1000
        stats.backfill_context_window(50.0, "some-model")
        assert stats.context_used_tokens == 0  # left untouched

    def test_derives_used_from_kept_window(self):
        stats = AcpPromptStats()
        stats.context_window_tokens = 1000  # a surviving window (e.g. post-compaction)
        stats.backfill_context_window(25.0, "some-model")
        assert stats.context_used_tokens == 250

    def test_no_model_and_no_window_leaves_zero(self):
        stats = AcpPromptStats()
        stats.backfill_context_window(50.0, "")
        assert stats.context_window_tokens == 0
        assert stats.context_used_tokens == 0

    def test_malformed_pct_does_not_overflow(self):
        stats = AcpPromptStats()
        stats.context_window_tokens = 1000
        stats.backfill_context_window(math.nan, "some-model")
        assert stats.context_used_tokens == 0

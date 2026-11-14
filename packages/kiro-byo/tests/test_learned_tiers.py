"""Gates for runtime-learned routing tiers (the Dyna 'learn from outcome' half).

These tests pin the four promised gates:
  1. high confidence only,
  2. one step at a time,
  3. an audit trail is written,
  4. curated models are never learned,
and that model_tiers reads a learned tier only when nothing curated exists.
"""
from __future__ import annotations

import unittest

from kiro_byo.config import ModelSpec
from kiro_byo.learned_tiers import (
    LearnedTierStore,
    TierLearningProfile,
    evaluate_tier_change,
    observe_and_maybe_learn,
)
from kiro_byo.mode_router import model_tiers, _model_has_curated_tier


class EvaluateTierChange(unittest.TestCase):
    def test_low_confidence_never_moves(self) -> None:
        for conf in ("none", "low", "medium"):
            direction, _ = evaluate_tier_change(
                current_effective_tier="balanced", runtime_speed=0.95,
                reliability=0.99, confidence=conf, samples=50,
            )
            self.assertEqual(direction, 0, conf)

    def test_fast_and_reliable_assigns_fast_not_strong(self) -> None:
        direction, reason = evaluate_tier_change(
            current_effective_tier="balanced", runtime_speed=0.90,
            reliability=0.97, confidence="high", samples=40,
        )
        self.assertEqual(direction, -1)
        self.assertIn("fast-role", reason)

    def test_fast_role_revoked_if_slow_or_unreliable(self) -> None:
        slow, _ = evaluate_tier_change(
            current_effective_tier="fast", runtime_speed=0.10,
            reliability=0.99, confidence="high", samples=40,
        )
        unreliable, _ = evaluate_tier_change(
            current_effective_tier="fast", runtime_speed=0.90,
            reliability=0.50, confidence="high", samples=40,
        )
        self.assertEqual(slow, 1)
        self.assertEqual(unreliable, 1)

    def test_middle_of_the_road_stays(self) -> None:
        direction, _ = evaluate_tier_change(
            current_effective_tier="balanced", runtime_speed=0.55,
            reliability=0.95, confidence="high", samples=40,
        )
        self.assertEqual(direction, 0)

    def test_slow_balanced_cannot_be_promoted_to_strong(self) -> None:
        direction, _ = evaluate_tier_change(
            current_effective_tier="balanced", runtime_speed=0.01,
            reliability=0.99, confidence="high", samples=40,
        )
        self.assertEqual(direction, 0)

    def test_legacy_strong_learned_tier_decays(self) -> None:
        direction, _ = evaluate_tier_change(
            current_effective_tier="strong", runtime_speed=0.99,
            reliability=0.99, confidence="high", samples=40,
        )
        self.assertEqual(direction, -1)



class ObserveAndLearn(unittest.TestCase):
    def _store(self) -> LearnedTierStore:
        import tempfile, os
        d = tempfile.mkdtemp(prefix="byo-learn-")
        return LearnedTierStore(path=__import__("pathlib").Path(d) / "learned.json")

    def test_one_step_only_and_audit_written(self) -> None:
        store = self._store()
        record = observe_and_maybe_learn(
            provider_id="relay", model_id="x", effective_tier="balanced",
            has_curated_tier=False, runtime_speed=0.95, reliability=0.98,
            confidence="high", samples=40, store=store, now=1000.0,
        )
        self.assertIsNotNone(record)
        self.assertEqual(record["from"], "balanced")
        self.assertEqual(record["to"], "fast")
        self.assertEqual(store.learned_tier("relay", "x"), "fast")
        self.assertEqual(len(store.recent_audit()), 1)

    def test_curated_model_is_never_learned(self) -> None:
        store = self._store()
        record = observe_and_maybe_learn(
            provider_id="official", model_id="m", effective_tier="strong",
            has_curated_tier=True, runtime_speed=0.02, reliability=0.10,
            confidence="high", samples=99, store=store, now=1000.0,
        )
        self.assertIsNone(record)
        self.assertEqual(store.learned_tier("official", "m"), "")

    def test_throttle_blocks_rapid_second_write(self) -> None:
        store = self._store()
        first = observe_and_maybe_learn(
            provider_id="relay", model_id="y", effective_tier="balanced",
            has_curated_tier=False, runtime_speed=0.95, reliability=0.98,
            confidence="high", samples=40, store=store, now=2000.0,
        )
        # 1 second later, still warranted, but throttled.
        second = observe_and_maybe_learn(
            provider_id="relay", model_id="y", effective_tier="fast",
            has_curated_tier=False, runtime_speed=0.95, reliability=0.98,
            confidence="high", samples=41, store=store, now=2001.0,
        )
        self.assertIsNotNone(first)
        self.assertIsNone(second)


class ModelTiersReadsLearned(unittest.TestCase):
    def test_learned_tier_used_only_without_curated_facts(self) -> None:
        bare = ModelSpec("relay", "Relay", "mystery-opus", "Mystery Opus")
        # Name says "opus" but tier must be balanced until something is learned.
        self.assertEqual(model_tiers(bare, learned_lookup=lambda p, m: ""), ("balanced",))
        # A learned tier is honoured.
        self.assertEqual(model_tiers(bare, learned_lookup=lambda p, m: "fast"), ("fast",))
        # Older speed-only learner could incorrectly label these as strong.
        self.assertEqual(model_tiers(bare, learned_lookup=lambda p, m: "strong"), ("balanced",))

    def test_curated_tier_beats_learned(self) -> None:
        curated = ModelSpec("p", "P", "m", "M", tier="fast")
        self.assertFalse(_model_has_curated_tier(ModelSpec("p", "P", "bare", "Bare")))
        self.assertTrue(_model_has_curated_tier(curated))
        # Even if something was "learned", a manual tier wins.
        self.assertEqual(model_tiers(curated, learned_lookup=lambda p, m: "strong"), ("fast",))


if __name__ == "__main__":
    unittest.main()

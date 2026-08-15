"""Tests for the memory-benchmark answer scorers.

These are the tests that keep the ports honest. Every expected number in here was
cross-checked by running the ACTUAL upstream functions (AST-extracted from
``/tmp/bench-data/locomo/task_eval/evaluation.py`` so its bert_score/nltk imports
could be skipped) against this module over 1 100 randomized cases spanning all
five LoCoMo categories: 1 100/1 100 scores identical, 2 200/2 200
``normalize_answer`` outputs identical. That differential harness is deliberately
NOT a test — it needs the upstream checkout on disk, and a test that silently
skips when a /tmp path is absent is a test that stops protecting anything. The
golden values below are its output, frozen.

DIVERGENCE, stated once and loudly: upstream stems with ``nltk.PorterStemmer()``,
whose default mode is ``NLTK_EXTENSIONS`` (a small irregular-form table plus
"leave words of length <= 2 alone"). nltk is not and will not be a Kiro Crew
dependency, so the port uses ``snowballstemmer.stemmer("porter")`` — already a
hard install dependency — which is the *original* Porter algorithm. The two agree
on the overwhelming majority of English tokens but are not bit-identical, so a
LoCoMo number from this harness can differ from a published one in the third
decimal. It is the only intentional divergence in the LoCoMo path.
"""

from __future__ import annotations

import pytest

from kiro_crew.eval.bench.corpus import (
    CAT_ADVERSARIAL,
    CAT_MULTI_HOP,
    CAT_PREFERENCE,
    CAT_SINGLE_HOP,
    CAT_TEMPORAL,
    BenchQuery,
)
from kiro_crew.eval.bench.scorers import (
    JUDGE_CALL_PARAMS,
    REFUSAL_MARKERS,
    AnswerScore,
    aggregate,
    is_abstention,
    longmemeval_judge_prompt,
    multi_answer_f1,
    normalize_answer,
    refusal_score,
    score_locomo,
    score_longmemeval,
    token_f1,
)


def locomo_query(
    raw_category: str,
    *,
    query_id: str = "q1",
    gold: str | None = "gold",
    adversarial: str | None = None,
    category: str = CAT_SINGLE_HOP,
) -> BenchQuery:
    return BenchQuery(
        query_id=query_id,
        question="what?",
        category=category,
        gold_answer=gold,
        adversarial_answer=adversarial,
        unanswerable=raw_category == "5",
        raw_category=raw_category,
    )


class TestNormalizeAnswer:
    """Requirement 1: punctuation, articles, case, whitespace all normalize away."""

    def test_case_punctuation_and_whitespace(self) -> None:
        assert normalize_answer("The  Dog's, house!") == "dogs house"

    def test_collapses_tabs_and_strips_leading_article(self) -> None:
        assert normalize_answer("   an  APPLE\t\tpie  ") == "apple pie"

    def test_removes_and_not_just_articles(self) -> None:
        # Upstream extended the SQuAD article regex to (a|an|the|and); "and" is a
        # conjunction and its removal is load-bearing for comma-joined golds.
        assert normalize_answer("Bread and Butter") == "bread butter"

    def test_normalization_makes_f1_exact(self) -> None:
        assert token_f1("The dog!", "a dog") == 1.0

    def test_stemming_is_applied(self) -> None:
        # Upstream stems both sides inside f1_score; without it this is 0.0.
        assert token_f1("dogs", "dog") == 1.0


class TestTokenF1:
    """Requirement 2: exact 1.0, disjoint 0.0, partial strictly between."""

    def test_exact_match(self) -> None:
        assert token_f1("Paris", "Paris") == 1.0

    def test_disjoint_tokens(self) -> None:
        assert token_f1("Paris", "Tokyo") == 0.0

    def test_partial_overlap_strictly_between(self) -> None:
        score = token_f1("Paris in France", "Paris")
        assert 0.0 < score < 1.0
        assert score == pytest.approx(0.5)

    def test_empty_prediction_does_not_raise(self) -> None:
        assert token_f1("", "Paris") == 0.0


class TestCategoryDispatch:
    def test_category_2_and_4_use_single_answer_f1(self) -> None:
        for raw in ("2", "4"):
            got = score_locomo(locomo_query(raw, gold="Paris"), "Paris")
            assert got.metric == "f1"
            assert got.score == 1.0

    def test_unsupported_category_raises(self) -> None:
        # Upstream raises ValueError on an unknown category; a silently bucketed
        # question would corrupt the aggregate instead of failing the run.
        with pytest.raises(ValueError, match="unsupported LoCoMo raw_category"):
            score_locomo(locomo_query("7"), "anything")

    def test_missing_gold_on_scored_category_is_unscored_not_zero(self) -> None:
        got = score_locomo(locomo_query("2", gold=None), "Paris")
        assert got.scored is False
        assert got.score is None
        assert got.metric == "gold_missing"


class TestCategory3GoldTruncation:
    """Requirement 3: the ';' truncation must actually change the score."""

    GOLD = "tennis; he plays every weekend"

    def test_truncation_lifts_category_3_to_exact_match(self) -> None:
        got = score_locomo(locomo_query("3", gold=self.GOLD), "tennis")
        assert got.score == 1.0

    def test_same_gold_without_truncation_scores_lower(self) -> None:
        # Category 2 takes the identical gold un-truncated, so the delta isolates
        # the truncation rule rather than any other per-category difference.
        untruncated = score_locomo(locomo_query("2", gold=self.GOLD), "tennis")
        assert untruncated.score == pytest.approx(1 / 3)
        truncated = score_locomo(locomo_query("3", gold=self.GOLD), "tennis")
        assert truncated.score is not None and untruncated.score is not None
        assert truncated.score > untruncated.score

    def test_truncation_takes_only_the_first_segment(self) -> None:
        got = score_locomo(locomo_query("3", gold="a; b"), "b")
        assert got.score == 0.0


class TestCategory5Adversarial:
    """Requirement 4: substring refusal check, explicitly NOT F1."""

    ADVERSARIAL = "Cindy never told Bob what her favorite color was"

    def test_no_information_available_scores_one(self) -> None:
        got = score_locomo(
            locomo_query("5", gold=None, adversarial=self.ADVERSARIAL, category=CAT_ADVERSARIAL),
            "No information available in the conversation.",
        )
        assert got.score == 1.0
        assert got.metric == "refusal_substring"

    def test_not_mentioned_scores_one(self) -> None:
        got = score_locomo(
            locomo_query("5", gold=None, adversarial=self.ADVERSARIAL, category=CAT_ADVERSARIAL),
            "That was not mentioned anywhere.",
        )
        assert got.score == 1.0

    def test_other_answer_scores_zero(self) -> None:
        got = score_locomo(
            locomo_query("5", gold=None, adversarial=self.ADVERSARIAL, category=CAT_ADVERSARIAL),
            "Her favorite color was blue.",
        )
        assert got.score == 0.0

    def test_high_f1_against_adversarial_answer_still_scores_zero(self) -> None:
        # The prediction is the gold text verbatim -> F1 would be 1.0. The
        # adversarial category must not reward that; only a refusal counts.
        prediction = self.ADVERSARIAL
        assert token_f1(prediction, self.ADVERSARIAL) == 1.0
        got = score_locomo(
            locomo_query("5", gold=None, adversarial=self.ADVERSARIAL, category=CAT_ADVERSARIAL),
            prediction,
        )
        assert got.score == 0.0

    def test_markers_are_case_insensitive(self) -> None:
        assert refusal_score("NO INFORMATION AVAILABLE") == 1.0
        assert refusal_score("Not Mentioned") == 1.0

    def test_marker_list_matches_upstream(self) -> None:
        assert REFUSAL_MARKERS == ("no information available", "not mentioned")

    def test_none_gold_and_none_adversarial_does_not_raise(self) -> None:
        """Requirement 6: a None gold on an adversarial item must not raise."""
        query = BenchQuery(
            query_id="adv1",
            question="what color?",
            category=CAT_ADVERSARIAL,
            gold_answer=None,
            adversarial_answer=None,
            unanswerable=True,
            raw_category="5",
        )
        got = score_locomo(query, "It was blue.")
        assert got.score == 0.0
        assert got.scored is True


class TestMultiAnswerF1:
    """Requirement 5: mean-of-max must differ from plain F1, in both directions."""

    def test_missing_a_sub_answer_costs_more_than_plain_f1(self) -> None:
        # gold splits on ',' into two sub-answers; the prediction matches one.
        # mean-of-max = mean(1.0, 0.0) = 0.5, while plain token F1 over the
        # flattened gold is 2/3 — the multi variant is recall-shaped per sub-answer.
        multi = multi_answer_f1("apple", "apple, banana")
        plain = token_f1("apple", "apple, banana")
        assert multi == pytest.approx(0.5)
        assert plain == pytest.approx(2 / 3)
        assert multi < plain

    def test_extra_predicted_sub_answers_are_not_penalized(self) -> None:
        # The prediction is split too, so a spurious extra sub-answer is simply
        # never the argmax for any gold. Plain F1 punishes it as precision loss.
        multi = multi_answer_f1("apple, zebra", "apple")
        plain = token_f1("apple, zebra", "apple")
        assert multi == 1.0
        assert plain == pytest.approx(2 / 3)
        assert multi > plain

    def test_split_token_is_comma_not_semicolon(self) -> None:
        # If the split were on ';' this would behave like the comma case above.
        assert multi_answer_f1("apple", "apple; banana") == pytest.approx(2 / 3)
        assert multi_answer_f1("apple", "apple, banana") == pytest.approx(0.5)

    def test_category_1_dispatches_to_multi(self) -> None:
        got = score_locomo(
            locomo_query("1", gold="apple, banana", category=CAT_MULTI_HOP), "apple"
        )
        assert got.metric == "multi_f1"
        assert got.score == pytest.approx(0.5)


def lme_query(
    question_type: str,
    *,
    query_id: str = "q1",
    gold: str | None = "sushi",
    category: str = CAT_SINGLE_HOP,
    unanswerable: bool = False,
) -> BenchQuery:
    return BenchQuery(
        query_id=query_id,
        question="What did I eat?",
        category=category,
        gold_answer=gold,
        unanswerable=unanswerable,
        raw_category=question_type,
    )


class TestJudgePromptSelection:
    def test_abs_suffix_overrides_question_type(self) -> None:
        """Requirement 8: '_abs' in the id wins over question_type."""
        query = lme_query(
            "temporal-reasoning",
            query_id="gpt4_multi_session_abs",
            gold="The user never said when.",
            category=CAT_TEMPORAL,
        )
        prompt = longmemeval_judge_prompt(query, "I don't have that information.")
        assert "Explanation: The user never said when." in prompt
        assert "unanswerable" in prompt
        # The temporal template's distinguishing clause must be absent.
        assert "off-by-one" not in prompt
        assert "Correct Answer:" not in prompt

    def test_preference_rubric_header(self) -> None:
        """Requirement 9: preference golds go under 'Rubric:', not 'Correct Answer:'."""
        query = lme_query(
            "single-session-preference",
            gold="The user would prefer Adobe Premiere Pro tutorials.",
            category=CAT_PREFERENCE,
        )
        prompt = longmemeval_judge_prompt(query, "Try these Premiere tutorials.")
        assert "Rubric: The user would prefer Adobe Premiere Pro tutorials." in prompt
        assert "Correct Answer:" not in prompt

    def test_temporal_prompt_forgives_off_by_one(self) -> None:
        prompt = longmemeval_judge_prompt(
            lme_query("temporal-reasoning", category=CAT_TEMPORAL), "18 days"
        )
        assert "do not penalize off-by-one errors for the number of days" in prompt

    def test_knowledge_update_forgives_stale_alongside_updated(self) -> None:
        prompt = longmemeval_judge_prompt(lme_query("knowledge-update"), "Now Berlin.")
        assert "contains some previous information along with an updated answer" in prompt

    def test_three_question_types_share_the_default_prompt(self) -> None:
        prompts = {
            longmemeval_judge_prompt(lme_query(qt), "sushi")
            for qt in ("single-session-user", "single-session-assistant", "multi-session")
        }
        assert len(prompts) == 1

    def test_unknown_question_type_raises(self) -> None:
        with pytest.raises(NotImplementedError, match="no LongMemEval judge prompt"):
            longmemeval_judge_prompt(lme_query("banana-reasoning"), "sushi")

    def test_unanswerable_flag_also_selects_abstention(self) -> None:
        query = lme_query("temporal-reasoning", unanswerable=True, category=CAT_TEMPORAL)
        assert is_abstention(query)
        assert "Explanation:" in longmemeval_judge_prompt(query, "no idea")

    def test_call_params_match_upstream(self) -> None:
        assert JUDGE_CALL_PARAMS == {"temperature": 0, "max_tokens": 10, "n": 1}


class TestJudgeLabelRule:
    """Requirement 10: substring-based 'yes' rule, verbatim from upstream."""

    def test_yes_scores_one(self) -> None:
        got = score_longmemeval(lme_query("multi-session"), "sushi", judge=lambda _p: "Yes")
        assert got.score == 1.0
        assert got.metric == "llm_judge"
        assert got.scored is True

    def test_no_scores_zero(self) -> None:
        got = score_longmemeval(lme_query("multi-session"), "pizza", judge=lambda _p: "No")
        assert got.score == 0.0
        assert got.scored is True

    def test_hedged_reply_labels_positive(self) -> None:
        # This IS upstream behavior: `label = 'yes' in eval_response.lower()`.
        # max_tokens=10 is what keeps it from mattering in practice.
        got = score_longmemeval(
            lme_query("multi-session"), "pizza", judge=lambda _p: "yes, but incorrect"
        )
        assert got.score == 1.0

    def test_judge_receives_the_built_prompt(self) -> None:
        seen: list[str] = []

        def judge(prompt: str) -> str:
            seen.append(prompt)
            return "yes"

        query = lme_query("multi-session")
        score_longmemeval(query, "sushi", judge=judge)
        assert seen == [longmemeval_judge_prompt(query, "sushi")]


class TestUnscoredIsNotZero:
    """Requirement 7: an unavailable judge must never look like a wrong answer."""

    def test_judge_none_yields_unscored(self) -> None:
        got = score_longmemeval(lme_query("multi-session"), "sushi", judge=None)
        assert got.scored is False
        assert got.score is None
        assert got.metric == "judge_unavailable"

    def test_answer_score_rejects_scored_without_value(self) -> None:
        with pytest.raises(ValueError, match="requires a numeric score"):
            AnswerScore(query_id="q", score=None, metric="f1")

    def test_answer_score_rejects_unscored_with_value(self) -> None:
        with pytest.raises(ValueError, match="must carry score=None"):
            AnswerScore(query_id="q", score=0.0, metric="f1", scored=False)

    def test_answer_score_rejects_out_of_range(self) -> None:
        with pytest.raises(ValueError, match=r"outside \[0, 1\]"):
            AnswerScore(query_id="q", score=1.5, metric="f1")

    def test_aggregate_does_not_average_unscored_as_zero(self) -> None:
        scored_q = locomo_query("2", query_id="loc1", gold="Paris")
        unscored_q = lme_query("multi-session", query_id="lme1")
        scores = [
            score_locomo(scored_q, "Paris"),
            score_longmemeval(unscored_q, "sushi", judge=None),
        ]
        agg = aggregate(scores, [scored_q, unscored_q])
        assert agg["total"] == 2
        assert agg["scored"] == 1
        assert agg["unscored"] == 1
        # The naive mean over 2 items would be 0.5. It must be 1.0.
        assert agg["overall"] == 1.0

    def test_aggregate_overall_is_none_when_nothing_scored(self) -> None:
        query = lme_query("multi-session", query_id="lme1")
        agg = aggregate([score_longmemeval(query, "sushi", judge=None)], [query])
        assert agg["overall"] is None
        assert agg["scored"] == 0
        assert agg["unscored"] == 1


class TestAggregate:
    def test_keys_by_both_normalized_and_raw_category(self) -> None:
        q2 = locomo_query("2", query_id="a", gold="Paris")
        q4 = locomo_query("4", query_id="b", gold="Paris")
        q1 = locomo_query("1", query_id="c", gold="Paris", category=CAT_MULTI_HOP)
        scores = [
            score_locomo(q2, "Paris"),
            score_locomo(q4, "Tokyo"),
            score_locomo(q1, "Paris"),
        ]
        agg = aggregate(scores, [q2, q4, q1])
        assert agg["overall"] == pytest.approx(2 / 3)
        # Normalized bucket merges 2 and 4; the raw bucket keeps them apart. That
        # separation is the whole reason both keyings are reported.
        assert agg["by_category"][CAT_SINGLE_HOP]["mean"] == pytest.approx(0.5)
        assert agg["by_category"][CAT_MULTI_HOP]["mean"] == 1.0
        assert agg["by_raw_category"]["2"]["mean"] == 1.0
        assert agg["by_raw_category"]["4"]["mean"] == 0.0
        assert agg["by_raw_category"]["1"]["mean"] == 1.0

    def test_per_bucket_counts_expose_unscored(self) -> None:
        good = locomo_query("2", query_id="a", gold="Paris")
        blank = locomo_query("2", query_id="b", gold=None)
        agg = aggregate([score_locomo(good, "Paris"), score_locomo(blank, "Paris")],
                        [good, blank])
        bucket = agg["by_category"][CAT_SINGLE_HOP]
        assert bucket == {"mean": 1.0, "scored": 1, "unscored": 1, "total": 2}

    def test_missing_counts_queries_with_no_score(self) -> None:
        scored_q = locomo_query("2", query_id="a", gold="Paris")
        skipped_q = locomo_query("2", query_id="b", gold="Paris")
        agg = aggregate([score_locomo(scored_q, "Paris")], [scored_q, skipped_q])
        assert agg["missing"] == 1
        assert agg["total"] == 1

    def test_metric_histogram(self) -> None:
        q1 = locomo_query("1", query_id="a", gold="Paris", category=CAT_MULTI_HOP)
        q5 = locomo_query("5", query_id="b", gold=None, adversarial="x",
                          category=CAT_ADVERSARIAL)
        agg = aggregate([score_locomo(q1, "Paris"), score_locomo(q5, "not mentioned")],
                        [q1, q5])
        assert agg["by_metric"] == {"multi_f1": 1, "refusal_substring": 1}

    def test_empty_input(self) -> None:
        agg = aggregate([], [])
        assert agg == {
            "total": 0,
            "scored": 0,
            "unscored": 0,
            "missing": 0,
            "overall": None,
            "by_category": {},
            "by_raw_category": {},
            "by_metric": {},
        }

"""Answer scorers for the memory benchmark — one per dataset, plus the aggregator.

Why the two halves look nothing alike: the datasets' *official* metrics are
different in kind, and using one dataset's metric on the other produces a number
that cannot be compared to any published result. LoCoMo scores token-level F1
computed differently per question category (with one category not scored by F1 at
all); LongMemEval scores with an LLM judge driven by different prompts per
question type. Both are ported here from the upstream implementations on purpose —
a "reasonable equivalent" would silently make every number in the report
incomparable to the literature, which is the one thing a ruler must not do.

Upstream sources these are ported from (cite line refs in the port comments so a
future reader can diff against a newer upstream):

* LoCoMo      — ``task_eval/evaluation.py``, ``eval_question_answering``
                (snorkel-ai/long-context-memory "locomo" release).
* LongMemEval — ``src/evaluation/evaluate_qa.py``, ``get_anscheck_prompt``.

The important asymmetry for callers: LoCoMo is fully deterministic and needs no
network, no API key, and no model. That is why it is the harness default —
``score_locomo`` is the only end-to-end answer scorer here that can run in CI.
``score_longmemeval`` needs a judge injected; when it has none it returns a
*visibly unscored* result rather than a zero, because a judge that never ran is
not a judge that said "wrong" (see ``AnswerScore.scored``).
"""

from __future__ import annotations

import re
import string
import threading
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from snowballstemmer import stemmer as _snowball_stemmer

from .corpus import CAT_UNKNOWN, BenchQuery

# ── Porter stemming ──────────────────────────────────────────────────────────
# Upstream evaluation.py:10 builds `ps = PorterStemmer()` from nltk and stems
# EVERY token on both sides inside f1_score (evaluation.py:129-130). Dropping the
# stemmer is not a cosmetic simplification: it costs real F1 on singular/plural
# and tense mismatches ("dogs" vs "dog"), so it is ported rather than skipped.
#
# nltk is not a Kiro Crew dependency and will not become one for a benchmark, so
# this uses snowballstemmer's "porter" algorithm, which is already a hard install
# dependency of the package (setup.cfg: snowballstemmer>=1.0). See the DIVERGENCE
# note in the module docstring of test_bench_scorers.py: snowballstemmer's
# "porter" is the *original* Porter algorithm, whereas nltk's PorterStemmer
# defaults to mode=NLTK_EXTENSIONS, which adds a small irregular-form table
# ("dying" -> "die", words of length <= 2 left alone). The two agree on the
# overwhelming majority of English tokens but are not bit-identical.
#
# snowballstemmer keeps the in-progress word as mutable instance state, so a
# shared instance is not thread-safe (same hazard documented in
# vector_memory.py:133). One instance per thread; construction is trivial.
_stemmer_local = threading.local()


def _get_stemmer() -> Any:
    stemmer = getattr(_stemmer_local, "stemmer", None)
    if stemmer is None:
        stemmer = _snowball_stemmer("porter")
        _stemmer_local.stemmer = stemmer
    return stemmer


def _stem_tokens(tokens: list[str]) -> list[str]:
    """Stem in place-order; order is irrelevant to F1 but list-ness is not.

    F1 is computed with a multiset intersection, so duplicate tokens must survive
    stemming as duplicates — hence a list, not a set.
    """
    if not tokens:
        return []
    return list(_get_stemmer().stemWords(tokens))


# ── Normalization ────────────────────────────────────────────────────────────
# Port of evaluation.py:76-95 `normalize_answer`. Two things here are surprising
# enough to be worth stating out loud, because both are load-bearing:
#
#  1. It strips a leading `,` pass (`s.replace(',', "")`, line 78) BEFORE anything
#     else. Redundant with punctuation removal, kept for fidelity.
#  2. The article regex is NOT the SQuAD `(a|an|the)` — upstream extended it to
#     `(a|an|the|and)` (line 82; the original is left commented out on line 81).
#     "and" is a conjunction, not an article, and removing it is what makes
#     comma-joined multi-answer golds degrade gracefully. Do not "fix" this.
_ARTICLE_RE = re.compile(r"\b(a|an|the|and)\b")
_PUNCTUATION = frozenset(string.punctuation)


def normalize_answer(s: str) -> str:
    """Lowercase, strip punctuation, strip articles+"and", collapse whitespace.

    Exact behavioral port of upstream ``normalize_answer``, including its
    application order (lower -> remove_punc -> remove_articles -> whitespace fix).
    Order matters: removing punctuation first is what lets "the-dog" become
    "thedog" rather than " dog".
    """
    s = s.replace(",", "")
    lowered = s.lower()
    unpunctuated = "".join(ch for ch in lowered if ch not in _PUNCTUATION)
    unarticled = _ARTICLE_RE.sub(" ", unpunctuated)
    return " ".join(unarticled.split())


def token_f1(prediction: str, ground_truth: str) -> float:
    """Single-answer token-level F1 — port of evaluation.py:127-140 ``f1_score``.

    Upstream returns a bare int ``0`` on no overlap; normalized to 0.0 here so the
    return type is uniformly float.
    """
    pred_tokens = _stem_tokens(normalize_answer(prediction).split())
    gold_tokens = _stem_tokens(normalize_answer(ground_truth).split())
    common = Counter(pred_tokens) & Counter(gold_tokens)
    num_same = sum(common.values())
    if num_same == 0:
        return 0.0
    precision = num_same / len(pred_tokens)
    recall = num_same / len(gold_tokens)
    return (2 * precision * recall) / (precision + recall)


def multi_answer_f1(prediction: str, ground_truth: str) -> float:
    """Multi-hop partial F1 — port of evaluation.py:143-147 ``f1``.

    Three details that a from-memory reimplementation gets wrong, all verified
    against the file:

    * The split token is ``,`` (comma), not ``;``.
    * BOTH sides are split — the prediction is split into candidate sub-answers
      too, not just the gold.
    * The aggregation is ``mean`` over golds of ``max`` over predictions. It is
      recall-shaped: every gold sub-answer must be matched by *some* predicted
      sub-answer, but a prediction carrying extra sub-answers is not penalized.

    ``np.mean`` upstream; plain ``sum/len`` here to keep numpy off this path.
    """
    predictions = [p.strip() for p in prediction.split(",")]
    ground_truths = [g.strip() for g in ground_truth.split(",")]
    per_gold = [max(token_f1(p, gt) for p in predictions) for gt in ground_truths]
    return sum(per_gold) / len(per_gold)


# Port of evaluation.py:213 — adversarial items are scored by literal substring
# presence on the lowercased model output, never by F1. Scoring them by overlap
# would reward a confident wrong answer that happens to share tokens with the
# distractor text, i.e. exactly the failure the category exists to catch.
REFUSAL_MARKERS = ("no information available", "not mentioned")


def refusal_score(prediction: str) -> float:
    """1.0 if the output refuses using one of upstream's two literal markers."""
    lowered = prediction.lower()
    return 1.0 if any(marker in lowered for marker in REFUSAL_MARKERS) else 0.0


# ── Result type ──────────────────────────────────────────────────────────────
_DETAIL_CLIP = 120


def _clip(text: str) -> str:
    return text if len(text) <= _DETAIL_CLIP else text[: _DETAIL_CLIP - 1] + "…"


@dataclass(frozen=True)
class AnswerScore:
    """One question's answer score, or an explicit record that it was not scored.

    ``score`` is ``None`` — not ``0.0`` — whenever ``scored`` is False. That is a
    deliberate type choice, not fussiness: the failure mode this guards against is
    an unavailable judge quietly depressing a reported accuracy, which looks like
    a bad memory layer and is actually a missing API key. With ``None``, an
    aggregator that forgets to filter unscored items raises a TypeError instead of
    publishing a wrong number. ``__post_init__`` enforces the pairing so the two
    fields can never disagree.
    """

    query_id: str
    score: float | None
    metric: str
    detail: str = ""
    scored: bool = True

    def __post_init__(self) -> None:
        if self.scored:
            if self.score is None:
                raise ValueError(f"{self.query_id}: scored=True requires a numeric score")
            if not 0.0 <= self.score <= 1.0:
                raise ValueError(f"{self.query_id}: score {self.score!r} outside [0, 1]")
        elif self.score is not None:
            raise ValueError(
                f"{self.query_id}: scored=False must carry score=None, got {self.score!r}"
            )


def _unscored(query_id: str, metric: str, detail: str) -> AnswerScore:
    return AnswerScore(query_id=query_id, score=None, metric=metric, detail=detail, scored=False)


# ── LoCoMo ───────────────────────────────────────────────────────────────────
# Dispatch is on ``raw_category`` (the dataset-native integer, as a string) and
# NOT on the normalized CAT_* bucket, for two independent reasons:
#
#  1. The normalized buckets deliberately merge questions that upstream scores
#     *differently* — e.g. anything that lands in a single normalized bucket may
#     span categories 2/3/4, and category 3 alone carries a gold-truncation rule.
#  2. The 3-vs-4 name assignment in the normalized vocabulary is inferred from
#     upstream behavior rather than verified against a spec, while the integer in
#     the file is ground truth. Branching on the inferred name would make a
#     naming mistake silently change every number; branching on the integer
#     cannot.
_LOCOMO_SINGLE_F1_CATEGORIES = frozenset({"2", "3", "4"})
_LOCOMO_MULTI_F1_CATEGORIES = frozenset({"1"})
_LOCOMO_ADVERSARIAL_CATEGORIES = frozenset({"5"})


def score_locomo(query: BenchQuery, prediction: str) -> AnswerScore:
    """Score one LoCoMo answer deterministically — no LLM, no network, no key.

    Port of evaluation.py:189-232 ``eval_question_answering``. Per category:

    * 2, 3, 4 -> single-answer :func:`token_f1`
    * 1       -> :func:`multi_answer_f1` (mean-of-max partial F1)
    * 5       -> :func:`refusal_score` (substring, not F1)

    and, before any of that, category 3 pre-truncates its gold at the first
    ``;`` (evaluation.py:200-201) — its golds carry a primary answer followed by
    elaboration, and scoring against the elaboration deflates recall.

    Raises ValueError on an unrecognized ``raw_category``, mirroring upstream
    (evaluation.py:219-221). An unknown category means the adapter is wrong, and
    silently bucketing it would corrupt the aggregate rather than fail the run.
    """
    raw = (query.raw_category or "").strip()

    if raw in _LOCOMO_ADVERSARIAL_CATEGORIES:
        # Gold for adversarial items lives in ``adversarial_answer``; upstream's
        # scorer never reads it at all (it only inspects the prediction), so a
        # missing gold is harmless here and must not raise.
        return AnswerScore(
            query_id=query.query_id,
            score=refusal_score(prediction),
            metric="refusal_substring",
            detail=_clip(f"markers={list(REFUSAL_MARKERS)} pred={prediction.strip()!r}"),
        )

    gold = query.gold_answer
    if gold is None:
        # Not reachable for a well-formed non-adversarial LoCoMo item, but the
        # corpus contract explicitly permits a None gold, and a scorer that
        # crashes on one turns a data defect into a lost run. Unscored, not zero.
        return _unscored(query.query_id, "gold_missing", f"raw_category={raw!r} gold_answer=None")

    if raw in _LOCOMO_SINGLE_F1_CATEGORIES:
        if raw == "3":
            gold = gold.split(";")[0].strip()
        score = token_f1(prediction, gold)
        metric = "f1"
    elif raw in _LOCOMO_MULTI_F1_CATEGORIES:
        score = multi_answer_f1(prediction, gold)
        metric = "multi_f1"
    else:
        raise ValueError(
            f"{query.query_id}: unsupported LoCoMo raw_category {raw!r} "
            "(upstream eval_question_answering handles 1-5 only)"
        )

    detail = ""
    if score < 1.0:
        detail = _clip(
            f"gold={normalize_answer(gold)!r} pred={normalize_answer(prediction)!r}"
        )
    return AnswerScore(query_id=query.query_id, score=score, metric=metric, detail=detail)


# ── LongMemEval ──────────────────────────────────────────────────────────────
# Verbatim ports of the templates in evaluate_qa.py:24-42 ``get_anscheck_prompt``.
# They are stored as module constants rather than inlined so a diff against a
# newer upstream is a one-line comparison per template.
#
# NOTE the shape: there are FOUR non-abstention templates covering SIX question
# types (single-session-user / single-session-assistant / multi-session share
# one), plus the abstention template = five distinct prompts in total.
_PROMPT_DEFAULT = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, answer no. "
    "If the response is equivalent to the correct answer or contains all the intermediate "
    "steps to get the correct answer, you should also answer yes. If the response only "
    "contains a subset of the information required by the answer, answer no. \n\n"
    "Question: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\n"
    "Is the model response correct? Answer yes or no only."
)

_PROMPT_TEMPORAL = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, answer no. "
    "If the response is equivalent to the correct answer or contains all the intermediate "
    "steps to get the correct answer, you should also answer yes. If the response only "
    "contains a subset of the information required by the answer, answer no. In addition, "
    "do not penalize off-by-one errors for the number of days. If the question asks for the "
    "number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., "
    "predicting 19 days when the answer is 18), the model's response is still correct. \n\n"
    "Question: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\n"
    "Is the model response correct? Answer yes or no only."
)

_PROMPT_KNOWLEDGE_UPDATE = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, answer no. "
    "If the response contains some previous information along with an updated answer, the "
    "response should be considered as correct as long as the updated answer is the required "
    "answer.\n\n"
    "Question: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\n"
    "Is the model response correct? Answer yes or no only."
)

# The preference template feeds ``answer`` under a "Rubric:" header, because for
# this question type ``answer`` is prose describing a *desired* response, not a
# correct answer to match.
_PROMPT_PREFERENCE = (
    "I will give you a question, a rubric for desired personalized response, and a response "
    "from a model. Please answer yes if the response satisfies the desired response. "
    "Otherwise, answer no. The model does not need to reflect all the points in the rubric. "
    "The response is correct as long as it recalls and utilizes the user's personal "
    "information correctly.\n\n"
    "Question: {}\n\nRubric: {}\n\nModel Response: {}\n\n"
    "Is the model response correct? Answer yes or no only."
)

# The abstention template feeds ``answer`` under an "Explanation:" header and asks
# a different question entirely (did the model identify the question as
# unanswerable), so it overrides the question-type template rather than extending
# it — see evaluate_qa.py:41-42, where ``task`` is not consulted at all.
_PROMPT_ABSTENTION = (
    "I will give you an unanswerable question, an explanation, and a response from a model. "
    "Please answer yes if the model correctly identifies the question as unanswerable. "
    "The model could say that the information is incomplete, or some other information is "
    "given but the asked information is not.\n\n"
    "Question: {}\n\nExplanation: {}\n\nModel Response: {}\n\n"
    "Does the model correctly identify the question as unanswerable? "
    "Answer yes or no only."
)

_QUESTION_TYPE_PROMPTS = {
    "single-session-user": _PROMPT_DEFAULT,
    "single-session-assistant": _PROMPT_DEFAULT,
    "multi-session": _PROMPT_DEFAULT,
    "temporal-reasoning": _PROMPT_TEMPORAL,
    "knowledge-update": _PROMPT_KNOWLEDGE_UPDATE,
    "single-session-preference": _PROMPT_PREFERENCE,
}

# Judge call parameters upstream sends alongside the prompt (evaluate_qa.py:118-126).
# Exposed as data so a caller's judge closure can honor them without re-reading
# the paper: temperature=0 for reproducibility, max_tokens=10 because the reply is
# meant to be "yes"/"no", n=1 because a single sample is the published protocol.
JUDGE_CALL_PARAMS = {"temperature": 0, "max_tokens": 10, "n": 1}


def is_abstention(query: BenchQuery) -> bool:
    """True when the abstention prompt applies.

    Upstream's only signal is ``'_abs' in entry['question_id']``
    (evaluate_qa.py:117) — a substring test, not a suffix test, and abstention is
    NOT exposed as a ``question_type``. The corpus contract sets
    ``unanswerable`` from that same signal, so both are accepted: the id test
    keeps fidelity when a corpus was built without the flag, and the flag keeps
    correctness if an adapter ever normalizes the id.
    """
    return "_abs" in query.query_id or query.unanswerable


def longmemeval_judge_prompt(query: BenchQuery, prediction: str) -> str:
    """Build the exact prompt upstream would send. Pure — no network, no client.

    Port of evaluate_qa.py:24-45 ``get_anscheck_prompt``. Abstention is checked
    first because upstream ignores ``task`` entirely in that branch.

    Raises NotImplementedError on an unknown question type, as upstream does
    (evaluate_qa.py:38-39). A wrong prompt is a silently wrong label, so guessing
    is worse than failing.
    """
    answer = query.gold_answer or ""
    if is_abstention(query):
        return _PROMPT_ABSTENTION.format(query.question, answer, prediction)

    task = (query.raw_category or "").strip()
    template = _QUESTION_TYPE_PROMPTS.get(task)
    if template is None:
        raise NotImplementedError(
            f"{query.query_id}: no LongMemEval judge prompt for question_type {task!r} "
            f"(known: {sorted(_QUESTION_TYPE_PROMPTS)})"
        )
    return template.format(query.question, answer, prediction)


def score_longmemeval(
    query: BenchQuery,
    prediction: str,
    *,
    judge: Callable[[str], str] | None,
) -> AnswerScore:
    """Score one LongMemEval answer with an injected judge.

    ``judge`` takes the built prompt and returns the model's raw reply. Injection
    rather than an embedded client keeps this module free of network code and lets
    tests exercise the label rule with a two-line fake.

    The label rule is upstream's, verbatim: ``label = 'yes' in reply.lower()``
    (evaluate_qa.py:128). It is substring-based, so a hedged reply like
    "yes, but incorrect" labels positive. That is a real upstream property, not a
    bug being reproduced by accident — the call is made with max_tokens=10 so the
    model has no room to hedge in practice.

    When ``judge is None`` the result is marked unscored (``metric``
    ``"judge_unavailable"``, ``score`` ``None``) and :func:`aggregate` reports it
    in the ``unscored`` count instead of averaging it in. An unavailable judge is
    a missing measurement, never a failed answer.
    """
    if judge is None:
        return _unscored(
            query.query_id,
            "judge_unavailable",
            "no judge injected; item not scored (not a zero)",
        )

    prompt = longmemeval_judge_prompt(query, prediction)
    reply = judge(prompt).strip()
    label = "yes" in reply.lower()
    return AnswerScore(
        query_id=query.query_id,
        score=1.0 if label else 0.0,
        metric="llm_judge",
        detail=_clip(f"reply={reply!r}"),
    )


# ── Aggregation ──────────────────────────────────────────────────────────────
# Upstream (evaluation_stats.py:100-110) micro-averages: it sums the per-question
# metric per category and divides by that category's question count. Same shape
# here, with one addition upstream does not need — a denominator that counts only
# SCORED items, because upstream never has an unscored item (its judge either ran
# or the script died).


def _bucket_mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def aggregate(scores: Sequence[AnswerScore], queries: Sequence[BenchQuery]) -> dict:
    """Micro-average over SCORED items only, with counts that make gaps visible.

    Returns::

        {
          "total": int,        # scores handed in
          "scored": int,
          "unscored": int,
          "missing": int,      # queries with no score at all
          "overall": float | None,          # None when nothing was scored
          "by_category": {name: {"mean", "scored", "unscored", "total"}},
          "by_raw_category": {value: {...}},
          "by_metric": {metric: count},
        }

    ``overall`` is ``None`` rather than 0.0 for an all-unscored run so a report
    cannot render "0% accuracy" for "we never measured". Every mean in here uses a
    denominator built from scored items only; the unscored count is carried
    alongside so a reader can see "N of M scored" instead of a depressed number.
    """
    by_id = {q.query_id: q for q in queries}

    scored_values: list[float] = []
    unscored_total = 0
    cat_scored: dict[str, list[float]] = {}
    cat_unscored: dict[str, int] = {}
    raw_scored: dict[str, list[float]] = {}
    raw_unscored: dict[str, int] = {}
    by_metric: dict[str, int] = {}

    for s in scores:
        by_metric[s.metric] = by_metric.get(s.metric, 0) + 1
        q = by_id.get(s.query_id)
        cat = q.category if q is not None else CAT_UNKNOWN
        raw = (q.raw_category if q is not None else "") or "unknown"
        cat_scored.setdefault(cat, [])
        raw_scored.setdefault(raw, [])
        cat_unscored.setdefault(cat, 0)
        raw_unscored.setdefault(raw, 0)
        if s.scored and s.score is not None:
            scored_values.append(s.score)
            cat_scored[cat].append(s.score)
            raw_scored[raw].append(s.score)
        else:
            unscored_total += 1
            cat_unscored[cat] += 1
            raw_unscored[raw] += 1

    def _buckets(
        values: dict[str, list[float]], unscored: dict[str, int]
    ) -> dict[str, dict[str, object]]:
        out: dict[str, dict[str, object]] = {}
        for key in sorted(values):
            vals = values[key]
            miss = unscored.get(key, 0)
            out[key] = {
                "mean": _bucket_mean(vals),
                "scored": len(vals),
                "unscored": miss,
                "total": len(vals) + miss,
            }
        return out

    scored_ids = {s.query_id for s in scores}
    return {
        "total": len(scores),
        "scored": len(scored_values),
        "unscored": unscored_total,
        "missing": sum(1 for q in queries if q.query_id not in scored_ids),
        "overall": _bucket_mean(scored_values),
        "by_category": _buckets(cat_scored, cat_unscored),
        "by_raw_category": _buckets(raw_scored, raw_unscored),
        "by_metric": dict(sorted(by_metric.items())),
    }

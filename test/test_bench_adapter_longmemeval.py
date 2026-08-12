"""Tests for the LongMemEval → Corpus adapter.

Pure python: no network, no dataset file. Every fixture is inline and every test
targets one of the traps the real file sets — the always-present ``has_answer``
flag, abstention hiding in ``question_id`` rather than ``question_type``, the
``answer_`` label leak on evidence session ids, and the oracle variant's
distractor-free haystack.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.eval.bench.adapters.longmemeval import (
    LongMemEvalSchemaError,
    is_evidence_only,
    load_longmemeval,
    load_longmemeval_file,
)
from kiro_crew.eval.bench.corpus import (
    CAT_KNOWLEDGE_UPDATE,
    CAT_MULTI_HOP,
    CAT_PREFERENCE,
    CAT_SINGLE_HOP,
    CAT_TEMPORAL,
    CAT_UNKNOWN,
)

_DATE = "2023/04/10 (Mon) 23:07"

# ── Fixture builders ──


def _turn(role: str, content: str, has_answer: bool | None = None) -> dict:
    """One turn dict. ``has_answer=None`` omits the key entirely (older dumps)."""
    turn: dict = {"role": role, "content": content}
    if has_answer is not None:
        turn["has_answer"] = has_answer
    return turn


def _instance(
    *,
    sessions: list[tuple[str, list[dict]]],
    answer_session_ids: list[str],
    question_id: str = "qid_1",
    question_type: str = "single-session-user",
    question: str = "What colour was the Plesiosaur?",
    answer: str | None = "The Plesiosaur had a blue scaly body.",
    question_date: str = _DATE,
    dates: list[str] | None = None,
) -> dict:
    """A LongMemEval instance in the real field order, with parallel arrays."""
    session_ids = [sid for sid, _ in sessions]
    return {
        "question_id": question_id,
        "question_type": question_type,
        "question": question,
        "answer": answer,
        "question_date": question_date,
        "haystack_dates": dates if dates is not None else [_DATE] * len(sessions),
        "haystack_session_ids": session_ids,
        "haystack_sessions": [turns for _, turns in sessions],
        "answer_session_ids": answer_session_ids,
    }


def _evidence_only_raw() -> list[dict]:
    """One instance whose only session is its gold session (the oracle shape)."""
    return [
        _instance(
            sessions=[
                (
                    "answer_sharegpt_YkWn1Ne_0",
                    [
                        _turn("user", "I saw a Plesiosaur.", has_answer=False),
                        _turn("assistant", "It had a blue scaly body.", has_answer=True),
                    ],
                )
            ],
            answer_session_ids=["answer_sharegpt_YkWn1Ne_0"],
        )
    ]


# ── 1. Parallel-array length mismatch ──


def test_parallel_array_mismatch_raises_with_a_clear_message() -> None:
    raw = _evidence_only_raw()
    raw[0]["haystack_dates"] = [_DATE, "2023/05/01 (Mon) 10:00"]

    with pytest.raises(LongMemEvalSchemaError) as excinfo:
        load_longmemeval(raw, variant="oracle")

    message = str(excinfo.value)
    assert "parallel" in message
    assert "haystack_dates=2" in message
    assert "haystack_session_ids=1" in message
    assert "haystack_sessions=1" in message
    # The offending instance must be identifiable in a 500-instance file.
    assert "qid_1" in message


# ── 2. Missing has_answer ──


def test_turn_without_has_answer_defaults_to_not_evidence() -> None:
    raw = [
        _instance(
            sessions=[
                (
                    "s1",
                    [
                        _turn("user", "no flag on this turn at all"),
                        _turn("assistant", "nor this one"),
                    ],
                )
            ],
            answer_session_ids=["s1"],
        )
    ]

    corpus = load_longmemeval(raw, variant="oracle")

    turns = list(corpus.instances[0].iter_turns())
    assert [t.is_evidence for t in turns] == [False, False]
    assert corpus.instances[0].queries[0].gold_turn_ids == ()


# ── 3. has_answer present but False ──


def test_has_answer_false_on_every_turn_yields_no_evidence() -> None:
    """Presence of the key means nothing; only its value does."""
    raw = [
        _instance(
            sessions=[
                (
                    "s1",
                    [
                        _turn("user", "a", has_answer=False),
                        _turn("assistant", "b", has_answer=False),
                    ],
                )
            ],
            answer_session_ids=["s1"],
        )
    ]

    corpus = load_longmemeval(raw, variant="oracle")

    assert all(not t.is_evidence for t in corpus.instances[0].iter_turns())
    assert corpus.instances[0].queries[0].gold_turn_ids == ()
    # Session-level gold is independent of has_answer and survives.
    assert corpus.instances[0].queries[0].gold_session_ids == ("s1",)


def test_has_answer_true_turns_become_gold_turn_ids() -> None:
    raw = [
        _instance(
            sessions=[
                (
                    "s1",
                    [
                        _turn("user", "a", has_answer=False),
                        _turn("assistant", "b", has_answer=True),
                        _turn("user", "c", has_answer=True),
                    ],
                )
            ],
            answer_session_ids=["s1"],
        )
    ]

    query = load_longmemeval(raw, variant="oracle").instances[0].queries[0]

    assert query.gold_turn_ids == ("s1#1", "s1#2")


# ── 4. Abstention lives in question_id, not question_type ──


def test_abs_suffix_sets_unanswerable_and_leaves_question_type_alone() -> None:
    raw = [
        _instance(
            question_id="gpt4_ef1b2c3_abs",
            question_type="knowledge-update",
            answer=(
                "The information provided is not enough. You mentioned fixing the fence "
                "but did not mention purchasing cows from Peter."
            ),
            sessions=[("s1", [_turn("user", "I fixed the fence.", has_answer=True)])],
            answer_session_ids=["s1"],
        )
    ]

    query = load_longmemeval(raw, variant="oracle").instances[0].queries[0]

    assert query.unanswerable is True
    assert query.raw_category == "knowledge-update"
    assert query.category == CAT_KNOWLEDGE_UPDATE
    # The answer field holds an unanswerability explanation, carried verbatim.
    assert query.gold_answer is not None
    assert query.gold_answer.startswith("The information provided is not enough.")


def test_non_abs_question_id_is_answerable() -> None:
    corpus = load_longmemeval(_evidence_only_raw(), variant="oracle")
    assert corpus.instances[0].queries[0].unanswerable is False


# ── 5. Category mapping ──


@pytest.mark.parametrize(
    ("question_type", "expected"),
    [
        ("temporal-reasoning", CAT_TEMPORAL),
        ("multi-session", CAT_MULTI_HOP),
        ("knowledge-update", CAT_KNOWLEDGE_UPDATE),
        ("single-session-user", CAT_SINGLE_HOP),
        ("single-session-assistant", CAT_SINGLE_HOP),
        ("single-session-preference", CAT_PREFERENCE),
    ],
)
def test_question_type_maps_to_category(question_type: str, expected: str) -> None:
    raw = [
        _instance(
            question_type=question_type,
            sessions=[("s1", [_turn("user", "a", has_answer=True)])],
            answer_session_ids=["s1"],
        )
    ]

    query = load_longmemeval(raw, variant="oracle").instances[0].queries[0]

    assert query.category == expected
    # raw_category is what keeps single-session-user and -assistant distinct
    # after they collapse into the same normalized bucket.
    assert query.raw_category == question_type


def test_unknown_question_type_becomes_unknown_without_raising() -> None:
    raw = [
        _instance(
            question_type="seventh-type-from-the-future",
            sessions=[("s1", [_turn("user", "a", has_answer=True)])],
            answer_session_ids=["s1"],
        )
    ]

    query = load_longmemeval(raw, variant="oracle").instances[0].queries[0]

    assert query.category == CAT_UNKNOWN
    assert query.raw_category == "seventh-type-from-the-future"


# ── 6. Turn id synthesis and uniqueness ──


def test_turn_ids_are_unique_within_an_instance() -> None:
    raw = [
        _instance(
            sessions=[
                ("s1", [_turn("user", "a"), _turn("assistant", "b")]),
                ("s2", [_turn("user", "c"), _turn("assistant", "d")]),
            ],
            answer_session_ids=["s1"],
        )
    ]

    instance = load_longmemeval(raw, variant="oracle").instances[0]

    turn_ids = [t.turn_id for t in instance.iter_turns()]
    assert turn_ids == ["s1#0", "s1#1", "s2#0", "s2#1"]
    assert len(turn_ids) == len(set(turn_ids))


def test_same_session_id_twice_in_one_haystack_raises() -> None:
    """A repeat would collide synthesized turn ids, so it is refused.

    Session ids repeating ACROSS instances is fine (each instance is its own
    store); within one instance the id is part of the turn key.
    """
    raw = [
        _instance(
            sessions=[
                ("dup", [_turn("user", "a")]),
                ("dup", [_turn("user", "b")]),
            ],
            answer_session_ids=["dup"],
        )
    ]

    with pytest.raises(LongMemEvalSchemaError) as excinfo:
        load_longmemeval(raw, variant="oracle")

    assert "repeats 'dup'" in str(excinfo.value)


def test_same_session_id_across_instances_is_accepted() -> None:
    raw = [
        _instance(
            question_id="qid_1",
            sessions=[("shared", [_turn("user", "a", has_answer=True)])],
            answer_session_ids=["shared"],
        ),
        _instance(
            question_id="qid_2",
            sessions=[("shared", [_turn("user", "b", has_answer=True)])],
            answer_session_ids=["shared"],
        ),
    ]

    corpus = load_longmemeval(raw, variant="oracle")

    assert [i.instance_id for i in corpus.instances] == ["qid_1", "qid_2"]
    assert corpus.turn_count == 2


# ── 7. The answer_ prefix is not a gold signal ──


def test_answer_prefixed_session_not_in_answer_session_ids_is_not_gold() -> None:
    """Guards the label leak: gold comes from answer_session_ids only."""
    raw = [
        _instance(
            sessions=[
                ("answer_leaky_but_not_gold_0", [_turn("user", "distractor", has_answer=False)]),
                ("plain_id_that_is_gold", [_turn("assistant", "evidence", has_answer=True)]),
            ],
            answer_session_ids=["plain_id_that_is_gold"],
        )
    ]

    query = load_longmemeval(raw, variant="oracle").instances[0].queries[0]

    assert query.gold_session_ids == ("plain_id_that_is_gold",)
    assert "answer_leaky_but_not_gold_0" not in query.gold_session_ids
    assert query.gold_turn_ids == ("plain_id_that_is_gold#0",)


# ── 8. is_evidence_only ──


def test_is_evidence_only_true_for_an_evidence_only_corpus() -> None:
    corpus = load_longmemeval(_evidence_only_raw(), variant="oracle")
    assert is_evidence_only(corpus) is True


def test_is_evidence_only_false_once_a_distractor_session_is_added() -> None:
    raw = _evidence_only_raw()
    raw[0]["haystack_dates"].append("2023/05/02 (Tue) 08:15")
    raw[0]["haystack_session_ids"].append("distractor_0")
    raw[0]["haystack_sessions"].append([_turn("user", "unrelated chatter", has_answer=False)])

    corpus = load_longmemeval(raw, variant="s_cleaned")

    assert is_evidence_only(corpus) is False


def test_is_evidence_only_false_when_any_single_instance_has_a_distractor() -> None:
    raw = _evidence_only_raw() + [
        _instance(
            question_id="qid_2",
            sessions=[
                ("gold_2", [_turn("user", "evidence", has_answer=True)]),
                ("distractor_2", [_turn("user", "noise", has_answer=False)]),
            ],
            answer_session_ids=["gold_2"],
        )
    ]

    corpus = load_longmemeval(raw, variant="s_cleaned")

    assert is_evidence_only(corpus) is False


# ── 9. Fingerprint stability ──


def test_fingerprint_is_stable_across_two_loads_of_the_same_input() -> None:
    first = load_longmemeval(_evidence_only_raw(), variant="oracle")
    second = load_longmemeval(_evidence_only_raw(), variant="oracle")

    assert first.fingerprint() == second.fingerprint()


def test_fingerprint_changes_when_the_haystack_changes() -> None:
    baseline = load_longmemeval(_evidence_only_raw(), variant="oracle")
    changed_raw = _evidence_only_raw()
    changed_raw[0]["haystack_sessions"][0].append(_turn("user", "one more turn"))

    changed = load_longmemeval(changed_raw, variant="oracle")

    assert baseline.fingerprint() != changed.fingerprint()


# ── Corpus-level shape ──


def test_corpus_metadata_and_one_query_per_instance() -> None:
    corpus = load_longmemeval(_evidence_only_raw(), variant="oracle", source_path="/tmp/x.json")

    assert corpus.name == "longmemeval"
    assert corpus.variant == "oracle"
    assert corpus.source_path == "/tmp/x.json"
    assert corpus.notes
    assert corpus.query_count == 1
    assert len(corpus.instances[0].queries) == 1
    query = corpus.instances[0].queries[0]
    assert query.query_id == corpus.instances[0].instance_id == "qid_1"
    assert query.ask_date == _DATE


def test_turn_fields_carry_role_and_raw_session_date() -> None:
    corpus = load_longmemeval(_evidence_only_raw(), variant="oracle")

    session = corpus.instances[0].sessions[0]
    assert session.timestamp == _DATE
    assert [t.speaker for t in session.turns] == ["user", "assistant"]
    assert all(t.timestamp == _DATE for t in session.turns)
    assert session.turns[1].text == "It had a blue scaly body."


def test_top_level_must_be_an_array() -> None:
    with pytest.raises(LongMemEvalSchemaError) as excinfo:
        load_longmemeval({"question_id": "qid_1"}, variant="oracle")
    assert "top level" in str(excinfo.value)


def test_load_longmemeval_file_records_its_path(tmp_path) -> None:
    path = tmp_path / "mini_oracle.json"
    path.write_text(json.dumps(_evidence_only_raw()), encoding="utf-8")

    corpus = load_longmemeval_file(path, variant="oracle")

    assert corpus.source_path == str(path)
    assert corpus.fingerprint() == load_longmemeval(
        _evidence_only_raw(), variant="oracle"
    ).fingerprint()


# ── `answer` is not always a string in the real file ─────────────────────────
# Found by loading the real longmemeval_oracle.json rather than a fixture: 32 of
# its 500 instances carry an int here, because counting questions ("how many …")
# were serialized as bare numbers. The adapter originally required a string and
# aborted the whole 500-instance load on instance 60. These tests pin the coercion
# and, just as importantly, pin what is still refused.


def test_int_answer_is_coerced_rather_than_aborting_the_load() -> None:
    """A bare number is semantically a string answer; refusing it loses the corpus."""
    raw = [
        _instance(
            sessions=[("s1", [_turn("user", "How many cows did I buy?", has_answer=True)])],
            answer_session_ids=["s1"],
            answer=3,  # type: ignore[arg-type]  # deliberately the real file's shape
        )
    ]
    corpus = load_longmemeval(raw, variant="oracle")
    assert corpus.instances[0].queries[0].gold_answer == "3"


def test_float_answer_is_coerced() -> None:
    raw = [
        _instance(
            sessions=[("s1", [_turn("user", "What was the total?", has_answer=True)])],
            answer_session_ids=["s1"],
            answer=2.5,  # type: ignore[arg-type]
        )
    ]
    assert load_longmemeval(raw, variant="oracle").instances[0].queries[0].gold_answer == "2.5"


def test_container_answer_still_raises() -> None:
    """A dict or list means the file's shape genuinely changed.

    Stringifying it would feed a Python repr to the judge as if it were the gold
    answer, which is worse than failing the load.
    """
    for bad in ({"a": 1}, ["a"]):
        raw = [
            _instance(
                sessions=[("s1", [_turn("user", "q", has_answer=True)])],
                answer_session_ids=["s1"],
                answer=bad,  # type: ignore[arg-type]
            )
        ]
        with pytest.raises(LongMemEvalSchemaError):
            load_longmemeval(raw, variant="oracle")


def test_bool_answer_raises_and_is_not_treated_as_an_int() -> None:
    """bool subclasses int, so a naive numeric coercion would yield "True"."""
    raw = [
        _instance(
            sessions=[("s1", [_turn("user", "q", has_answer=True)])],
            answer_session_ids=["s1"],
            answer=True,  # type: ignore[arg-type]
        )
    ]
    with pytest.raises(LongMemEvalSchemaError):
        load_longmemeval(raw, variant="oracle")

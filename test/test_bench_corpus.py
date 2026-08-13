"""The corpus contract's invariants, each pinned where a refactor would break it.

Nothing here touches the network, a dataset file, or a store — the contract is
pure dataclasses, and the properties that matter (a session that cannot lie about
which turns it holds, a fingerprint whose sensitivity is deliberate rather than
accidental, a subset that is a slice rather than a sample) are all provable in
memory.

The fingerprint asymmetry is the reason this file exists. ``fingerprint()`` sorts
queries but not sessions and not turns, which means query order is not part of the
corpus identity while session and turn order are. That is a choice, not an
oversight: a dataset's question list is a set, but its transcript is a sequence
whose order changes what a session fragment contains. Both halves are asserted so
a future "let's just sort everything" cleanup shows up as a failing test rather
than as two baselines that silently stopped being comparable.
"""

from __future__ import annotations

import pytest

from kiro_crew.eval.bench.corpus import (
    CAT_MULTI_HOP,
    CAT_SINGLE_HOP,
    BenchInstance,
    BenchQuery,
    BenchSession,
    BenchTurn,
    Corpus,
)


def _turn(turn_id: str, session_id: str = "s1", text: str = "hello there", **kw: object) -> BenchTurn:
    return BenchTurn(
        turn_id=turn_id,
        session_id=session_id,
        speaker=str(kw.pop("speaker", "Alice")),
        text=text,
        **kw,  # type: ignore[arg-type]
    )


def _query(query_id: str = "q1", **kw: object) -> BenchQuery:
    base: dict[str, object] = {
        "query_id": query_id,
        "question": "what happened?",
        "category": CAT_SINGLE_HOP,
    }
    base.update(kw)
    return BenchQuery(**base)  # type: ignore[arg-type]


def _corpus(*instances: BenchInstance, name: str = "toy", variant: str = "v0") -> Corpus:
    return Corpus(name=name, variant=variant, instances=instances)


# ── BenchSession: a session cannot lie about which turns it holds ─────────────


def test_session_rejects_a_turn_filed_under_a_different_session() -> None:
    """A mis-filed turn would corrupt session-level recall without raising.

    ``resolve_gold`` and the ingester both key on ``turn.session_id`` vs
    ``session.session_id`` interchangeably, so a disagreement between them makes
    gold attribution depend on which of the two a given code path happened to
    read. Fail at construction instead.
    """
    with pytest.raises(ValueError, match="claims session 'other'"):
        BenchSession(session_id="s1", turns=(_turn("t1", session_id="other"),))


def test_session_accepts_matching_turns() -> None:
    session = BenchSession(session_id="s1", turns=(_turn("t1"), _turn("t2")))
    assert [t.turn_id for t in session.turns] == ["t1", "t2"]


# ── BenchQuery: category vocabulary is closed ───────────────────────────────


def test_query_rejects_a_category_outside_the_normalized_vocabulary() -> None:
    """Report buckets are a closed set; an unmapped adapter value must not leak in.

    Adapters are expected to degrade an unknown upstream value to ``CAT_UNKNOWN``
    themselves. A raw dataset-native string reaching this constructor means the
    mapping was skipped, and the cost of allowing it is a report bucket nobody
    can compare across datasets.
    """
    with pytest.raises(ValueError, match="unknown category 'single-session-user'"):
        _query(category="single-session-user")


@pytest.mark.parametrize(
    ("sessions", "turns", "expected"),
    [
        ((), (), False),
        (("s1",), (), True),
        ((), ("t1",), True),
        (("s1",), ("t1",), True),
    ],
)
def test_scorable_retrieval_is_false_only_when_both_gold_sets_are_empty(
    sessions: tuple[str, ...], turns: tuple[str, ...], expected: bool
) -> None:
    """Either level of gold is enough to score; only a total absence excludes.

    LoCoMo ships items with an empty evidence list and LongMemEval supplies no
    turn ids at all, so a query with session gold and no turn gold is the normal
    case rather than a degenerate one.
    """
    q = _query(gold_session_ids=sessions, gold_turn_ids=turns)
    assert q.scorable_retrieval is expected


# ── resolve_gold ─────────────────────────────────────────────────────────────


def _haystack() -> tuple[BenchSession, ...]:
    return (
        BenchSession("s1", (_turn("t1"), _turn("t2"))),
        BenchSession("s2", (_turn("t3", session_id="s2"),)),
    )


def test_resolve_gold_drops_a_dangling_turn_id_and_keeps_the_real_one() -> None:
    inst = BenchInstance(
        "i1",
        _haystack(),
        (_query(gold_turn_ids=("t2", "t404"), gold_session_ids=("s1",)),),
    )
    q = inst.resolve_gold().queries[0]
    assert q.gold_turn_ids == ("t2",)
    assert q.gold_session_ids == ("s1",)


def test_resolve_gold_drops_a_dangling_session_id() -> None:
    inst = BenchInstance(
        "i1",
        _haystack(),
        (_query(gold_session_ids=("s2", "s404")),),
    )
    assert inst.resolve_gold().queries[0].gold_session_ids == ("s2",)


def test_resolve_gold_can_empty_a_gold_set_and_make_the_query_unscorable() -> None:
    """An entirely dangling evidence set must become a skip, not a zero.

    Counting a query whose ground truth is absent from the haystack as a miss
    measures the dataset's bookkeeping rather than the memory layer, so the
    resolved query has to report itself as unscorable.
    """
    inst = BenchInstance(
        "i1",
        _haystack(),
        (_query(gold_session_ids=("s404",), gold_turn_ids=("t404",)),),
    )
    q = inst.resolve_gold().queries[0]
    assert (q.gold_session_ids, q.gold_turn_ids) == ((), ())
    assert q.scorable_retrieval is False


def test_resolve_gold_preserves_every_non_gold_field() -> None:
    """The rewrite reconstructs BenchQuery field by field, so it can drop one."""
    inst = BenchInstance(
        "i1",
        _haystack(),
        (
            _query(
                question="who moved?",
                category=CAT_MULTI_HOP,
                gold_answer="Alice",
                adversarial_answer="unknown",
                gold_turn_ids=("t404",),
                unanswerable=True,
                ask_date="2023/05/08",
                raw_category="5",
            ),
        ),
    )
    q = inst.resolve_gold().queries[0]
    assert (q.question, q.category, q.gold_answer) == ("who moved?", CAT_MULTI_HOP, "Alice")
    assert (q.adversarial_answer, q.unanswerable) == ("unknown", True)
    assert (q.ask_date, q.raw_category) == ("2023/05/08", "5")


def test_resolve_gold_leaves_the_haystack_untouched() -> None:
    inst = BenchInstance("i1", _haystack(), (_query(gold_turn_ids=("t404",)),))
    assert inst.resolve_gold().sessions is inst.sessions


# ── fingerprint ──────────────────────────────────────────────────────────────


def _instance(text: str = "hello there", **qkw: object) -> BenchInstance:
    return BenchInstance(
        "i1",
        (BenchSession("s1", (_turn("t1", text=text),)),),
        (_query(**qkw),),
    )


def test_fingerprint_is_stable_across_two_identical_constructions() -> None:
    assert _corpus(_instance()).fingerprint() == _corpus(_instance()).fingerprint()


def test_fingerprint_changes_when_a_turns_text_changes() -> None:
    """Turn text is what reaches the store, so it must be part of corpus identity."""
    a = _corpus(_instance(text="hello there")).fingerprint()
    b = _corpus(_instance(text="hello there!")).fingerprint()
    assert a != b


def test_fingerprint_changes_when_a_gold_set_changes() -> None:
    a = _corpus(_instance(gold_turn_ids=("t1",))).fingerprint()
    b = _corpus(_instance(gold_turn_ids=())).fingerprint()
    assert a != b


def test_fingerprint_is_insensitive_to_query_order_within_an_instance() -> None:
    """Deliberate: fingerprint() sorts an instance's queries by query_id.

    A dataset's question list is a set as far as scoring is concerned — every
    query is measured independently — so two adapters that emit the same
    questions in a different order describe the same measurement and must pin to
    the same hash.
    """
    q1, q2 = _query("q1"), _query("q2", question="and then?")
    sessions = (BenchSession("s1", (_turn("t1"),)),)
    forward = _corpus(BenchInstance("i1", sessions, (q1, q2))).fingerprint()
    reverse = _corpus(BenchInstance("i1", sessions, (q2, q1))).fingerprint()
    assert forward == reverse


def test_fingerprint_is_sensitive_to_session_order() -> None:
    """The other half of the asymmetry, and it is equally deliberate.

    Sessions are a timeline. Their order drives which one the anchored timeline
    treats as newest and therefore the whole decay structure of the ingested
    store, so two corpora differing only in session order are not the same
    measurement and must not share a fingerprint.
    """
    a = BenchSession("s1", (_turn("t1"),))
    b = BenchSession("s2", (_turn("t2", session_id="s2"),))
    forward = _corpus(BenchInstance("i1", (a, b), (_query(),))).fingerprint()
    reverse = _corpus(BenchInstance("i1", (b, a), (_query(),))).fingerprint()
    assert forward != reverse


def test_fingerprint_is_sensitive_to_turn_order_within_a_session() -> None:
    """Turn order changes a session-granularity fragment's text verbatim."""
    t1, t2 = _turn("t1", text="first"), _turn("t2", text="second")
    forward = _corpus(
        BenchInstance("i1", (BenchSession("s1", (t1, t2)),), (_query(),))
    ).fingerprint()
    reverse = _corpus(
        BenchInstance("i1", (BenchSession("s1", (t2, t1)),), (_query(),))
    ).fingerprint()
    assert forward != reverse


def test_fingerprint_changes_with_corpus_name_or_variant() -> None:
    base = _corpus(_instance())
    assert base.fingerprint() != _corpus(_instance(), name="other").fingerprint()
    assert base.fingerprint() != _corpus(_instance(), variant="v1").fingerprint()


# ── subset ───────────────────────────────────────────────────────────────────


def _wide_corpus(n_instances: int = 5, n_queries: int = 4) -> Corpus:
    instances = tuple(
        BenchInstance(
            f"i{i}",
            (BenchSession(f"s{i}", (_turn(f"t{i}", session_id=f"s{i}"),)),),
            tuple(_query(f"i{i}#q{j}") for j in range(n_queries)),
        )
        for i in range(n_instances)
    )
    return _corpus(*instances)


def test_subset_takes_the_first_n_instances_not_a_sample() -> None:
    """Head-slice, and identical on a second call.

    A random subset would need its seed pinned and reported for two arms to be
    comparable at all; a head slice is comparable by construction. Called twice
    so a future ``random.sample`` cannot pass by accident.
    """
    full = _wide_corpus()
    first = full.subset(instances=2)
    second = full.subset(instances=2)
    assert [i.instance_id for i in first.instances] == ["i0", "i1"]
    assert [i.instance_id for i in second.instances] == ["i0", "i1"]
    assert first.fingerprint() == second.fingerprint()


def test_subset_takes_the_first_n_queries_per_instance_not_a_sample() -> None:
    full = _wide_corpus()
    sliced = full.subset(queries_per_instance=2)
    assert [q.query_id for q in sliced.instances[0].queries] == ["i0#q0", "i0#q1"]
    assert full.subset(queries_per_instance=2).fingerprint() == sliced.fingerprint()
    # Every instance is sliced, not just the first.
    assert {len(i.queries) for i in sliced.instances} == {2}
    assert sliced.query_count == 10


def test_subset_records_the_slice_in_the_variant_string() -> None:
    """The report reads variant, so a slice must be impossible to present as full."""
    full = _wide_corpus()
    assert full.subset(instances=2).variant == "v0[2i/allq]"
    assert full.subset(queries_per_instance=3).variant == "v0[alli/3q]"
    assert full.subset(instances=2, queries_per_instance=3).variant == "v0[2i/3q]"
    assert full.subset().variant == "v0[alli/allq]"


def test_subset_preserves_name_source_path_and_notes() -> None:
    full = Corpus(
        name="locomo",
        variant="locomo10",
        instances=_wide_corpus().instances,
        source_path="/tmp/locomo10.json",
        notes=("INFERRED: category mapping",),
    )
    sliced = full.subset(instances=1)
    assert (sliced.name, sliced.source_path, sliced.notes) == (
        "locomo",
        "/tmp/locomo10.json",
        ("INFERRED: category mapping",),
    )


def test_subset_of_a_slice_composes_and_stays_a_head_slice() -> None:
    full = _wide_corpus()
    twice = full.subset(instances=3).subset(instances=2)
    assert [i.instance_id for i in twice.instances] == ["i0", "i1"]
    assert twice.variant == "v0[3i/allq][2i/allq]"


# ── Derived counts ───────────────────────────────────────────────────────────


def test_counts_and_iteration_walk_every_level() -> None:
    corpus = _wide_corpus(n_instances=3, n_queries=2)
    assert (corpus.session_count, corpus.turn_count, corpus.query_count) == (3, 3, 6)
    inst = corpus.instances[0]
    assert inst.turn_count == 1
    assert [t.turn_id for t in inst.iter_turns()] == ["t0"]

"""The LoCoMo adapter's traps, each pinned by a fixture that reproduces it.

No network and no real dataset file on purpose. Every assertion here is about a
shape the adapter must survive, and all of those shapes fit in a few lines of
inline JSON — a test that needs the 2.8 MB corpus to prove that `q['answer']`
would have raised is a test nobody runs.

The failure mode these guard against is silent: a phantom session, a
membership derived from the wrong string, or a dropped caption does not raise,
it just moves the benchmark number.
"""

from __future__ import annotations

import pytest

from kiro_crew.eval.bench.adapters.locomo import IMAGE_MARKER, load_locomo
from kiro_crew.eval.bench.corpus import (
    CAT_ADVERSARIAL,
    CAT_COMMONSENSE,
    CAT_MULTI_HOP,
    CAT_SINGLE_HOP,
    CAT_TEMPORAL,
    CAT_UNKNOWN,
    BenchInstance,
)


def _turn(dia_id: str, speaker: str = "Caroline", text: str = "hi", **extra: object) -> dict:
    return {"dia_id": dia_id, "speaker": speaker, "text": text, **extra}


def _conv(
    sample_id: str = "conv-26",
    *,
    conversation: dict | None = None,
    qa: list[dict] | None = None,
) -> dict:
    """A minimal conversation with the real 6-key top level."""
    base = {
        "speaker_a": "Caroline",
        "speaker_b": "Melanie",
        "session_1": [_turn("D1:1"), _turn("D1:2", speaker="Melanie")],
        "session_1_date_time": "1:56 pm on 8 May, 2023",
    }
    return {
        "sample_id": sample_id,
        "conversation": base if conversation is None else conversation,
        "qa": [] if qa is None else qa,
        "event_summary": {},
        "observation": {},
        "session_summary": {},
    }


def _only(raw: list[dict], *, include_blip_captions: bool = True) -> BenchInstance:
    corpus = load_locomo(raw, include_blip_captions=include_blip_captions)
    assert len(corpus.instances) == 1
    return corpus.instances[0]


# ── Trap 1: orphan *_date_time keys ──────────────────────────────────────────


def test_orphan_date_time_keys_produce_no_phantom_sessions() -> None:
    """conv-26 ships session_20_date_time..session_35_date_time with no turns.

    Deriving sessions from the date keys would invent 16 empty sessions here.
    """
    conversation = {
        "speaker_a": "Caroline",
        "speaker_b": "Melanie",
        "session_1": [_turn("D1:1")],
        "session_1_date_time": "1:56 pm on 8 May, 2023",
        "session_20_date_time": "2:00 pm on 9 May, 2023",
        "session_21_date_time": "3:00 pm on 10 May, 2023",
        "session_35_date_time": "4:00 pm on 11 May, 2023",
    }
    inst = _only([_conv(conversation=conversation)])
    assert [s.session_id for s in inst.sessions] == ["conv-26#session_1"]


def test_a_session_list_with_no_date_key_still_loads() -> None:
    """The orphan relationship also runs the other way; .get() covers both."""
    conversation = {"session_1": [_turn("D1:1")], "session_2": [_turn("D2:1")]}
    inst = _only([_conv(conversation=conversation)])
    assert [s.timestamp for s in inst.sessions] == ["", ""]


# ── Trap: lexical vs numeric session ordering ────────────────────────────────


def test_session_ordering_is_numeric_not_lexical() -> None:
    """session_10 must land after session_9, and session_2 before session_10."""
    conversation = {f"session_{n}": [_turn(f"D{n}:1")] for n in (10, 2, 9, 1, 11)}
    inst = _only([_conv(conversation=conversation)])
    assert [s.session_id for s in inst.sessions] == [
        "conv-26#session_1",
        "conv-26#session_2",
        "conv-26#session_9",
        "conv-26#session_10",
        "conv-26#session_11",
    ]


# ── Trap 2: `answer` absent on adversarial items ─────────────────────────────


def test_adversarial_item_without_answer_key_does_not_raise() -> None:
    """444 of 1 986 items have no `answer` key at all."""
    qa = [
        {
            "question": "When did Caroline move to Berlin?",
            "adversarial_answer": "No information available.",
            "category": 5,
            "evidence": ["D1:1"],
        }
    ]
    query = _only([_conv(qa=qa)]).queries[0]
    assert query.gold_answer is None
    assert query.adversarial_answer == "No information available."
    assert query.unanswerable is True
    assert query.category == CAT_ADVERSARIAL


def test_item_with_both_answer_keys_keeps_both() -> None:
    """Two real items carry `answer` AND `adversarial_answer`."""
    qa = [
        {
            "question": "What did she say?",
            "answer": "She said yes.",
            "adversarial_answer": "Not stated.",
            "category": 5,
            "evidence": ["D1:2"],
        }
    ]
    query = _only([_conv(qa=qa)]).queries[0]
    assert query.gold_answer == "She said yes."
    assert query.adversarial_answer == "Not stated."


def test_only_category_five_is_unanswerable() -> None:
    qa = [
        {"question": "q", "answer": "a", "category": n, "evidence": ["D1:1"]}
        for n in (1, 2, 3, 4, 5)
    ]
    queries = _only([_conv(qa=qa)]).queries
    assert [q.unanswerable for q in queries] == [False, False, False, False, True]
    assert [q.category for q in queries] == [
        CAT_MULTI_HOP,
        CAT_TEMPORAL,
        CAT_COMMONSENSE,
        CAT_SINGLE_HOP,
        CAT_ADVERSARIAL,
    ]
    # The dataset-native value survives so a report number can be traced back.
    assert [q.raw_category for q in queries] == ["1", "2", "3", "4", "5"]


def test_unexpected_category_degrades_to_unknown_instead_of_raising() -> None:
    """A new upstream category should cost one report bucket, not the corpus."""
    qa = [{"question": "q", "answer": "a", "category": 99, "evidence": ["D1:1"]}]
    query = _only([_conv(qa=qa)]).queries[0]
    assert query.category == CAT_UNKNOWN
    assert query.raw_category == "99"
    assert query.unanswerable is False


# ── Dangling and empty evidence ──────────────────────────────────────────────


def test_dangling_evidence_ref_is_dropped_and_query_becomes_unscorable() -> None:
    """7 of 2 815 real refs name a dia_id present in no conversation."""
    qa = [{"question": "q", "answer": "a", "category": 4, "evidence": ["D99:99"]}]
    query = _only([_conv(qa=qa)]).queries[0]
    assert query.gold_turn_ids == ()
    assert query.gold_session_ids == ()
    assert query.scorable_retrieval is False


def test_dangling_ref_alongside_a_real_one_keeps_the_real_one() -> None:
    qa = [{"question": "q", "answer": "a", "category": 4, "evidence": ["D99:99", "D1:2"]}]
    query = _only([_conv(qa=qa)]).queries[0]
    assert query.gold_turn_ids == ("D1:2",)
    assert query.gold_session_ids == ("conv-26#session_1",)
    assert query.scorable_retrieval is True


def test_empty_evidence_list_yields_unscorable_retrieval() -> None:
    """4 real items ship an empty evidence list."""
    qa = [{"question": "q", "answer": "a", "category": 4, "evidence": []}]
    query = _only([_conv(qa=qa)]).queries[0]
    assert query.gold_turn_ids == ()
    assert query.scorable_retrieval is False
    # Still a query — it scores for QA, just not for retrieval.
    assert query.gold_answer == "a"


# ── Membership is the file, not the id string ───────────────────────────────


def test_gold_sessions_trust_membership_over_a_misleading_dia_id() -> None:
    """`dia_id` looks like D<session>:<turn> — but the list it sits in wins.

    A turn with dia_id "D9:1" filed under session_1 must resolve to session_1.
    Parsing the 9 out of the string would file the gold session under a session
    that has nothing to do with the evidence, and nothing would raise.
    """
    conversation = {
        "session_1": [_turn("D1:1"), _turn("D9:1", text="the evidence turn")],
        "session_1_date_time": "1:56 pm on 8 May, 2023",
        "session_9": [_turn("D9:2")],
        "session_9_date_time": "2:00 pm on 1 June, 2023",
    }
    qa = [{"question": "q", "answer": "a", "category": 4, "evidence": ["D9:1"]}]
    inst = _only([_conv(conversation=conversation, qa=qa)])
    assert inst.queries[0].gold_session_ids == ("conv-26#session_1",)
    by_id = {t.turn_id: t.session_id for t in inst.iter_turns()}
    assert by_id["D9:1"] == "conv-26#session_1"


def test_turn_level_evidence_flag_marks_every_referenced_turn() -> None:
    qa = [{"question": "q", "answer": "a", "category": 1, "evidence": ["D1:2"]}]
    inst = _only([_conv(qa=qa)])
    flags = {t.turn_id: t.is_evidence for t in inst.iter_turns()}
    assert flags == {"D1:1": False, "D1:2": True}


# ── Image turns ──────────────────────────────────────────────────────────────


def test_image_turn_variants_all_parse() -> None:
    """All six observed turn key-sets, including hyphenated `re-download`."""
    conversation = {
        "session_1": [
            _turn("D1:1"),
            _turn(
                "D1:2",
                blip_caption="a dog on a beach",
                img_url=["https://example.invalid/a.jpg"],
                query="dog beach",
            ),
            _turn("D1:3", blip_caption="a caption with no img_url"),
            _turn(
                "D1:4",
                blip_caption="a cake",
                img_url=["https://example.invalid/b.jpg"],
                query="cake",
                **{"re-download": 1},
            ),
            _turn(
                "D1:5",
                blip_caption="a bike",
                img_url=["https://example.invalid/c.jpg"],
                **{"re-download": 1},
            ),
            _turn("D1:6", blip_caption="a boat", **{"re-download": 1}),
        ]
    }
    inst = _only([_conv(conversation=conversation)])
    texts = {t.turn_id: t.text for t in inst.iter_turns()}
    assert texts["D1:1"] == "hi"
    assert texts["D1:2"] == f"hi\n{IMAGE_MARKER} a dog on a beach"
    # blip_caption without img_url still contributes its caption.
    assert texts["D1:3"] == f"hi\n{IMAGE_MARKER} a caption with no img_url"
    assert texts["D1:6"] == f"hi\n{IMAGE_MARKER} a boat"
    # The URL itself is never ingested — it is not content a text store can use.
    assert "example.invalid" not in "".join(texts.values())


def test_caption_only_turn_has_no_leading_blank_line() -> None:
    conversation = {"session_1": [_turn("D1:1", text="", blip_caption="a sunset")]}
    inst = _only([_conv(conversation=conversation)])
    assert next(inst.iter_turns()).text == f"{IMAGE_MARKER} a sunset"


def test_caption_inclusion_can_be_turned_off() -> None:
    conversation = {"session_1": [_turn("D1:1", blip_caption="a dog")]}
    inst = _only([_conv(conversation=conversation)], include_blip_captions=False)
    assert next(inst.iter_turns()).text == "hi"


# ── Fingerprint ──────────────────────────────────────────────────────────────


def _fixture_with_caption() -> list[dict]:
    conversation = {
        "session_1": [_turn("D1:1"), _turn("D1:2", blip_caption="a dog on a beach")],
        "session_1_date_time": "1:56 pm on 8 May, 2023",
    }
    qa = [{"question": "q", "answer": "a", "category": 4, "evidence": ["D1:2"]}]
    return [_conv(conversation=conversation, qa=qa)]


def test_fingerprint_is_stable_across_two_loads_of_the_same_input() -> None:
    raw = _fixture_with_caption()
    assert load_locomo(raw).fingerprint() == load_locomo(raw).fingerprint()


def test_fingerprint_changes_when_the_caption_flag_flips() -> None:
    """Ingestion content is a dominant score driver, so it must be pinned."""
    raw = _fixture_with_caption()
    with_captions = load_locomo(raw, include_blip_captions=True).fingerprint()
    without = load_locomo(raw, include_blip_captions=False).fingerprint()
    assert with_captions != without


# ── Corpus-level shape ───────────────────────────────────────────────────────


def test_corpus_identity_and_counts() -> None:
    raw = [_conv("conv-26"), _conv("conv-30")]
    corpus = load_locomo(raw, source_path="/tmp/locomo10.json")
    assert (corpus.name, corpus.variant) == ("locomo", "locomo10")
    assert corpus.source_path == "/tmp/locomo10.json"
    assert [i.instance_id for i in corpus.instances] == ["conv-26", "conv-30"]
    assert corpus.session_count == 2
    assert corpus.turn_count == 4
    # One instance == one memory store, so ids must be unique corpus-wide.
    ids = [q.query_id for i in corpus.instances for q in i.queries]
    assert len(ids) == len(set(ids))


def test_query_ids_are_positional_and_namespaced_by_sample_id() -> None:
    qa = [{"question": f"q{n}", "answer": "a", "category": 4, "evidence": []} for n in range(3)]
    corpus = load_locomo([_conv("conv-41", qa=qa)])
    assert [q.query_id for q in corpus.instances[0].queries] == [
        "conv-41#q0",
        "conv-41#q1",
        "conv-41#q2",
    ]


def test_notes_record_the_inferences_and_the_caption_knob() -> None:
    joined = " | ".join(load_locomo([_conv()]).notes)
    assert "INFERRED" in joined
    assert "blip_caption folded into turn text: True" in joined
    assert "resolve_gold" in joined


def test_non_list_top_level_is_an_actionable_error() -> None:
    with pytest.raises(ValueError, match="JSON array of conversations"):
        load_locomo({"data": []})


def test_speaker_is_the_raw_name_not_a_role() -> None:
    inst = _only([_conv()])
    assert [t.speaker for t in inst.iter_turns()] == ["Caroline", "Melanie"]


def test_timestamp_is_the_raw_unparsed_string() -> None:
    inst = _only([_conv()])
    assert inst.sessions[0].timestamp == "1:56 pm on 8 May, 2023"
    assert all(t.timestamp == "1:56 pm on 8 May, 2023" for t in inst.iter_turns())


def test_locomo_has_no_ask_date() -> None:
    qa = [{"question": "q", "answer": "a", "category": 2, "evidence": ["D1:1"]}]
    assert _only([_conv(qa=qa)]).queries[0].ask_date == ""

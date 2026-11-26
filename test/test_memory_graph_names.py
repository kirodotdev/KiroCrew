"""Topic names for the memory graph (memory_graph_names): c-TF-IDF over tags and
phrases, handles and ids pushed down, a weak topic named by its typical memory."""

from __future__ import annotations

from pathlib import Path

from kiro_crew import memory_graph_names as names


def test_a_handle_id_or_code_is_not_a_subject() -> None:
    for term in ("pmm", "cr3119", "v2", "SDO", "b324a3518", "captain"):
        assert names.looks_like_handle(term, {"captain"}), term
    for term in ("rebase", "conflict resolution", "Security", "playwright"):
        assert not names.looks_like_handle(term, {"captain"}), term


def test_people_are_found_by_handle_and_by_a_reporting_verb() -> None:
    found = names.mentions(
        ["Asked @jevonly about it.", "Ray decided to keep it.", "Bolin said no."]
    )
    assert {"jevonly", "ray", "bolin"} <= found


def test_bot_names_come_from_the_stores_beside_this_one(tmp_path: Path) -> None:
    (tmp_path / "memory_stores" / ("member-kirocrew-captain-" + "a" * 32)).mkdir(parents=True)
    (tmp_path / "memory_stores" / "not-a-member").mkdir()
    assert names.known_names(tmp_path / "memory.db") == {"kirocrew-captain", "captain"}


def _score(members, tokens, texts, known=frozenset()):
    return names.cluster_terms(members, tokens, texts, set(known), lambda t: True)[0]


def test_a_term_of_this_cluster_beats_one_every_cluster_shares() -> None:
    members = {0: [f"a{i}" for i in range(10)], 1: [f"b{i}" for i in range(10)]}
    tokens = {r: ["kirocrew", "rebase"] for r in members[0]} | {
        r: ["kirocrew", "playwright"] for r in members[1]
    }
    scored = _score(members, tokens, {})
    assert scored[0][0][0] == "rebase"
    assert scored[1][0][0] == "playwright"


def test_a_handle_loses_to_a_subject_even_when_carried_more() -> None:
    members = {0: [f"a{i}" for i in range(10)], 1: [f"b{i}" for i in range(10)]}
    tokens = {r: ["jevonly", "rebase"] if i < 9 else ["jevonly"] for i, r in enumerate(members[0])}
    tokens |= {r: ["other"] for r in members[1]}
    assert names.pick_terms(_score(members, tokens, {}, {"jevonly"})[0])[0] == "rebase"


def test_a_phrase_the_topic_repeats_names_it_and_an_id_never_does() -> None:
    members = {0: [f"a{i}" for i in range(10)], 1: [f"b{i}" for i in range(10)]}
    texts = {r: f"Resolved the conflict markers in pr{i}" for i, r in enumerate(members[0])}
    texts |= {r: "Ran the gate." for r in members[1]}
    terms = [t for t, _ in _score(members, {}, texts)[0]]
    assert terms[0] == "conflict markers"
    assert not any(any(ch.isdigit() for ch in t) for t in terms)


def test_a_weak_topic_is_named_by_its_typical_memory() -> None:
    assert names.is_weak([]) and names.is_weak([("x", names.WEAK_SCORE / 2)])
    assert (
        names.memory_title("2026-08-13 14:05 PDT: Ray reverted the release pick, then", {"ray"})
        == "release pick"
    )


def test_a_name_never_ends_in_a_function_word_or_verb_fragment() -> None:
    assert names.clean_term("check runs per") == "check runs"
    assert names.clean_term("findings reasons rather") == "findings reasons"
    assert names.clean_term("the conflict markers") == "conflict markers"
    assert names.clean_term("ray closed out") == "ray"
    assert names.clean_term("per") is None
    assert names.clean_term("testing") == "testing"


def test_two_terms_join_only_when_the_same_memories_carry_both() -> None:
    rows = [f"r{i}" for i in range(10)]
    together = {r: {"git": 1.0, "rebase": 1.0} for r in rows}
    apart = {r: ({"git": 1.0} if i < 5 else {"rebase": 1.0}) for i, r in enumerate(rows)}
    scored = [("git", 1.0), ("rebase", 0.9)]
    assert names.name_terms(scored, rows, together) == ["git", "rebase"]
    assert names.name_terms(scored, rows, apart) == ["git"]

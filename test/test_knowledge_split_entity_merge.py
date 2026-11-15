"""``merge_split_entities``: one node per real-world entity once an alias bridges two."""

from __future__ import annotations

import pytest
from knowledge_helpers import store_fixture  # noqa: F401

from kiro_crew.knowledge.entity_resolution import merge_split_entities
from kiro_crew.knowledge.store import MAX_ENTITY_ALIASES, KnowledgeStore

# Full alias lists: the cap may have dropped one that names a third entity.
_CAPPED_KEEPER = ["Music Bank", *(f"뮤직뱅크 {n}" for n in range(1, MAX_ENTITY_ALIASES))]
_CAPPED_TARGET = [f"Music Bank {n}" for n in range(MAX_ENTITY_ALIASES)]


def _mentioned(store, name, entity_type="program", aliases=None):
    """An entity mentioned by one item of its own, as an ingest leaves it."""
    eid = store.add_entity(name, entity_type, aliases=aliases)
    store.add_mention(store.add_item(f"doc about {name}", "content", "document"), eid)
    return eid


def _entity_ids(store):
    return {row[0] for row in store.db.execute("SELECT id FROM entities")}


def test_a_split_pair_becomes_one_entity_with_both_mentions_and_relations(store):
    korean = _mentioned(store, "뮤직뱅크")
    english = _mentioned(store, "Music Bank")
    kbs = store.add_entity("한국방송공사", "org")
    seoul = store.add_entity("Seoul", "place")
    store.add_entity_relation(korean, kbs, "part_of")
    store.add_entity_relation(english, seoul, "uses")
    # A later ingest of a Korean document attaches the English alias.
    store.add_entity_aliases(korean, ["Music Bank"])

    assert merge_split_entities(store) == 1

    assert _entity_ids(store) == {korean, kbs, seoul}
    mentions = store.db.execute(
        "SELECT COUNT(*) FROM mentions WHERE entity_id = ?", (korean,)
    ).fetchone()[0]
    targets = {
        row[0]
        for row in store.db.execute(
            "SELECT target_id FROM entity_relations WHERE source_id = ?", (korean,)
        )
    }
    assert (mentions, targets) == (2, {kbs, seoul})
    assert store.find_entity("Music Bank")["id"] == korean
    assert merge_split_entities(store) == 0


def test_a_cased_non_latin_target_pairs_by_casefold(store):
    english = _mentioned(store, "Moscow", "place", aliases=["Москва"])
    _mentioned(store, "Москва", "place")
    assert merge_split_entities(store) == 1
    assert _entity_ids(store) == {english}


@pytest.mark.parametrize(
    "entities",
    [
        [("뮤직뱅크", ["Music Bank"]), ("Music Bank", ["KBS Music Bank"])],
        # The kept canonical name never answers it: SQLite's LOWER() folds ASCII only.
        [("München", ["Мюнхен"]), ("Мюнхен", ["MÜNCHEN"])],
        # The kept entity holds it too, but so does an older third one.
        [("Music Bank", ["뮤뱅"]), ("음악중심", ["뮤뱅"]), ("뮤직뱅크", ["Music Bank", "뮤뱅"])],
    ],
    ids=["other-alias", "non-ascii-case", "shared-alias"],
)
def test_a_pair_whose_merge_would_move_one_of_its_names_stays_split(store, entities):
    ids = {_mentioned(store, name, aliases=aliases) for name, aliases in entities}
    names = {n for name, aliases in entities for n in (name, *aliases)}
    before = {n: store.find_entity(n)["id"] for n in names}
    assert merge_split_entities(store) == 0
    assert _entity_ids(store) == ids
    assert {n: store.find_entity(n)["id"] for n in names} == before


@pytest.mark.parametrize(
    ("kept", "merged", "expected"),
    [
        ("KBS 음악 프로그램", "A weekly chart show", "KBS 음악 프로그램\nA weekly chart show"),
        (None, "A weekly chart show", "A weekly chart show"),
        ("KBS 음악 프로그램", None, "KBS 음악 프로그램"),
        ("Music show", "Music show", "Music show"),
    ],
    ids=["both", "kept-empty", "merged-empty", "same"],
)
def test_the_kept_entity_keeps_both_descriptions(store, kept, merged, expected):
    korean = store.add_entity("뮤직뱅크", "program", kept, aliases=["Music Bank"])
    store.add_entity("Music Bank", "program", merged)
    assert merge_split_entities(store) == 1
    assert store.find_entity("뮤직뱅크")["id"] == korean
    assert store.find_entity("뮤직뱅크")["description"] == expected


def test_nothing_is_paired_before_the_alias_fill_completes(store):
    _mentioned(store, "뮤직뱅크", aliases=["Music Bank"])
    _mentioned(store, "Music Bank")
    store.db.execute("DROP INDEX idx_entity_aliases_key")
    assert merge_split_entities(store) == 0
    assert len(_entity_ids(store)) == 2


@pytest.mark.parametrize(
    "entities",
    [
        # Alias-to-alias overlap: neither canonical name is the shared alias.
        [("뮤직뱅크", "program", ["Music Bank"]), ("음악방송", "program", ["Music Bank"])],
        [("뮤직뱅크", "program", ["Music Bank"]), ("Music Bank", "org", None)],
        # Two entities claim the same canonical: no unique target.
        [
            ("뮤직뱅크", "program", ["Music Bank"]),
            ("ミュージックバンク", "program", ["Music Bank"]),
            ("Music Bank", "program", None),
        ],
        # One entity's aliases name two canonicals: no unique target.
        [
            ("뮤직뱅크", "program", ["Music Bank", "ミュージックバンク"]),
            ("Music Bank", "program", None),
            ("ミュージックバンク", "program", None),
        ],
        # A chain: the middle entity has a partner on each side.
        [
            ("뮤직뱅크", "program", ["Music Bank"]),
            ("Music Bank", "program", ["ミュージックバンク"]),
            ("ミュージックバンク", "program", None),
        ],
        # A short Latin abbreviation is lookup-only.
        [("방탄소년단", "group", ["BTS"]), ("BTS", "group", None)],
        # A full alias list proves nothing unique, on either side.
        [("뮤직뱅크", "program", _CAPPED_KEEPER), ("Music Bank", "program", None)],
        [("뮤직뱅크", "program", ["Music Bank"]), ("Music Bank", "program", _CAPPED_TARGET)],
        # ...and still counts as a second claimant.
        [
            ("뮤직뱅크", "program", _CAPPED_KEEPER),
            ("ミュージックバンク", "program", ["Music Bank"]),
            ("Music Bank", "program", None),
        ],
        # A third entity answering to the bridge would take the merged name's lookup.
        [
            ("음악방송", "org", ["Music Bank"]),
            ("뮤직뱅크", "program", ["Music Bank"]),
            ("Music Bank", "program", None),
        ],
        [
            ("뮤직뱅크", "program", ["Music Bank"]),
            ("Music Bank", "program", None),
            ("MUSIC BANK", "org", None),
        ],
    ],
    ids=[
        "alias-to-alias",
        "type-mismatch",
        "two-claimants",
        "two-targets",
        "chain",
        "abbreviation",
        "capped-keeper",
        "capped-target",
        "capped-claimant",
        "claimant-of-another-type",
        "canonical-twin",
    ],
)
def test_pairs_the_ingestion_rule_rejects_stay_split(store, entities):
    ids = {_mentioned(store, name, entity_type, aliases) for name, entity_type, aliases in entities}
    assert merge_split_entities(store) == 0
    assert _entity_ids(store) == ids


def test_a_mutual_pair_merges_once_and_keeps_the_older_entity(store, caplog):
    korean = _mentioned(store, "뮤직뱅크", aliases=["Music Bank"])
    _mentioned(store, "Music Bank", aliases=["뮤직뱅크"])
    with caplog.at_level("WARNING", logger="kiro_crew.knowledge.entity_resolution"):
        assert merge_split_entities(store) == 1
    assert _entity_ids(store) == {korean}
    assert [r.message for r in caplog.records if r.levelname == "WARNING"] == []


def test_a_mutual_pair_keeps_the_newer_entity_when_only_it_keeps_every_name(store):
    _mentioned(store, "뮤직뱅크", aliases=["Music Bank"])
    english = _mentioned(store, "Music Bank", aliases=["뮤직뱅크", "KBS Music Bank"])
    assert merge_split_entities(store) == 1
    assert _entity_ids(store) == {english}


def test_a_mutual_pair_keeps_every_letter_case_of_its_names(store):
    _mentioned(store, "Москва", "place", aliases=["Moscow"])
    english = _mentioned(store, "Moscow", "place", aliases=["Москва"])
    assert merge_split_entities(store) == 1
    names = ("Москва", "москва", "МОСКВА", "moscow")
    assert {store.find_entity(n)["id"] for n in names} == {english}


def test_one_failing_pair_does_not_stop_the_others(store, monkeypatch):
    first = _mentioned(store, "뮤직뱅크", aliases=["Music Bank"])
    first_target = _mentioned(store, "Music Bank")
    second = _mentioned(store, "인기가요", aliases=["Inkigayo Show"])
    _mentioned(store, "Inkigayo Show")
    original = KnowledgeStore.merge_entities

    def failing_first(self, keep_id, merge_id):
        if keep_id == first:
            raise RuntimeError("database is locked")
        return original(self, keep_id, merge_id)

    monkeypatch.setattr(KnowledgeStore, "merge_entities", failing_first)
    assert merge_split_entities(store) == 1
    assert _entity_ids(store) == {first, first_target, second}

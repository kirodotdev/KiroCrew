"""Identity is narrower than retrieval, regardless of bridge direction or row order."""

from __future__ import annotations

import json

import pytest
from knowledge_helpers import store_fixture  # noqa: F401

from kiro_crew.knowledge import ingestion as ingestion_module
from kiro_crew.knowledge import store as store_module
from kiro_crew.knowledge.ingestion import IngestionPipeline


def _ingest(store, name, aliases=(), entity_type="service"):
    pipeline = IngestionPipeline(store, None, None, None, None)
    item = store.add_item("Identity example", "content", "document")
    pipeline._store_entities(
        {"entities": [{"name": name, "type": entity_type, "aliases": list(aliases)}]}, item
    )
    row = store.db.execute("SELECT entity_id FROM mentions WHERE item_id = ?", (item,)).fetchone()
    return row[0] if row else None


@pytest.fixture(params=["indexed", "pre-index"])
def identity_store(store, request):
    if request.param == "pre-index":
        store.db.execute("DROP INDEX idx_entity_aliases_key")
    return store


@pytest.mark.parametrize("direction", ["name-to-alias", "alias-to-canonical"])
@pytest.mark.parametrize(
    ("stored", "incoming", "allowed"),
    [
        ("뮤직뱅크", "Music Bank", True),
        ("Music Bank", "뮤직뱅크", True),
        ("ポケモン", "Pokemon", True),
        ("音楽番組ミュージック", "Music Program", True),
        ("서울특별시", "Seoul", True),
        ("Kubernetes", "Innocent Service", False),
        ("Code Review", "Change Request", False),
        ("뮤직 Music", "Music Bank", False),
        ("뮤직 Music", "뮤직뱅크", False),
        ("音楽番組", "音楽プログラム", False),
        ("ᎠᎡᎢᎣᎤ", "Example Service", False),
        ("뮤직\u200b뱅크", "Music Bank", False),
        ("ー", "Music Bank", False),
        ("ーー", "Music Bank", False),
    ],
)
def test_bridge_policy_in_both_directions(identity_store, direction, stored, incoming, allowed):
    aliases = [incoming] if direction == "name-to-alias" else []
    eid = identity_store.add_entity(stored, "service", aliases=aliases)
    got = _ingest(identity_store, incoming, [stored] if direction == "alias-to-canonical" else [])
    assert (got == eid) is allowed
    assert identity_store.db.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == (
        1 if allowed else 2
    )


@pytest.mark.parametrize(
    "abbreviation", ["CR", "cr", "ＣＲ", "C.R.", "C R", "BTS", "KBS", "NASA", "Go", "S3"]
)
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("direction", ["name-to-alias", "alias-to-canonical"])
def test_short_endpoints_never_merge(identity_store, abbreviation, reverse, direction):
    stored, incoming = (abbreviation, "방탄소년단") if reverse else ("방탄소년단", abbreviation)
    eid = identity_store.add_entity(
        stored, "service", aliases=[incoming] if direction == "name-to-alias" else []
    )
    if direction == "name-to-alias":
        assert identity_store.find_entity(incoming)["id"] == eid
    got = _ingest(identity_store, incoming, [stored] if direction == "alias-to-canonical" else [])
    assert got != eid


@pytest.mark.parametrize("direction", ["name-to-alias", "alias-to-canonical"])
def test_type_conflict_rejected(identity_store, direction):
    eid = identity_store.add_entity(
        "뮤직뱅크", "org", aliases=["Music Bank"] if direction == "name-to-alias" else []
    )
    assert (
        _ingest(
            identity_store, "Music Bank", ["뮤직뱅크"] if direction == "alias-to-canonical" else []
        )
        != eid
    )


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("routes", ["stored-aliases", "extracted-aliases", "both"])
def test_known_ambiguity_abstains(identity_store, reverse, routes):
    targets = ["뮤직뱅크", "음악은행"]
    if reverse:
        targets.reverse()
    for target in targets:
        identity_store.add_entity(
            target, "service", aliases=["Music Bank"] if routes != "extracted-aliases" else []
        )
    aliases = targets if routes != "stored-aliases" else []
    got = _ingest(identity_store, "Music Bank", aliases)
    assert identity_store.find_entity("Music Bank")["id"] == got
    assert identity_store.db.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 3


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("stored_bridge", [False, True])
def test_alias_evidence_overflow_abstains_on_both_routes(identity_store, reverse, stored_bridge):
    first = identity_store.add_entity(
        "뮤직뱅크", "service", aliases=["Music Bank"] if stored_bridge else []
    )
    second = identity_store.add_entity("음악은행", "service")
    aliases = ["뮤직뱅크"] + [
        f"Search Spelling {n}" for n in range(store_module.MAX_ENTITY_ALIASES - 1)
    ]
    aliases.append("음악은행")
    if reverse:
        aliases.reverse()
    got = _ingest(identity_store, "Music Bank", aliases)
    assert got not in {first, second}
    row = identity_store.find_entity("Music Bank")
    assert row["id"] == got
    assert len(json.loads(row["aliases"])) == store_module.MAX_ENTITY_ALIASES


def test_alias_evidence_at_the_bound_can_reuse(identity_store):
    eid = identity_store.add_entity("뮤직뱅크", "service")
    aliases = ["뮤직뱅크"] + [
        f"Search Spelling {n}" for n in range(store_module.MAX_ENTITY_ALIASES - 1)
    ]
    assert _ingest(identity_store, "Music Bank", aliases) == eid


@pytest.mark.parametrize("direction", ["name-to-alias", "alias-to-canonical"])
def test_truncated_bridge_cannot_manufacture_identity(identity_store, direction):
    prefix = "A" * store_module.MAX_ENTITY_ALIAS_LEN
    if direction == "name-to-alias":
        eid = _ingest(identity_store, "뮤직뱅크", [prefix + " different name"])
        assert identity_store.find_entity(prefix)["id"] == eid
        assert _ingest(identity_store, prefix) != eid
    else:
        eid = identity_store.add_entity(prefix, "service")
        assert _ingest(identity_store, "뮤직뱅크", [prefix + " different name"]) != eid


def test_bridge_below_length_bound_remains_eligible(identity_store):
    name = "A" * (store_module.MAX_ENTITY_ALIAS_LEN - 1)
    eid = identity_store.add_entity(name, "service")
    assert _ingest(identity_store, "뮤직뱅크", [name]) == eid


def test_alias_overflow_retains_canonical_priority(identity_store):
    eid = identity_store.add_entity("Music Bank", "org")
    aliases = [f"Search Spelling {n}" for n in range(store_module.MAX_ENTITY_ALIASES + 1)]
    assert _ingest(identity_store, "music bank", aliases, "service") == eid
    assert (
        len(json.loads(identity_store.find_entity("Music Bank")["aliases"]))
        == store_module.MAX_ENTITY_ALIASES
    )


def test_invalid_and_duplicate_aliases_do_not_consume_evidence_budget(identity_store):
    eid = identity_store.add_entity("뮤직뱅크", "service")
    aliases = [None, "", "뮤직뱅크", "뮤직뱅크"] * (store_module.MAX_ENTITY_ALIASES + 1)
    assert _ingest(identity_store, "Music Bank", aliases) == eid


@pytest.mark.parametrize("reverse", [False, True])
def test_conflict_between_routes_abstains(identity_store, reverse):
    rows = [("뮤직뱅크", ["Music Bank"]), ("음악은행", [])]
    if reverse:
        rows.reverse()
    for name, aliases in rows:
        identity_store.add_entity(name, "service", aliases=aliases)
    got = _ingest(identity_store, "Music Bank", ["음악은행"])
    assert identity_store.find_entity("Music Bank")["id"] == got
    assert identity_store.db.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 3


def test_both_routes_to_same_target_reuse_without_alias_propagation(identity_store):
    eid = identity_store.add_entity("뮤직뱅크", "service", aliases=["Music Bank"])
    assert _ingest(identity_store, "Music Bank", ["뮤직뱅크", "Other Service"]) == eid
    assert json.loads(identity_store.find_entity("뮤직뱅크")["aliases"]) == ["Music Bank"]


@pytest.mark.parametrize("reverse", [False, True])
def test_ineligible_candidate_does_not_hide_eligible_one(identity_store, reverse):
    rows = [("Unrelated Service", "service"), ("뮤직뱅크", "service"), ("음악은행", "org")]
    if reverse:
        rows.reverse()
    ids = {name: identity_store.add_entity(name, typ, aliases=["Music Bank"]) for name, typ in rows}
    assert _ingest(identity_store, "Music Bank") == ids["뮤직뱅크"]


def test_canonical_hit_retains_priority_and_type_semantics(identity_store):
    identity_store.add_entity("뮤직뱅크", "service", aliases=["Music Bank"])
    eid = identity_store.add_entity("Music Bank", "org")
    assert _ingest(identity_store, "music bank", ["뮤직뱅크"], "service") == eid


def test_alias_only_and_transitive_paths_do_not_merge(identity_store):
    eid = identity_store.add_entity("Music Bank", "service", aliases=["Shared Bridge"])
    second = _ingest(identity_store, "뮤직뱅크", ["Shared Bridge"])
    assert second != eid
    assert _ingest(identity_store, "음악은행", ["Music Bank", "Unrelated Service"]) == eid
    assert _ingest(identity_store, "Unrelated Service") != eid


@pytest.mark.parametrize("reverse", [False, True])
def test_cr_collision_keeps_full_names_and_lookup_alias(identity_store, reverse):
    first, second = ("Code Review", "Change Request")
    if reverse:
        first, second = second, first
    eid = identity_store.add_entity(first, "service", aliases=["CR"])
    assert identity_store.find_entity("cr")["id"] == eid
    assert _ingest(identity_store, second, ["CR"]) != eid
    assert _ingest(identity_store, "CR") not in {eid, identity_store.find_entity(second)["id"]}


def test_same_type_malicious_alias_cannot_absorb_trusted_mentions(identity_store):
    trusted_item = identity_store.add_item("Trusted", "trusted content", "document")
    trusted = identity_store.add_entity("Kubernetes", "service")
    identity_store.add_mention(trusted_item, trusted)
    incoming = _ingest(identity_store, "Innocent Service", ["Kubernetes"])
    assert incoming != trusted
    assert (
        identity_store.db.execute(
            "SELECT entity_id FROM mentions WHERE item_id = ?", (trusted_item,)
        ).fetchone()[0]
        == trusted
    )
    assert (
        identity_store.db.execute(
            "SELECT COUNT(*) FROM mentions WHERE entity_id = ?", (trusted,)
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize("name", ["\ud800", " \udfff ", None, 42, ""])
def test_malformed_canonical_never_reaches_lookup(store, monkeypatch, name):
    def unexpected(*args):
        pytest.fail("malformed canonical reached SQLite lookup")

    monkeypatch.setattr(store, "entity_identity_candidates", unexpected)
    assert _ingest(store, name) is None


def test_decision_normalization_preserves_storage_and_lookup(identity_store):
    from kiro_crew.knowledge.entity_resolution import alias_merge_allowed

    eid = identity_store.add_entity("뮤직뱅크", "service", aliases=["Ｍｕｓｉｃ Ｂａｎｋ"])
    assert _ingest(identity_store, "Ｍｕｓｉｃ Ｂａｎｋ") == eid
    assert json.loads(identity_store.find_entity("뮤직뱅크")["aliases"]) == ["Ｍｕｓｉｃ Ｂａｎｋ"]
    assert identity_store.find_entity("Music Bank") is None
    assert alias_merge_allowed("Cafe\u0301 Music", "뮤직뱅크", "Café Music")


def test_redacted_empty_alias_does_not_restore_original(monkeypatch):
    monkeypatch.setattr("kiro_crew.knowledge.ingestion._redact", lambda text: "")
    assert ingestion_module._coerce_aliases(["sensitive original"], "Example") is None


def test_identity_index_path_never_parses_unrelated_alias_rows(store):
    eid = store.add_entity("뮤직뱅크", "service", aliases=["Music Bank"])
    store.add_entity("Unrelated", "service", aliases=["Other"])
    store.db.execute("UPDATE entities SET aliases = '{malformed' WHERE name = 'Unrelated'")
    assert _ingest(store, "Music Bank") == eid


def test_candidate_queries_stay_indexed_and_do_not_grow_per_alias(store):
    traces = []
    for count in (2, 10):
        statements = []
        store.db.set_trace_callback(statements.append)
        try:
            assert (
                list(
                    store.entity_identity_candidates(
                        "뮤직뱅크", [f"Example Service {n}" for n in range(count)]
                    )
                )
                == []
            )
        finally:
            store.db.set_trace_callback(None)
        traces.append(len(statements))
    assert traces[0] == traces[1]
    alias_plan = store.db.execute(
        "EXPLAIN QUERY PLAN SELECT e.* FROM entity_aliases a "
        "JOIN entities e ON e.id = a.entity_id WHERE a.alias_key = ?",
        ("Music Bank",),
    ).fetchall()
    canonical_plan = store.db.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM entities WHERE LOWER(name) IN (LOWER(?), LOWER(?))",
        ("Music Bank", "Other Service"),
    ).fetchall()
    assert any("idx_entity_aliases_key" in row[3] for row in alias_plan)
    assert any("idx_entities_name_lower" in row[3] for row in canonical_plan)

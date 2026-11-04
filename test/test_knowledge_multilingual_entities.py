"""Cross-language entity resolution: bare source-language names, aliases, and the alias index."""

from __future__ import annotations

import json

from knowledge_helpers import store_factory_fixture, store_fixture  # noqa: F401

from kiro_crew.knowledge.extractor import EntityExtractor


class TestMultilingualEntityResolution:
    def test_parse_response_preserves_aliases(self):
        # aliases pass through _parse_response intact for a non-Latin chunk.
        # The canonical name is the bare source-language form; the English
        # translation and the parenthetical form travel as aliases.
        ext = EntityExtractor()
        raw = json.dumps(
            {
                "title": "뮤직뱅크 1위",
                "entities": [
                    {
                        "name": "뮤직뱅크",
                        "type": "service",
                        "description": "KBS 음악 프로그램",
                        "aliases": ["Music Bank", "뮤직뱅크 (Music Bank)"],
                    },
                    {
                        "name": "방탄소년단",
                        "type": "org",
                        "description": "케이팝 그룹",
                        "aliases": ["BTS", "방탄소년단 (BTS)"],
                    },
                ],
                "relations": [],
                "category": "report",
                "summary": "방탄소년단이 뮤직뱅크에서 1위를 달성했다.",
            }
        )
        result = ext._parse_response(raw)
        assert result["title"] == "뮤직뱅크 1위"
        assert result["summary"] == "방탄소년단이 뮤직뱅크에서 1위를 달성했다."
        entities = {e["name"]: e for e in result["entities"]}
        assert "뮤직뱅크" in entities
        assert entities["뮤직뱅크"]["aliases"] == ["Music Bank", "뮤직뱅크 (Music Bank)"]
        assert "방탄소년단" in entities
        assert entities["방탄소년단"]["aliases"] == ["BTS", "방탄소년단 (BTS)"]

    def test_aliases_stored_and_searchable_via_bare_form(self, store):
        # Bare canonical name resolves via exact match (O(1)).
        # English translation and parenthetical form resolve via aliases scan.
        store.add_entity("뮤직뱅크", "service", aliases=["Music Bank", "뮤직뱅크 (Music Bank)"])
        # Exact match — should hit without aliases scan
        assert store.find_entity("뮤직뱅크") is not None
        # Aliases scan — English and parenthetical form both resolve
        assert store.find_entity("Music Bank") is not None
        assert store.find_entity("뮤직뱅크 (Music Bank)") is not None

    def test_aliases_stored_and_searchable_arabic(self, store):
        # Non-Latin: Arabic bare canonical with English alias.
        store.add_entity("القاهرة", "org", aliases=["Cairo", "القاهرة (Cairo)"])
        assert store.find_entity("القاهرة") is not None
        assert store.find_entity("Cairo") is not None
        assert store.find_entity("القاهرة (Cairo)") is not None

    def test_find_entity_bare_canonical_hits_exact_match(self, store):
        # Bare canonical must be found via exact match (not aliases scan).
        # English translation and parenthetical form must also resolve via aliases.
        store.add_entity(
            "뮤직뱅크",
            "service",
            description="KBS music chart show",
            aliases=["Music Bank", "뮤직뱅크 (Music Bank)"],
        )
        # Exact match on bare canonical
        result = store.find_entity("뮤직뱅크")
        assert result is not None
        assert result["name"] == "뮤직뱅크"
        # Parenthetical must NOT be the stored canonical
        assert result["name"] != "뮤직뱅크 (Music Bank)"
        # English alias must also resolve to the same entity
        result_en = store.find_entity("Music Bank")
        assert result_en is not None
        assert result_en["name"] == "뮤직뱅크"

    def test_cross_language_entity_reuse_via_aliases(self, store):
        # Regression: an entity stored under an English name first must be found
        # when a later chunk emits the Korean form, if the English entity already
        # carries the Korean string as an alias. The store's find_entity already
        # performs an alias scan — this test pins that behavior at the store layer.
        store.add_entity(
            "Music Bank",
            "service",
            aliases=["뮤직뱅크", "뮤직뱅크 (Music Bank)"],
        )
        result = store.find_entity("뮤직뱅크")
        assert (
            result is not None
        ), "Korean alias must resolve to the English-first entity via store alias scan"
        assert result["name"] == "Music Bank"

    def test_aliases_non_list_values_are_safe(self):
        # The LLM may return aliases as null, a string, or a number.
        # Exercises the production _coerce_aliases helper directly so deleting
        # it would break this test (not a local copy that drifts silently).
        from kiro_crew.knowledge.ingestion import _coerce_aliases

        assert _coerce_aliases(None, "X") is None
        assert _coerce_aliases("bare_string", "X") is None
        assert _coerce_aliases(42, "X") is None
        assert _coerce_aliases([], "X") is None
        # name itself is stripped from aliases
        assert _coerce_aliases(["X", "alias"], "X") == ["alias"]
        # normal case: bare canonical, aliases contain English + parenthetical
        assert _coerce_aliases(["Music Bank", "뮤직뱅크 (Music Bank)"], "뮤직뱅크") == [
            "Music Bank",
            "뮤직뱅크 (Music Bank)",
        ]

    def test_aliases_count_and_length_bounds(self):
        # _coerce_aliases enforces a count cap of 10 and a per-string length cap of 200
        # so unbounded LLM output cannot grow the DB or slow the alias scan.
        # Literal limits pin the intended values — importing the constants would let
        # either cap loosen without failing the test.
        from kiro_crew.knowledge.ingestion import _coerce_aliases

        # Count bound: only the first 10 VALID entries survive (valid-count cap,
        # not raw input position cap).
        many = [f"alias_{i}" for i in range(15)]
        result = _coerce_aliases(many, "canonical")
        assert result is not None
        assert len(result) == 10

        # Length bound: strings longer than 200 chars are truncated
        long_alias = "x" * 250
        result = _coerce_aliases([long_alias], "canonical")
        assert result is not None
        assert len(result[0]) == 200

    def test_coerce_aliases_valid_count_cap_skips_invalid_entries(self):
        """Valid-count cap: invalid entries must not consume valid alias slots.

        A raw list of 10 None values followed by a valid alias must keep the valid
        alias, not discard it because the Nones "filled" the first 10 slots.
        This was the bug with the old raw[:MAX] approach.
        """
        from kiro_crew.knowledge.ingestion import _coerce_aliases

        raw = [None] * 10 + ["Music Bank"]  # 10 invalid + 1 valid
        result = _coerce_aliases(raw, "뮤직뱅크")
        assert result == [
            "Music Bank"
        ], "valid alias after 10 invalid entries must survive (valid-count cap)"

    # ---------------------------------------------------------------------------
    # A-prime: Conservative Lexical Entity Identity Resolution invariants
    # ---------------------------------------------------------------------------

    def test_entity_resolution_korean_then_english(self, store):
        """KO doc first, EN doc second -> must produce one entity, two mentions.

        The real-world scenario: EN doc's entity "Music Bank" has NO aliases
        (new prompt rule: Latin-script names need no translation). Resolution
        uses Tier 2 (name-to-existing-alias): find_entity("Music Bank") returns
        the Korean entity because "Music Bank" is in its persisted aliases,
        and the guard confirms "Music Bank" ∈ existing aliases (casefold).
        """
        from kiro_crew.knowledge.ingestion import IngestionPipeline, _coerce_aliases

        # Simulate ingestion of a Korean chunk: entity "뮤직뱅크" with alias "Music Bank"
        ko_name = "뮤직뱅크"
        ko_aliases = _coerce_aliases(["Music Bank"], ko_name)
        eid = store.add_entity(ko_name, "service", aliases=ko_aliases)
        item1 = store.add_item("Korean doc", "뮤직뱅크 관련 내용", "document")
        store.add_mention(item1, eid)

        # Simulate ingestion of an English chunk: entity "Music Bank"
        # with NO aliases — this is what the new extractor prompt produces
        # for Latin-script names ("Latin-script names need no translation").
        # Resolution must use Tier 2: "Music Bank" ∈ existing entity's aliases.
        pipeline = IngestionPipeline(
            store=store, extractor=None, chunker=None, reader=None, embedder=None
        )
        extraction = {
            "entities": [
                {
                    "name": "Music Bank",
                    "type": "service",
                    "description": "KBS music chart show",
                    "aliases": [],
                }
            ],
            "relations": [],
        }
        item2 = store.add_item("English doc", "Music Bank chart results", "document")
        pipeline._store_entities(extraction, item2)

        # Must be exactly one entity
        entities = store.db.execute("SELECT id FROM entities").fetchall()
        assert len(entities) == 1, (
            f"expected 1 entity, got {len(entities)} "
            "(KO→EN dedup failed: English name not resolved via existing alias)"
        )
        assert entities[0]["id"] == eid

        # Both items must have a mention attached to that one entity
        mentions = store.db.execute(
            "SELECT item_id FROM mentions WHERE entity_id = ?", (eid,)
        ).fetchall()
        mention_items = {m["item_id"] for m in mentions}
        assert item1 in mention_items
        assert item2 in mention_items
        assert len(mentions) == 2

    def test_entity_resolution_english_then_korean(self, store):
        """EN doc first, KO doc second -> must produce one entity, two mentions."""
        from kiro_crew.knowledge.ingestion import IngestionPipeline

        # Simulate ingestion of an English chunk: entity "Music Bank"
        en_name = "Music Bank"
        eid = store.add_entity(en_name, "service")
        item1 = store.add_item("English doc", "Music Bank chart results", "document")
        store.add_mention(item1, eid)

        # Simulate ingestion of a Korean chunk: entity "뮤직뱅크" with alias "Music Bank"
        # _store_entities must resolve via alias fallback: "Music Bank" == entity.name casefold.
        pipeline = IngestionPipeline(
            store=store, extractor=None, chunker=None, reader=None, embedder=None
        )
        extraction = {
            "entities": [
                {
                    "name": "뮤직뱅크",
                    "type": "service",
                    "description": "KBS 음악 프로그램",
                    "aliases": ["Music Bank"],
                }
            ],
            "relations": [],
        }
        item2 = store.add_item("Korean doc", "뮤직뱅크 관련 내용", "document")
        pipeline._store_entities(extraction, item2)

        entities = store.db.execute("SELECT id FROM entities").fetchall()
        assert len(entities) == 1, (
            f"expected 1 entity, got {len(entities)} "
            "(ingestion-order dependent dedup failed: EN->KO)"
        )
        assert entities[0]["id"] == eid

        mentions = store.db.execute(
            "SELECT item_id FROM mentions WHERE entity_id = ?", (eid,)
        ).fetchall()
        mention_items = {m["item_id"] for m in mentions}
        assert item1 in mention_items
        assert item2 in mention_items
        assert len(mentions) == 2

    def test_alias_lookup_identity(self, store):
        """find_entity("뮤직뱅크") and find_entity("Music Bank") return the same entity_id."""
        eid = store.add_entity("뮤직뱅크", "service", aliases=["Music Bank", "MBC Music Bank"])
        result_ko = store.find_entity("뮤직뱅크")
        result_en = store.find_entity("Music Bank")
        assert result_ko is not None
        assert result_en is not None
        assert result_ko["id"] == eid
        assert result_en["id"] == eid
        assert result_ko["id"] == result_en["id"]

    def test_relation_keeps_acronym_separate_from_cross_script_name(self, store):
        """A relation may reuse a full-name bridge but never an acronym endpoint."""
        from kiro_crew.knowledge.ingestion import IngestionPipeline

        # Seed: English-first entities with Korean aliases
        eid_bts = store.add_entity("BTS", "org", aliases=["방탄소년단"])
        eid_mb = store.add_entity("Music Bank", "service", aliases=["뮤직뱅크"])

        pipeline = IngestionPipeline(
            store=store, extractor=None, chunker=None, reader=None, embedder=None
        )

        # Korean document: 방탄소년단 -> 뮤직뱅크
        extraction_ko = {
            "entities": [
                {
                    "name": "방탄소년단",
                    "type": "org",
                    "description": "K-pop group",
                    "aliases": ["BTS"],
                },
                {
                    "name": "뮤직뱅크",
                    "type": "service",
                    "description": "KBS music show",
                    "aliases": ["Music Bank"],
                },
            ],
            "relations": [
                {
                    "source": "방탄소년단",
                    "target": "뮤직뱅크",
                    "type": "works_on",
                    "description": "performed on",
                },
            ],
        }
        item_ko = store.add_item("KO doc", "방탄소년단이 뮤직뱅크에 출연", "document")
        pipeline._store_entities(extraction_ko, item_ko)

        # The full program name is reused; the ambiguous acronym stays separate.
        entity_count = store.db.execute("SELECT COUNT(*) AS c FROM entities").fetchone()["c"]
        assert entity_count == 3

        # The relation follows resolved names while preserving the acronym node.
        rel = store.db.execute(
            "SELECT source_id, target_id FROM entity_relations WHERE source_item_id = ?", (item_ko,)
        ).fetchone()
        assert rel is not None
        assert rel["source_id"] != eid_bts
        assert store.find_entity("방탄소년단")["id"] == rel["source_id"]
        assert rel["target_id"] == eid_mb

    def test_tier3_entity_type_conflict_blocks_merge(self, store):
        """Tier 3 must reject merge when entity types conflict.

        person "Jordan" (aliases=[]) + incoming org "요르단" (aliases=["Jordan"])
        → Tier 3: find_entity("Jordan") hits person entity (canonical-equality guard passes),
          but entity_type conflict (person vs org) → reject → new entity created.
        """
        from kiro_crew.knowledge.ingestion import IngestionPipeline

        store.add_entity("Jordan", "person")
        pipeline = IngestionPipeline(
            store=store, extractor=None, chunker=None, reader=None, embedder=None
        )
        item = store.add_item("org doc", "요르단 관련 내용", "document")
        pipeline._store_entities(
            {
                "entities": [
                    {
                        "name": "요르단",
                        "type": "org",
                        "description": "Middle East country",
                        "aliases": ["Jordan"],
                    }
                ],
                "relations": [],
            },
            item,
        )
        # Must be 2 entities — type conflict must block the merge
        count = store.db.execute("SELECT COUNT(*) AS c FROM entities").fetchone()["c"]
        assert count == 2, (
            f"expected 2 entities (person Jordan + org 요르단), got {count} "
            "(entity_type conflict must block Tier 3 merge)"
        )
        jordan_entity = store.find_entity("Jordan")
        assert (
            jordan_entity["entity_type"] == "person"
        ), "person Jordan must not have been corrupted to org"

    def test_tier2_entity_type_conflict_blocks_merge(self, store):
        """Tier 2 must also reject merge when entity types conflict.

        existing: name="Paris", aliases=["파리"], entity_type="city"
        incoming: name="파리", aliases=[], entity_type="org"
        → Tier 2: find_entity("파리") returns Paris entity (alias scan),
          "파리" ∈ existing aliases, BUT entity_type conflict (city vs org)
          → reject → new entity created for "파리".
        """
        from kiro_crew.knowledge.ingestion import IngestionPipeline

        store.add_entity("Paris", "city", aliases=["파리"])
        pipeline = IngestionPipeline(
            store=store, extractor=None, chunker=None, reader=None, embedder=None
        )
        item = store.add_item("org doc", "파리 협정 관련", "document")
        pipeline._store_entities(
            {
                "entities": [
                    {"name": "파리", "type": "org", "description": "Paris Agreement", "aliases": []}
                ],
                "relations": [],
            },
            item,
        )
        # Must be 2 entities — type conflict must block the Tier 2 merge
        count = store.db.execute("SELECT COUNT(*) AS c FROM entities").fetchone()["c"]
        assert count == 2, (
            f"expected 2 entities (city Paris + org 파리), got {count} "
            "(entity_type conflict must block Tier 2 merge)"
        )
        paris = store.find_entity("Paris")
        assert paris["entity_type"] == "city", "city Paris must not have been corrupted by org 파리"

    def test_coerce_aliases_casefold_dedupe(self):
        """_coerce_aliases must casefold-deduplicate aliases within a single call."""
        from kiro_crew.knowledge.ingestion import _coerce_aliases

        # "KBS" and "kbs" are casefold-equivalent → only the first should survive
        result = _coerce_aliases(["KBS", "kbs", "KBS 1TV", "kbs 1tv"], "SomeName")
        assert result is not None
        result_cf = [a.casefold() for a in result]
        assert len(result_cf) == len(
            set(result_cf)
        ), "casefold duplicates must not appear in _coerce_aliases output"
        assert "KBS" in result, "first occurrence of KBS must be kept"
        assert "kbs" not in result, "casefold duplicate kbs must be dropped"
        assert "KBS 1TV" in result, "first occurrence of KBS 1TV must be kept"
        assert "kbs 1tv" not in result, "casefold duplicate kbs 1tv must be dropped"

    def test_alias_safety_boundary_is_complete(self, store):
        """Both safety boundaries must block merges — negative test.

        Path A: Two unrelated entities share a common alias but have different
        canonical names and different aliases. Neither should merge into the other
        via alias-to-alias collision on the Tier 3 fallback.

        existing1: name="Alpha", aliases=["Shared"]
        existing2: name="Beta",  aliases=["Shared"]
        incoming:  name="Gamma", aliases=["Shared"]
        -> "Shared" ∈ existing1.aliases AND existing2.aliases
        -> Tier 3: find_entity("Shared") returns one of them, but
           that entity's canonical name != "Shared" -> guard blocks
        -> Gamma must be a new, distinct entity

        Path B: incoming alias matches another entity's alias (not canonical) ->
        alias fallback guard must block (alias != existing canonical).
        """
        from kiro_crew.knowledge.ingestion import IngestionPipeline

        store.add_entity("Alpha", "service", aliases=["Shared"])
        store.add_entity("Beta", "service", aliases=["Shared"])
        # --- Path A ---
        # Two entities both have "Shared" as an alias.
        # Incoming entity "Gamma" with alias "Shared" must NOT merge with either.
        pipeline = IngestionPipeline(
            store=store, extractor=None, chunker=None, reader=None, embedder=None
        )
        item_a = store.add_item("Path A doc", "content", "document")
        pipeline._store_entities(
            {
                "entities": [
                    {
                        "name": "Gamma",
                        "type": "service",
                        "description": "new entity",
                        "aliases": ["Shared"],
                    }
                ],
                "relations": [],
            },
            item_a,
        )
        # Must have 3 entities: Alpha, Beta, Gamma (not merged via alias-to-alias)
        entity_count_a = store.db.execute("SELECT COUNT(*) AS c FROM entities").fetchone()["c"]
        assert entity_count_a == 3, (
            f"Path A: expected 3 entities (Alpha, Beta, Gamma), got {entity_count_a} "
            "(alias-to-alias merge must not occur via Tier 3)"
        )

        store.add_entity("Delta", "concept", aliases=["Omega"])
        # --- Path B ---
        # existing: name="Delta", aliases=["Omega"]
        # incoming: name="Epsilon", aliases=["Omega"]
        # Tier 3 alias fallback: find_entity("Omega") -> returns Delta,
        # but Delta.name ("Delta") != "Omega" -> guard blocks merge.
        item_b = store.add_item("Path B doc", "content", "document")
        pipeline._store_entities(
            {
                "entities": [
                    {
                        "name": "Epsilon",
                        "type": "concept",
                        "description": "another entity",
                        "aliases": ["Omega"],
                    }
                ],
                "relations": [],
            },
            item_b,
        )
        # After Path A: 3 entities. After Path B: Delta + Epsilon = 5 total.
        final_count = store.db.execute("SELECT COUNT(*) AS c FROM entities").fetchone()["c"]
        assert final_count == 5, (
            f"Path B: expected 5 total entities (Alpha, Beta, Gamma, Delta, Epsilon), "
            f"got {final_count} (alias-to-alias merge was not blocked)"
        )
        epsilon = store.find_entity("Epsilon")
        delta = store.find_entity("Delta")
        assert epsilon is not None, "Epsilon entity must exist as its own entity"
        assert delta is not None, "Delta entity must still exist"
        assert (
            epsilon["id"] != delta["id"]
        ), "Epsilon must NOT have been merged into Delta via alias-to-alias match"


def _alias_rows(store, entity_id):
    return sorted(
        r[0]
        for r in store.db.execute(
            "SELECT alias_key FROM entity_aliases WHERE entity_id = ?", (entity_id,)
        )
    )


def _drop_alias_index(store):
    """Put the store back in the state of a library that predates the alias index."""
    store.db.execute("DROP INDEX IF EXISTS idx_entity_aliases_key")
    store.db.execute("DELETE FROM entity_aliases")


class TestEntityAliasIndex:
    """find_entity's alias leg is an indexed probe, kept current by every alias writer."""

    def test_a_new_library_has_the_alias_index_at_once(self, store):
        assert store.entity_alias_index_ready()

    def test_the_alias_leg_uses_the_index_not_a_scan(self, store):
        store.add_entity("뮤직뱅크", "service", aliases=["Music Bank"])
        plan = " ".join(
            str(r[3])
            for r in store.db.execute(
                "EXPLAIN QUERY PLAN SELECT e.* FROM entity_aliases a "
                "JOIN entities e ON e.id = a.entity_id "
                "WHERE a.alias_key = LOWER(?) ORDER BY e.rowid LIMIT 1",
                ("music bank",),
            )
        )
        assert "idx_entity_aliases_key" in plan
        # No json parse of entity rows on the lookup path: a raw scan would
        # have to decode the aliases column of every row.
        assert store.find_entity("MUSIC BANK")["name"] == "뮤직뱅크"
        assert store.find_entity("nothing like it") is None

    def test_insert_update_and_delete_keep_the_table_current(self, store):
        eid = store.add_entity("방탄소년단", "org", aliases=["BTS", "Bangtan Sonyeondan"])
        assert _alias_rows(store, eid) == ["bangtan sonyeondan", "bts"]
        store.add_entity_aliases(eid, ["Bangtan Boys"])
        assert _alias_rows(store, eid) == ["bangtan boys", "bangtan sonyeondan", "bts"]
        assert store.find_entity("bangtan boys")["id"] == eid
        store.db.execute("DELETE FROM entities WHERE id = ?", (eid,))
        assert _alias_rows(store, eid) == []
        assert store.find_entity("BTS") is None

    def test_merge_and_orphan_prune_drop_the_alias_rows(self, store):
        keep = store.add_entity("KBS", "org", aliases=["Korea Broadcasting System"])
        gone = store.add_entity("한국방송공사", "org", aliases=["KBS Corp"])
        store.merge_entities(keep, gone)
        assert _alias_rows(store, gone) == []
        assert store.find_entity("KBS Corp") is None
        store.db.execute("BEGIN IMMEDIATE")
        store._prune_orphan_entities()
        store.db.execute("COMMIT")
        assert store.db.execute("SELECT COUNT(*) FROM entity_aliases").fetchone()[0] == 0

    def test_the_fill_indexes_only_string_aliases_and_skips_damaged_rows(self, store):
        mixed = store.add_entity("Cairo", "org")
        damaged = store.add_entity("Giza", "org")
        shaped = store.add_entity("Luxor", "org")
        # Legacy values no in-tree writer produces any more.
        for eid, text in (
            (mixed, '["القاهرة", 7, null]'),
            (damaged, "not json"),
            (shaped, '{"a": "b"}'),
        ):
            store.db.execute("UPDATE entities SET aliases = ? WHERE id = ?", (text, eid))
        _drop_alias_index(store)
        store.ensure_search_indexes()
        assert store.entity_alias_index_ready()
        assert _alias_rows(store, mixed) == ["القاهرة"]
        assert _alias_rows(store, damaged) == []
        assert _alias_rows(store, shaped) == []
        assert store.find_entity("القاهرة")["id"] == mixed

    def test_alias_lookup_folds_unicode_case(self, store):
        munich = store.add_entity("München", "org", aliases=["MÜNCHEN Stadt"])
        street = store.add_entity("Hauptstraße", "place", aliases=["Große Straße"])
        assert store.find_entity("münchen stadt")["id"] == munich
        assert store.find_entity("GROSSE STRASSE")["id"] == street
        # Same answers from the scan a library uses before its index is built.
        _drop_alias_index(store)
        assert store.find_entity("münchen stadt")["id"] == munich
        assert store.find_entity("GROSSE STRASSE")["id"] == street

    def test_a_failed_alias_write_leaves_no_entity_row(self, store, monkeypatch):
        import pytest

        from kiro_crew.knowledge.store_parts import entity_graph

        def boom(*_a, **_k):
            raise RuntimeError("alias write failed")

        monkeypatch.setattr(entity_graph, "index_entity_aliases", boom)
        with pytest.raises(RuntimeError):
            store.add_entity("Orphan", "concept", aliases=["x"])
        assert store.db.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0
        assert not store.db.in_transaction

    def test_a_library_without_the_index_scans_until_the_build_runs(self, store):
        eid = store.add_entity("뮤직뱅크", "service", aliases=["Music Bank"])
        store.ensure_fts_index_current()
        assert not store.has_pending_one_time_work()
        _drop_alias_index(store)
        assert not store.entity_alias_index_ready()
        assert store.has_pending_one_time_work()
        # Results do not depend on the index: the old scan still finds it.
        assert store.find_entity("music bank")["id"] == eid
        # A write made before the build is not doubled by it.
        other = store.add_entity("할시", "person", aliases=["Halsey"])
        store.ensure_search_indexes()
        assert store.entity_alias_index_ready()
        assert not store.has_pending_one_time_work()
        assert _alias_rows(store, eid) == ["music bank"]
        assert _alias_rows(store, other) == ["halsey"]
        assert store.find_entity("music bank")["id"] == eid
        assert store.find_entity("HALSEY")["id"] == other
        # Idempotent.
        store.ensure_search_indexes()
        assert _alias_rows(store, other) == ["halsey"]

    def test_imported_entities_are_indexed(self, store, store_factory):
        store.add_entity("뮤직뱅크", "service", aliases=["Music Bank"])
        bundle = store.export_all()
        other = store_factory("other.db")
        other.import_bundle(bundle)
        assert other.find_entity("Music Bank")["name"] == "뮤직뱅크"


class TestAddEntityAliasesSaturation:
    def test_a_full_entity_logs_one_warning_and_keeps_its_aliases(self, store, caplog):
        from kiro_crew.knowledge.store import MAX_ENTITY_ALIASES

        full = [f"alias {i}" for i in range(MAX_ENTITY_ALIASES)]
        eid = store.add_entity("Saturated", "concept", aliases=full)
        with caplog.at_level("WARNING", logger="kiro_crew.knowledge.store"):
            store.add_entity_aliases(eid, ["one more", "and another"])
        warnings = [r for r in caplog.records if "add_entity_aliases" in r.getMessage()]
        assert len(warnings) == 1
        assert "2 alias(es) discarded" in warnings[0].getMessage()
        stored = json.loads(store.find_entity("Saturated")["aliases"])
        assert stored == full

    def test_room_left_is_filled_then_the_rest_is_counted(self, store, caplog):
        from kiro_crew.knowledge.store import MAX_ENTITY_ALIASES

        eid = store.add_entity("Almost", "concept", aliases=[f"a{i}" for i in range(9)])
        assert MAX_ENTITY_ALIASES == 10
        with caplog.at_level("WARNING", logger="kiro_crew.knowledge.store"):
            store.add_entity_aliases(eid, ["ALMOST", "A1", "fits", "spills"])
        stored = json.loads(store.find_entity("Almost")["aliases"])
        assert stored[-1] == "fits" and len(stored) == 10
        assert any("1 alias(es) discarded" in r.getMessage() for r in caplog.records)

    def test_no_change_writes_nothing_and_missing_entity_raises(self, store):
        import pytest

        eid = store.add_entity("Same", "concept", aliases=["x"])
        before = store.find_entity("Same")["updated_at"]
        store.add_entity_aliases(eid, ["SAME", "X"])
        assert store.find_entity("Same")["updated_at"] == before
        with pytest.raises(ValueError, match="not found"):
            store.add_entity_aliases("no-such-id", ["y"])


def test_an_unbindable_alias_does_not_fail_an_import_or_an_add(store, store_factory):
    """A lone surrogate survives json.loads but not a SQLite bind: it indexes nothing."""
    store.add_entity("Seoul", "place", aliases=["서울"])
    bundle = store.export_all()
    bundle["entities"][0]["aliases"] = '["\\ud800", "Soul"]'
    other = store_factory("other.db")
    other.import_bundle(bundle)
    assert other.find_entity("soul")["name"] == "Seoul"
    eid = store.add_entity("Busan", "place", aliases=["\ud800", "Pusan"])
    assert store.find_entity("PUSAN")["id"] == eid


def test_an_unbindable_extracted_alias_is_dropped_before_any_lookup(store):
    """A lone surrogate from the model never reaches find_entity, so the chunk still stores."""
    from kiro_crew.knowledge.ingestion import IngestionPipeline, _coerce_aliases

    assert _coerce_aliases(["\ud800", "Pusan"], "부산") == ["Pusan"]
    assert _coerce_aliases(["\ud800"], "부산") is None
    pipeline = IngestionPipeline(
        store=store, extractor=None, chunker=None, reader=None, embedder=None
    )
    item = store.add_item("Busan doc", "부산 관련 내용", "document")
    extraction = {
        "entities": [{"name": "부산", "type": "place", "aliases": ["\ud800", "Pusan"]}],
        "relations": [],
    }
    pipeline._store_entities(extraction, item)
    assert store.find_entity("pusan")["name"] == "부산"

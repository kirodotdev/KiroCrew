"""``memory.relevance_rule``: one relevance matcher for findings, task facts and activity.

Pinned: ``off`` renders today's blocks byte for byte and records no stage field;
``shadow`` renders the same blocks and records which ids the shared rule would
add or drop; ``enforce`` stops a short word (``a``, ``hi``) from admitting a
finding, a fact or an activity section, keeps a CJK pair match, on the default
and the private store alike; a fact with no comparable vector is judged on its
lexical evidence alone; an unknown value reads as ``off``; and the receipt merges
the blocks' records without one block erasing another's.
"""

from __future__ import annotations

import pytest
from member_memory_helpers import declare_v2_store

from kiro_crew import injection_receipts, memory_relevance_rule, memory_stores
from kiro_crew.config.memory_sections import MemoryConfig, relevance_rule_mode_from
from kiro_crew.config.section_builders import _build_memory_config
from kiro_crew.memory import MemoryStore
from kiro_crew.recall_pool import lessons_args
from kiro_crew.vector_memory import VectorMemoryStore, open_member_database

STAGE = injection_receipts.RELEVANCE_RULE


@pytest.fixture(params=["default", "private"])
def store(request, tmp_path, monkeypatch):
    if request.param == "default":
        opened = VectorMemoryStore(db_path=tmp_path / "memory.db")
        opened.init()
    else:
        monkeypatch.setattr(memory_stores, "memory_stores_root", lambda: tmp_path / "memory_stores")
        directory = declare_v2_store(tmp_path, "member-alice")
        opened = open_member_database(
            directory / "memory.db", member_id="alice", store_id="member-alice"
        )
    try:
        yield opened
    finally:
        opened.close()


def _findings(store, query: str, mode: str | None) -> tuple[str, dict]:
    kwargs = {} if mode is None else {"relevance_rule": mode}
    with injection_receipts.collect() as receipts:
        out = store.get_lessons_context(
            query,
            background=True,
            hard_cap=99_000,
            directive_budget=2_000,
            experience_budget=2_000,
            **kwargs,
        )
    return out, receipts


def _seed_findings(store) -> None:
    assert store.write_lesson("a quokka moved to the east wing", applies="on_topic")
    assert store.write_lesson("部署脚本需要先检查磁盘空间", applies="on_topic")


class TestFindings:
    def test_off_is_todays_block_and_records_no_stage(self, store):
        _seed_findings(store)
        default, default_receipts = _findings(store, "a", None)
        off, off_receipts = _findings(store, "a", "off")
        assert off == default
        assert "quokka" in off, "today's gate admits the tier on the word 'a'"
        assert STAGE not in off_receipts and off_receipts == default_receipts

    def test_shadow_keeps_the_block_and_records_the_old_only_rows(self, store):
        _seed_findings(store)
        off, off_receipts = _findings(store, "a", "off")
        shadow, receipts = _findings(store, "a", "shadow")
        assert shadow == off
        assert receipts[injection_receipts.FINDINGS] == off_receipts[injection_receipts.FINDINGS]
        stage = receipts[STAGE]
        assert stage["mode"] == "shadow"
        assert stage["policy"] == memory_relevance_rule.POLICY_VERSION
        assert stage["would_add"] == []
        assert set(stage["would_drop"]) == set(off_receipts[injection_receipts.FINDINGS]["ids"])
        assert stage["would_drop"] and all(k.startswith("lesson.") for k in stage["would_drop"])

    @pytest.mark.parametrize("query", ["a", "hi", "hi a"])
    def test_enforce_withholds_on_a_short_word(self, store, query):
        _seed_findings(store)
        out, receipts = _findings(store, query, "enforce")
        assert "quokka" not in out
        assert "[Withheld all 2 past findings" in out
        assert receipts[STAGE]["mode"] == "enforce"

    def test_enforce_keeps_a_long_word_and_a_cjk_pair(self, store):
        _seed_findings(store)
        out, _ = _findings(store, "where is the quokka", "enforce")
        assert "quokka" in out
        out, _ = _findings(store, "检查一下", "enforce")
        assert "部署脚本" in out

    @pytest.mark.parametrize(
        "lesson,query", [("山﨑さんの見積もり", "山﨑"), ("설정 변경 완료", "설정 해줘")]
    )
    def test_enforce_keeps_compatibility_ideograph_and_hangul_pairs(self, store, lesson, query):
        assert store.write_lesson(lesson, applies="on_topic")
        off, _ = _findings(store, query, "off")
        out, _ = _findings(store, query, "enforce")
        assert lesson in off and lesson in out

    def test_shadow_records_nothing_when_both_rules_agree(self, store):
        _seed_findings(store)
        _, receipts = _findings(store, "quokka", "shadow")
        assert receipts[STAGE]["would_drop"] == []

    def test_enforce_never_withholds_what_the_old_test_admits_on_a_long_word(self, store):
        assert store.write_lesson("api_timeout retries need bounded polling", applies="on_topic")
        for query in ("api_timeout", "timeout"):
            off, _ = _findings(store, query, "off")
            out, _ = _findings(store, query, "enforce")
            assert ("bounded polling" in off) == ("bounded polling" in out)


def _facts(store, query: str, mode: str | None) -> tuple[str, dict]:
    kwargs = {} if mode is None else {"relevance_rule": mode}
    with injection_receipts.collect() as receipts:
        out = store.get_semantic_context(query, 4_000, facts_only=True, **kwargs)
    return out, receipts


def _seed_facts(store) -> None:
    assert (
        store.set_semantic("project.east.wing", "a quokka lives here", 0.9, "user_explicit") is None
    )
    assert store.set_semantic("project.deploy.disk", "部署前检查磁盘", 0.9, "user_explicit") is None


class TestFacts:
    def test_off_is_todays_block_and_records_no_stage(self, store):
        _seed_facts(store)
        default, default_receipts = _facts(store, "hi a quokka", None)
        off, off_receipts = _facts(store, "hi a quokka", "off")
        assert off == default
        assert STAGE not in off_receipts and STAGE not in default_receipts

    def test_enforce_drops_a_short_word_match_and_keeps_real_ones(self, store):
        _seed_facts(store)
        out, receipts = _facts(store, "a", "enforce")
        assert "quokka" not in out
        assert receipts[STAGE]["mode"] == "enforce"
        assert "quokka" in _facts(store, "quokka east", "enforce")[0]
        cjk_off = _facts(store, "检查磁盘", "off")[0]
        cjk_enforce = _facts(store, "检查磁盘", "enforce")[0]
        if store.algorithm_version == "v2":
            assert "部署前检查磁盘" in cjk_enforce
        else:
            # The default store's fact ranker does not split CJK text, so the
            # gate, which only filters its candidates, keeps what it had.
            assert cjk_enforce == cjk_off

    def test_shadow_keeps_the_block_and_records_what_would_change(self, tmp_path):
        # The default store admits any shared word, so "a" reaches the block today.
        store = VectorMemoryStore(db_path=tmp_path / "memory.db")
        store.init()
        try:
            _seed_facts(store)
            off, _ = _facts(store, "a", "off")
            shadow, receipts = _facts(store, "a", "shadow")
            assert shadow == off and "quokka" in off
            assert receipts[STAGE]["would_drop"] == ["project.east.wing"]
            assert receipts[STAGE]["would_add"] == []
        finally:
            store.close()


class TestFactEvidence:
    """The private store's evidence gate, as task facts on both stores read it."""

    def test_no_vector_means_lexical_evidence_only(self):
        terms = memory_relevance_rule.fact_terms("quokka east")
        assert memory_relevance_rule.fact_admitted(terms, "project east wing quokka", None)
        assert not memory_relevance_rule.fact_admitted(terms, "something else", None)

    def test_a_cosine_above_the_floor_admits(self):
        terms = memory_relevance_rule.fact_terms("quokka east")
        assert memory_relevance_rule.fact_admitted(terms, "something else", 0.7)
        assert not memory_relevance_rule.fact_admitted(terms, "something else", 0.5)

    def test_short_words_are_never_evidence(self):
        assert memory_relevance_rule.fact_terms("hi a 7") == frozenset()
        assert memory_relevance_rule.fact_terms("检查") == {"检查"}

    def test_hangul_and_full_width_requests_keep_their_terms(self):
        assert "배포" in memory_relevance_rule.fact_terms("배포스크립트")
        assert "redis" in memory_relevance_rule.fact_terms("Ｒｅｄｉｓ cache")
        terms = memory_relevance_rule.fact_terms("배포스크립트")
        assert memory_relevance_rule.fact_admitted(
            terms, "project deploy script 배포스크립트", None
        )


def _activity(store: MemoryStore, query: str, mode: str | None) -> tuple[str, dict]:
    kwargs = {} if mode is None else {"relevance_rule": mode}
    with injection_receipts.collect() as receipts:
        out = store.get_activity_context(query=query, **kwargs)
    return out, receipts


@pytest.fixture
def activity_store(tmp_path):
    store = MemoryStore(workspace=tmp_path)
    store.init()
    store.write_projects(
        "# Active Projects\n\n## Quokka\n- east wing move\n\n## 部署\n- 检查磁盘空间\n"
    )
    store.append_history("a quokka moved")
    store.append_history("rebuilt the 部署脚本")
    return store


class TestActivity:
    def test_off_is_todays_block_and_records_no_stage(self, activity_store):
        default, default_receipts = _activity(activity_store, "hi", None)
        off, off_receipts = _activity(activity_store, "hi", "off")
        assert off == default and "east wing move" in off
        assert STAGE not in off_receipts and STAGE not in default_receipts

    def test_shadow_keeps_the_block_and_records_section_ids(self, activity_store):
        off, _ = _activity(activity_store, "hi", "off")
        shadow, receipts = _activity(activity_store, "hi", "shadow")
        assert shadow == off
        dropped = receipts[STAGE]["would_drop"]
        assert dropped and all(i.split(":")[0] in {"projects", "history"} for i in dropped)
        # Ids only, never the section text.
        assert not any("quokka" in i for i in dropped)

    def test_enforce_drops_every_section_for_a_greeting(self, activity_store):
        out, _ = _activity(activity_store, "hi", "enforce")
        assert "## Active Projects" not in out and "## Recent History" not in out

    def test_enforce_keeps_the_matching_section_and_its_heading(self, activity_store):
        out, _ = _activity(activity_store, "quokka", "enforce")
        assert "## Quokka" in out and "east wing move" in out
        assert "# Active Projects" in out
        assert "检查磁盘空间" not in out
        assert "a quokka moved" in out and "部署脚本" not in out

    def test_enforce_keeps_a_cjk_pair_match(self, activity_store):
        out, _ = _activity(activity_store, "部署", "enforce")
        assert "检查磁盘空间" in out and "部署脚本" in out
        assert "east wing move" not in out

    def test_a_two_syllable_hangul_word_matches_as_in_findings(self, activity_store):
        activity_store.append_history("설정 변경 완료")
        out, _ = _activity(activity_store, "설정 변경 해줘", "enforce")
        assert "설정 변경 완료" in out
        assert "east wing move" not in out

    def test_a_compatibility_ideograph_pair_matches(self, activity_store):
        activity_store.append_history("## 山﨑さんの案件\n- 見積もり送付")
        out, _ = _activity(activity_store, "山﨑", "enforce")
        assert "山﨑さんの案件" in out
        assert "east wing move" not in out

    def test_no_request_gates_nothing(self, activity_store):
        assert (
            _activity(activity_store, "", "enforce")[0] == _activity(activity_store, "", "off")[0]
        )


class TestGateDocument:
    @pytest.mark.parametrize(
        "text",
        [
            "",
            "plain\n",
            "# A\nx\n## B\ny\n",
            "pre\n### C\tz\n####### seven\n#nospace\n  # indented\n",
            "# A\r\n## B\r\nbody\n",
            "####\n# \n#\tT\n",
        ],
    )
    def test_heading_split_matches_the_regex_it_replaced(self, text):
        import re

        heading = re.compile(r"^(#{1,6})[ \t]", re.MULTILINE)
        starts = [m.start() for m in heading.finditer(text)]
        if not starts or starts[0] != 0:
            starts.insert(0, 0)
        expected = []
        for i, start in enumerate(starts):
            piece = text[start : starts[i + 1] if i + 1 < len(starts) else len(text)]
            match = heading.match(piece)
            expected.append((len(match.group(1)) if match else 0, piece))
        assert memory_relevance_rule._sections(text) == expected

    def test_one_classifier_for_pairs_compatibility_ideographs_and_hangul(self):
        # The shared text_scoring floor; U+FA11 is a CJK compatibility ideograph.
        from kiro_crew.vector_memory_runtime.text_scoring import _is_selective_keyword

        for token in ("山﨑", "部署", "설정"):
            assert _is_selective_keyword(token)
        for token in ("山", "hi", "a1"):
            assert not _is_selective_keyword(token)
        assert "山﨑" in memory_relevance_rule.request_tokens("山﨑")
        assert "설정" in memory_relevance_rule.request_tokens("설정 해줘")

    def test_sections_join_back_and_a_parent_follows_its_children(self):
        text = "# Day\n\n#### 10:00\nquokka\n\n#### 11:00\nother\n"
        tokens = memory_relevance_rule.request_tokens("quokka")
        kept, add, drop = memory_relevance_rule.gate_document("history", text, tokens, 10_000)
        assert kept == "# Day\n\n#### 10:00\nquokka\n\n"
        assert add == [] and len(drop) == 1
        everything = memory_relevance_rule.request_tokens("quokka other")
        assert memory_relevance_rule.gate_document("history", text, everything, 10_000)[0] == text

    def test_a_dropped_section_lets_a_later_one_into_the_cap(self):
        text = "## A\n" + "x" * 50 + "\n## B\nquokka\n"
        tokens = memory_relevance_rule.request_tokens("quokka")
        _, add, drop = memory_relevance_rule.gate_document("projects", text, tokens, 20)
        assert len(add) == 1 and len(drop) == 1


class TestReceipt:
    def test_parts_merge_and_a_rerender_replaces_only_its_part(self):
        with injection_receipts.collect() as sink:
            for part, ids in (("facts", ["f1"]), ("findings", ["l1", "l2"]), ("findings", ["l1"])):
                injection_receipts.record_stage(
                    STAGE, mode="shadow", policy=1, would_drop=ids, part=part
                )
        line = injection_receipts.build_line(
            {**sink, injection_receipts.LESSONS: {"ids": [], "omitted": 0}},
            session_key="dashboard:x",
            turn_index=0,
            user_msg_id="",
            model="",
            agent="",
            phase="",
            store="",
        )
        assert line is not None
        assert line[STAGE] == {
            "mode": "shadow",
            "policy": 1,
            "would_add": [],
            "would_drop": ["f1", "l1"],
        }

    def test_a_stage_record_alone_still_writes_a_line(self):
        # Lessons switched off: the activity block's shadow ids must not be lost.
        with injection_receipts.collect() as sink:
            injection_receipts.record_stage(
                STAGE, mode="shadow", policy=1, would_drop=["projects:abc"], part="activity"
            )
        line = injection_receipts.build_line(
            sink,
            session_key="dashboard:x",
            turn_index=0,
            user_msg_id="",
            model="",
            agent="",
            phase="",
            store="",
        )
        assert line is not None and line[STAGE]["would_drop"] == ["projects:abc"]
        assert (
            injection_receipts.build_line(
                {},
                session_key="dashboard:x",
                turn_index=0,
                user_msg_id="",
                model="",
                agent="",
                phase="",
                store="",
            )
            is None
        )

    def test_replay_merges_into_parts_already_recorded(self):
        with injection_receipts.collect() as child:
            injection_receipts.record_stage(
                STAGE, mode="shadow", policy=1, would_drop=["l1"], part="findings"
            )
        with injection_receipts.collect() as sink:
            injection_receipts.record_stage(
                STAGE, mode="shadow", policy=1, would_drop=["f1"], part="facts"
            )
            injection_receipts.replay(child)
        assert sink[STAGE]["would_drop"] == ["f1", "l1"]


class TestShadowIsOffByteForByte:
    """Shadow may only add receipt fields: every block it renders is off's, exactly."""

    QUERIES = [
        "",
        "hi",
        "a",
        "hi a quokka",
        "quokka east wing",
        "检查磁盘",
        "api_timeout",
        "山﨑",
        "설정 해줘",
    ]

    @pytest.mark.parametrize("query", QUERIES)
    def test_findings_and_rules(self, store, query):
        _seed_findings(store)
        assert store.write_lesson("always keep the quokka fed", applies="always")
        assert _findings(store, query, "shadow")[0] == _findings(store, query, "off")[0]

    @pytest.mark.parametrize("query", QUERIES)
    def test_task_facts(self, store, query):
        _seed_facts(store)
        assert _facts(store, query, "shadow")[0] == _facts(store, query, "off")[0]

    @pytest.mark.parametrize("query", QUERIES)
    def test_activity(self, activity_store, query):
        assert (
            _activity(activity_store, query, "shadow")[0]
            == _activity(activity_store, query, "off")[0]
        )

    @pytest.mark.parametrize("query", QUERIES)
    def test_the_whole_session_start_read(self, activity_store, query, monkeypatch):
        from kiro_crew import context as ctx
        from kiro_crew.context_assembly import store_admission

        caps = ctx._resolve_caps(None)
        reads = {}
        for mode in ("off", "shadow"):
            monkeypatch.setattr(memory_relevance_rule, "configured_mode", lambda m=mode: m)
            reads[mode] = store_admission.read_session_memory(
                activity_store, caps, query, inject_activity=True
            )
        off, shadow = reads["off"], reads["shadow"]
        assert (shadow.memory_ctx, shadow.activity, shadow.activity_ctx) == (
            off.memory_ctx,
            off.activity,
            off.activity_ctx,
        )
        assert shadow.omissions == off.omissions
        # Only the receipt differs, and only by the stage field.
        assert STAGE not in off.receipts
        assert {k: v for k, v in shadow.receipts.items() if k != STAGE} == off.receipts


class TestTheSessionStartRead:
    def test_the_read_returns_its_stage_record_for_the_build_to_replay(
        self, activity_store, monkeypatch
    ):
        # A prefetch runs this read on another thread, outside the build's
        # collection, so the record must travel with the result.
        from kiro_crew import context as ctx
        from kiro_crew.context_assembly import store_admission

        monkeypatch.setattr(memory_relevance_rule, "configured_mode", lambda: "shadow")
        reads = store_admission.read_session_memory(
            activity_store, ctx._resolve_caps(None), "hi", inject_activity=True
        )
        assert reads.receipts[STAGE]["mode"] == "shadow"
        assert reads.receipts[STAGE]["would_drop"]
        with injection_receipts.collect() as sink:
            injection_receipts.replay(reads.receipts)
        assert sink[STAGE]["would_drop"] == reads.receipts[STAGE]["would_drop"]

    def test_off_reads_record_nothing(self, activity_store, monkeypatch):
        from kiro_crew import context as ctx
        from kiro_crew.context_assembly import store_admission

        monkeypatch.setattr(memory_relevance_rule, "configured_mode", lambda: "off")
        reads = store_admission.read_session_memory(
            activity_store, ctx._resolve_caps(None), "hi", inject_activity=True
        )
        assert STAGE not in reads.receipts

    def test_a_mode_flip_invalidates_a_prefetched_read(self, activity_store, monkeypatch):
        from kiro_crew import context as ctx
        from kiro_crew.context_assembly import store_admission

        caps = ctx._resolve_caps(None)
        monkeypatch.setattr(memory_relevance_rule, "configured_mode", lambda: "shadow")
        before = store_admission._memory_read_inputs(activity_store, caps, "hi", True)
        monkeypatch.setattr(memory_relevance_rule, "configured_mode", lambda: "enforce")
        assert store_admission._memory_read_inputs(activity_store, caps, "hi", True) != before


class TestTheFlag:
    def test_default_is_off_and_unknown_reads_as_off(self):
        assert MemoryConfig().relevance_rule == "off"
        assert relevance_rule_mode_from("ENFORCE") == "off"
        assert relevance_rule_mode_from(None) == "off"
        assert _build_memory_config({"relevance_rule": "bogus"}).relevance_rule == "off"
        assert _build_memory_config({"relevance_rule": "shadow"}).relevance_rule == "shadow"

    def test_the_recall_child_gets_the_mode(self, tmp_path):
        store = VectorMemoryStore(db_path=tmp_path / "memory.db")
        store.init()
        try:
            args = lessons_args(
                store, "q", 0, None, None, True, 0, 0, 0, "off", relevance_rule="enforce"
            )
            assert args["relevance_rule"] == "enforce"
        finally:
            store.close()

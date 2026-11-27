"""``memory.relevance_rule``: one relevance matcher for findings, task facts and activity.

Pinned: ``enforce`` (the default) stops a short word (``a``, ``hi``) from
admitting a finding, a fact or an activity section and keeps a CJK pair match,
on the default and the private store alike; ``off`` renders the old blocks; a
fact with no comparable vector is judged on its lexical evidence alone; an
unknown value, a stored ``shadow`` included, reads as ``enforce``; and no mode
writes a receipt field.
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

STAGE = "relevance_rule"


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
    def test_off_admits_on_a_short_word_and_records_no_stage(self, store):
        _seed_findings(store)
        off, off_receipts = _findings(store, "a", "off")
        assert "quokka" in off, "the old gate admits the tier on the word 'a'"
        assert STAGE not in off_receipts

    @pytest.mark.parametrize("query", ["a", "hi", "hi a"])
    def test_enforce_withholds_on_a_short_word(self, store, query):
        _seed_findings(store)
        out, receipts = _findings(store, query, "enforce")
        assert "quokka" not in out
        assert "[Withheld all 2 past findings" in out
        assert STAGE not in receipts

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
    def test_off_records_no_stage(self, store):
        _seed_facts(store)
        _, off_receipts = _facts(store, "hi a quokka", "off")
        assert STAGE not in off_receipts

    def test_enforce_drops_a_short_word_match_and_keeps_real_ones(self, store):
        _seed_facts(store)
        out, receipts = _facts(store, "a", "enforce")
        assert "quokka" not in out
        assert STAGE not in receipts
        assert "quokka" in _facts(store, "quokka east", "enforce")[0]
        cjk_off = _facts(store, "检查磁盘", "off")[0]
        cjk_enforce = _facts(store, "检查磁盘", "enforce")[0]
        if store.algorithm_version == "v2":
            assert "部署前检查磁盘" in cjk_enforce
        else:
            # The default store's fact ranker does not split CJK text, so the
            # gate, which only filters its candidates, keeps what it had.
            assert cjk_enforce == cjk_off

    def test_off_lets_a_short_word_in_on_the_default_store(self, tmp_path):
        # The default store's old gate admits any shared word, so "a" reaches the block.
        store = VectorMemoryStore(db_path=tmp_path / "memory.db")
        store.init()
        try:
            _seed_facts(store)
            assert "quokka" in _facts(store, "a", "off")[0]
            assert "quokka" not in _facts(store, "a", "enforce")[0]
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
    def test_off_keeps_every_section_and_records_no_stage(self, activity_store):
        off, off_receipts = _activity(activity_store, "hi", "off")
        assert "east wing move" in off
        assert STAGE not in off_receipts

    def test_enforce_drops_every_section_for_a_greeting(self, activity_store):
        out, receipts = _activity(activity_store, "hi", "enforce")
        assert "## Active Projects" not in out and "## Recent History" not in out
        assert STAGE not in receipts

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
        kept = memory_relevance_rule.gate_document(text, tokens)
        assert kept == "# Day\n\n#### 10:00\nquokka\n\n"
        everything = memory_relevance_rule.request_tokens("quokka other")
        assert memory_relevance_rule.gate_document(text, everything) == text


class TestAStoredShadowReadsAsEnforce:
    """A ``shadow`` left in a config renders the default ``enforce`` blocks."""

    QUERIES = ["", "hi", "a", "hi a quokka", "quokka east wing", "检查磁盘", "山﨑", "설정 해줘"]

    @pytest.mark.parametrize("query", QUERIES)
    def test_findings_and_rules(self, store, query):
        _seed_findings(store)
        assert store.write_lesson("always keep the quokka fed", applies="always")
        assert _findings(store, query, "shadow")[0] == _findings(store, query, "enforce")[0]

    @pytest.mark.parametrize("query", QUERIES)
    def test_task_facts(self, store, query):
        _seed_facts(store)
        assert _facts(store, query, "shadow")[0] == _facts(store, query, "enforce")[0]

    @pytest.mark.parametrize("query", QUERIES)
    def test_activity(self, activity_store, query):
        assert (
            _activity(activity_store, query, "shadow")[0]
            == _activity(activity_store, query, "enforce")[0]
        )


class TestTheSessionStartRead:
    @pytest.mark.parametrize("mode", ["off", "enforce"])
    def test_no_mode_records_a_stage_field(self, activity_store, monkeypatch, mode):
        from kiro_crew import context as ctx
        from kiro_crew.context_assembly import store_admission

        monkeypatch.setattr(memory_relevance_rule, "configured_mode", lambda: mode)
        reads = store_admission.read_session_memory(
            activity_store, ctx._resolve_caps(None), "hi", inject_activity=True
        )
        assert STAGE not in reads.receipts

    def test_enforce_gates_the_whole_session_start_read(self, activity_store, monkeypatch):
        from kiro_crew import context as ctx
        from kiro_crew.context_assembly import store_admission

        caps = ctx._resolve_caps(None)
        reads = {}
        for mode in ("off", "enforce"):
            monkeypatch.setattr(memory_relevance_rule, "configured_mode", lambda m=mode: m)
            reads[mode] = store_admission.read_session_memory(
                activity_store, caps, "hi", inject_activity=True
            )
        assert "east wing move" in reads["off"].activity_ctx
        assert "east wing move" not in reads["enforce"].activity_ctx

    def test_a_mode_flip_invalidates_a_prefetched_read(self, activity_store, monkeypatch):
        from kiro_crew import context as ctx
        from kiro_crew.context_assembly import store_admission

        caps = ctx._resolve_caps(None)
        monkeypatch.setattr(memory_relevance_rule, "configured_mode", lambda: "off")
        before = store_admission._memory_read_inputs(activity_store, caps, "hi", True)
        monkeypatch.setattr(memory_relevance_rule, "configured_mode", lambda: "enforce")
        assert store_admission._memory_read_inputs(activity_store, caps, "hi", True) != before


class TestTheFlag:
    def test_default_is_enforce_and_unknown_reads_as_enforce(self):
        assert MemoryConfig().relevance_rule == "enforce"
        assert relevance_rule_mode_from("ENFORCE") == "enforce"
        assert relevance_rule_mode_from(None) == "enforce"
        assert relevance_rule_mode_from("off") == "off"
        assert _build_memory_config({"relevance_rule": "bogus"}).relevance_rule == "enforce"
        assert _build_memory_config({"relevance_rule": "off"}).relevance_rule == "off"

    def test_a_stored_shadow_reads_as_the_default_enforce(self):
        assert relevance_rule_mode_from("shadow") == "enforce"
        assert _build_memory_config({"relevance_rule": "shadow"}).relevance_rule == "enforce"

    def test_the_receipts_carry_no_relevance_stage(self):
        assert STAGE not in injection_receipts.STAGE_KINDS
        with pytest.raises(ValueError):
            injection_receipts.record_stage(STAGE, mode="enforce", policy=1)

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

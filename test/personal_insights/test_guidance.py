from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.learn import Lesson, LessonStore
from kiro_crew.personal_insights import insights_capture as capture
from kiro_crew.personal_insights import insights_guidance as guidance
from kiro_crew.personal_insights.insights_ontology import (
    PREDICATE_CODES,
    BehaviorKey,
    OntologyError,
)

SUBJECT = "session_owner"


def _bkey(predicate: str = "repeats_context_setup", scope: str = "global") -> BehaviorKey:
    return BehaviorKey(
        subject_code=SUBJECT, predicate_code=predicate, polarity="positive", scope=scope
    )


# ── ontology / behavior key ──


def test_behavior_key_canonical_is_subject_predicate_polarity_scope() -> None:
    key = _bkey()
    assert key.canonical() == "session_owner|repeats_context_setup|positive|global"


def test_behavior_key_rejects_unknown_predicate() -> None:
    with pytest.raises(OntologyError):
        BehaviorKey(SUBJECT, "invented_code", "positive", "global")


def test_behavior_key_rejects_unknown_polarity() -> None:
    with pytest.raises(OntologyError):
        BehaviorKey(SUBJECT, "verifies_changes", "sideways", "global")


def test_predicate_codes_are_exactly_spec_v6() -> None:
    assert PREDICATE_CODES == frozenset(
        {
            "works_in_area",
            "repeats_context_setup",
            "requests_iterative_refinement",
            "verifies_changes",
            "verification_omission_explicit",
            "retries_operation",
            "uses_parallel_delegation",
            "encounters_tool_errors",
            "interrupts_long_steps",
            "reuses_effective_prompt",
            "needs_capability_discovery",
            "preserves_reversible_changes",
            "notable_singular_event",
        }
    )


# ── phase one grammar and outcomes ──


def test_exact_tag_line_is_tagged_behavior_match() -> None:
    match = guidance.classify_phase_one(
        "lesson:1", "text\nkiro-insights-behavior: verifies_changes\n"
    )
    assert match.match_state == guidance.MATCH_TAGGED
    assert match.predicate_code == "verifies_changes"


def test_tag_without_single_space_is_ambiguous() -> None:
    for bad in (
        "kiro-insights-behavior:verifies_changes",
        "kiro-insights-behavior:  verifies_changes",
        "kiro-insights-behavior: verifies_changes trailing",
    ):
        match = guidance.classify_phase_one("lesson:1", bad)
        assert match.match_state == guidance.MATCH_AMBIGUOUS, bad
        assert match.predicate_code is None


def test_unknown_tag_code_is_ambiguous() -> None:
    match = guidance.classify_phase_one("lesson:1", "kiro-insights-behavior: not_a_code")
    assert match.match_state == guidance.MATCH_AMBIGUOUS


def test_multiple_distinct_tags_are_ambiguous() -> None:
    text = "kiro-insights-behavior: verifies_changes\nkiro-insights-behavior: retries_operation\n"
    match = guidance.classify_phase_one("lesson:1", text)
    assert match.match_state == guidance.MATCH_AMBIGUOUS


def test_repeated_same_tag_is_one_distinct() -> None:
    text = "kiro-insights-behavior: verifies_changes\nkiro-insights-behavior: verifies_changes\n"
    match = guidance.classify_phase_one("lesson:1", text)
    assert match.match_state == guidance.MATCH_TAGGED
    assert match.predicate_code == "verifies_changes"


def test_unambiguous_legacy_lexicon_match() -> None:
    match = guidance.classify_phase_one("steering:1", "always rerun the same command")
    assert match.match_state == guidance.MATCH_LEGACY
    assert match.predicate_code == "retries_operation"


def test_ambiguous_legacy_overlap() -> None:
    text = "rerun the same command and also handle the tool error path"
    match = guidance.classify_phase_one("steering:1", text)
    assert match.match_state == guidance.MATCH_AMBIGUOUS


def test_no_detected_overlap() -> None:
    match = guidance.classify_phase_one("steering:1", "ordinary prose without any behavior phrase")
    assert match.match_state == guidance.MATCH_NONE


@pytest.mark.parametrize("state", [guidance.MATCH_TAGGED, guidance.MATCH_LEGACY])
def test_tagged_and_legacy_block_enumeration(state: str) -> None:
    match = guidance.PhaseOneMatch(state, "verifies_changes", "0" * 64)
    assert guidance.phase_one_blocks_enumeration(match) is True


@pytest.mark.parametrize("state", [guidance.MATCH_AMBIGUOUS, guidance.MATCH_NONE])
def test_ambiguous_and_none_do_not_block(state: str) -> None:
    match = guidance.PhaseOneMatch(state, None, "0" * 64)
    assert guidance.phase_one_blocks_enumeration(match) is False


# ── phase two exact artifact match ──


def test_phase_two_exact_artifact_match_text() -> None:
    bodies = ["use this exact prompt\n"]
    assert guidance.phase_two_exact_artifact_match("use this exact prompt", bodies, "text") is True
    assert guidance.phase_two_exact_artifact_match("different prompt", bodies, "text") is False


def test_phase_two_exact_artifact_match_steering() -> None:
    bodies = ["compiled steering body\n"]
    assert (
        guidance.phase_two_exact_artifact_match(
            "compiled steering body\r\n", bodies, "steering_patch"
        )
        is True
    )


# ── changed-digest replay of both phases ──


def test_changed_digest_forces_rematch_of_both_phases() -> None:
    docs = [
        guidance.GuidanceDocument("lesson:1", "kiro-insights-behavior: verifies_changes", "lesson")
    ]
    digests = guidance.guidance_source_digests(docs)
    assert guidance.replay_requires_rematch(digests, docs) is False
    changed = [
        guidance.GuidanceDocument("lesson:1", "kiro-insights-behavior: retries_operation", "lesson")
    ]
    assert guidance.replay_requires_rematch(digests, changed) is True


# ── text-free match record ──


def test_match_record_has_no_raw_text_and_no_source_identity() -> None:
    secret = "SENSITIVE RAW GUIDANCE TEXT"
    key = _bkey("verifies_changes")
    record = guidance.match_record(
        snapshot_digest="a" * 64,
        source_digests=("b" * 64,),
        behavior_key=key,
        phase=guidance.PHASE_BEHAVIOR,
        match_state=guidance.MATCH_TAGGED,
        captured_at="1970-01-01T00:00:00Z",
    )
    serialized = repr(record)
    assert secret not in serialized
    assert "source_identity" not in serialized
    assert "source_id" not in serialized
    assert record.schema_version == "kiro.personal-insights.active-guidance-match/1.0"
    assert record.source_digests == ("b" * 64,)
    assert record.artifact_digest is None


# ── memory-only capture over injected temp roots ──


def test_capture_reads_lessons_and_steering_over_temp_roots(tmp_path: Path) -> None:
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    store = LessonStore(base_dir=lessons_dir)
    store.save(
        Lesson(
            ts="1970-01-01T00:00:00Z",
            rule="kiro-insights-behavior: verifies_changes",
            category="preference",
        )
    )
    steering_root = tmp_path / "steering"
    steering_root.mkdir()
    (steering_root / "rule.md").write_text(
        "---\nalways: true\n---\nalways rerun the same command\n", encoding="utf-8"
    )
    home = tmp_path / "home"
    home.mkdir()
    snapshot = capture.capture_memory_only(
        lessons_dir, [str(steering_root)], home=home, workspace_scope="repo:none"
    )
    texts = [doc.text for doc in snapshot.documents]
    assert any("verifies_changes" in t for t in texts)
    assert any("rerun the same command" in t for t in texts)
    assert len(snapshot.snapshot_digest) == 64


def test_capture_applies_lesson_scope(tmp_path: Path) -> None:
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    store = LessonStore(base_dir=lessons_dir)
    store.save(
        Lesson(
            ts="1970-01-01T00:00:00Z",
            rule="global applicable rule",
            category="preference",
        )
    )
    store.save(
        Lesson(
            ts="1970-01-01T00:00:01Z",
            rule="other-repo scoped rule",
            category="preference",
            repo_scope="src/other",
        )
    )
    home = tmp_path / "home"
    home.mkdir()
    snapshot = capture.capture_memory_only(
        lessons_dir, [], home=home, workspace_scope="repo:src/mine"
    )
    texts = [doc.text for doc in snapshot.documents]
    assert any("global applicable rule" in t for t in texts)
    assert not any("other-repo scoped rule" in t for t in texts)


def test_capture_snapshot_digest_changes_with_content(tmp_path: Path) -> None:
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    store = LessonStore(base_dir=lessons_dir)
    store.save(Lesson(ts="1970-01-01T00:00:00Z", rule="rule one", category="preference"))
    home = tmp_path / "home"
    home.mkdir()
    first = capture.capture_memory_only(lessons_dir, [], home=home, workspace_scope="global")
    store.save(Lesson(ts="1970-01-01T00:00:01Z", rule="rule two", category="preference"))
    second = capture.capture_memory_only(lessons_dir, [], home=home, workspace_scope="global")
    assert first.snapshot_digest != second.snapshot_digest


# ── Spec v6 defect-closure behavioral tests ──


def test_exact_tag_cannot_strip_outer_whitespace() -> None:
    for bad in (
        "   kiro-insights-behavior: verifies_changes",
        "\tkiro-insights-behavior: verifies_changes",
        "kiro-insights-behavior: verifies_changes   ",
    ):
        match = guidance.classify_phase_one("lesson:1", bad)
        assert match.match_state == guidance.MATCH_AMBIGUOUS, bad


def test_blocking_is_behavior_and_scope_specific() -> None:
    tagged = guidance.classify_phase_one("lesson:1", "kiro-insights-behavior: verifies_changes")
    key_match = _bkey("verifies_changes", scope="global")
    key_other = _bkey("retries_operation", scope="global")
    assert guidance.phase_one_blocks_behavior(tagged, key_match) is True
    assert guidance.phase_one_blocks_behavior(tagged, key_other) is False
    key_other_scope = _bkey("verifies_changes", scope="ws-abc123defg45")
    assert guidance.phase_one_blocks_behavior(tagged, key_other_scope) is True


def test_reinforcement_is_text_free() -> None:
    tagged = guidance.classify_phase_one(
        "lesson:1", "SECRET\nkiro-insights-behavior: verifies_changes"
    )
    reinforcement = guidance.reinforcement_from_match(tagged, _bkey("verifies_changes"))
    text = repr(reinforcement)
    assert "SECRET" not in text
    assert reinforcement["predicate_code"] == "verifies_changes"
    assert reinforcement["reinforced"] is True


def test_ambiguous_abstains_and_does_not_block() -> None:
    ambiguous = guidance.PhaseOneMatch(guidance.MATCH_AMBIGUOUS, None, "0" * 64)
    assert guidance.phase_one_blocks_behavior(ambiguous, _bkey("verifies_changes")) is False


def test_phase_two_type_specific_prompt_vs_structured_lesson() -> None:
    assert guidance.phase_two_matches("prompt", "use this prompt", ["use this prompt\n"]) is True
    lesson_a = {
        "rule": "r",
        "category": "preference",
        "negative": None,
        "repo_scope": None,
        "applies": None,
    }
    lesson_b = dict(lesson_a)
    assert guidance.phase_two_matches("lesson_proposal", lesson_a, [lesson_b]) is True
    lesson_c = dict(lesson_a, rule="different")
    assert guidance.phase_two_matches("lesson_proposal", lesson_a, [lesson_c]) is False


def test_phase_two_type_specific_argv_and_steering() -> None:
    assert (
        guidance.phase_two_matches(
            "static_argv", ["kirocrew", "learn", "list"], [["kirocrew", "learn", "list"]]
        )
        is True
    )
    assert (
        guidance.phase_two_matches(
            "static_argv", ["kirocrew", "learn"], [["kirocrew", "learn", "list"]]
        )
        is False
    )
    assert (
        guidance.phase_two_matches("steering_patch", "compiled body\r\n", ["compiled body\n"])
        is True
    )


def test_active_guidance_record_is_validated() -> None:
    key = _bkey("verifies_changes")
    with pytest.raises(guidance.GuidanceError):
        guidance.validate_match_record(
            guidance.match_record(
                snapshot_digest="short",
                source_digests=("b" * 64,),
                behavior_key=key,
                phase=guidance.PHASE_BEHAVIOR,
                match_state=guidance.MATCH_TAGGED,
                captured_at="1970-01-01T00:00:00Z",
            )
        )
    good = guidance.match_record(
        snapshot_digest="a" * 64,
        source_digests=("b" * 64,),
        behavior_key=key,
        phase=guidance.PHASE_BEHAVIOR,
        match_state=guidance.MATCH_TAGGED,
        captured_at="1970-01-01T00:00:00Z",
    )
    guidance.validate_match_record(good)


def test_source_digest_binds_kind_and_scope_without_locator_or_text() -> None:
    one = guidance.bound_source_digest("lesson", "global", "SECRET TEXT")
    two = guidance.bound_source_digest("steering", "global", "SECRET TEXT")
    three = guidance.bound_source_digest("lesson", "ws-abc123defg45", "SECRET TEXT")
    assert one != two != three
    assert "SECRET" not in one and len(one) == 64


def test_changed_digest_orchestration_recaptures_and_reruns_both_phases() -> None:
    captures: list[int] = []

    def recapture() -> list[guidance.GuidanceDocument]:
        captures.append(1)
        if len(captures) == 1:
            return [
                guidance.GuidanceDocument(
                    "lesson:1", "kiro-insights-behavior: verifies_changes", "lesson"
                )
            ]
        return [
            guidance.GuidanceDocument(
                "lesson:1", "kiro-insights-behavior: retries_operation", "lesson"
            )
        ]

    first_docs = recapture()
    previous = guidance.guidance_bound_digests(first_docs, scope="global")
    outcome = guidance.rerun_if_changed(
        previous,
        recapture,
        scope="global",
        compiled_candidates=[],
        active_guidance_artifacts={},
    )
    assert outcome.changed is True
    assert len(captures) == 2
    assert outcome.phase_one[0].match_state == guidance.MATCH_TAGGED
    assert outcome.phase_one[0].predicate_code == "retries_operation"


# ── defect 2: both-phase changed-digest orchestration with bound digests ──


def test_bound_digests_used_for_change_detection_not_content_only() -> None:
    docs_a = [guidance.GuidanceDocument("lesson:1", "same text", "lesson")]
    docs_b = [guidance.GuidanceDocument("steering:1", "same text", "steering")]
    da = guidance.guidance_bound_digests(docs_a, scope="global")
    db = guidance.guidance_bound_digests(docs_b, scope="global")
    assert da != db


def test_bound_digests_change_with_scope() -> None:
    docs = [guidance.GuidanceDocument("lesson:1", "same text", "lesson")]
    g = guidance.guidance_bound_digests(docs, scope="global")
    w = guidance.guidance_bound_digests(docs, scope="ws-abc123defg45")
    assert g != w


def test_rerun_if_changed_reruns_both_phases_for_affected_candidates() -> None:
    captures: list[int] = []

    def recapture() -> list[guidance.GuidanceDocument]:
        captures.append(1)
        if len(captures) == 1:
            return [
                guidance.GuidanceDocument(
                    "lesson:1", "kiro-insights-behavior: verifies_changes", "lesson"
                )
            ]
        return [
            guidance.GuidanceDocument(
                "lesson:1", "kiro-insights-behavior: retries_operation", "lesson"
            )
        ]

    first_docs = recapture()
    previous = guidance.guidance_bound_digests(first_docs, scope="global")
    compiled = [
        guidance.CompiledCandidate(
            behavior_key=_bkey("verifies_changes"),
            artifact_kind="text",
            compiled="use this prompt",
        )
    ]
    outcome = guidance.rerun_if_changed(
        previous,
        recapture,
        scope="global",
        compiled_candidates=compiled,
        active_guidance_artifacts={"text": ["use this prompt\n"]},
    )
    assert outcome.changed is True
    assert len(captures) == 2
    assert outcome.phase_one[0].match_state == guidance.MATCH_TAGGED
    assert outcome.phase_one[0].predicate_code == "retries_operation"
    assert outcome.phase_two is not None
    assert outcome.phase_two[0].matched is True


def test_rerun_if_changed_no_change_returns_unchanged_without_phase_two() -> None:
    docs = [
        guidance.GuidanceDocument("lesson:1", "kiro-insights-behavior: verifies_changes", "lesson")
    ]
    previous = guidance.guidance_bound_digests(docs, scope="global")
    outcome = guidance.rerun_if_changed(
        previous,
        lambda: list(docs),
        scope="global",
        compiled_candidates=[],
        active_guidance_artifacts={},
    )
    assert outcome.changed is False
    assert outcome.phase_two is None


# ── panel round: guidance defect-closure ──


def test_global_guidance_applies_across_workspace_scoped_behavior() -> None:
    match = guidance.PhaseOneMatch(
        guidance.MATCH_TAGGED, "verifies_changes", "0" * 64, scope="global"
    )
    global_key = _bkey("verifies_changes", scope="global")
    workspace_key = _bkey("verifies_changes", scope="ws-abc123defg45")
    assert guidance.phase_one_blocks_behavior(match, global_key) is True
    assert guidance.phase_one_blocks_behavior(match, workspace_key) is True


def test_workspace_guidance_applies_only_to_its_workspace() -> None:
    match = guidance.PhaseOneMatch(
        guidance.MATCH_TAGGED, "verifies_changes", "0" * 64, scope="ws-abc123defg45"
    )
    same = _bkey("verifies_changes", scope="ws-abc123defg45")
    other = _bkey("verifies_changes", scope="ws-zzz999aaa111")
    glob = _bkey("verifies_changes", scope="global")
    assert guidance.phase_one_blocks_behavior(match, same) is True
    assert guidance.phase_one_blocks_behavior(match, other) is False
    assert guidance.phase_one_blocks_behavior(match, glob) is False


def test_workspace_guidance_still_predicate_specific() -> None:
    match = guidance.PhaseOneMatch(
        guidance.MATCH_TAGGED, "verifies_changes", "0" * 64, scope="ws-abc123defg45"
    )
    other_pred = _bkey("retries_operation", scope="ws-abc123defg45")
    assert guidance.phase_one_blocks_behavior(match, other_pred) is False


def _good_record() -> guidance.ActiveGuidanceMatchRecord:
    return guidance.match_record(
        snapshot_digest="a" * 64,
        source_digests=("b" * 64,),
        behavior_key=_bkey("verifies_changes"),
        phase=guidance.PHASE_BEHAVIOR,
        match_state=guidance.MATCH_TAGGED,
        captured_at="1970-01-01T00:00:00Z",
    )


def test_validate_match_record_rejects_source_id_and_source_path() -> None:
    for forbidden in ("source_id", "source_path"):
        rec = _good_record()
        rec.behavior_key[forbidden] = "secret/path"
        with pytest.raises(guidance.GuidanceError):
            guidance.validate_match_record(rec)


def test_validate_match_record_requires_hex_digests() -> None:
    rec = guidance.match_record(
        snapshot_digest="z" * 64,
        source_digests=("b" * 64,),
        behavior_key=_bkey("verifies_changes"),
        phase=guidance.PHASE_BEHAVIOR,
        match_state=guidance.MATCH_TAGGED,
        captured_at="1970-01-01T00:00:00Z",
    )
    with pytest.raises(guidance.GuidanceError, match="hex"):
        guidance.validate_match_record(rec)


def test_validate_match_record_requires_exact_behavior_key_keys() -> None:
    rec = _good_record()
    del rec.behavior_key["polarity"]
    with pytest.raises(guidance.GuidanceError, match="behavior key"):
        guidance.validate_match_record(rec)


def test_validate_match_record_requires_captured_at() -> None:
    rec = guidance.match_record(
        snapshot_digest="a" * 64,
        source_digests=("b" * 64,),
        behavior_key=_bkey("verifies_changes"),
        phase=guidance.PHASE_BEHAVIOR,
        match_state=guidance.MATCH_TAGGED,
        captured_at="",
    )
    with pytest.raises(guidance.GuidanceError, match="captured_at"):
        guidance.validate_match_record(rec)


def test_validate_match_record_phase_artifact_consistency() -> None:
    behavior_phase_with_artifact = guidance.match_record(
        snapshot_digest="a" * 64,
        source_digests=("b" * 64,),
        behavior_key=_bkey("verifies_changes"),
        phase=guidance.PHASE_BEHAVIOR,
        match_state=guidance.MATCH_TAGGED,
        captured_at="1970-01-01T00:00:00Z",
        artifact_digest="c" * 64,
    )
    with pytest.raises(guidance.GuidanceError, match="artifact"):
        guidance.validate_match_record(behavior_phase_with_artifact)
    artifact_phase_without_digest = guidance.match_record(
        snapshot_digest="a" * 64,
        source_digests=("b" * 64,),
        behavior_key=_bkey("verifies_changes"),
        phase=guidance.PHASE_ARTIFACT,
        match_state=guidance.MATCH_EXACT_ARTIFACT,
        captured_at="1970-01-01T00:00:00Z",
        artifact_digest=None,
    )
    with pytest.raises(guidance.GuidanceError, match="artifact"):
        guidance.validate_match_record(artifact_phase_without_digest)


def test_replay_changed_then_unchanged_using_returned_bound_digests() -> None:
    def recapture() -> list[guidance.GuidanceDocument]:
        return [
            guidance.GuidanceDocument(
                "lesson:1", "kiro-insights-behavior: verifies_changes", "lesson"
            )
        ]

    initial = [
        guidance.GuidanceDocument("lesson:1", "kiro-insights-behavior: works_in_area", "lesson")
    ]
    previous = guidance.guidance_bound_digests(initial, scope="global")
    first = guidance.rerun_if_changed(
        previous, recapture, scope="global", compiled_candidates=[], active_guidance_artifacts={}
    )
    assert first.changed is True
    assert first.phase_two is not None
    second = guidance.rerun_if_changed(
        first.bound_digests,
        recapture,
        scope="global",
        compiled_candidates=[],
        active_guidance_artifacts={},
    )
    assert second.changed is False
    assert second.phase_two is None


# ── final parent round: match-record BehaviorKey + captured_at validation ──


def _record_with_behavior_key(bk: dict) -> guidance.ActiveGuidanceMatchRecord:
    return guidance.ActiveGuidanceMatchRecord(
        schema_version=guidance.ACTIVE_GUIDANCE_MATCH_SCHEMA,
        snapshot_digest="a" * 64,
        source_digests=("b" * 64,),
        behavior_key=bk,
        artifact_digest=None,
        phase=guidance.PHASE_BEHAVIOR,
        match_state=guidance.MATCH_TAGGED,
        captured_at="1970-01-01T00:00:00+00:00",
    )


def test_validate_match_record_rejects_invalid_subject_even_with_four_keys() -> None:
    bad = {
        "subject_code": "not_a_subject",
        "predicate_code": "verifies_changes",
        "polarity": "positive",
        "scope": "global",
    }
    with pytest.raises(guidance.GuidanceError):
        guidance.validate_match_record(_record_with_behavior_key(bad))


def test_validate_match_record_rejects_invalid_predicate() -> None:
    bad = {
        "subject_code": "session_owner",
        "predicate_code": "not_a_predicate",
        "polarity": "positive",
        "scope": "global",
    }
    with pytest.raises(guidance.GuidanceError):
        guidance.validate_match_record(_record_with_behavior_key(bad))


def test_validate_match_record_rejects_incompatible_polarity() -> None:
    bad = {
        "subject_code": "session_owner",
        "predicate_code": "verifies_changes",
        "polarity": "negative",
        "scope": "global",
    }
    with pytest.raises(guidance.GuidanceError):
        guidance.validate_match_record(_record_with_behavior_key(bad))


def test_validate_match_record_rejects_invalid_scope() -> None:
    bad = {
        "subject_code": "session_owner",
        "predicate_code": "verifies_changes",
        "polarity": "positive",
        "scope": "not a workspace id",
    }
    with pytest.raises(guidance.GuidanceError):
        guidance.validate_match_record(_record_with_behavior_key(bad))


def test_validate_match_record_accepts_valid_behavior_key_values() -> None:
    good = {
        "subject_code": "session_owner",
        "predicate_code": "verifies_changes",
        "polarity": "positive",
        "scope": "ws-abc123defg45",
    }
    guidance.validate_match_record(_record_with_behavior_key(good))


def test_validate_match_record_requires_timezone_aware_iso_captured_at() -> None:
    for bad in ("1970-01-01T00:00:00", "not-a-timestamp", "1970-01-01", "   "):
        rec = guidance.ActiveGuidanceMatchRecord(
            schema_version=guidance.ACTIVE_GUIDANCE_MATCH_SCHEMA,
            snapshot_digest="a" * 64,
            source_digests=("b" * 64,),
            behavior_key={
                "subject_code": "session_owner",
                "predicate_code": "verifies_changes",
                "polarity": "positive",
                "scope": "global",
            },
            artifact_digest=None,
            phase=guidance.PHASE_BEHAVIOR,
            match_state=guidance.MATCH_TAGGED,
            captured_at=bad,
        )
        with pytest.raises(guidance.GuidanceError, match="captured_at"):
            guidance.validate_match_record(rec)


def test_validate_match_record_accepts_offset_and_z_captured_at() -> None:
    for good in ("1970-01-01T00:00:00+00:00", "2026-10-09T03:00:00Z", "2026-10-09T03:00:00-04:00"):
        rec = guidance.ActiveGuidanceMatchRecord(
            schema_version=guidance.ACTIVE_GUIDANCE_MATCH_SCHEMA,
            snapshot_digest="a" * 64,
            source_digests=("b" * 64,),
            behavior_key={
                "subject_code": "session_owner",
                "predicate_code": "verifies_changes",
                "polarity": "positive",
                "scope": "global",
            },
            artifact_digest=None,
            phase=guidance.PHASE_BEHAVIOR,
            match_state=guidance.MATCH_TAGGED,
            captured_at=good,
        )
        guidance.validate_match_record(rec)

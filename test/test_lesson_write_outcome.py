"""What a lesson write reports, and the four surfaces that read it.

A bare ``bool`` return cannot carry this: one ``False`` would mean several
unrelated things at once -- validation refused the value, a dedup rule claimed the
write, the submit was a no-op, or a bare re-submit deliberately kept a stored
NOT-clause. The first group means "your lesson did not land"; the second means
"your lesson is fine, there was nothing to do". These tests pin the outcome each
path reports, that the result's TRUTH VALUE answers the plain bool predicate (so
the callers that only
branch on success are unaffected -- see ``TestWriteLessonTruthValueIsTheOldBool``), and
that the three surfaces a human or a model reads -- the CLI, the ``/api/lessons``
response and the ``learn_add`` tool result -- stop saying "Saved" for a write that
stored nothing.

This file pins the volatile-fact boundary every lesson producer shares and the
durable model-version references it lets through, then the outcome, reason and winner
each ``write_lesson`` path reports and the result's truth value; its boundary tests
also reach the ``learn_add`` tool and the ``api_lessons`` list where each meets that
boundary. What the ``learn add`` CLI, the ``/api/lessons`` routes and the
``learn_add`` tool report for each write outcome, including the rules a supersede
removed, and what ``learn_list`` shows for a withheld row, live in
``test_lesson_write_outcome_surfaces.py``; what the store reports and records on disk
when a write supersedes a stored lesson lives in
``test_lesson_write_outcome_supersede.py``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from lesson_write_outcome_helpers import (
    _INJECTION,
    _rule_of,
    _store,
)

from kiro_crew.model_registry import MODEL_ID_LITERAL_PATTERN
from kiro_crew.vector_memory import LessonWriteOutcome, LessonWriteResult


class TestVolatileLessonWriteBoundary:
    """Every vector lesson producer shares one volatile-fact boundary."""

    VOLATILE_IDENTITY = "The current model identity is gpt-5.6-sol."

    def test_consolidation_source_refuses_volatile_identity(self, tmp_path) -> None:
        store = _store(tmp_path)
        try:
            result = store.write_lesson(
                self.VOLATILE_IDENTITY,
                "knowledge",
                source="consolidation",
            )

            assert result.outcome is LessonWriteOutcome.REFUSED
            assert result.reason == "volatile_session_fact"
            assert store.get_lessons() == []
        finally:
            store.close()

    def test_concrete_model_preference_is_refused_as_a_stale_pin(self, tmp_path) -> None:
        store = _store(tmp_path)
        try:
            result = store.write_lesson(
                "always use gpt-5.6-sol for cron summaries",
                "preference",
            )

            assert result.outcome is LessonWriteOutcome.REFUSED
            assert result.reason == "volatile_session_fact"
        finally:
            store.close()

    def test_concrete_model_knowledge_fact_still_writes(self, tmp_path) -> None:
        store = _store(tmp_path)
        try:
            result = store.write_lesson(
                "the vision endpoint only accepts gpt-5.6-sol",
                "knowledge",
            )

            assert result.outcome is LessonWriteOutcome.INSERTED
            assert "gpt-5.6-sol" in store.get_lessons_context()
        finally:
            store.close()

    @pytest.mark.parametrize(
        "rule",
        [
            "the selected model is gpt-5.6-sol",
            "the session model is gpt-5.6-sol",
        ],
    )
    def test_selected_or_session_identity_is_refused_in_knowledge(
        self, tmp_path, rule: str
    ) -> None:
        store = _store(tmp_path)
        try:
            result = store.write_lesson(rule, "knowledge")

            assert result.outcome is LessonWriteOutcome.REFUSED
            assert result.reason == "volatile_session_fact"
        finally:
            store.close()

    def test_direct_running_as_identity_is_refused_in_knowledge(self, tmp_path) -> None:
        store = _store(tmp_path)
        try:
            result = store.write_lesson(
                "This session is running as gpt-5.6-sol",
                "knowledge",
            )

            assert result.outcome is LessonWriteOutcome.REFUSED
            assert result.reason == "volatile_session_fact"
        finally:
            store.close()

    @pytest.mark.parametrize(
        "rule",
        [
            "the Session model is persisted per tab",
            "the selected model is the source of truth for login state",
            "Use of gpt-5.6-sol requires the vision flag",
        ],
    )
    def test_data_model_or_compatibility_fact_still_writes(self, tmp_path, rule: str) -> None:
        store = _store(tmp_path)
        try:
            result = store.write_lesson(rule, "knowledge")

            assert result.outcome is LessonWriteOutcome.INSERTED
        finally:
            store.close()

    def test_behavioral_pin_cannot_bypass_through_knowledge(self, tmp_path) -> None:
        store = _store(tmp_path)
        try:
            result = store.write_lesson(
                "always use gpt-5.6-sol for cron summaries",
                "knowledge",
            )

            assert result.outcome is LessonWriteOutcome.REFUSED
            assert result.reason == "volatile_session_fact"
        finally:
            store.close()

    def test_noninitial_behavioral_pin_cannot_bypass_through_knowledge(self, tmp_path) -> None:
        store = _store(tmp_path)
        try:
            result = store.write_lesson(
                "in cron jobs, always use gpt-5.6-sol",
                "knowledge",
            )

            assert result.outcome is LessonWriteOutcome.REFUSED
            assert result.reason == "volatile_session_fact"
        finally:
            store.close()

    @pytest.mark.parametrize(
        "rule",
        [
            "Compatibility varies. Select gpt-5.6-sol for reviews",
            "Compatibility varies! Select gpt-5.6-sol for reviews",
            "Compatibility varies? Select gpt-5.6-sol for reviews",
            "Compatibility varies\nSelect gpt-5.6-sol for reviews",
        ],
    )
    def test_sentence_boundary_behavioral_pin_cannot_bypass_through_knowledge(
        self, tmp_path, rule: str
    ) -> None:
        store = _store(tmp_path)
        try:
            result = store.write_lesson(rule, "knowledge")

            assert result.outcome is LessonWriteOutcome.REFUSED
            assert result.reason == "volatile_session_fact"
        finally:
            store.close()

    @pytest.mark.parametrize(
        "rule",
        [
            "Please use gpt-5.6-sol for reviews",
            "Kindly select gpt-5.6-sol for reviews",
            "Do choose gpt-5.6-sol for reviews",
            "Please do prefer gpt-5.6-sol for reviews",
        ],
    )
    def test_prefixed_direct_imperative_cannot_bypass_through_knowledge(
        self, tmp_path, rule: str
    ) -> None:
        store = _store(tmp_path)
        try:
            result = store.write_lesson(rule, "knowledge")

            assert result.outcome is LessonWriteOutcome.REFUSED
            assert result.reason == "volatile_session_fact"
        finally:
            store.close()

    @pytest.mark.parametrize(
        "rule",
        [
            "always use gpt-5.6-sol",
            "always use gpt-5.6-sol.",
            "Select gpt-5.6-sol for reviews",
            "use the gpt-5.6-sol model",
            "prefer opus-4.8 over gpt-5.6-sol",
            "switch to gpt-5.6-sol",
            "Please use our fallback backend gpt-5.6-sol for reviews",
            "Stick with the same opus-4.8 model",
            "always use gpt-5.6-sol, never opus-4.8",
        ],
    )
    def test_selected_model_object_is_refused(self, tmp_path, rule: str) -> None:
        store = _store(tmp_path)
        try:
            result = store.write_lesson(rule, "preference")

            assert result.outcome is LessonWriteOutcome.REFUSED
            assert result.reason == "volatile_session_fact"
        finally:
            store.close()

    def test_descriptive_second_sentence_with_concrete_model_still_writes(self, tmp_path) -> None:
        store = _store(tmp_path)
        try:
            result = store.write_lesson(
                "Pricing changed. gpt-5.6-sol costs less than opus",
                "knowledge",
            )

            assert result.outcome is LessonWriteOutcome.INSERTED
        finally:
            store.close()

    def test_vector_context_hides_legacy_sentence_boundary_pin(self, tmp_path) -> None:
        store = _store(tmp_path)
        try:
            assert (
                store.set_semantic(
                    "lesson.legacysentencepin",
                    {
                        "rule": "Compatibility varies. Select gpt-5.6-sol for reviews",
                        "category": "knowledge",
                        "negative": None,
                    },
                    1.0,
                    "user_explicit",
                )
                is None
            )
            assert store.get_lessons(), "the legacy row must remain visible for deletion"

            assert "gpt-5.6-sol" not in store.get_lessons_context()
        finally:
            store.close()

    def test_jsonl_context_hides_legacy_sentence_boundary_pin(self, tmp_path) -> None:
        import json as _json

        from kiro_crew.learn import LessonStore

        (tmp_path / "lessons.jsonl").write_text(
            _json.dumps(
                {
                    "ts": "2026-09-11T00:00:00+00:00",
                    "rule": "Compatibility varies. Select gpt-5.6-sol for reviews",
                    "category": "knowledge",
                    "negative": None,
                    "repo_scope": None,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        store = LessonStore(base_dir=tmp_path)
        assert store.load_all(), "the legacy row must remain visible for deletion"

        assert "gpt-5.6-sol" not in store.get_context()

    def test_onboarding_reports_sentence_boundary_jsonl_directive_rejected(self, tmp_path) -> None:
        from kiro_crew import onboarding_import
        from kiro_crew.learn import LessonStore

        item = onboarding_import._Item(
            "codex",
            "instructions",
            "sentence-pin",
            {"rule": "Compatibility varies. Select gpt-5.6-sol for reviews"},
        )

        outcome = onboarding_import._write_instruction(
            item,
            LessonStore(base_dir=tmp_path),
        )

        assert outcome.status == "rejected"

    def test_active_model_operational_rule_still_writes(self, tmp_path) -> None:
        store = _store(tmp_path)
        try:
            result = store.write_lesson(
                "always confirm the active model supports the required context window",
                "preference",
            )

            assert result.outcome is LessonWriteOutcome.INSERTED
        finally:
            store.close()

    def test_volatile_negative_clause_is_refused(self, tmp_path) -> None:
        store = _store(tmp_path)
        try:
            result = store.write_lesson(
                "use automatic model selection",
                "preference",
                negative="use gpt-5.6-sol",
            )

            assert result.outcome is LessonWriteOutcome.REFUSED
            assert result.reason == "volatile_session_fact"
            assert store.get_lessons() == []
        finally:
            store.close()

    def test_knowledge_negative_clause_cannot_carry_an_imperative_model_pin(self, tmp_path) -> None:
        store = _store(tmp_path)
        try:
            result = store.write_lesson(
                "automatic selection is safest",
                "knowledge",
                negative="Select gpt-5.6-sol for reviews",
            )

            assert result.outcome is LessonWriteOutcome.REFUSED
            assert result.reason == "volatile_session_fact"
            assert store.get_lessons() == []
        finally:
            store.close()

    def test_automatic_jsonl_save_reports_refusal(self, tmp_path) -> None:
        from kiro_crew.learn import Lesson, LessonStore

        store = LessonStore(base_dir=tmp_path)
        outcome = store.save(
            Lesson(
                ts="2026-09-11T00:00:00+00:00",
                rule=self.VOLATILE_IDENTITY,
                category="knowledge",
            )
        )

        assert outcome == "refused"
        assert store.load_all() == []

    def test_vector_context_hides_preexisting_volatile_lesson(self, tmp_path) -> None:
        store = _store(tmp_path)
        try:
            assert (
                store.set_semantic(
                    "lesson.legacyvolatile",
                    {
                        "rule": self.VOLATILE_IDENTITY,
                        "category": "preference",
                        "negative": None,
                    },
                    1.0,
                    "user_explicit",
                )
                is None
            )
            assert store.get_lessons(), "the legacy row must remain visible for deletion"

            assert "gpt-5.6-sol" not in store.get_lessons_context()
        finally:
            store.close()

    def test_vector_context_hides_legacy_knowledge_negative_pin(self, tmp_path) -> None:
        store = _store(tmp_path)
        try:
            assert (
                store.set_semantic(
                    "lesson.legacynegative",
                    {
                        "rule": "automatic selection is safest",
                        "category": "knowledge",
                        "negative": "Select gpt-5.6-sol for reviews",
                    },
                    1.0,
                    "user_explicit",
                )
                is None
            )
            assert store.get_lessons(), "the legacy row must remain visible for deletion"

            assert "gpt-5.6-sol" not in store.get_lessons_context()
        finally:
            store.close()

    def test_vector_context_hides_key_confirmed_legacy_negative_pin(self, tmp_path) -> None:
        from kiro_crew.vector_memory import _LESSON_NEGATIVE_SEP, _lesson_key

        rule = "automatic selection is safest"
        store = _store(tmp_path)
        try:
            assert (
                store.set_semantic(
                    _lesson_key(rule),
                    f"{rule}{_LESSON_NEGATIVE_SEP}Select gpt-5.6-sol for reviews",
                    1.0,
                    "user_explicit",
                )
                is None
            )
            assert store.get_lessons(), "the legacy row must remain visible for deletion"

            assert store.has_any_lesson() is False
            assert "gpt-5.6-sol" not in store.get_lessons_context()
        finally:
            store.close()

    def test_vector_context_keeps_key_confirmed_legacy_version_reference(self, tmp_path) -> None:
        from kiro_crew.vector_memory import _LESSON_NEGATIVE_SEP, _lesson_key

        rule = "check streaming compatibility before release"
        negative = "assume gpt-5.6-sol supports the streaming flag"
        store = _store(tmp_path)
        try:
            assert (
                store.set_semantic(
                    _lesson_key(rule),
                    f"{rule}{_LESSON_NEGATIVE_SEP}{negative}",
                    1.0,
                    "user_explicit",
                )
                is None
            )

            assert store.has_any_lesson() is True
            rendered = store.get_lessons_context()
            assert rule in rendered
            assert negative in rendered
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_lessons_api_marks_key_confirmed_legacy_negative_pin_withheld(
        self, tmp_path
    ) -> None:
        import json as _json

        from kiro_crew.dashboard.handlers import cron
        from kiro_crew.vector_memory import _LESSON_NEGATIVE_SEP, _lesson_key

        rule = "automatic selection is safest"
        store = _store(tmp_path)
        try:
            assert (
                store.set_semantic(
                    _lesson_key(rule),
                    f"{rule}{_LESSON_NEGATIVE_SEP}Select gpt-5.6-sol for reviews",
                    1.0,
                    "user_explicit",
                )
                is None
            )
            request = MagicMock()
            state = MagicMock()
            request.app = {"state": state}
            request.headers = {"X-Session-Key": "dashboard:ui"}
            request.query = {}
            with (
                patch.object(cron, "_blocks_reads_session", return_value=False),
                patch.object(
                    cron,
                    "resolve_lesson_memory_store",
                    new=AsyncMock(return_value=(None, None)),
                ),
                patch.object(
                    cron,
                    "_prepare_member_lesson_store",
                    new=AsyncMock(return_value=None),
                ),
                patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=store)),
            ):
                resp = await cron.api_lessons(request)

            body = _json.loads(resp.text)
            assert body["lessons"][0]["withheld_reason"] == "volatile_session_fact"
        finally:
            store.close()

    def test_jsonl_context_hides_preexisting_volatile_lesson(self, tmp_path) -> None:
        import json as _json

        from kiro_crew.learn import LessonStore

        (tmp_path / "lessons.jsonl").write_text(
            _json.dumps(
                {
                    "ts": "2026-09-11T00:00:00+00:00",
                    "rule": self.VOLATILE_IDENTITY,
                    "category": "preference",
                    "negative": None,
                    "repo_scope": None,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        store = LessonStore(base_dir=tmp_path)
        assert store.load_all(), "the legacy row must remain visible for deletion"

        assert "gpt-5.6-sol" not in store.get_context()

    def test_jsonl_consolidation_does_not_count_a_refusal(self, tmp_path) -> None:
        from kiro_crew.history import HistoryConsolidator
        from kiro_crew.learn import LessonStore

        store = LessonStore(base_dir=tmp_path)
        consolidator = HistoryConsolidator(
            log=MagicMock(),
            memory=MagicMock(),
            lesson_store=store,
        )
        with patch("kiro_crew.history_consolidation._HISTORY_LOGGER.info") as info:
            consolidator._save_lessons([{"rule": self.VOLATILE_IDENTITY, "category": "knowledge"}])

        assert store.load_all() == []
        info.assert_not_called()

    def test_onboarding_reports_volatile_jsonl_directive_rejected(self, tmp_path) -> None:
        from kiro_crew import onboarding_import
        from kiro_crew.learn import LessonStore

        item = onboarding_import._Item(
            "codex",
            "instructions",
            "volatile",
            {"rule": self.VOLATILE_IDENTITY},
        )

        outcome = onboarding_import._write_instruction(
            item,
            LessonStore(base_dir=tmp_path),
        )

        assert outcome.status == "rejected"

    def test_onboarding_reports_volatile_vector_directive_rejected(self, tmp_path) -> None:
        from kiro_crew import onboarding_import
        from kiro_crew.learn import LessonStore

        vector_store = _store(tmp_path)
        try:
            item = onboarding_import._Item(
                "codex",
                "instructions",
                "volatile",
                {"rule": self.VOLATILE_IDENTITY},
            )

            outcome = onboarding_import._write_instruction(
                item,
                LessonStore(base_dir=tmp_path),
                vector_store,
            )

            assert outcome.status == "rejected"
            assert vector_store.get_lessons() == []
        finally:
            vector_store.close()

    def test_durable_model_family_preference_still_writes(self, tmp_path) -> None:
        store = _store(tmp_path)
        try:
            result = store.write_lesson(
                "prefer the latest Claude Sonnet reviewer",
                "preference",
                source="consolidation",
            )

            assert result.outcome is LessonWriteOutcome.INSERTED
            assert _rule_of(store.get_lessons()[0]) == "prefer the latest Claude Sonnet reviewer"
        finally:
            store.close()

    def test_workflow_literal_regex_matches_the_product_guard(self) -> None:
        workflow_path = Path(__file__).parents[1] / ".github" / "workflows" / "code-review.yml"
        workflow = workflow_path.read_text(encoding="utf-8")
        model_gate = workflow.split(
            'echo "::group::model-selection — no hardcoded model id as a default/fallback"',
            1,
        )[1].split('echo "::endgroup::"', 1)[0]

        assert MODEL_ID_LITERAL_PATTERN in model_gate
        assert "from kiro_crew.model_registry" not in model_gate


class TestDurableModelVersionReferences:
    """A version literal alone is durable; identity claims and imperatives are not."""

    RULE = "the Python 3.12 wheel needs claude-opus-4.8 pinned in role_models"
    NEGATIVE = "assume gpt-5.6-sol supports the streaming flag"
    MODEL_TOOLING_RULES = (
        ("Always use the tokenizer shipped with gpt-5.6-sol", "tool"),
        ("Prefer the retry wrapper when calling opus-4.8 endpoints", "preference"),
        ("Never use the streaming flag with gpt-5.6-sol", "tool"),
        ("Always use the gpt-5-compatible tokenizer", "tool"),
        ("Always use the gpt-5 compatible tokenizer", "tool"),
        ("Never use gpt-5.6-sol endpoints without the retry wrapper", "tool"),
    )
    BACKEND_PROCESS_RULES = (
        ("Never run as the backend service account", "tool"),
        ("The service is running as the backend worker", "preference"),
        ("The service is running as the active backend service account", "tool"),
        ("The service is running as the current backend worker", "preference"),
        ("Restart the backend after config changes", "tool"),
        ("Run the worker as the backend user, never as root", "preference"),
    )
    TOOLING_RULES = MODEL_TOOLING_RULES + BACKEND_PROCESS_RULES

    @pytest.mark.parametrize(
        "rule",
        [
            "This session is running as the current backend",
            "This session is running as the model backend",
            "This session is running as gpt-5.6-sol",
            "This session is running as the backend gpt-5.6-sol",
            "The active model is opus-4.8",
        ],
    )
    def test_model_identity_controls_are_refused(self, tmp_path: Path, rule: str) -> None:
        store = _store(tmp_path)
        try:
            result = store.write_lesson(rule, "knowledge")

            assert result.outcome is LessonWriteOutcome.REFUSED
            assert result.reason == "volatile_session_fact"
        finally:
            store.close()

    @pytest.mark.parametrize(
        "rule,category,negative",
        [
            (RULE, "tool", None),
            ("check streaming compatibility before release", "preference", NEGATIVE),
            *[(rule, category, None) for rule, category in TOOLING_RULES],
        ],
    )
    def test_learn_add_writes_durable_version_references(
        self,
        tmp_path: Path,
        rule: str,
        category: str,
        negative: str | None,
    ) -> None:
        from kiro_crew.mcp_tools import learn

        store = _store(tmp_path)

        def _post(path: str, payload: dict[str, str]) -> dict[str, object]:
            assert path == "/api/lessons"
            result = store.write_lesson(
                payload["rule"],
                payload["category"],
                payload.get("negative"),
            )
            return {
                "ok": result.stored,
                "outcome": result.outcome.value,
                "reason": result.reason,
                "superseded": list(result.superseded),
            }

        try:
            args = {"rule": rule, "category": category}
            if negative is not None:
                args["negative"] = negative
            with (
                patch.object(learn.mcp_core, "_post", side_effect=_post),
                patch.object(learn.mcp_core, "_resolve_session_key", return_value="dashboard:ui"),
                patch.object(learn.mcp_core, "_vet_memory_writes_governance", return_value=None),
            ):
                text = learn.learn_add("learn_add", args)

            assert text.startswith("Saved lesson")
            assert store.has_any_lesson() is True
            rendered = store.get_lessons_context()
            assert rule in rendered
            if negative is not None:
                assert negative in rendered
        finally:
            store.close()

    def test_consolidation_keeps_durable_version_references(self, tmp_path: Path) -> None:
        from kiro_crew.history import HistoryConsolidator
        from kiro_crew.learn import LessonStore

        store = LessonStore(base_dir=tmp_path)
        consolidator = HistoryConsolidator(
            log=MagicMock(),
            memory=MagicMock(),
            lesson_store=store,
        )

        consolidator._save_lessons(
            [
                {"rule": self.RULE, "category": "tool"},
                {
                    "rule": "check streaming compatibility before release",
                    "category": "preference",
                    "negative": self.NEGATIVE,
                },
                *[
                    {"rule": rule, "category": category}
                    for rule, category in self.MODEL_TOOLING_RULES
                ],
            ]
        )
        consolidator._save_lessons(
            [{"rule": rule, "category": category} for rule, category in self.BACKEND_PROCESS_RULES]
        )

        assert [lesson.rule for lesson in store.load_all()] == [
            self.RULE,
            "check streaming compatibility before release",
            *[rule for rule, _category in self.TOOLING_RULES],
        ]
        context = store.get_context()
        assert self.RULE in context
        assert self.NEGATIVE in context
        for rule, _category in self.TOOLING_RULES:
            assert rule in context

    @pytest.mark.parametrize("use_vector_store", [False, True])
    @pytest.mark.parametrize(
        "rule",
        [RULE, *[rule for rule, _category in TOOLING_RULES]],
    )
    def test_onboarding_keeps_durable_version_reference(
        self, tmp_path: Path, use_vector_store: bool, rule: str
    ) -> None:
        from kiro_crew import onboarding_import
        from kiro_crew.learn import LessonStore

        jsonl_store = LessonStore(base_dir=tmp_path)
        vector_store = _store(tmp_path) if use_vector_store else None
        item = onboarding_import._Item(
            "codex",
            "instructions",
            "durable-model-version",
            {"rule": rule},
        )
        try:
            outcome = onboarding_import._write_instruction(
                item,
                jsonl_store,
                vector_store,
            )

            assert outcome.status == "imported"
            if vector_store is not None:
                assert vector_store.has_any_lesson() is True
                assert rule in vector_store.get_lessons_context()
            else:
                assert rule in jsonl_store.get_context()
        finally:
            if vector_store is not None:
                vector_store.close()

    def test_tool_description_states_the_narrow_boundary(self) -> None:
        from kiro_crew.mcp_tools import learn

        descriptor = next(item for item in learn.schemas() if item["name"] == "learn_add")
        description = descriptor["description"]
        assert "runtime identity assertions" in description
        assert "'running as' phrase needs an unambiguous model noun" in description
        assert "qualified 'backend' that ends its clause" in description
        assert "backend service-account and process wording remains durable" in description
        assert "model-selection imperatives" in description
        assert "selected object is a concrete model ID" in description
        assert "at the end of its clause" in description
        assert "other backend IDs are not lesson-refused" in description
        assert "do not disguise either refused class" in description
        assert "A model version mentioned by itself is allowed" in description


class TestWriteLessonOutcomes:
    """Each declining path names WHICH rule declined, not just that one did."""

    def test_first_write_is_inserted(self, tmp_path):
        store = _store(tmp_path)
        try:
            result = store.write_lesson("prefer ruff over flake8", "tool")
            assert result.outcome is LessonWriteOutcome.INSERTED
            assert result.reason is None
        finally:
            store.close()

    def test_attaching_a_clause_to_a_stored_rule_is_enriched(self, tmp_path):
        store = _store(tmp_path)
        try:
            store.write_lesson("prefer ruff over flake8", "tool")
            result = store.write_lesson("prefer ruff over flake8", "tool", "not for typing")
            assert result.outcome is LessonWriteOutcome.ENRICHED
            assert result.reason is None
        finally:
            store.close()

    def test_identical_resubmit_is_unchanged_not_a_failure(self, tmp_path):
        """The case that made the CLI write a redundant JSONL record.

        Nothing was written, but the lesson IS stored exactly as submitted, so a
        caller told "this did not land" would act on a false premise.
        """
        store = _store(tmp_path)
        try:
            store.write_lesson("prefer ruff over flake8", "tool", "not for typing")
            result = store.write_lesson("prefer ruff over flake8", "tool", "not for typing")
            assert result.outcome is LessonWriteOutcome.UNCHANGED
            assert result.reason is None
            assert result.stored is True, "the lesson is in the store; only the write was a no-op"
            assert result.wrote is False
        finally:
            store.close()

    def test_bare_resubmit_keeping_a_stored_clause_says_so(self, tmp_path):
        store = _store(tmp_path)
        try:
            store.write_lesson("prefer ruff over flake8", "tool", "not for typing")
            result = store.write_lesson("prefer ruff over flake8", "tool")
            assert result.outcome is LessonWriteOutcome.UNCHANGED
            assert result.reason == "kept_stored_clause"
            assert result.stored is True
        finally:
            store.close()

    def test_refused_value_reports_the_reject_code(self, tmp_path):
        """A refusal stores NOTHING, and names which validation refused it."""
        store = _store(tmp_path)
        try:
            result = store.write_lesson("run the deploy script", "tool", _INJECTION)
            assert result.outcome is LessonWriteOutcome.REFUSED
            assert result.reason == "injection_blocked"
            assert result.stored is False
            assert store.get_lessons() == []
        finally:
            store.close()

    def test_inadmissible_scope_is_refused_with_its_own_reason(self, tmp_path):
        store = _store(tmp_path)
        try:
            result = store.write_lesson("gate the repo", "tool", None, repo_scope="/src/pkg")
            assert result.outcome is LessonWriteOutcome.REFUSED
            assert result.reason == "scope_inadmissible"
        finally:
            store.close()

    def test_substring_covered_rule_is_deduped_not_refused(self, tmp_path):
        """A dedup decline is a different event from a validation refusal.

        Nothing new was stored, but the guidance IS in effect through the broader
        lesson -- so a surface can say "already covered" instead of "failed".
        """
        store = _store(tmp_path)
        try:
            store.write_lesson("always run the linter before pushing a branch", "tool")
            result = store.write_lesson("run the linter", "tool")
            assert result.outcome is LessonWriteOutcome.DEDUPED
            assert result.reason == "substring_covered"
            assert result.stored is False
        finally:
            store.close()

    def test_a_deduped_write_names_the_stored_lesson_it_lost_to(self, tmp_path):
        """The decline names the winner verbatim, NOT-clause included, with its scope.

        Without it the caller knows only that "an existing lesson covers it" and has
        to page the list route to find which one before it can replace it.
        """
        store = _store(tmp_path)
        try:
            store.write_lesson(
                "always run the linter before pushing a branch",
                "tool",
                "for a docs-only change",
                repo_scope="src/pkg",
            )
            result = store.write_lesson("run the linter", "tool", repo_scope="src/pkg")
            assert result.outcome is LessonWriteOutcome.DEDUPED
            assert result.winner is not None
            assert result.winner.startswith("always run the linter before pushing a branch")
            assert "for a docs-only change" in result.winner
            assert result.winner_repo_scope == "src/pkg"
        finally:
            store.close()

    def test_a_global_winner_reports_no_scope(self, tmp_path):
        store = _store(tmp_path)
        try:
            store.write_lesson("always run the linter before pushing a branch", "tool")
            result = store.write_lesson("run the linter", "tool")
            assert result.winner == "always run the linter before pushing a branch"
            assert result.winner_repo_scope is None
        finally:
            store.close()

    def test_a_write_that_lands_names_no_winner(self, tmp_path):
        store = _store(tmp_path)
        try:
            result = store.write_lesson("always run the linter before pushing a branch", "tool")
            assert result.wrote
            assert result.winner is None
            assert result.winner_repo_scope is None
        finally:
            store.close()


class TestWriteLessonTruthValueIsTheOldBool:
    """The result's TRUTH VALUE must keep answering exactly "did this write something".

    This is what let the bool return be replaced outright instead of shipped beside a
    second method. ~55 assertions and three production callers read the old answer with
    a bare ``if``/``assert``; returning an ordinary object would have made every one of
    them unconditionally true, silently, with no type error for mypy to catch. So
    ``__bool__`` is load-bearing, not a convenience -- these pin it directly, and the
    bare-truthy assertions across the lesson suites exercise it on every path.
    """

    @pytest.mark.parametrize(
        "negative,expect_truthy",
        [(None, True), (_INJECTION, False)],
    )
    def test_truth_value_matches_wrote_for_the_same_input(self, tmp_path, negative, expect_truthy):
        store = _store(tmp_path)
        try:
            result = store.write_lesson("a fresh rule about caching", "tool", negative)
            assert bool(result) is expect_truthy
            assert bool(result) is result.wrote
        finally:
            store.close()

    def test_truth_value_is_false_for_a_no_op_resubmit(self, tmp_path):
        """A bare ``if`` on a re-submit must still read False, as it did before."""
        store = _store(tmp_path)
        try:
            assert store.write_lesson("prefer ruff over flake8", "tool")
            assert not store.write_lesson("prefer ruff over flake8", "tool")
        finally:
            store.close()

    @pytest.mark.parametrize(
        "outcome,expect_truthy",
        [
            (LessonWriteOutcome.INSERTED, True),
            (LessonWriteOutcome.ENRICHED, True),
            (LessonWriteOutcome.UNCHANGED, False),
            (LessonWriteOutcome.DEDUPED, False),
            (LessonWriteOutcome.REFUSED, False),
        ],
    )
    def test_truth_value_is_wrote_for_every_outcome(self, outcome, expect_truthy):
        """Exhaustive, so a new outcome cannot join without a decision here.

        ``UNCHANGED`` is the one a reader might expect to be truthy -- the lesson IS
        stored. It is deliberately falsy, because the predicate the old bool answered
        was "did this call write", and every caller branching on the truth value was
        written against that. ``stored`` is where "is my lesson in there" lives.
        """
        assert bool(LessonWriteResult(outcome)) is expect_truthy

    def test_unchanged_is_not_counted_as_a_write(self):
        """``wrote`` and ``stored`` disagree on exactly one outcome, deliberately."""
        result = LessonWriteResult(LessonWriteOutcome.UNCHANGED)
        assert result.wrote is False
        assert result.stored is True

    def test_wire_values_are_pinned_against_the_jsonl_store_vocabulary(self):
        """The route's JSONL branch matches these words as inline literals.

        Both stores describe the same three events, and the route checks the JSONL
        store's return against the literal words rather than importing this enum
        (there is one reader, and the import would have no reason to be lazy). This
        pins the enum's wire values so the two sides cannot drift apart in silence.
        """
        assert LessonWriteOutcome.INSERTED.value == "inserted"
        assert LessonWriteOutcome.ENRICHED.value == "enriched"
        assert LessonWriteOutcome.UNCHANGED.value == "unchanged"
        # The two the JSONL store can never produce: it validates no content, and it
        # matches on the rule alone so it never dedups one rule against another.
        assert LessonWriteOutcome.REFUSED.value == "refused"
        assert LessonWriteOutcome.DEDUPED.value == "deduped"

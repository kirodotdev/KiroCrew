from pathlib import Path

import pytest

from kiro_crew.automatic_routing import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CREW_KNOWLEDGE_QUALITY,
    CREW_NONE,
    CREW_QUALITY_ENGINEERING,
    CREW_SOFTWARE_DELIVERY,
    ROUTE_E2E_VALIDATION,
    ROUTE_FEATURE,
    ROUTE_FULL_QUALITY_REVIEW,
    ROUTE_PRODUCTION_CHANGE,
    ROUTE_QA_PLAN,
    ROUTE_RETRIEVAL_AUDIT,
    ROUTE_RETRIEVAL_AUDIT_RISK,
    ROUTE_SMALL_CHANGE,
    ROUTE_UX_REVIEW,
    classify_clarification_answer,
    classify_message,
)
from kiro_crew.crew_dispatch import automatic_workflow_source
from kiro_crew.crew_registry import (
    INTERNAL_CREW_AGENT_NAMES,
    is_internal_crew_worker,
)
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.workflows.validate import validate


def test_clear_software_requests_select_all_three_routes(tmp_path: Path) -> None:
    project = str(tmp_path)

    small = classify_message("Fix the API bug in this project", project_path=project)
    feature = classify_message("Implement a new feature in the project", project_path=project)
    production = classify_message("Deploy the project to production", project_path=project)

    assert (small.crew_id, small.route, small.confidence) == (
        CREW_SOFTWARE_DELIVERY,
        ROUTE_SMALL_CHANGE,
        CONFIDENCE_HIGH,
    )
    assert (feature.crew_id, feature.route) == (CREW_SOFTWARE_DELIVERY, ROUTE_FEATURE)
    assert (production.crew_id, production.route) == (
        CREW_SOFTWARE_DELIVERY,
        ROUTE_PRODUCTION_CHANGE,
    )
    assert production.approval_required is True


def test_quality_engineering_routes_are_specific_and_high_confidence(tmp_path: Path) -> None:
    project = str(tmp_path)
    cases = (
        ("Create a test plan", ROUTE_QA_PLAN),
        ("Review Playwright browser flow", ROUTE_E2E_VALIDATION),
        ("Review accessibility and usability", ROUTE_UX_REVIEW),
        ("Check release readiness with full validation", ROUTE_FULL_QUALITY_REVIEW),
    )

    for message, route in cases:
        decision = classify_message(message, project_path=project)
        assert (decision.crew_id, decision.route, decision.confidence) == (
            CREW_QUALITY_ENGINEERING,
            route,
            CONFIDENCE_HIGH,
        )
        assert decision.project_path == project


def test_quality_routing_preserves_implementation_precedence_and_question_fallback(
    tmp_path: Path,
) -> None:
    project = str(tmp_path)
    implementation = classify_message("Implement a Playwright browser flow", project_path=project)
    question = classify_message("Can Playwright validate this browser flow?", project_path=project)
    missing_project = classify_message("Create a test plan")

    assert implementation.crew_id == CREW_SOFTWARE_DELIVERY
    assert implementation.route == ROUTE_SMALL_CHANGE
    assert question.crew_id == CREW_NONE
    assert question.route == "none"
    assert question.reason_codes == ("no_high_confidence_route",)
    assert (missing_project.crew_id, missing_project.route, missing_project.confidence) == (
        CREW_QUALITY_ENGINEERING,
        ROUTE_QA_PLAN,
        CONFIDENCE_LOW,
    )
    assert "project_path.required" in missing_project.reason_codes


def test_knowledge_audit_requires_explicit_audit_language() -> None:
    ordinary_search = classify_message("Search my Knowledge base for the auth design")
    audit = classify_message("Audit retrieval ranking in the Knowledge store")
    risk = classify_message("Validate Knowledge retrieval with a read-only WAL risk review")

    assert ordinary_search.crew_id == CREW_NONE
    assert ordinary_search.route == "none"
    assert (audit.crew_id, audit.route) == (CREW_KNOWLEDGE_QUALITY, ROUTE_RETRIEVAL_AUDIT)
    assert (risk.crew_id, risk.route) == (CREW_KNOWLEDGE_QUALITY, ROUTE_RETRIEVAL_AUDIT_RISK)


def test_ambiguous_request_is_low_confidence_and_missing_path_is_not_high() -> None:
    ambiguous = classify_message("Please review this")
    missing_project = classify_message("Fix the code", project_path="relative/project")

    assert ambiguous.confidence == CONFIDENCE_LOW
    assert ambiguous.reason_codes == ("ambiguous_task_intent",)
    assert missing_project.confidence == CONFIDENCE_LOW
    assert "project_path.required" in missing_project.reason_codes


def test_clarification_answer_is_classified_once_and_unresolved_falls_back(tmp_path: Path) -> None:
    project = str(tmp_path)
    code = classify_clarification_answer("Please review this", "Code change", project_path=project)
    default = classify_clarification_answer(
        "Please review this", "Something else", project_path=project
    )
    unresolved = classify_clarification_answer("Please review this", "Maybe", project_path=project)

    assert (code.crew_id, code.route, code.confidence) == (
        CREW_SOFTWARE_DELIVERY,
        ROUTE_SMALL_CHANGE,
        CONFIDENCE_HIGH,
    )
    assert (default.crew_id, default.route, default.confidence) == (
        CREW_NONE,
        "none",
        CONFIDENCE_HIGH,
    )
    assert (unresolved.crew_id, unresolved.confidence) == (CREW_NONE, CONFIDENCE_HIGH)


def test_automatic_workflow_source_uses_existing_native_context_contract() -> None:
    result = validate(automatic_workflow_source())

    assert result.ok, result.errors
    assert result.meta and result.meta["name"] == "automatic-crew-routing"
    assert "workflow_run" not in automatic_workflow_source()


def test_internal_workers_are_namespaced_but_hidden_from_alias_lookup() -> None:
    assert len(INTERNAL_CREW_AGENT_NAMES) == 10
    assert all(name.startswith("kirocrew-") for name in INTERNAL_CREW_AGENT_NAMES)
    assert is_internal_crew_worker("kirocrew-software-delivery-engineer")
    assert is_internal_crew_worker("software-delivery-engineer")
    assert not is_internal_crew_worker("kirocrew")
    assert not is_internal_crew_worker("my-project-agent")


def test_knowledge_store_read_only_mode_fails_closed(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    writable = KnowledgeStore(str(database_path))
    writable.add_item("Title", "Content", "note")
    writable.close()

    read_only = KnowledgeStore(str(database_path), read_only=True)
    try:
        with pytest.raises(Exception, match="readonly"):
            read_only.db.execute("UPDATE items SET title = title")
    finally:
        read_only.close()

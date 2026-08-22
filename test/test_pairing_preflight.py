"""Pure contract tests for Crew-level pairing preflight."""

from __future__ import annotations

import pytest

from kiro_crew.pairing_preflight import (
    build_pairing_prompt,
    build_pairing_question,
    classify_pairing_task,
    parse_pairing_mode,
)


@pytest.mark.parametrize(
    ("message", "reason_code"),
    [
        ("Implement a new feature and add acceptance tests", "behavior_change"),
        ("Find the root cause of this intermittent authentication bug", "unknown_root_cause"),
        ("Refactor the routing boundary and compare two designs", "tradeoff_or_boundary"),
        ("Review the security implications of this data migration", "risk_sensitive"),
        ("I want to practice and understand why this implementation works", "learning_requested"),
    ],
)
def test_classifies_non_trivial_work_conservatively(message: str, reason_code: str) -> None:
    result = classify_pairing_task(message)

    assert result.eligible is True
    assert result.reason_code == reason_code
    assert result.reason


@pytest.mark.parametrize(
    "message",
    [
        "Explain what this function does without changing files",
        "Find references to DashboardState",
        "Fix the typo in this comment",
        "Run the exact command I provided and report its output",
    ],
)
def test_skips_pairing_for_trivial_work(message: str) -> None:
    result = classify_pairing_task(message)

    assert result.eligible is False
    assert result.reason_code == "mechanical_or_lookup"
    assert result.reason


def test_ambiguous_work_fails_closed_as_non_trivial() -> None:
    result = classify_pairing_task("Please review this")

    assert result.eligible is True
    assert result.reason_code == "ambiguous"


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("Guided", "guided"),
        ("ช่วยทำแบบจับคู่ โดยคุณเป็น Navigator", "guided"),
        ("Practice: ให้ฉันเสนอแผนก่อน", "practice"),
        ("คาดการณ์ก่อน แล้วค่อยเฉลย", "practice"),
        ("ทำงานปกติ ไม่ใช้ Pair Programming", "normal"),
        ("normal", "normal"),
    ],
)
def test_parses_explicit_pairing_modes(answer: str, expected: str) -> None:
    assert parse_pairing_mode(answer) == expected


@pytest.mark.parametrize("answer", ["", "maybe", "guided and practice", "whatever"])
def test_rejects_missing_ambiguous_or_unknown_modes(answer: str) -> None:
    assert parse_pairing_mode(answer) is None


def test_question_has_bounded_reason_and_exactly_three_modes() -> None:
    questions = build_pairing_question("มีการเปลี่ยน behavior และต้องมี validation")

    assert len(questions) == 1
    question = questions[0]
    assert question["header"] == "PAIRING"
    assert "มีการเปลี่ยน behavior" in question["question"]
    assert question["multiSelect"] is False
    assert [option["label"] for option in question["options"]] == [
        "Guided",
        "Practice",
        "ทำงานปกติ",
    ]


def test_guided_prompt_contains_one_skill_token_and_p1_context() -> None:
    prompt = build_pairing_prompt("Implement the requested change", "task-123", "guided")

    assert prompt.count("$learning-pairing") == 1
    assert "task.id: task-123" in prompt
    assert "pairing.eligible: true" in prompt
    assert "pairing.mode: guided" in prompt
    assert "pairing.scope: task" in prompt
    assert "pairing.decision_source: user" in prompt
    assert "checkpoint: P1" in prompt
    assert "Implement the requested change" in prompt


def test_practice_prompt_uses_p2() -> None:
    prompt = build_pairing_prompt("Design the next slice", "task-456", "practice")

    assert "pairing.mode: practice" in prompt
    assert "checkpoint: P2" in prompt
    assert "$learning-pairing" in prompt


def test_normal_cannot_be_encoded_as_a_pairing_protocol_prompt() -> None:
    with pytest.raises(ValueError, match="pairing mode"):
        build_pairing_prompt("Do the work normally", "task-789", "normal")


@pytest.mark.parametrize("message", ["hello", "Hi!", "สวัสดี"])
def test_skips_pairing_for_ordinary_greetings(message: str) -> None:
    result = classify_pairing_task(message)

    assert result.eligible is False
    assert result.reason_code == "ordinary_chat"

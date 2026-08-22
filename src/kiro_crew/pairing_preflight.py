"""Pure contracts for the Crew-level pairing preflight."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

PairingMode = Literal["guided", "practice", "normal"]
PAIRING_SKILL = "learning-pairing"
PAIRING_SCOPE = "task"


@dataclass(frozen=True)
class PairingEligibility:
    """The bounded, side-effect-free result of pairing eligibility classification."""

    eligible: bool
    reason_code: str
    reason: str


_REASON_TEXT = {
    "behavior_change": "มีการเปลี่ยน behavior หรือเพิ่มความสามารถและต้องมี validation",
    "unknown_root_cause": "ต้องวิเคราะห์ root cause ก่อนแก้ไขและตรวจผลกระทบ",
    "tradeoff_or_boundary": "มี trade-off หรือการตัดสินใจเกี่ยวกับ boundary ของระบบ",
    "risk_sensitive": "เกี่ยวข้องกับความเสี่ยงด้าน security, data หรือ performance",
    "learning_requested": "ผู้ใช้ระบุว่าต้องการเรียนรู้หรือฝึกระหว่างทำงาน",
    "acceptance_validation": "ต้องกำหนด acceptance criteria หรือ validation หลายขั้นตอน",
    "ambiguous": "รายละเอียดงานยังไม่พอให้ตัดสินอย่างปลอดภัย",
    "mechanical_or_lookup": "เป็นงาน lookup, explanation หรือ mechanical change ที่ตรงไปตรงมา",
}


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def classify_pairing_task(message: str) -> PairingEligibility:
    """Classify one request without I/O, LLM calls, or state mutation.

    The classifier intentionally prefers ``eligible=True`` when intent is not
    clear. A conservative preflight is safer than silently choosing normal work
    for a request that may contain a material decision.
    """

    if not isinstance(message, str) or not message.strip():
        return PairingEligibility(True, "ambiguous", _REASON_TEXT["ambiguous"])

    text = " ".join(message.casefold().split())

    if _contains_any(
        text,
        (
            "root cause",
            "intermittent",
            "flaky",
            "reproduce",
            "debug",
            "unknown bug",
            "ยังไม่ทราบสาเหตุ",
            "หาสาเหตุ",
        ),
    ):
        return PairingEligibility(True, "unknown_root_cause", _REASON_TEXT["unknown_root_cause"])

    if _contains_any(
        text,
        (
            "security",
            "authentication",
            "authorization",
            "data loss",
            "migration",
            "performance",
            "ความปลอดภัย",
            "ข้อมูลหาย",
            "ย้ายข้อมูล",
        ),
    ):
        return PairingEligibility(True, "risk_sensitive", _REASON_TEXT["risk_sensitive"])

    if _contains_any(
        text,
        (
            "refactor",
            "boundary",
            "architecture",
            "architectural",
            "trade-off",
            "tradeoff",
            "compare designs",
            "ออกแบบ",
            "ปรับโครงสร้าง",
            "ขอบเขต module",
        ),
    ):
        return PairingEligibility(
            True, "tradeoff_or_boundary", _REASON_TEXT["tradeoff_or_boundary"]
        )

    if _contains_any(
        text,
        (
            "practice",
            "learn",
            "learning",
            "pair programming",
            "เรียนรู้",
            "ฝึก",
            "อธิบายเหตุผล",
            "เสนอแผน",
            "คาดการณ์ก่อน",
        ),
    ):
        return PairingEligibility(True, "learning_requested", _REASON_TEXT["learning_requested"])

    if _contains_any(
        text,
        (
            "typo",
            "spelling",
            "comment",
            "format",
            "formatting",
            "whitespace",
            "find references",
            "search for",
            "explain",
            "what does",
            "lookup",
            "run the exact command",
            "รันคำสั่งที่ให้",
            "ค้นหา reference",
            "อธิบาย",
        ),
    ):
        return PairingEligibility(
            False, "mechanical_or_lookup", _REASON_TEXT["mechanical_or_lookup"]
        )

    if _contains_any(
        text,
        (
            "implement",
            "feature",
            "behavior",
            "change",
            "bug",
            "fix",
            "add",
            "เพิ่มความสามารถ",
            "เปลี่ยน behavior",
            "แก้ bug",
        ),
    ):
        return PairingEligibility(True, "behavior_change", _REASON_TEXT["behavior_change"])

    if _contains_any(
        text,
        (
            "acceptance test",
            "acceptance criteria",
            "validation",
            "validate",
            "เพิ่ม test",
            "เขียน test",
            "ตรวจสอบผล",
        ),
    ) and _contains_any(
        text,
        (
            "implement",
            "change",
            "feature",
            "fix",
            "เพิ่ม",
            "แก้",
            "เปลี่ยน",
        ),
    ):
        return PairingEligibility(
            True, "acceptance_validation", _REASON_TEXT["acceptance_validation"]
        )

    ordinary_chat = text.strip("!?.,")
    if ordinary_chat in {
        "hello",
        "hi",
        "hey",
        "hello there",
        "hi there",
        "hey there",
        "สวัสดี",
    }:
        return PairingEligibility(False, "ordinary_chat", "เป็นการทักทายทั่วไป")

    return PairingEligibility(True, "ambiguous", _REASON_TEXT["ambiguous"])


def parse_pairing_mode(answer: str) -> PairingMode | None:
    """Parse one explicit mode answer; conflicting answers return ``None``."""

    if not isinstance(answer, str) or not answer.strip():
        return None
    text = " ".join(answer.casefold().split())
    modes: set[PairingMode] = set()

    normal_phrase = _contains_any(
        text,
        (
            "normal",
            "ทำงานปกติ",
            "ไม่ใช้ pairing",
            "ไม่ใช้ pair programming",
        ),
    )
    if normal_phrase:
        modes.add("normal")

    if _contains_any(text, ("practice", "ฝึก", "เสนอแผน", "คาดการณ์ก่อน")):
        modes.add("practice")

    # The normal phrase deliberately suppresses the word "pair" inside
    # "ไม่ใช้ pair programming"; otherwise an explicit normal answer would
    # look like a Guided + normal conflict.
    if not normal_phrase and _contains_any(text, ("guided", "guide", "pair", "จับคู่", "นำทาง")):
        modes.add("guided")

    return next(iter(modes)) if len(modes) == 1 else None


def build_pairing_question(reason: str) -> list[dict[str, Any]]:
    """Build the one-question, three-mode stateless dashboard card."""

    bounded_reason = " ".join(str(reason or "").split())[:240]
    if not bounded_reason:
        bounded_reason = _REASON_TEXT["ambiguous"]
    return [
        {
            "header": "PAIRING",
            "question": (f"งานนี้เป็น non-trivial เพราะ {bounded_reason}\n\n" "เลือกวิธีทำงาน:"),
            "options": [
                {
                    "label": "Guided",
                    "description": "ทำ Pair Programming โดย Kiro เป็น Navigator",
                },
                {
                    "label": "Practice",
                    "description": "ให้คุณเสนอแผนหรือคาดการณ์ก่อน",
                },
                {
                    "label": "ทำงานปกติ",
                    "description": "ไม่ใช้ Pair Programming",
                },
            ],
            "multiSelect": False,
        }
    ]


def build_pairing_prompt(original_message: str, task_id: str, mode: PairingMode) -> str:
    """Encode a Guided/Practice decision for the Default Agent only."""

    if mode not in {"guided", "practice"}:
        raise ValueError("pairing mode must be guided or practice")
    if not isinstance(original_message, str) or not original_message.strip():
        raise ValueError("pairing original message is required")
    if not isinstance(task_id, str) or not task_id.strip() or "\n" in task_id:
        raise ValueError("pairing task id is required")

    checkpoint = "P1" if mode == "guided" else "P2"
    return (
        f"${PAIRING_SKILL}\n\n"
        "[Pairing task context]\n"
        f"task.id: {task_id.strip()}\n"
        "pairing.eligible: true\n"
        f"pairing.mode: {mode}\n"
        f"pairing.scope: {PAIRING_SCOPE}\n"
        "pairing.decision_source: user\n"
        f"checkpoint: {checkpoint}\n\n"
        "Original request:\n"
        f"{original_message.strip()}"
    )


__all__ = [
    "PAIRING_SCOPE",
    "PAIRING_SKILL",
    "PairingEligibility",
    "PairingMode",
    "build_pairing_prompt",
    "build_pairing_question",
    "classify_pairing_task",
    "parse_pairing_mode",
]

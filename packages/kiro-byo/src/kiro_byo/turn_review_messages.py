"""Pure message construction for detached, tool-free post-stage reviews.

The model instructions and ordering are intentionally unchanged from the R5
TurnRunner implementation. This module makes that contract independently testable.
"""
from __future__ import annotations

from typing import Any


def build_review_messages(
    *, agent_prompt: str, runtime_mode_context: str, language_instruction: str,
    role: str, transcript: str, draft: str,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if agent_prompt:
        messages.append({"role": "system", "content": agent_prompt})
    if runtime_mode_context:
        messages.append({"role": "system", "content": runtime_mode_context})
    if language_instruction:
        messages.append({"role": "system", "content": language_instruction})
    messages.append({
        "role": "system",
        "content": (
            f"Kiro BYO detached {role} pass. "
            "Check the draft against the turn transcript. Return only the final answer for the user. "
            + (
                "For verification, preserve the draft unless a material correctness, omission, or reasoning issue needs fixing. "
                if role == "verify" else
                "For final review, actively improve correctness, completeness, clarity, and reasoning where useful. "
            )
            + "Do not claim tools were used in this secondary stage and do not mention this stage. "
            + "Never add actions, file changes, command results, or facts that the turn transcript does not show; "
            "for long agentic work, keep the draft's account of what was actually done and only fix errors, gaps, or unclear conclusions."
        ),
    })
    if language_instruction:
        # Preserve the historical second language instruction, at the output boundary.
        messages.append({"role": "system", "content": language_instruction})
    messages.append({
        "role": "user",
        "content": f"TURN TRANSCRIPT:\n{transcript}\n\nDRAFT ANSWER:\n{draft}",
    })
    return messages

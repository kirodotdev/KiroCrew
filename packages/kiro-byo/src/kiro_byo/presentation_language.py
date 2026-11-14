"""Single per-turn language decision for BYO-produced user-visible text.

The host owns its own UI locale. This policy never mutates ACP tool inputs,
raw provider reasoning or host-owned activity labels. Explicitly configured
independent language controls remain supported for backward compatibility.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .i18n import normalize_ui_language
from .reasoning_language import (
    detect_conversation_language, explicit_response_language,
    normalize_reasoning_language, normalize_supported_language,
    reasoning_language_instruction, response_language_instruction,
)


@dataclass(frozen=True)
class PresentationLanguageDecision:
    language: str
    source: str
    response_language: str
    reasoning_language: str
    tool_display_language: str
    reasoning_source: str
    tool_display_source: str
    reasoning_instruction: str
    response_instruction: str


def resolve_presentation_language(
    current_text: str, history: Iterable[Any] = (), *,
    ui_preference: str = "auto", response_preference: str = "auto",
    reasoning_preference: str = "auto",
) -> PresentationLanguageDecision:
    """Choose once, then apply to automatic output channels.

    Priority for user-authored natural language:
    explicit request > fixed answer choice > fixed UI choice > conversation.
    A separately fixed reasoning choice intentionally overrides reasoning only.
    A fixed UI choice continues to control tool titles (not the host's own UI).
    """
    ui = normalize_ui_language(ui_preference)
    answer = normalize_supported_language(response_preference)
    reasoning = normalize_reasoning_language(reasoning_preference)
    explicit = explicit_response_language(current_text)
    if explicit:
        language, source = explicit, "explicit-user-request"
    elif answer != "auto":
        language, source = answer, "response-preference"
    elif ui != "auto":
        language, source = ui, "ui-preference"
    else:
        language, source = detect_conversation_language(current_text, history)

    visible_reasoning = reasoning if reasoning != "auto" else language
    reasoning_source = "reasoning-preference" if reasoning != "auto" else "follow-presentation"
    if ui != "auto":
        display, display_source = ui, "ui-preference"
    else:
        try:
            display = normalize_ui_language(language)
        except ValueError:
            display = "en"  # BYO ships only five locales; no OS-induced drift.
        display_source = "follow-presentation" if display == language else "unsupported-locale-fallback"

    return PresentationLanguageDecision(
        language=language, source=source,
        response_language=language, reasoning_language=visible_reasoning,
        tool_display_language=display,
        reasoning_source=reasoning_source, tool_display_source=display_source,
        reasoning_instruction=reasoning_language_instruction(visible_reasoning),
        response_instruction=response_language_instruction(language),
    )

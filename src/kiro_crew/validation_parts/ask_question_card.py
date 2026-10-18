"""The ``ask_question`` card: its bounds, its tool schema and its payload validator.

The ``_ASK_*`` bounds have one definition, here: ``ASK_QUESTION_SCHEMA``,
:func:`validate_ask_user_question` and the dashboard's answer route read them, and
:func:`format_ask_answers` renders the answered tool result within the same budget.
``website/src/test/askQuestionTool.test.ts`` reads the bounds from this file.

Callers import these names from :mod:`kiro_crew.validation`, which serves them; this
module never imports that facade.
"""

from __future__ import annotations

import json
from typing import Iterable

from kiro_crew.session_directive import MAX_TOOL_RESULT_CHARS
from kiro_crew.validation_parts.field_engine import FieldSpec, ToolSchema, ValidationError

# Bounds for the question-card payload, shared by ASK_QUESTION_SCHEMA (agent-facing
# arg check) and validate_ask_user_question (authoritative payload normalization).
_ASK_MAX_QUESTIONS = 4
_ASK_MAX_OPTIONS = 6
_ASK_MAX_QUESTION_LEN = 500
_ASK_MAX_HEADER_LEN = 50
_ASK_MAX_LABEL_LEN = 200
_ASK_MAX_DESC_LEN = 500
# The answer side is bounded too: answers are echoed verbatim into the agent's
# transcript, so an oversized custom answer would consume model context. The
# bound is derived so that the worst-case tool result (the header line, then
# one '"<question>" -> "<answer>"' line per question at the max question length)
# fits MAX_TOOL_RESULT_CHARS; the aggregate guard in the answer handler stays
# as the backstop for JSON escaping or redaction growing the text past this.
_ASK_ANSWERS_HEADER = "User has answered your questions:"
#: Between the two JSON strings of a pair. Neither `=` nor `:`: the transport
#: scrub's key=value patterns accept a quote and either of those after a key
#: name, so they would join a question ending in a key name to its answer.
_ASK_PAIR_SEPARATOR = " -> "
# Newline, the quotes around key and answer, and the separator between them.
_ASK_ANSWER_LINE_OVERHEAD = len(f'\n""{_ASK_PAIR_SEPARATOR}""') + _ASK_MAX_QUESTION_LEN
_ASK_MAX_ANSWER_LEN = (
    MAX_TOOL_RESULT_CHARS
    - len(_ASK_ANSWERS_HEADER)
    - _ASK_MAX_QUESTIONS * _ASK_ANSWER_LINE_OVERHEAD
) // _ASK_MAX_QUESTIONS

#: One wait slice of a blocking ask, fixed server-side and shared by the
#: gateway's wait route and the MCP tool's deferred ask step. A step holds one
#: of DEFERRED_STEP_WORKERS threads for one slice, so with DEFERRED_PARKED_MAX
#: cards parked the worst gap between two polls of one card is
#: ceil(64 / 8) * 5 = 40s, well under the dashboard's 120s answer grace
#: (_AGENT_ASK_STALE_SECS); at 20s the same queue would take 160s and an answer
#: could go stale before its card was polled again. At 2s an idle card would cost a
#: request every 2s, 32 requests a second at 64 cards; at 5s it is one request
#: every 5s. The keepalive between slices still resets the ACP tool-stall
#: watchdog long before its 600s.
ASK_WAIT_SLICE_SECS = 5


def format_ask_answers(pairs: Iterable[tuple[str, str]]) -> str:
    """The answered tool result: the header, then one ``"<question>" -> "<answer>"`` line per pair."""
    # Imported here: kiro_crew.agent_sdk pulls in the ACP stack this module otherwise avoids.
    # The transport scrubs the serialized result again, and a scrub that cuts through
    # a JSON escape leaves a line that does not decode. Scrubbing the decoded values
    # first leaves it nothing to match, and the size guard sizes this same text.
    from kiro_crew.agent_sdk import tool_row_text as _scrub

    lines = [_ASK_ANSWERS_HEADER]
    for q, a in pairs:
        # JSON-escaped so a quote or newline in either side cannot forge another pair.
        lines.append(
            json.dumps(_scrub(q), ensure_ascii=False)
            + _ASK_PAIR_SEPARATOR
            + json.dumps(_scrub(a), ensure_ascii=False)
        )
    return "\n".join(lines)


# ask_question opens a BLOCKING dashboard question card: the MCP tool holds the
# call open and returns the answers as its result (see
# mcp_tools.control.ask_question). `questions` is only shape-checked here (a
# bounded list); the per-question/per-option limits are enforced server-side by
# validate_ask_user_question, which is the single source of truth for the card
# payload.
ASK_QUESTION_SCHEMA = ToolSchema(
    tool_name="ask_question",
    fields=[
        FieldSpec("questions", list, required=True, max_items=_ASK_MAX_QUESTIONS),
        # Accepted but IGNORED: the tool opens the card with only `questions`,
        # so nothing downstream reads a timeout. The bound still
        # mirrors DashboardState._QUESTION_TIMEOUT_MAX, which governs the legacy
        # blocking POST /api/ask-question path. Kept lenient rather than removed
        # so a caller still passing it gets its card instead of a validation
        # error, while the tool's inputSchema does not advertise it — a knob
        # with no effect should not be offered to a model.
        FieldSpec("timeout_secs", int, min_val=15, max_val=540),
    ],
)


# ── AskUserQuestion Schema Validation ──
# Bounds live near ASK_QUESTION_SCHEMA above (single definition, two consumers).


def validate_ask_user_question(raw: object) -> list[dict]:
    """Validate and normalize AskUserQuestion tool input.

    Returns a list of validated question dicts ready for broadcast.
    Raises ValidationError if the top-level structure is invalid.
    Skips individual malformed questions/options defensively.
    """
    if not isinstance(raw, dict):
        raise ValidationError("tool_input", "must be a JSON object")
    questions = raw.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValidationError("questions", "must be a non-empty list")

    result: list[dict] = []
    seen_questions: set[str] = set()
    for q in questions[:_ASK_MAX_QUESTIONS]:
        if not isinstance(q, dict):
            continue
        qt = str(q.get("question") or "")[:_ASK_MAX_QUESTION_LEN]
        if not qt:
            continue
        qh = str(q.get("header") or "")[:_ASK_MAX_HEADER_LEN]
        raw_opts = q.get("options")
        if not isinstance(raw_opts, list):
            continue
        opts: list[dict] = []
        seen_labels: set[str] = set()
        for o in raw_opts[:_ASK_MAX_OPTIONS]:
            if not isinstance(o, dict):
                continue
            label = str(o.get("label") or "")[:_ASK_MAX_LABEL_LEN]
            if not label:
                continue
            # Option labels are their end-to-end identity: the frontend keys
            # selection state by label and sends labels back as the answer.
            # Descriptions are display-only, so duplicate normalized labels
            # would make distinct-looking rows submit the same value.
            norm_label = " ".join(label.split()).casefold()
            if norm_label in seen_labels:
                raise ValidationError("questions", "duplicate option labels are not allowed")
            seen_labels.add(norm_label)
            desc = str(o.get("description") or "")[:_ASK_MAX_DESC_LEN]
            opts.append({"label": label, "description": desc})
        if not opts:
            continue
        # Answers are keyed by question text end-to-end (the frontend builds an
        # answer map keyed on the question string, and the tool result echoes
        # that map). Two questions with the same text collapse to one entry —
        # the user answers both but only the last reaches the blocked agent.
        # Reject duplicates (normalized on whitespace/case) so a multi-question
        # card can never silently drop an answer.
        norm = " ".join(qt.split()).casefold()
        if norm in seen_questions:
            raise ValidationError("questions", "duplicate question text is not allowed")
        seen_questions.add(norm)
        result.append(
            {
                "question": qt,
                "header": qh,
                "options": opts,
                "multiSelect": bool(q.get("multiSelect")),
            }
        )
    if not result:
        raise ValidationError("questions", "no valid questions after validation")
    return result

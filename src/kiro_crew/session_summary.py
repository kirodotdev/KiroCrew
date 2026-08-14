"""Transcript extraction and payload shaping for intent-level session summaries.

Two jobs, both deliberately free of any model call so they can be tested
directly:

* :func:`extract_turns` turns raw transcript records into the bounded input a
  summarization pass reads.
* :func:`normalize_payload` validates and clamps whatever the model returns
  before it reaches the sidecar cache, so a malformed generation degrades to a
  smaller valid summary instead of poisoning the panel.

Why the input is shaped the way it is: a session transcript is dominated by
assistant records and their tool payloads, while intent lives almost entirely in
the (small) user messages. Reading ``role``/``content`` only, keeping user text
whole and excerpting assistant text, reproduces the shape that worked when the
prompt was prototyped against real sessions -- around 1% of a transcript's bytes
-- and drops tool output entirely, which added nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

# Roles worth reading. Tool rows carry display titles and raw payloads that the
# assistant has already distilled, and error rows duplicate content that shows
# up in the following assistant turn.
_READ_ROLES = frozenset({"user", "assistant"})

# A record can carry role "user" without a human having typed it: automation
# injects cron notifications, subagent completions, auto-nudge cycles, tool
# refusals and restored webhook context under that role. Counting one as intent
# invents a goal the user never had, so they are marked and excluded from the
# intent signal while staying visible as context.
_INJECTED_PREFIXES = (
    "[cron notification",
    "[subagent completion event]",
    "[subagent batch completion event]",
    "[auto-nudge cycle",
    "[tool refusal",
    "[tool stall",
    "=== restored context",
    "[system]",
)

# A pasted stack trace or log dump under role "user" is not a goal either, but
# it is only recognizable by shape. Cap user text so one paste cannot crowd out
# the rest of the session; intent survives truncation, volume does not help.
_MAX_USER_CHARS = 4000


@dataclass
class TranscriptTurn:
    """One transcript row, reduced to what a summarization pass needs."""

    index: int
    """1-based position among the rows that were kept."""

    role: str
    text: str
    ts: str | None = None

    user_turn: int | None = None
    """1-based index among user turns, or None for assistant rows.

    Intent ranges are expressed in user turns because that is the unit a person
    recognizes when they read the summary back.
    """

    injected: bool = False
    """True when this row came from automation rather than the person."""

    repeat_of: int | None = None
    """Set when this row repeats the previous user row verbatim.

    A resend looks identical to insistence in a raw transcript, and reading it
    as insistence produces "the user asked repeatedly, so it was ignored" for a
    request that already succeeded.
    """

    truncated: bool = False


def _excerpt(text: str, keep_each_end: int) -> str:
    """Keep the head and tail of *text*, marking the elision.

    Assistant messages open with the outcome and close with what was offered
    next, so both ends carry signal while the middle is usually a diff or a
    listing.
    """
    if len(text) <= keep_each_end * 2:
        return text
    dropped = len(text) - keep_each_end * 2
    head, tail = text[:keep_each_end], text[-keep_each_end:]
    return f"{head}\n[... {dropped} characters omitted ...]\n{tail}"


def _is_injected(text: str) -> bool:
    head = text.lstrip().lower()
    return any(head.startswith(p) for p in _INJECTED_PREFIXES)


def extract_turns(
    records: list[dict],
    *,
    assistant_excerpt_chars: int = 400,
    max_user_chars: int = _MAX_USER_CHARS,
) -> list[TranscriptTurn]:
    """Reduce raw transcript *records* to the rows a summarization pass reads.

    Reads ``role``, ``content`` and ``ts`` only. Every other field -- notably the
    ``meta`` blob on assistant rows, which can be most of a transcript's bytes --
    is ignored rather than excerpted, because none of it reaches the model.
    """
    turns: list[TranscriptTurn] = []
    user_count = 0
    last_user_text: str | None = None
    last_user_index: int | None = None

    for rec in records:
        if not isinstance(rec, dict):
            continue
        role = rec.get("role")
        if role not in _READ_ROLES:
            continue
        content = rec.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        ts = rec.get("ts") if isinstance(rec.get("ts"), str) else None

        if role == "user":
            injected = _is_injected(content)
            text = content if len(content) <= max_user_chars else content[:max_user_chars]
            truncated = len(content) > max_user_chars
            repeat_of = None
            if not injected:
                user_count += 1
                normalized = " ".join(text.split())
                if last_user_text is not None and normalized == last_user_text:
                    repeat_of = last_user_index
                last_user_text = normalized
                last_user_index = user_count
            turns.append(
                TranscriptTurn(
                    index=len(turns) + 1,
                    role="user",
                    text=text,
                    ts=ts,
                    user_turn=None if injected else user_count,
                    injected=injected,
                    repeat_of=repeat_of,
                    truncated=truncated,
                )
            )
            continue

        excerpt = _excerpt(content, assistant_excerpt_chars)
        turns.append(
            TranscriptTurn(
                index=len(turns) + 1,
                role="assistant",
                text=excerpt,
                ts=ts,
                truncated=excerpt != content,
            )
        )

    return turns


def count_user_turns(turns: list[TranscriptTurn]) -> int:
    """Number of genuine user turns, excluding automation injections."""
    return sum(1 for t in turns if t.role == "user" and not t.injected)


def last_activity_ts(turns: list[TranscriptTurn]) -> str | None:
    """Timestamp of the most recent row that carries one."""
    for turn in reversed(turns):
        if turn.ts:
            return turn.ts
    return None


def render_input(turns: list[TranscriptTurn]) -> str:
    """Render *turns* as the text block a summarization pass reads.

    Annotations are inline and explicit -- an injected row and a resend are
    labelled rather than silently dropped, so the pass can see the shape of the
    conversation without being misled by it.
    """
    lines: list[str] = []
    for turn in turns:
        if turn.role == "user":
            if turn.injected:
                lines.append("[automation, not the user]")
                lines.append(turn.text)
            else:
                label = f"USER (turn {turn.user_turn})"
                if turn.repeat_of is not None:
                    label += f" [resend of turn {turn.repeat_of}, not a new request]"
                if turn.ts:
                    label += f" [{turn.ts}]"
                lines.append(label)
                lines.append(turn.text)
        else:
            lines.append("ASSISTANT [excerpt]" if turn.truncated else "ASSISTANT")
            lines.append(turn.text)
        lines.append("")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Payload shaping
# ---------------------------------------------------------------------------

_PROGRESS_STATES = frozenset({"active", "completed", "abandoned"})

STATE_DONE = "done"
STATE_NEEDS_YOU = "needs-you"
STATE_IN_PROGRESS = "in-progress"
STATE_DROPPED = "dropped"


def derive_state(status: str, verified: bool | None) -> str:
    """Collapse the two status axes into the single word the panel shows.

    Progress and verification are stored separately because they disagree in the
    cases that matter most on re-entry: work whose discussion ended while the
    goal was never actually reached ("answered but not fixed", "merged but never
    looked at"). One field cannot express that, and a summary that renders such
    an intent as finished actively helps the reader forget it.
    """
    if status == "abandoned":
        return STATE_DROPPED
    if status == "completed":
        return STATE_NEEDS_YOU if verified is False else STATE_DONE
    return STATE_IN_PROGRESS


@dataclass
class NextStep:
    what: str
    why: str = ""
    expect: str = ""


@dataclass
class Intent:
    title: str
    initial_intent: str = ""
    progress: list[str] = field(default_factory=list)
    next_steps: list[NextStep] = field(default_factory=list)
    ranges: list[list[int]] = field(default_factory=list)
    status: str = "active"
    verified: bool | None = None
    origin_turn: int | None = None

    @property
    def last_touched_turn(self) -> int:
        """Highest user turn this intent covers -- what the panel sorts on.

        Derived rather than stored: ranges are the source of truth, and a
        separate field could disagree with them.
        """
        return max((r[-1] for r in self.ranges if r), default=0)

    @property
    def state(self) -> str:
        return derive_state(self.status, self.verified)


def _coerce_ranges(raw: object) -> list[list[int]]:
    """Accept a list of turn ranges; drop anything that is not a real range.

    Ranges are a list rather than one interval because an intent can go dormant
    and resume, and because one intent can sit inside another's span. Overlap is
    allowed on purpose -- rejecting it would force a lie about what happened.
    """
    out: list[list[int]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            start, end = item
            if isinstance(start, int) and isinstance(end, int) and start >= 1 and end >= start:
                out.append([start, end])
        elif isinstance(item, int) and item >= 1:
            out.append([item, item])
    return out


def _coerce_next_steps(raw: object) -> list[NextStep]:
    out: list[NextStep] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(NextStep(what=item.strip()))
        elif isinstance(item, dict):
            what = item.get("what")
            if isinstance(what, str) and what.strip():
                out.append(
                    NextStep(
                        what=what.strip(),
                        why=str(item.get("why") or "").strip(),
                        expect=str(item.get("expect") or "").strip(),
                    )
                )
    return out


def _coerce_intent(raw: object) -> Intent | None:
    if not isinstance(raw, dict):
        return None
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    status = raw.get("status")
    if status not in _PROGRESS_STATES:
        status = "active"
    verified = raw.get("verified")
    if not isinstance(verified, bool):
        verified = None
    origin = raw.get("origin_turn")
    origin_turn = origin if isinstance(origin, int) and origin >= 1 else None
    raw_progress = raw.get("progress")
    progress: list[str] = []
    if isinstance(raw_progress, list):
        progress = [p.strip() for p in raw_progress if isinstance(p, str) and p.strip()]
    return Intent(
        title=title.strip(),
        initial_intent=str(raw.get("initial_intent") or "").strip(),
        progress=progress,
        next_steps=_coerce_next_steps(raw.get("next_steps")),
        ranges=_coerce_ranges(raw.get("ranges")),
        status=status,
        verified=verified,
        origin_turn=origin_turn,
    )


def redact_payload(value: Any) -> Any:
    """Recursively redact credentials and exfiltration URLs in a summary payload.

    The payload is model output derived from transcript text, so any secret or
    beacon URL that appeared in the conversation can be reproduced inside it.
    The sidecar is read straight back to the dashboard, so redaction has to
    happen before the write, not at render time -- the same
    ``redact_credentials`` + ``redact_exfiltration_urls`` chain every other
    LLM-controlled value in this codebase is put through before it is cached.

    Recursive because the payload is nested (intents -> next_steps -> strings);
    a single top-level pass would leave every field the panel actually renders
    unredacted.
    """
    if isinstance(value, str):
        cleaned, _ = redact_credentials(value)
        cleaned, _ = redact_exfiltration_urls(cleaned)
        return cleaned
    if isinstance(value, list):
        return [redact_payload(v) for v in value]
    if isinstance(value, dict):
        return {k: redact_payload(v) for k, v in value.items()}
    return value


def normalize_payload(
    raw: object,
    *,
    max_intents: int = 8,
    max_constraints: int = 5,
) -> dict | None:
    """Validate and clamp a generated summary into the stored payload shape.

    Returns None when nothing usable survives, so the caller keeps the previous
    cached summary rather than replacing it with an empty one. Intents are
    ordered most-recently-touched first, which is the order the panel renders
    and therefore the order the payload should already be in.
    """
    if not isinstance(raw, dict):
        return None
    intents = [i for i in (_coerce_intent(x) for x in raw.get("intents", []) or []) if i]
    if not intents:
        return None
    intents.sort(key=lambda i: i.last_touched_turn, reverse=True)
    if len(intents) > max_intents:
        # Trim from the tail: those are the oldest-touched, which the panel
        # collapses to a single line anyway.
        intents = intents[:max_intents]

    constraints_raw = raw.get("constraints")
    constraints: list[str] = []
    if isinstance(constraints_raw, list):
        for c in constraints_raw:
            if isinstance(c, str) and c.strip():
                constraints.append(c.strip())
    constraints = constraints[:max_constraints]

    return redact_payload(
        {
            "intents": [
                {
                    "title": i.title,
                    "initial_intent": i.initial_intent,
                    "progress": i.progress,
                    "next_steps": [
                        {"what": s.what, "why": s.why, "expect": s.expect} for s in i.next_steps
                    ],
                    "ranges": i.ranges,
                    "status": i.status,
                    "verified": i.verified,
                    "state": i.state,
                    "last_touched_turn": i.last_touched_turn,
                    "origin_turn": i.origin_turn,
                }
                for i in intents
            ],
            "constraints": constraints,
        }
    )

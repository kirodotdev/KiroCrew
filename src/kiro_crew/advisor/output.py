"""Strict validation of reviewer output.

The reviewer must return a machine-readable envelope; the host never injects
unvalidated reviewer prose into the primary session. Malformed output raises
:class:`MalformedReviewerOutput`, which callers surface as supervision
degradation rather than advice.

Envelope shape (version 1)::

    {"version": 1, "notes": [{"severity": "nit|concern|blocker",
                              "text": "...", "evidence": "optional ref"}]}
"""

from __future__ import annotations

from dataclasses import dataclass

#: Current reviewer envelope schema version.
ENVELOPE_VERSION = 1

#: Upper bound on one note's text. A note is advice, not a report; anything
#: longer is malformed rather than silently truncated, so a rambling
#: reviewer degrades visibly instead of flooding the parent transcript.
NOTE_TEXT_MAX_CHARS = 2000

SEVERITIES = ("nit", "concern", "blocker")


class MalformedReviewerOutput(ValueError):
    """The reviewer returned something other than a valid envelope."""


@dataclass(frozen=True)
class AdvisorNote:
    """One validated advisory note."""

    severity: str
    text: str
    evidence: str | None = None


def parse_reviewer_envelope(raw: object) -> list[AdvisorNote]:
    """Validate a reviewer envelope and return its notes.

    Raises :class:`MalformedReviewerOutput` for anything that is not a
    version-1 envelope with a well-formed notes list.
    """
    if not isinstance(raw, dict):
        raise MalformedReviewerOutput(
            f"reviewer output is {type(raw).__name__}, not an envelope object"
        )
    version = raw.get("version")
    if version != ENVELOPE_VERSION:
        raise MalformedReviewerOutput(f"unsupported reviewer envelope version: {version!r}")
    notes_raw = raw.get("notes")
    if not isinstance(notes_raw, list):
        raise MalformedReviewerOutput(f"envelope notes is {type(notes_raw).__name__}, not a list")
    return [_parse_note(item, index) for index, item in enumerate(notes_raw)]


def _parse_note(item: object, index: int) -> AdvisorNote:
    if not isinstance(item, dict):
        raise MalformedReviewerOutput(f"note {index} is {type(item).__name__}, not an object")
    severity = item.get("severity")
    if severity not in SEVERITIES:
        raise MalformedReviewerOutput(f"note {index} has invalid severity: {severity!r}")
    text = item.get("text")
    if not isinstance(text, str):
        raise MalformedReviewerOutput(f"note {index} has no text")
    if len(text) > NOTE_TEXT_MAX_CHARS:
        raise MalformedReviewerOutput(f"note {index} text exceeds {NOTE_TEXT_MAX_CHARS} chars")
    evidence = item.get("evidence")
    if evidence is not None and not isinstance(evidence, str):
        raise MalformedReviewerOutput(f"note {index} evidence is not a string")
    return AdvisorNote(severity=severity, text=text, evidence=evidence)

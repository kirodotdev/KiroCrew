"""Business validation for Slack message text and Block Kit payloads.

This validates a message BEFORE it is sent, against the limits Kiro Crew's own
send path actually enforces — not against Slack's raw platform maxima. Two
distinctions matter and are wrong in most naive implementations:

- **Text is bounded by the SHIPPED send path, not the platform ceiling.**
  Kiro Crew splits an outgoing message at ``SLACK_MSG_LIMIT`` (3900) in
  ``slack/format.py``; that is the real cut point every reply goes through. The
  platform's ``chat.postMessage`` ``text`` accepts far more (~40000, and
  ``msg_too_long`` above that), but a validator that advertised the platform
  ceiling would green-light a 39000-char message the renderer would never send
  whole. So the single source for the text limit here is the imported
  ``SLACK_MSG_LIMIT``, and this module deliberately does NOT re-declare or
  raise it. A message over the limit is reported as needing a split (a caller
  can split with ``format.split_message``), not as a hard rejection — long text
  is normal and handled, whereas a malformed block is a real error.

- **Block Kit has structural caps that ARE hard errors.** A message carries at
  most 50 blocks; a ``section``'s ``text`` is capped; ``blocks`` must be a list
  of typed objects. These come from the Block Kit reference
  (https://api.slack.com/reference/block-kit/blocks) and a violation is a
  request Slack rejects (``invalid_blocks`` / ``invalid_blocks_format``), so this
  module returns them as errors, not advisories.

Pure logic: shapes in, a :class:`ValidationReport` out. No IO, no send.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kiro_crew.slack.format import SLACK_MSG_LIMIT

# --- Block Kit structural limits, from the reference ------------------------
#: Maximum number of blocks in one message (Block Kit `blocks` array cap).
MAX_BLOCKS_PER_MESSAGE = 50
#: Maximum number of blocks in a modal / home-tab view surface.
MAX_BLOCKS_PER_VIEW = 100
#: `section` block `text` character cap.
MAX_SECTION_TEXT_CHARS = 3000
#: `header` block `text` (plain_text) character cap.
MAX_HEADER_TEXT_CHARS = 150
#: A `context` block holds at most this many elements.
MAX_CONTEXT_ELEMENTS = 10
#: An `actions` block holds at most this many interactive elements.
MAX_ACTIONS_ELEMENTS = 25

#: The text limit is the SHIPPED send-path split point, imported (never
#: re-declared) so this module and the renderer cannot disagree.
TEXT_SPLIT_LIMIT = SLACK_MSG_LIMIT


@dataclass
class ValidationReport:
    """The outcome of validating one message's text and/or blocks.

    ``errors`` — hard problems Slack would reject (malformed blocks, over-cap
    structures). A non-empty ``errors`` means do not send as-is.
    ``needs_split`` — the text exceeds the shipped split limit and must be split
    before sending; this is NOT an error (long text is normal).
    ``text_len`` — the measured text length, for a caller that wants to log it.
    """

    errors: list[str] = field(default_factory=list)
    needs_split: bool = False
    text_len: int = 0

    @property
    def ok(self) -> bool:
        """True when there is nothing Slack would reject (splitting is allowed)."""
        return not self.errors


def validate_text(text: str) -> ValidationReport:
    """Validate outgoing message text against the shipped send-path limit.

    Over-limit text sets ``needs_split`` (a caller splits with
    ``format.split_message``); it is never reported as the platform ~40000
    ceiling. Empty text is an error only for a text-only message — a
    blocks-carrying message may legitimately have empty top-level text, so that
    case is handled by :func:`validate_message`, not here.
    """
    report = ValidationReport(text_len=len(text))
    if len(text) > TEXT_SPLIT_LIMIT:
        report.needs_split = True
    return report


def validate_blocks(blocks: Any, *, surface: str = "message") -> ValidationReport:
    """Validate a Block Kit ``blocks`` payload's structure.

    ``surface`` selects the block-count cap: ``"message"`` (50) or ``"view"``
    (100, for modals / home tabs). Returns hard ``errors`` for anything Slack
    would reject: a non-list or empty payload, an over-cap count, a block that is
    not a dict, a block missing ``type``, and the per-block text/element caps this
    module knows.
    """
    report = ValidationReport()
    if not isinstance(blocks, list):
        report.errors.append(f"blocks must be a list, got {type(blocks).__name__}")
        return report
    if not blocks:
        report.errors.append("blocks must contain at least one block")

    cap = MAX_BLOCKS_PER_VIEW if surface == "view" else MAX_BLOCKS_PER_MESSAGE
    if len(blocks) > cap:
        report.errors.append(f"too many blocks: {len(blocks)} > {cap} (surface={surface})")

    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            report.errors.append(f"block[{index}] must be an object, got {type(block).__name__}")
            continue
        btype = block.get("type")
        if not btype or not isinstance(btype, str):
            report.errors.append(f"block[{index}] is missing a string 'type'")
            continue
        _validate_one_block(index, btype, block, report)

    return report


def _text_value(node: Any) -> str | None:
    """Extract the string from a Block Kit text object, or ``None`` if malformed."""
    if isinstance(node, dict):
        val = node.get("text")
        return val if isinstance(val, str) else None
    return None


def _validate_one_block(index: int, btype: str, block: dict, report: ValidationReport) -> None:
    """Per-block-type structural checks for the block types this module knows.

    Unknown block types are NOT rejected — Slack adds block types over time, and
    a validator that rejected every type it had not heard of would block valid
    payloads. It only enforces the caps it can state from the reference.
    """
    if btype == "section":
        text_valid = False
        if "text" in block:
            text = _text_value(block["text"])
            if text is None:
                report.errors.append(f"block[{index}] section has malformed required field 'text'")
            else:
                text_valid = True
                if len(text) > MAX_SECTION_TEXT_CHARS:
                    report.errors.append(
                        f"block[{index}] section text {len(text)} > "
                        f"{MAX_SECTION_TEXT_CHARS} chars"
                    )

        fields = block.get("fields")
        fields_valid = isinstance(fields, list) and bool(fields)
        if "fields" in block and not fields_valid:
            report.errors.append(
                f"block[{index}] section has malformed field 'fields'; expected a non-empty list"
            )
        if not text_valid and not fields_valid:
            report.errors.append(
                f"block[{index}] section requires valid 'text' or non-empty 'fields'"
            )
    elif btype == "header":
        text = _text_value(block.get("text"))
        if text is None:
            report.errors.append(
                f"block[{index}] header has missing or malformed required field 'text'"
            )
        elif len(text) > MAX_HEADER_TEXT_CHARS:
            report.errors.append(
                f"block[{index}] header text {len(text)} > " f"{MAX_HEADER_TEXT_CHARS} chars"
            )
    elif btype in {"context", "actions"}:
        elements = block.get("elements")
        if not isinstance(elements, list):
            report.errors.append(
                f"block[{index}] {btype} has missing or malformed required field 'elements'"
            )
            return
        cap = MAX_CONTEXT_ELEMENTS if btype == "context" else MAX_ACTIONS_ELEMENTS
        if len(elements) > cap:
            report.errors.append(f"block[{index}] {btype} has {len(elements)} elements > {cap}")


def validate_message(
    *, text: str = "", blocks: Any = None, surface: str = "message"
) -> ValidationReport:
    """Validate a whole outgoing message (text and/or blocks together).

    A message must carry SOMETHING: empty text with no blocks is an error
    (Slack's ``no_text``). When blocks are present, empty top-level text is
    fine. Merges the text report (split signal) and the block report (hard
    errors) into one.
    """
    text_report = validate_text(text)
    report = ValidationReport(needs_split=text_report.needs_split, text_len=text_report.text_len)

    has_blocks = blocks is not None
    if not text and not has_blocks:
        report.errors.append("message has neither text nor blocks (no_text)")
        return report

    if has_blocks:
        block_report = validate_blocks(blocks, surface=surface)
        report.errors.extend(block_report.errors)

    return report

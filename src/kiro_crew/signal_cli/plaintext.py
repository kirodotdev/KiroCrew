"""Markdown flattening and message splitting for Signal.

signal-cli sends a message body as plain text (Signal's own styling travels in
separate ``textStyles`` ranges, which this channel does not emit), so an answer
written in markdown would arrive as literal asterisks and backticks. The
conversion is exactly the iMessage channel's -- fenced code passes through
verbatim, splitting runs last and respects grapheme clusters -- so it is
re-exported rather than copied: one flattener, one set of tests for its edge
cases.
"""

from __future__ import annotations

from kiro_crew.imessage.plaintext import chunk_plaintext, to_plaintext

__all__ = ["chunk_plaintext", "to_plaintext"]

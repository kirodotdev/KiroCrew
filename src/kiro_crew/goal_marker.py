"""The standalone ``[GOAL: ...]`` line protocol: its scanner and the projection boundary.

A goal suggestion is one standalone line that only the full dashboard chat renders,
as a review card. Every plain-text projection removes it through
:func:`strip_control_comments`, which strips the goal lines and then the trailing
control-tag lines :func:`kiro_crew.constants.strip_trailing_control_tags` owns.

:mod:`kiro_crew.constants` is the import path of both public names here. This module
imports only ``re`` at load, so constants imports it at its head and stays a leaf;
:func:`strip_control_comments` reads the control-tag grammar from constants when it
runs.
"""

from __future__ import annotations

import re

# A goal suggestion is a single standalone line. It is rendered as a card only
# by the full dashboard chat, but every transcript projection must remove the
# protocol line rather than expose it as prose. This bounded scanner, rather than
# a pattern with overlapping whitespace quantifiers, keeps an unterminated marker
# linear in the length of an untrusted assistant reply.
_GOAL_PREFIX = "[GOAL:"
#: Fast-path probe: a text without the prefix is returned as the same object, so
#: marker-free output stays byte-identical and a streaming frame costs one search.
_GOAL_PREFIX_PROBE = re.compile(r"\[goal:", re.IGNORECASE)
_GOAL_BODY_DELIMITER = re.compile(r"[\[\]\r]")


def _is_goal_marker_line(line: str) -> bool:
    """Return whether *line* is one complete standalone goal marker.

    A trailing ``\r`` is the CRLF line terminator, not content, so a reply with
    Windows line endings strips the same as one without.
    """
    start = 0
    while start < len(line) and line[start] in " \t":
        start += 1
    if line[start : start + len(_GOAL_PREFIX)].upper() != _GOAL_PREFIX:
        return False

    body_start = start + len(_GOAL_PREFIX)
    # The closer is the line's last ``]``; only spaces, tabs and a CRLF ``\r``
    # may follow it, matching the frontend parser.
    close = len(line.rstrip(" \t\r")) - 1
    if close <= body_start or line[close] != "]":
        return False
    # The objective's own brackets must balance, so ``[GOAL: a] b]`` stays prose.
    depth = 0
    for match in _GOAL_BODY_DELIMITER.finditer(line, body_start, close):
        char = match.group()
        if char == "\r":
            return False
        depth += 1 if char == "[" else -1
        if depth < 0:
            return False
    return depth == 0


def strip_goal_marker_lines(text: str) -> str:
    """Remove complete standalone ``[GOAL: ...]`` lines in one forward scan.

    Projections reach this through :func:`strip_control_comments`; call it
    directly only on a path that does not strip control tags.
    """
    if _GOAL_PREFIX_PROBE.search(text) is None:
        return text
    out: list[str] = []
    start = 0
    while start < len(text):
        newline = text.find("\n", start)
        line_end = len(text) if newline < 0 else newline
        line = text[start:line_end]
        if not _is_goal_marker_line(line):
            out.append(line)
        if newline < 0:
            break
        out.append("\n")
        start = newline + 1
    return "".join(out)


def strip_control_comments(text: str, *, hide_partial: bool = False) -> str:
    """Remove dashboard protocol lines from *text* for a plain-text projection
    (preview, TTS, channel delivery, mirror backfill).

    The ONE projection boundary for the two line protocols only the dashboard
    renders: complete standalone ``[GOAL: ...]`` lines anywhere in the text
    (:func:`strip_goal_marker_lines`), then the trailing control-tag lines
    (:func:`kiro_crew.constants.strip_trailing_control_tags`). Goal lines go first so one sitting
    after a tag cannot shield that tag from the tail anchor. Both modes of
    *hide_partial* strip complete goal lines the same way; the flag only
    governs a still-arriving control tag. A text carrying neither protocol is
    returned unchanged.

    The result is NOT a prefix of *text* once a goal line is removed, so a
    caller measuring a tail offset (``len(...)``) or asking "did a tag end this
    reply" calls :func:`kiro_crew.constants.strip_trailing_control_tags` instead.
    """
    from kiro_crew.constants import strip_trailing_control_tags

    return strip_trailing_control_tags(strip_goal_marker_lines(text), hide_partial=hide_partial)
